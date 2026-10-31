"""Price history: the default-rank rule, the latest price, suspect trades, liquidity and asking prices."""

from __future__ import annotations

import json

from wfm import prices
from wfm.catalog import upsert_item

from helpers import NOW, backfill


def test_unranked_items_price_and_history_without_a_rank(client, conn, tmp_path):
    from wfm.recorders import backfill_stats

    from helpers import ROUTES, with_market

    unranked = {"statistics_closed": {"90days": [
        {"datetime": "2026-10-29T00:00:00.000+00:00", "volume": 3, "median": 9.0},
        {"datetime": "2026-10-30T00:00:00.000+00:00", "volume": 4, "median": 11.0}]}}
    routes = {**ROUTES, "/v1/items/ash_prime_systems/statistics": {"payload": unranked}}
    with_market(tmp_path, lambda market: backfill_stats(conn, market, ["ash_prime_systems"]), routes)
    item = conn.execute("SELECT * FROM items WHERE slug = 'ash_prime_systems'").fetchone()
    assert prices.default_rank(item["max_rank"]) is None
    hist = prices.history(conn, item, "day")
    assert hist["rank"] is None and [p["median"] for p in hist["closed"]] == [9.0, 11.0]
    assert conn.execute("SELECT price FROM item_prices WHERE item_id = 'p1'").fetchone()[0] == 11.0
    assert prices.ranks_with_history(conn, "p1") == [None]
    body = client.get("/api/items/ash_prime_systems").json()
    assert (body["price"], body["default_rank"], body["historyRanks"]) == (11.0, None, [None])
    assert client.get("/api/ducats").json()[0]["ducats_per_plat"] == round(45 / 11.0, 2)


def _day(day: int, median: float, volume: int, month: int = 9) -> dict:
    return {"source": "closed", "granularity": "day", "mod_rank": None, "subtype": "", "order_type": "",
            "bucket": f"2026-{month:02d}-{day:02d}T00:00:00Z", "median": median, "volume": volume}


def _item(conn, item_id, **fields):
    upsert_item(conn, {"id": item_id, "slug": item_id, "name": item_id, "tags": "[]", **fields}, NOW, detail=False)
    conn.commit()


def test_faked_trades_are_flagged_and_left_out_of_prices(conn):
    """The Xiphos Set pattern: a quiet item 'sells' 199 units at 99,999p, 50 at 9,999p and dumps 151 in a day."""
    _item(conn, "x")
    days = [_day(1, 25, 1), _day(5, 100, 1), _day(9, 99999, 199), _day(12, 50, 1), _day(15, 9999, 50),
            _day(18, 100, 1), _day(21, 50, 151), _day(24, 250, 1), _day(27, 85, 1), _day(29, 99999, 2)]
    prices.store(conn, "x", days)

    flagged = [r[0][8:10] for r in conn.execute(
        "SELECT bucket FROM price_stats WHERE item_id = 'x' AND suspect = 1 ORDER BY bucket")]
    assert flagged == ["09", "15", "21", "29"]  # price 20,000x off, 100x off, a 150x volume dump, the newest day
    assert conn.execute("SELECT price FROM item_prices WHERE item_id = 'x'").fetchone()[0] == 85.0  # not 99,999
    hist = prices.history(conn, conn.execute("SELECT * FROM items WHERE id = 'x'").fetchone(), "day")
    assert [p["bucket"][8:10] for p in hist["excluded"]] == flagged
    assert max(p["median"] for p in hist["closed"]) == 250  # the chart keeps only genuine days

    # Re-storing recomputes from scratch: a corrected day is no longer suspect.
    prices.store(conn, "x", [_day(29, 90, 1)])
    assert conn.execute("SELECT COUNT(*) FROM price_stats WHERE item_id = 'x' AND suspect = 1").fetchone()[0] == 3


def test_the_real_xiphos_set_history_keeps_its_genuine_days(conn):
    """Xiphos Set as recorded: a genuine 50p day sits between two fakes, which must not make it the odd one out."""
    _item(conn, "xiphos")
    recorded = [("08-06", 25, 1), ("08-17", 100, 1), ("09-03", 99999, 199), ("09-09", 50, 1), ("09-21", 9999, 50),
                ("09-28", 100, 1), ("10-01", 50, 151), ("10-04", 250, 1), ("10-26", 85, 1)]
    prices.store(conn, "xiphos", [{**_day(1, median, volume), "bucket": f"2026-{day}T00:00:00Z"}
                                  for day, median, volume in recorded])
    flagged = [r[0][5:10] for r in conn.execute(
        "SELECT bucket FROM price_stats WHERE item_id = 'xiphos' AND suspect = 1 ORDER BY bucket")]
    assert flagged == ["09-03", "09-21", "10-01"]


def test_a_series_needs_enough_days_to_judge_and_real_busy_days_stay(conn):
    _item(conn, "new")
    prices.store(conn, "new", [_day(1, 10, 1), _day(2, 900, 1), _day(3, 11, 1)])  # only 2 other days each
    assert conn.execute("SELECT COUNT(*) FROM price_stats WHERE suspect = 1").fetchone()[0] == 0
    _item(conn, "busy")
    prices.store(conn, "busy", [_day(d, 10, 1) for d in range(1, 8)] + [_day(9, 11, 15)])  # 15x, under 20 trades
    assert conn.execute("SELECT COUNT(*) FROM price_stats WHERE suspect = 1").fetchone()[0] == 0


