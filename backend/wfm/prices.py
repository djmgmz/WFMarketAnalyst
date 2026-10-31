"""Price history module: what an item is worth and how that changed.

Interface::

    store(conn, item_id, records)          save Market price-history records (re-runs overwrite), then
                                           flag the item's suspect days
    flag_suspect(conn, item_ids=None)      (re)flag completed-trade days that look faked; returns the count
    liquid_series(conn)                    (item id, stored rank) series with enough genuine trading to rank
    asking_price(median, lowest, listings) what sellers really ask: the median, or the lowest listing when few
    fetched_since(conn, since)             ids of items whose history was stored at or after `since`
    default_rank(max_rank)                 the rank a price refers to when none is given
    history(conn, item, granularity, rank) completed-trade and listing series for one item
    movers(conn, days, min_volume, limit)  biggest median price changes over `days`
    ducat_values(conn, limit)              prime parts ranked by ducats per platinum

The latest price of every item is the `item_prices` view (schema.sql): join it
instead of re-deriving it. Completed-trade rows flagged `suspect` are excluded
everywhere prices are read (the view, history, movers, the forecast's dataset);
the raw rows are kept.

Behind the interface: the price_stats table and its conventions (unranked items
stored as rank -1, live rows split by order type), which rank a price means, the
suspect-trade rules, and the movers / ducat calculations.

Suspect trades: completed trades are self-reported, so one player can "sell" 199
units at 99,999p. A day is judged against the same series' days within
JUDGE_WINDOW_DAYS (each variant, such as a relic's refinement, is its own series).
It is suspect when its median is more than SUSPECT_PRICE_RATIO times off their
typical median and off the series' typical median overall, or its volume is more than SUSPECT_VOLUME_RATIO times their typical
volume, at least SUSPECT_MIN_VOLUME, and more than BURST_RATIO times the busiest
genuine day among them (a one-off dump, not an item regularly traded in bulk). A
set's day is also suspect when its median is above SET_PRICE_RATIO times the sum
of its parts' prices that day (and SET_MIN_PREMIUM above them). Only above: nobody really pays much more than the
parts cost, since anyone can build the set from them, while sets often trade well
below their parts.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from datetime import timedelta
from statistics import median
from typing import Any, Literal

from .db import parse_utc, utc_now

Granularity = Literal["day", "hour"]
UNRANKED = -1  # how price_stats stores "this item has no ranks"

SUSPECT_PRICE_RATIO = 5     # a day's median this many times above or below the series' typical median
SUSPECT_VOLUME_RATIO = 20   # or this many times its typical volume...
SUSPECT_MIN_VOLUME = 20     # ...and at least this many trades (a quiet item's busy day of 3 is real)
BURST_RATIO = 3             # ...and more than this many times the busiest genuine day near it
SET_PRICE_RATIO = 2         # a set's median above this many times the sum of its parts' prices that day
SET_MIN_PREMIUM = 20        # ...and at least this much above them (a few plat on a cheap set is normal)
JUDGE_WINDOW_DAYS = 21      # a day is judged against the same series' days this close to it
MIN_JUDGED_DAYS = 3         # other traded days within the window needed before a day can be judged
LIQUID_WINDOW_DAYS = 28     # an item ranks (movers, predicted movers) only with genuine trading
LIQUID_MIN_DAYS = 10        # on at least this many of the last LIQUID_WINDOW_DAYS days
THIN_LISTINGS = 5           # below this many listings, the median listing means nothing

Series = tuple[str, str, int, str]  # (item id, granularity, stored rank, subtype) of completed-trade rows


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
    flag_suspect(conn, [item_id])
    return len(rows)


def flag_suspect(conn: sqlite3.Connection, item_ids: Iterable[str] | None = None) -> int:
    """Recompute the suspect flag of every completed-trade row of these items (default: all).

    One transaction per call; returns how many rows are flagged. A set is also checked against
    its parts' typical prices, read from their rows as currently flagged.
    """
    ids = None if item_ids is None else list(item_ids)
    where = "" if ids is None else f"AND item_id IN ({','.join('?' * len(ids))})"
    rows = conn.execute(
        f"""SELECT item_id, granularity, mod_rank, subtype, bucket, volume, median FROM price_stats
            WHERE source = 'closed' {where} ORDER BY bucket""", ids or []).fetchall()
    # One series per variant: a relic's Intact and Radiant (or a fish's sizes) are priced apart.
    series: dict[Series, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        series[(r["item_id"], r["granularity"], r["mod_rank"], r["subtype"])].append(r)
    suspect = {(key, r["bucket"]) for key, points in series.items() for r in _suspect_days(points)}
    suspect |= _sets_out_of_line(conn, series)
    conn.executemany(
        """UPDATE price_stats SET suspect = ? WHERE item_id = ? AND source = 'closed' AND granularity = ?
                                                AND mod_rank = ? AND subtype = ? AND bucket = ?""",
        [(int(((r["item_id"], r["granularity"], r["mod_rank"], r["subtype"]), r["bucket"]) in suspect),
          r["item_id"], r["granularity"], r["mod_rank"], r["subtype"], r["bucket"]) for r in rows])
    conn.commit()
    return len(suspect)


def _suspect_days(points: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """Days of one series far out of line with the same series' other days.

    A price must be off against both the days within JUDGE_WINDOW_DAYS and the whole history:
    a genuine trend (a launch price settling) is normal locally, and a genuine day between two
    fakes is normal against the whole history; a fake is off against both. A busy day must also
    dwarf the busiest genuine day near it, so an item regularly traded in bulk is left alone
    while a one-off dump is caught.
    """
    traded = [(parse_utc(p["bucket"]), p) for p in points if (p["volume"] or 0) > 0 and (p["median"] or 0) > 0]
    window = timedelta(days=JUDGE_WINDOW_DAYS)
    out = []
    for when, p in traded:
        near = [o for t, o in traded if o is not p and abs(t - when) <= window]
        if len(near) < MIN_JUDGED_DAYS:
            continue
        overall = median(o["median"] for _, o in traded if o is not p)
        if (_off(p["median"], median(o["median"] for o in near)) > SUSPECT_PRICE_RATIO
                and _off(p["median"], overall) > SUSPECT_PRICE_RATIO):
            out.append(p)
            continue
        genuine = [o["volume"] for o in near if _off(o["median"], overall) <= SUSPECT_PRICE_RATIO]
        if (p["volume"] >= SUSPECT_MIN_VOLUME
                and p["volume"] > SUSPECT_VOLUME_RATIO * median(o["volume"] for o in near)
                and p["volume"] > BURST_RATIO * max(genuine, default=0)):
            out.append(p)
    return out


def _off(price: float, typical: float) -> float:
    """How many times above or below `typical` a price is (1 = equal)."""
    return max(price / typical, typical / price)


def _sets_out_of_line(conn: sqlite3.Connection,
                      series: dict[Series, list[sqlite3.Row]]) -> set[tuple[Series, str]]:
    """Daily rows of sets priced above SET_PRICE_RATIO times the sum of their parts' prices that day."""
    sets = {key[0]: points for key, points in series.items() if key[1:] == ("day", UNRANKED, "")}
    if not sets:
        return set()
    out = set()
    for set_id, set_parts in conn.execute(
            f"SELECT id, set_parts FROM items WHERE set_root = 1 AND set_parts IS NOT NULL "
            f"AND id IN ({','.join('?' * len(sets))})", list(sets)).fetchall():
        reference = _parts_by_day(conn, [p for p in json.loads(set_parts) if p != set_id])
        for r in sets[set_id]:
            parts_price = reference.get(r["bucket"])
            price = r["median"] or 0
            if parts_price and price > SET_PRICE_RATIO * parts_price and price - parts_price >= SET_MIN_PREMIUM:
                out.add(((set_id, "day", UNRANKED, ""), r["bucket"]))
    return out


