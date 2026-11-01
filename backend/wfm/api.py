"""FastAPI app serving the dashboard. Reads SQLite; only /top calls warframe.market live."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import __version__, forecast, jobs, prices
from .images import SAFE_NAME
from .catalog import CATEGORIES
from .market import HttpMarket, MarketError
from .config import settings
from .db import connect, get_meta, init_db, utc_now

_top_cache: dict[tuple[str, int | None, str | None], tuple[float, Any]] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = connect()
    init_db(conn)
    conn.close()
    async with HttpMarket() as market:
        app.state.market = market
        yield


app = FastAPI(title="WFMarketAnalyst API", version=__version__, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:4200", "http://127.0.0.1:4200"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


Db = Depends(get_db)

SUMMARY_COLUMNS = """
    i.id, i.slug, i.name, i.category, i.tags, i.rarity, i.max_rank, i.ducats,
    i.req_mastery_rank, i.trading_tax, i.vaulted, i.image
"""

# Item fields plus the latest price (item_prices view). Use as `FROM items i {PRICED}`.
PRICED = "LEFT JOIN item_prices p ON p.item_id = i.id"


def _row(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    for key in ("tags", "subtypes", "set_parts"):
        if key in out and isinstance(out[key], str):
            out[key] = json.loads(out[key])
    for key in ("vaulted", "tradable", "bulk_tradable", "set_root", "watched"):
        if key in out and out[key] is not None:
            out[key] = bool(out[key])
    return out


def _summaries(conn: sqlite3.Connection, ids: list[str]) -> dict[str, dict[str, Any]]:
    """Item summaries (with latest price) for the given ids."""
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    return {r["id"]: _row(r) for r in conn.execute(
        f"SELECT {SUMMARY_COLUMNS}, p.price FROM items i {PRICED} WHERE i.id IN ({marks})", ids)}


def _item_or_404(conn: sqlite3.Connection, slug: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM items WHERE slug = ?", (slug,)).fetchone()
    if not row:
        raise HTTPException(404, f"Unknown item '{slug}'")
    return row


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "version": __version__}


@app.get("/api/images/{name}")
def item_image(name: str) -> FileResponse:
    """An item image saved by the item-images job (items.image names the file)."""
    path = settings.images_dir / name
    if not SAFE_NAME.match(name) or not path.is_file():
        raise HTTPException(404, "No such image")
    return FileResponse(path, headers={"Cache-Control": "public, max-age=604800"})


@app.get("/api/config")
def config() -> dict[str, Any]:
    return {"platform": settings.platform, "crossplay": settings.crossplay}


@app.get("/api/overview")
def overview(conn: sqlite3.Connection = Db) -> dict[str, Any]:
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
    by_category = {r["category"]: r["n"] for r in conn.execute(
        "SELECT category, COUNT(*) n FROM items GROUP BY category")}
    online = get_meta(conn, "ws_online_report")
    return {
        "catalog": {
            "items": one("SELECT COUNT(*) FROM items"),
            "detailsSynced": one("SELECT COUNT(*) FROM items WHERE detail_synced_at IS NOT NULL"),
            "byCategory": [{"category": c, "count": by_category.get(c, 0)} for c in CATEGORIES],
            "primeParts": one("SELECT COUNT(*) FROM items WHERE ducats IS NOT NULL"),
            "relicsVaulted": one("SELECT COUNT(*) FROM items WHERE category = 'Relic' AND vaulted = 1"),
            "relicsTotal": one("SELECT COUNT(*) FROM items WHERE category = 'Relic'"),
            "auxiliary": {r["kind"]: r["n"] for r in conn.execute(
                "SELECT kind, COUNT(*) n FROM aux_catalog GROUP BY kind")},
            "listSyncedAt": get_meta(conn, "items_list_synced_at"),
            "detailSyncedAt": get_meta(conn, "items_detail_synced_at"),
        },
        "recording": {
            "ordersTotal": one("SELECT COUNT(*) FROM orders_seen"),
            "orders24h": one(
                "SELECT COUNT(*) FROM orders_seen WHERE created_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 day')"),
            "lastOrderAt": one("SELECT MAX(created_at) FROM orders_seen"),
            "itemsWithHistory": one("SELECT COUNT(DISTINCT item_id) FROM price_stats"),
            "statsBackfilledAt": get_meta(conn, "stats_backfilled_at"),
            "snapshots": one("SELECT COUNT(*) FROM top_snapshots"),
            "online": json.loads(online) if online else None,
        },
        "watchlist": one("SELECT COUNT(*) FROM watchlist"),
    }


@app.get("/api/jobs")
def job_status(conn: sqlite3.Connection = Db) -> list[dict[str, Any]]:
    """Every job definition (title, kind, command...) with its latest run."""
    return jobs.status(conn)


class JobStart(BaseModel):
    category: str | None = None
    slugs: list[str] = Field(default_factory=list, max_length=500)
    watchlist: bool = False
    with_history: bool = False


@app.post("/api/jobs/{name}/start", status_code=202)
def start_job(name: str, body: JobStart | None = None, conn: sqlite3.Connection = Db) -> dict[str, Any]:
    """Launch a job in the background. A resumable job picks up where it stopped."""
    body = body or JobStart()
    with _job_errors(name):
        jobs.start(conn, name, jobs.Scope(category=body.category, slugs=tuple(body.slugs), watchlist=body.watchlist,
                                          with_history=body.with_history))
    return {"name": name, "started": True}


@app.post("/api/jobs/{name}/stop")
def stop_job(name: str, conn: sqlite3.Connection = Db) -> dict[str, Any]:
    """Ask a running job to stop; it finishes cleanly (as with Ctrl+C) within a few seconds."""
    with _job_errors(name):
        outcome = jobs.stop(conn, name)
    return {"name": name, "outcome": outcome}


@contextmanager
def _job_errors(name: str) -> Iterator[None]:
    try:
        yield
    except KeyError:
        raise HTTPException(404, f"Unknown job '{name}'") from None
    except jobs.JobConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


@app.get("/api/categories")
def categories(conn: sqlite3.Connection = Db) -> list[dict[str, Any]]:
    counts = {r["category"]: r["n"] for r in conn.execute(
        "SELECT category, COUNT(*) n FROM items GROUP BY category")}
    return [{"category": c, "count": counts.get(c, 0)} for c in CATEGORIES]


@app.get("/api/tags")
def tags(conn: sqlite3.Connection = Db, category: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT t.tag, COUNT(*) n FROM item_tags t JOIN items i ON i.id = t.item_id"
    args: list[Any] = []
    if category:
        sql += " WHERE i.category = ?"
        args.append(category)
    sql += " GROUP BY t.tag ORDER BY n DESC"
    return [{"tag": r["tag"], "count": r["n"]} for r in conn.execute(sql, args)]


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------

SORTS = {
    "name": "i.name COLLATE NOCASE",
    "ducats": "i.ducats",
    "mastery": "i.req_mastery_rank",
    "tax": "i.trading_tax",
    "price": "p.price",
}


@app.get("/api/items")
def list_items(
    conn: sqlite3.Connection = Db,
    q: str | None = None,
    category: str | None = None,
    tag: str | None = None,
    vaulted: bool | None = None,
    sort: Literal["name", "ducats", "mastery", "tax", "price"] = "name",
    order: Literal["asc", "desc"] = "asc",
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    where, args = [], []
    if q:
        where.append("(i.name LIKE ? OR i.slug LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    if category:
        where.append("i.category = ?")
        args.append(category)
    if tag:
        where.append("EXISTS (SELECT 1 FROM item_tags t WHERE t.item_id = i.id AND t.tag = ?)")
        args.append(tag)
    if vaulted is not None:
        where.append("i.vaulted = ?")
        args.append(int(vaulted))
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM items i {clause}", args).fetchone()[0]
    direction = "DESC" if order == "desc" else "ASC"
    rows = conn.execute(
        f"""SELECT {SUMMARY_COLUMNS}, p.price,
                   EXISTS (SELECT 1 FROM watchlist w WHERE w.item_id = i.id) AS watched
            FROM items i {PRICED} {clause}
            ORDER BY {SORTS[sort]} IS NULL, {SORTS[sort]} {direction}, i.name COLLATE NOCASE
            LIMIT ? OFFSET ?""",
        [*args, limit, offset],
    ).fetchall()
    return {"total": total, "items": [_row(r) for r in rows]}


@app.get("/api/items/{slug}")
def get_item(slug: str, conn: sqlite3.Connection = Db) -> dict[str, Any]:
    stored = _item_or_404(conn, slug)
    item = _row(conn.execute(f"SELECT i.*, p.price FROM items i {PRICED} WHERE i.slug = ?", (slug,)).fetchone())
    set_items: list[dict[str, Any]] = []
    if item.get("set_parts"):
        marks = ",".join("?" * len(item["set_parts"]))
        set_items = [_row(r) for r in conn.execute(
            f"""SELECT {SUMMARY_COLUMNS}, i.set_root, i.quantity_in_set, p.price
                FROM items i {PRICED} WHERE i.id IN ({marks})
                ORDER BY i.set_root DESC, i.name""",
            item["set_parts"],
        )]
    watch = conn.execute("SELECT target_price, added_at FROM watchlist WHERE item_id = ?", (item["id"],)).fetchone()
    return {
        **item,
        "default_rank": prices.default_rank(item["max_rank"]),
        "historyRanks": prices.ranks_with_history(conn, item["id"]),
        "default_subtype": prices.default_subtype(conn, item["id"]),
        "historySubtypes": prices.subtypes_with_history(conn, stored),
        "setItems": set_items,
        "watch": dict(watch) if watch else None,
        "recordedOrders": conn.execute(
            "SELECT COUNT(*) FROM orders_seen WHERE item_id = ?", (item["id"],)).fetchone()[0],
    }


@app.get("/api/items/{slug}/history")
def item_history(
    slug: str,
    conn: sqlite3.Connection = Db,
    granularity: Literal["day", "hour"] = "day",
    rank: int | None = None,
    subtype: str | None = None,
) -> dict[str, Any]:
    return prices.history(conn, _item_or_404(conn, slug), granularity, rank, subtype)


@app.get("/api/items/{slug}/orders")
def item_orders(slug: str, conn: sqlite3.Connection = Db, limit: int = Query(50, ge=1, le=500)) -> list[dict[str, Any]]:
    item = _item_or_404(conn, slug)
    return [dict(r) for r in conn.execute(
        "SELECT * FROM orders_seen WHERE item_id = ? ORDER BY created_at DESC LIMIT ?", (item["id"], limit))]


@app.get("/api/items/{slug}/top")
async def item_top(slug: str, conn: sqlite3.Connection = Db, rank: int | None = None,
                   subtype: str | None = None) -> dict[str, Any]:
    """Live best buy/sell orders, proxied with a short cache. Not stored.

    Subtype defaults to the item's default subtype: a relic's Intact and Radiant orders are
    different markets.
    """
    item = _item_or_404(conn, slug)
    subtype = subtype if subtype is not None else prices.default_subtype(conn, item["id"])
    key = (slug, rank, subtype)
    cached = _top_cache.get(key)
    if cached and time.monotonic() - cached[0] < settings.top_cache_seconds:
        return cached[1]
    try:
        data = await app.state.market.best_orders(slug, rank, subtype)
    except MarketError as exc:
        raise HTTPException(502, f"warframe.market: {exc}") from exc
    result = {"fetchedAt": utc_now(), **data}
    _top_cache[key] = (time.monotonic(), result)
    return result


# ---------------------------------------------------------------------------
# Market views over recorded data
# ---------------------------------------------------------------------------

@app.get("/api/orders/recent")
def recent_orders(
    conn: sqlite3.Connection = Db,
    limit: int = Query(50, ge=1, le=500),
    type: Literal["buy", "sell"] | None = None,
) -> list[dict[str, Any]]:
    sql = """SELECT o.id, o.type, o.platinum, o.quantity, o.rank, o.user_name, o.user_reputation,
                    o.user_status, o.created_at, i.slug, i.name, i.image, i.category
             FROM orders_seen o JOIN items i ON i.id = o.item_id"""
    args: list[Any] = []
    if type:
        sql += " WHERE o.type = ?"
        args.append(type)
    sql += " ORDER BY o.created_at DESC LIMIT ?"
    return [dict(r) for r in conn.execute(sql, [*args, limit])]


@app.get("/api/ducats")
def ducats(conn: sqlite3.Connection = Db, limit: int = Query(100, ge=1, le=1000)) -> list[dict[str, Any]]:
    values = prices.ducat_values(conn, limit)
    items = _summaries(conn, [v["item_id"] for v in values])
    return [{**items[v["item_id"]], "ducats_per_plat": v["ducats_per_plat"]} for v in values]


@app.get("/api/movers")
def movers(
    conn: sqlite3.Connection = Db,
    days: int = Query(7, ge=1, le=60),
    min_volume: int = Query(5, ge=0),
    limit: int = Query(10, ge=1, le=50),
) -> dict[str, list[dict[str, Any]]]:
    """Biggest median price changes between the latest day and `days` earlier."""
    result = prices.movers(conn, days=days, min_volume=min_volume, limit=limit)
    items = _summaries(conn, [c["item_id"] for side in result.values() for c in side])
    return {side: [{**{k: v for k, v in c.items() if k != "item_id"}, "item": items[c["item_id"]]}
                   for c in changes if c["item_id"] in items]
            for side, changes in result.items()}


# ---------------------------------------------------------------------------
# Forecasts
# ---------------------------------------------------------------------------

@app.get("/api/items/{slug}/forecast")
def item_forecast(slug: str, conn: sqlite3.Connection = Db, rank: int | None = None) -> list[dict[str, Any]]:
    """Stored forecasts for one item, one per horizon, at a rank (default: its default rank)."""
    item = _item_or_404(conn, slug)
    return forecast.forecast_for(conn, item["id"], rank if rank is not None else prices.default_rank(item["max_rank"]))


def _horizon(horizon: int) -> int:
    if horizon not in forecast.HORIZONS:
        raise HTTPException(422, f"horizon must be one of {list(forecast.HORIZONS)}")
    return horizon


def _with_items(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Forecast rows with the item summary in place of item_id."""
    items = _summaries(conn, [r["item_id"] for r in rows])
    return [{**{k: v for k, v in r.items() if k != "item_id"}, "item": items[r["item_id"]]}
            for r in rows if r["item_id"] in items]


