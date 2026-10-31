"""Recorders: backfill, the order recorder and watchlist snapshots."""

from __future__ import annotations

import asyncio

from wfm.market import Connection, NewOrder, OnlineCount
from wfm.recorders import backfill_stats, record_new_orders, store_orders, top_summary

from helpers import FakeMarket, Notes, with_market


def test_backfill_resumes_by_skipping_items_fetched_today(client, conn, tmp_path):
    records = with_market(tmp_path, lambda market: market.price_history("arcane_energize"))

    class CountingMarket(FakeMarket):
        def __init__(self):
            super().__init__()
            self.asked = []

        async def price_history(self, slug):
            self.asked.append(slug)
            return records

    slugs = ["arcane_energize", "ash_prime_systems"]
    asyncio.run(backfill_stats(conn, CountingMarket(), slugs[:1]))  # a run that ended after the first item

    again, notes = CountingMarket(), Notes()
    result = asyncio.run(backfill_stats(conn, again, slugs, notes))
    assert again.asked == ["ash_prime_systems"]  # picks up where it ended
    assert result == (1, len(records), 1) and notes.done == 2 and notes.notes == {"skipped": 1}

    forced = CountingMarket()
    asyncio.run(backfill_stats(conn, forced, slugs, refetch=True))
    assert forced.asked == slugs

    conn.execute("UPDATE price_stats SET fetched_at = '2026-01-01T12:00:00Z'")  # fetched on an earlier day
    conn.commit()
    next_day = CountingMarket()
    asyncio.run(backfill_stats(conn, next_day, slugs))
    assert next_day.asked == slugs


def test_order_recorder_stores_new_orders_and_reports(client, conn):
    order = {"id": "o1", "item_id": "i1", "type": "sell", "platinum": 8, "quantity": 17,
             "user_name": "Tenno", "created_at": "2026-10-30T13:57:27Z"}
    market = FakeMarket(events=[Connection("connected"), NewOrder(order), NewOrder(order), OnlineCount(5, 2)])
    notes = Notes()
    asyncio.run(record_new_orders(conn, market, asyncio.Event(), notes))
    assert conn.execute("SELECT COUNT(*) FROM orders_seen").fetchone()[0] == 1  # duplicate ignored
    assert notes.done == 1 and notes.current == "Arcane Energize"
    assert notes.notes["connection"] == "connected" and notes.notes["online"] == {"connections": 5, "authorizedUsers": 2}


def test_store_orders_is_idempotent(conn):
    order = {"id": "o1", "item_id": "i1", "type": "sell", "platinum": 8, "quantity": 1}
    assert store_orders(conn, [order]) == 1
    assert store_orders(conn, [order]) == 0
    assert conn.execute("SELECT created_at FROM orders_seen").fetchone()[0]  # falls back to seen time


def test_top_summary():
    s = top_summary({"sell": [{"platinum": 9}, {"platinum": 8}], "buy": [{"platinum": 5}]})
    assert s == {"best_sell": 8, "best_buy": 5, "sell_count": 2, "buy_count": 1}