def test_regular_bulk_trading_and_a_settling_launch_price_are_not_flagged(conn):
    # Ayatan Anasa pattern: quiet days, and every few days hundreds traded at the usual price.
    _item(conn, "bulk")
    prices.store(conn, "bulk", [_day(d, 7, 500 if d % 4 == 0 else 10) for d in range(1, 29)])
    # Launch pattern: 300p on release, settling to 40p over two months (7x overall, gradual).
    _item(conn, "launch")
    prices.store(conn, "launch", [{**_day(1, 0, 0), "bucket": f"2026-{8 + d // 30:02d}-{d % 30 + 1:02d}T00:00:00Z",
                                   "median": round(300 * 0.967 ** d), "volume": 200} for d in range(60)])
    assert conn.execute("SELECT COUNT(*) FROM price_stats WHERE suspect = 1").fetchone()[0] == 0


def test_variants_of_one_item_are_judged_apart(conn):
    _item(conn, "relic")
    intact = [{**_day(d, 6, 40), "subtype": "intact"} for d in range(1, 8)]
    radiant = [{**_day(d, 60, 5), "subtype": "radiant"} for d in range(1, 8)]  # 10x the intact price, every day
    prices.store(conn, "relic", intact + radiant + [{**_day(9, 900, 5), "subtype": "radiant"}])
    flagged = conn.execute("SELECT subtype, bucket FROM price_stats WHERE item_id = 'relic' AND suspect = 1").fetchall()
    assert [(r[0], r[1][8:10]) for r in flagged] == [("radiant", "09")]  # only the radiant outlier


def test_a_set_far_from_the_sum_of_its_parts_is_flagged(conn):
    parts = ["bp", "barrel"]
    _item(conn, "set", set_root=True, set_parts=json.dumps(["set", *parts]))
    for part in parts:
        _item(conn, part, quantity_in_set=1)
        prices.store(conn, part, [_day(d, 30, 5) for d in range(1, 6)])  # parts: 60p together
    # 130p is within 5x of the set's own days but more than 2x the parts' 60p; 25p (a cheap set) is fine.
    prices.store(conn, "set", [_day(1, 55, 3), _day(2, 50, 3), _day(3, 130, 3), _day(4, 60, 3), _day(5, 25, 3)])
    flagged = [r[0][8:10] for r in conn.execute("SELECT bucket FROM price_stats WHERE item_id = 'set' AND suspect = 1")]
    assert flagged == ["03"]
    assert prices.flag_suspect(conn) == 1  # a full recompute agrees

    # A cheap set a few plat above its 2p parts is a normal premium, not a fake.
    _item(conn, "cheap", set_root=True, set_parts=json.dumps(["cheap", "c1", "c2"]))
    for part in ("c1", "c2"):
        _item(conn, part, quantity_in_set=1)
        prices.store(conn, part, [_day(d, 2, 5) for d in range(1, 6)])
    prices.store(conn, "cheap", [_day(d, 9, 3) for d in range(1, 6)])
    assert conn.execute("SELECT COUNT(*) FROM price_stats WHERE item_id = 'cheap' AND suspect = 1").fetchone()[0] == 0


def test_only_items_with_enough_genuine_trading_days_are_liquid(conn):
    _item(conn, "liquid")
    _item(conn, "thin")
    prices.store(conn, "liquid", [_day(d, 20, 3, month=10) for d in range(1, 11)])
    prices.store(conn, "thin", [_day(d, 20, 3, month=10) for d in range(1, 10)])  # nine days
    assert prices.liquid_series(conn) == {("liquid", prices.UNRANKED)}


def test_history_drops_asking_prices_nobody_pays(conn):
    _item(conn, "thin")
    listing = lambda day, count, low, mid: {"source": "live", "granularity": "day", "mod_rank": None, "subtype": "",
                                            "order_type": "sell", "bucket": f"2026-09-{day:02d}T00:00:00Z",
                                            "volume": count, "min_price": low, "median": mid}
    prices.store(conn, "thin", [_day(d, 100, 1) for d in range(1, 6)])
    prices.store(conn, "thin", [listing(1, 1, 80000, 80000), listing(2, 3, 120, 4500), listing(3, 8, 90, 110)])
    hist = prices.history(conn, conn.execute("SELECT * FROM items WHERE id = 'thin'").fetchone(), "day")
    assert [p["ask"] for p in hist["live"]["sell"]] == [None, 120, 110]  # junk, lowest of 3, median of 8


def test_asking_price_uses_the_lowest_listing_when_there_are_few():
    assert prices.asking_price(4500, 1000, 4) == 1000  # four listings, one at 80,000p: the median means nothing
    assert prices.asking_price(130, 110, 50) == 130
    assert prices.asking_price(None, None, None) is None


def test_backfill_flags_suspect_days_as_it_stores(client, conn, tmp_path):
    backfill(conn, tmp_path)  # the canned arcane_energize history is clean
    assert conn.execute("SELECT COUNT(*) FROM price_stats WHERE suspect = 1").fetchone()[0] == 0