@app.get("/api/forecast/movers")
def forecast_movers(
    conn: sqlite3.Connection = Db,
    horizon: int = 7,
    limit: int = Query(8, ge=1, le=50),
    by: Literal["gain", "change"] = "gain",
) -> dict[str, list[dict[str, Any]]]:
    """Biggest predicted rises and falls, by platinum gained ("gain") or percentage ("change")."""
    result = forecast.predicted_movers(conn, _horizon(horizon), limit, by)
    return {side: _with_items(conn, rows) for side, rows in result.items()}


@app.get("/api/forecast/search")
def forecast_search(
    conn: sqlite3.Connection = Db,
    q: str = Query(..., min_length=1),
    horizon: int = 7,
    limit: int = Query(25, ge=1, le=100),
) -> dict[str, Any]:
    """Stored forecasts for items whose name contains `q`, sorted by name."""
    result = forecast.search(conn, q.strip(), _horizon(horizon), limit)
    return {"total": result["total"], "forecasts": _with_items(conn, result["forecasts"])}


@app.get("/api/forecast/report")
def forecast_report(conn: sqlite3.Connection = Db) -> dict[str, Any] | None:
    """The latest training run's evaluation, or null if the forecast was never trained."""
    return forecast.latest_report(conn)


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------

class WatchIn(BaseModel):
    target_price: int | None = Field(default=None, ge=1, le=900000)


