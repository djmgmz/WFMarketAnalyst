"""Recorders: store market data. Nothing here runs unless started as a job.

* ``record_new_orders``  - every newly posted order (the order recorder).
* ``backfill_stats``     - 90-day / 48-hour price history for a list of items (backfill);
                           resumable: skips items already fetched today.
* ``snapshot_watchlist`` - periodic best buy/sell prices for watchlist items.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from typing import Any, NamedTuple

from . import prices
from .db import retry_locked, set_meta, utc_day_start, utc_now
from .jobs import NO_REPORT, Reporter
from .market import Connection, Market, MarketError, NewOrder, OnlineCount, Record

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def store_orders(conn: sqlite3.Connection, orders: list[Record]) -> int:
    """Store Market order records; returns how many were new."""
    if not orders:
        return 0
    seen_at = utc_now()
    rows = [{**o, "created_at": o.get("created_at") or seen_at, "seen_at": seen_at} for o in orders]
    cols = list(rows[0])
    before = conn.total_changes
    conn.executemany(
        f"INSERT OR IGNORE INTO orders_seen ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})",
        rows,
    )
    conn.commit()
    return conn.total_changes - before


async def record_new_orders(
    conn: sqlite3.Connection, market: Market, stop: asyncio.Event, job: Reporter = NO_REPORT
) -> None:
    """Store every newly posted order until `stop` is set. Each stored order advances the job."""
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM items")}
    async for event in market.new_orders(stop):
        match event:
            case Connection(state=state, reconnects=reconnects):
                job.note(connection=state, reconnects=reconnects)
            case NewOrder(order=order):
                if await retry_locked(conn, lambda: store_orders(conn, [order])):
                    job.advance()
                    job.working_on(names.get(order["item_id"], "unknown item"))
                    job.note(last_order_at=utc_now())
            case OnlineCount(connections=connections, authorized_users=authorized):
                online = {"connections": connections, "authorizedUsers": authorized}
                set_meta(conn, "ws_online_report", json.dumps(online))
                conn.commit()
                job.note(online=online)


# ---------------------------------------------------------------------------
# Price history
# ---------------------------------------------------------------------------

class Backfilled(NamedTuple):
    done: int     # items fetched this run
    stored: int   # statistic rows stored
    skipped: int  # items already fetched today


async def backfill_stats(
    conn: sqlite3.Connection, market: Market, slugs: list[str], job: Reporter = NO_REPORT,
    *, refetch: bool = False,
) -> Backfilled:
    """Fetch price history for each slug.

    Resumable: items whose history was already fetched today (UTC; the API publishes each day's
    statistics after midnight UTC) are skipped unless `refetch`, so starting the same scope again
    after a stop or failure picks up where it ended. A save that finds the database locked by
    another process is retried (db.retry_locked) instead of ending the run.
    """
    ids = {r["slug"]: r["id"] for r in conn.execute("SELECT id, slug FROM items")}
    fresh = set() if refetch else prices.fetched_since(conn, utc_day_start())
    todo = [slug for slug in slugs if ids.get(slug) not in fresh]
    skipped = len(slugs) - len(todo)
    if skipped:
        # Counted as done up front; the ETA uses only the items actually fetched
        job.note(skipped=skipped)
        for _ in range(skipped):
            job.advance()
    done = stored = 0
    for slug in todo:
        job.working_on(slug)
        ok = False
        if slug not in ids:
            log.warning("Unknown item slug %s, skipping", slug)
        else:
            try:
                records = await market.price_history(slug)
                stored += await retry_locked(conn, lambda: prices.store(conn, ids[slug], records))
                done += 1
                ok = True
            except MarketError as exc:
                log.warning("Statistics failed for %s: %s", slug, exc)
        job.advance(ok=ok)
    set_meta(conn, "stats_backfilled_at", utc_now())
    conn.commit()
    return Backfilled(done, stored, skipped)


# ---------------------------------------------------------------------------
# Watchlist snapshots
# ---------------------------------------------------------------------------

def top_summary(top: dict[str, list[Record]]) -> dict[str, Any]:
    sell, buy = top.get("sell") or [], top.get("buy") or []
    return {
        "best_sell": min((o["platinum"] for o in sell), default=None),
        "best_buy": max((o["platinum"] for o in buy), default=None),
        "sell_count": len(sell),
        "buy_count": len(buy),
    }


async def snapshot_watchlist(conn: sqlite3.Connection, market: Market) -> int:
    rows = conn.execute(
        "SELECT i.id, i.slug, i.max_rank FROM watchlist w JOIN items i ON i.id = w.item_id"
    ).fetchall()
    taken_at = utc_now()
    count = 0
    for row in rows:
        # Snapshots are of the default subtype, the one the item's price refers to.
        subtype = prices.default_subtype(conn, row["id"])
        for rank in [None] if row["max_rank"] is None else [0, row["max_rank"]]:
            try:
                s = top_summary(await market.best_orders(row["slug"], rank, subtype))
            except MarketError as exc:
                log.warning("Top orders failed for %s: %s", row["slug"], exc)
                continue
            conn.execute(
                """INSERT OR REPLACE INTO top_snapshots
                   (item_id, taken_at, rank, best_sell, best_buy, sell_count, buy_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (row["id"], taken_at, -1 if rank is None else rank,
                 s["best_sell"], s["best_buy"], s["sell_count"], s["buy_count"]),
            )
            conn.commit()
            count += 1
    return count


async def watchlist_loop(
    conn: sqlite3.Connection, market: Market, interval: int, stop: asyncio.Event, job: Reporter = NO_REPORT
) -> None:
    taken = 0
    while not stop.is_set():
        n = await snapshot_watchlist(conn, market)
        if n:
            log.info("Watchlist snapshot: %d price points", n)
        taken += n
        job.note(snapshots=taken, last_snapshot_at=utc_now())
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def run_recorders(
    conn: sqlite3.Connection, market: Market, *, snapshot_interval: int = 900, job: Reporter = NO_REPORT
) -> None:
    stop = asyncio.Event()
    await asyncio.gather(
        record_new_orders(conn, market, stop, job),
        watchlist_loop(conn, market, snapshot_interval, stop, job),
    )