def _parts_by_day(conn: sqlite3.Connection, parts: list[str]) -> dict[str, float]:
    """{day: sum of the parts' genuine medians times their quantity} for days on which every part traded."""
    if not parts:
        return {}
    marks = ",".join("?" * len(parts))
    qty = dict(conn.execute(f"SELECT id, COALESCE(quantity_in_set, 1) FROM items WHERE id IN ({marks})", parts))
    by_day: defaultdict[str, dict[str, float]] = defaultdict(dict)
    for item_id, bucket, price in conn.execute(
            f"""SELECT item_id, bucket, median FROM price_stats
                WHERE item_id IN ({marks}) AND source = 'closed' AND granularity = 'day' AND mod_rank = ?
                  AND subtype = '' AND suspect = 0 AND volume > 0 AND median > 0""", [*parts, UNRANKED]):
        by_day[bucket][item_id] = qty.get(item_id, 1) * price
    return {day: sum(prices_.values()) for day, prices_ in by_day.items() if len(prices_) == len(parts)}


def liquid_series(conn: sqlite3.Connection) -> set[tuple[str, int]]:
    """(item id, stored rank) series that traded genuinely on at least LIQUID_MIN_DAYS of the last
    LIQUID_WINDOW_DAYS days. Thinner markets are too easy to move to rank in movers or forecasts."""
    latest = conn.execute(
        "SELECT MAX(bucket) FROM price_stats WHERE source = 'closed' AND granularity = 'day'").fetchone()[0]
    if not latest:
        return set()
    since = (parse_utc(latest) - timedelta(days=LIQUID_WINDOW_DAYS - 1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {(r[0], r[1]) for r in conn.execute(
        """SELECT item_id, mod_rank FROM price_stats
           WHERE source = 'closed' AND granularity = 'day' AND suspect = 0 AND volume > 0 AND bucket >= ?
           GROUP BY item_id, mod_rank HAVING COUNT(DISTINCT bucket) >= ?""", (since, LIQUID_MIN_DAYS))}


def asking_price(median_price: float | None, lowest: float | None, listings: int | None) -> float | None:
    """What sellers really ask: the median listing, or the lowest one when there are fewer than
    THIN_LISTINGS listings (two or three joke listings make the median meaningless)."""
    return median_price if (listings or 0) >= THIN_LISTINGS else lowest


def fetched_since(conn: sqlite3.Connection, since: str) -> set[str]:
    return {r[0] for r in conn.execute("SELECT DISTINCT item_id FROM price_stats WHERE fetched_at >= ?", (since,))}


def history(conn: sqlite3.Connection, item: sqlite3.Row, granularity: Granularity,
            rank: int | None = None) -> dict[str, Any]:
    """Completed-trade series and sell/buy listing series at one rank (default: default_rank).

    `closed` leaves out suspect days, which come back separately as `excluded`. Each listing
    point carries `ask`, the asking_price, or None when it is more than SUSPECT_PRICE_RATIO
    times what the item genuinely trades for (a listing nobody pays is not an asking price).
    """
    rank = rank if rank is not None else default_rank(item["max_rank"])
    args = (item["id"], granularity, _stored(rank))
    closed: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for r in conn.execute(
        """SELECT bucket, volume, min_price, max_price, open_price, closed_price,
                  avg_price, wa_price, median, moving_avg, suspect
           FROM price_stats
           WHERE item_id = ? AND source = 'closed' AND granularity = ? AND mod_rank = ?
           ORDER BY bucket""", args):
        (excluded if r["suspect"] else closed).append({k: r[k] for k in r.keys() if k != "suspect"})
    live: dict[str, list[dict[str, Any]]] = {"sell": [], "buy": []}
    for r in conn.execute(
        """SELECT bucket, order_type, volume, min_price, max_price, median
           FROM price_stats
           WHERE item_id = ? AND source = 'live' AND granularity = ? AND mod_rank = ?
             AND order_type IN ('sell', 'buy')
           ORDER BY bucket""", args):
        point = {k: r[k] for k in ("bucket", "volume", "min_price", "max_price", "median")}
        live[r["order_type"]].append({**point, "ask": asking_price(r["median"], r["min_price"], r["volume"])})
    traded = [p["median"] for p in closed if (p["median"] or 0) > 0 and (p["volume"] or 0) > 0]
    if traded:
        ceiling = SUSPECT_PRICE_RATIO * median(traded)
        for point in live["sell"]:
            if point["ask"] is not None and point["ask"] > ceiling:
                point["ask"] = None
    return {"rank": rank, "granularity": granularity, "closed": closed, "excluded": excluded, "live": live}


def ranks_with_history(conn: sqlite3.Connection, item_id: str) -> list[int | None]:
    return [None if r[0] == UNRANKED else r[0] for r in conn.execute(
        "SELECT DISTINCT mod_rank FROM price_stats WHERE item_id = ? ORDER BY mod_rank", (item_id,))]


def movers(conn: sqlite3.Connection, *, days: int, min_volume: int, limit: int) -> dict[str, list[dict[str, Any]]]:
    """Biggest daily-median changes between the newest recorded day and `days` before it.

    Only liquid (item, rank) series (liquid_series) that reach the newest day and traded at
    least `min_volume` times in the window count; suspect days are ignored. Returns item ids;
    callers add names.
    """
    latest = conn.execute(
        "SELECT MAX(bucket) FROM price_stats WHERE source = 'closed' AND granularity = 'day'").fetchone()[0]
    if not latest:
        return {"gainers": [], "losers": []}
    since = (parse_utc(latest) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    liquid = liquid_series(conn)
    series: dict[tuple[str, int], list[sqlite3.Row]] = defaultdict(list)
    for r in conn.execute(
        """SELECT item_id, mod_rank, bucket, median, volume FROM price_stats
           WHERE source = 'closed' AND granularity = 'day' AND suspect = 0 AND bucket >= ?
           ORDER BY bucket""", (since,)):
        if (r["item_id"], r["mod_rank"]) in liquid:
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