@app.get("/api/watchlist")
def get_watchlist(conn: sqlite3.Connection = Db) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""SELECT {SUMMARY_COLUMNS}, p.price, w.target_price, w.added_at,
                   (SELECT best_sell FROM top_snapshots s WHERE s.item_id = i.id
                     ORDER BY taken_at DESC, rank LIMIT 1) AS best_sell,
                   (SELECT taken_at FROM top_snapshots s WHERE s.item_id = i.id
                     ORDER BY taken_at DESC LIMIT 1) AS snapshot_at
            FROM watchlist w JOIN items i ON i.id = w.item_id {PRICED}
            ORDER BY w.added_at DESC""").fetchall()
    return [_row(r) for r in rows]


@app.put("/api/watchlist/{slug}")
def watch_item(slug: str, body: WatchIn, conn: sqlite3.Connection = Db) -> dict[str, Any]:
    item = _item_or_404(conn, slug)
    conn.execute(
        """INSERT INTO watchlist (item_id, target_price) VALUES (?, ?)
           ON CONFLICT (item_id) DO UPDATE SET target_price = excluded.target_price""",
        (item["id"], body.target_price))
    return {"slug": slug, "target_price": body.target_price}


@app.delete("/api/watchlist/{slug}")
def unwatch_item(slug: str, conn: sqlite3.Connection = Db) -> dict[str, Any]:
    item = _item_or_404(conn, slug)
    conn.execute("DELETE FROM watchlist WHERE item_id = ?", (item["id"],))
    return {"slug": slug, "removed": True}
