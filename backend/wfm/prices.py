"""Price history module: what an item is worth and how that changed.

Interface::

    store(conn, item_id, records)          save Market price-history records (re-runs overwrite)
    fetched_since(conn, since)             ids of items whose history was stored at or after `since`
    default_rank(max_rank)                 the rank a price refers to when none is given
    history(conn, item, granularity, rank) completed-trade and listing series for one item
    movers(conn, days, min_volume, limit)  biggest median price changes over `days`
    ducat_values(conn, limit)              prime parts ranked by ducats per platinum

The latest price of every item is the `item_prices` view (schema.sql): join it
instead of re-deriving it.

Behind the interface: the price_stats table and its conventions (unranked items
stored as rank -1, live rows split by order type), which rank a price means, and
the movers / ducat calculations.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import timedelta
from typing import Any, Literal

from .db import parse_utc, utc_now

Granularity = Literal["day", "hour"]
UNRANKED = -1  # how price_stats stores "this item has no ranks"


def default_rank(max_rank: int | None) -> int | None:
    """Prices refer to rank 0 for ranked items, and to no rank otherwise.

    Mirrored by the `item_prices` view, which picks the latest price at this rank.
    """
    return None if max_rank is None else 0


def _stored(rank: int | None) -> int:
    return UNRANKED if rank is None else rank


def store(conn: sqlite3.Connection, item_id: str, records: list[dict[str, Any]]) -> int:
    """Save Market price-history records for one item; re-running overwrites, never duplicates."""
    if not records:
        return 0
    fetched_at = utc_now()
    rows = [{**r, "mod_rank": _stored(r["mod_rank"]), "item_id": item_id, "fetched_at": fetched_at}
            for r in records]
    cols = list(rows[0])
    conn.executemany(
        f"INSERT OR REPLACE INTO price_stats ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})",
        rows,
    )
    conn.commit()
    return len(rows)


def fetched_since(conn: sqlite3.Connection, since: str) -> set[str]:
    return {r[0] for r in conn.execute("SELECT DISTINCT item_id FROM price_stats WHERE fetched_at >= ?", (since,))}


def history(conn: sqlite3.Connection, item: sqlite3.Row, granularity: Granularity,
            rank: int | None = None) -> dict[str, Any]:
    """Completed-trade series and median sell/buy listing series at one rank (default: default_rank)."""
    rank = rank if rank is not None else default_rank(item["max_rank"])
    args = (item["id"], granularity, _stored(rank))
    closed = [dict(r) for r in conn.execute(
        """SELECT bucket, volume, min_price, max_price, open_price, closed_price,
                  avg_price, wa_price, median, moving_avg
           FROM price_stats
           WHERE item_id = ? AND source = 'closed' AND granularity = ? AND mod_rank = ?
           ORDER BY bucket""", args)]
    live: dict[str, list[dict[str, Any]]] = {"sell": [], "buy": []}
    for r in conn.execute(
        """SELECT bucket, order_type, volume, min_price, max_price, median
           FROM price_stats
           WHERE item_id = ? AND source = 'live' AND granularity = ? AND mod_rank = ?
             AND order_type IN ('sell', 'buy')
           ORDER BY bucket""", args):
        live[r["order_type"]].append({k: r[k] for k in ("bucket", "volume", "min_price", "max_price", "median")})
    return {"rank": rank, "granularity": granularity, "closed": closed, "live": live}


def ranks_with_history(conn: sqlite3.Connection, item_id: str) -> list[int | None]:
    return [None if r[0] == UNRANKED else r[0] for r in conn.execute(
        "SELECT DISTINCT mod_rank FROM price_stats WHERE item_id = ? ORDER BY mod_rank", (item_id,))]


def movers(conn: sqlite3.Connection, *, days: int, min_volume: int, limit: int) -> dict[str, list[dict[str, Any]]]:
    """Biggest daily-median changes between the newest recorded day and `days` before it.

    Only (item, rank) series that reach the newest day and traded at least
    `min_volume` times in the window count. Returns item ids; callers add names.
    """
    latest = conn.execute(
        "SELECT MAX(bucket) FROM price_stats WHERE source = 'closed' AND granularity = 'day'").fetchone()[0]
    if not latest:
        return {"gainers": [], "losers": []}
    since = (parse_utc(latest) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    series: dict[tuple[str, int], list[sqlite3.Row]] = defaultdict(list)
    for r in conn.execute(
        """SELECT item_id, mod_rank, bucket, median, volume FROM price_stats
           WHERE source = 'closed' AND granularity = 'day' AND bucket >= ? ORDER BY bucket""", (since,)):
        series[(r["item_id"], r["mod_rank"])].append(r)
    changes = []
    for (item_id, mod_rank), points in series.items():
        first, last = points[0], points[-1]
        volume = sum(p["volume"] or 0 for p in points)
        if len(points) < 2 or not first["median"] or last["bucket"] != latest or volume < min_volume:
            continue
        changes.append({"item_id": item_id, "rank": None if mod_rank == UNRANKED else mod_rank,
                        "from": first["median"], "to": last["median"], "volume": volume,
                        "change": round((last["median"] - first["median"]) / first["median"], 4)})
    gainers = sorted((c for c in changes if c["change"] > 0), key=lambda c: -c["change"])
    losers = sorted((c for c in changes if c["change"] < 0), key=lambda c: c["change"])
    return {"gainers": gainers[:limit], "losers": losers[:limit]}


def ducat_values(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    """Prime parts (never sets: Baro buys parts) with ducats per platinum, best value first.

    Returns item_id, ducats, price and ducats_per_plat (None until the part has a price).
    """
    rows = [dict(r) for r in conn.execute(
        """SELECT i.id AS item_id, i.ducats, p.price FROM items i JOIN item_prices p ON p.item_id = i.id
           WHERE i.ducats > 0
             AND NOT EXISTS (SELECT 1 FROM item_tags t WHERE t.item_id = i.id AND t.tag = 'set')""")]
    for r in rows:
        r["ducats_per_plat"] = round(r["ducats"] / r["price"], 2) if r["price"] else None
    rows.sort(key=lambda r: (r["ducats_per_plat"] is None, -(r["ducats_per_plat"] or 0), -r["ducats"]))
    return rows[:limit]
