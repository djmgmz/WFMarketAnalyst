"""Shared test data and adapters: canned warframe.market payloads, the wired HttpMarket, fakes."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import httpx

from wfm.catalog import upsert_item
from wfm.client import SharedRateLimiter, WfmClient
from wfm.jobs import Reporter
from wfm.market import COLLECTIONS, HttpMarket
from wfm.recorders import backfill_stats


NOW = "2026-10-31T00:00:00Z"


LIST_ITEM = {
    "id": "i1", "slug": "arcane_energize", "gameRef": "/Lotus/x",
    "tags": ["legendary", "arcane_enhancement"], "maxRank": 5,
    "i18n": {"en": {"name": "Arcane Energize", "icon": "a.png", "thumb": "a.t.png"}},
}


DETAIL_ITEM = {**LIST_ITEM, "rarity": "legendary", "tradingTax": 2100000, "tradable": True,
               "i18n": {"en": {**LIST_ITEM["i18n"]["en"], "description": "On Energy Pickup"}}}


PRIME_PART = {
    "id": "p1", "slug": "ash_prime_systems", "tags": ["prime", "component", "warframe"],
    "ducats": 45, "i18n": {"en": {"name": "Ash Prime Systems", "icon": "s.png"}},
}


PRIME_SET = {**PRIME_PART, "id": "s1", "slug": "ash_prime_set", "ducats": 300, "tags": ["prime", "set", "warframe"]}


STATS_PAYLOAD = {
    "statistics_closed": {
        "48hours": [{"datetime": "2026-10-29T06:00:00.000+00:00", "volume": 1, "median": 120.0, "mod_rank": 5}],
        "90days": [
            {"datetime": "2026-10-23T00:00:00.000+00:00", "volume": 10, "median": 100.0, "mod_rank": 0},
            {"datetime": "2026-10-30T00:00:00.000+00:00", "volume": 12, "median": 125.0, "mod_rank": 0},
        ],
    },
    "statistics_live": {
        "48hours": [],
        "90days": [{"datetime": "2026-10-30T00:00:00.000+00:00", "volume": 50, "min_price": 118,
                    "median": 130, "order_type": "sell", "mod_rank": 0}],
    },
}


WIRE_ORDER = {
    "id": "o1", "type": "sell", "platinum": 8, "quantity": 17, "perTrade": 1, "rank": 0,
    "createdAt": "2026-10-30T13:57:27Z", "itemId": "i1",
    "user": {"id": "u1", "ingameName": "Tenno", "reputation": 10, "platform": "pc",
             "crossplay": True, "status": "ingame"},
}


ROUTES = {
    "/v2/versions": {"data": {"collections": {name: "v1" for name in {"items", *COLLECTIONS.values()}}}},
    "/v2/items": {"data": [LIST_ITEM, PRIME_PART]},
    "/v2/item/arcane_energize": {"data": DETAIL_ITEM},
    "/v2/item/ash_prime_systems": {"data": PRIME_PART},
    "/v1/items/arcane_energize/statistics": {"payload": STATS_PAYLOAD},
    "/v2/orders/item/arcane_energize/top": {"data": {"sell": [WIRE_ORDER], "buy": []}},
    "/v2/riven/weapons": {"data": [{"id": "r1", "slug": "braton", "i18n": {"en": {"name": "Braton"}}}]},
}


def wire_market(tmp_path, routes=ROUTES) -> HttpMarket:
    """The real HTTP adapter, answered by canned payloads instead of the network."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in routes:
            return httpx.Response(200, json=routes[request.url.path])
        if request.url.path.startswith("/v2/") and request.url.path[4:].split("/")[0] in {
                "riven", "lich", "sister", "missions", "npcs", "locations"}:
            return httpx.Response(200, json={"data": []})
        return httpx.Response(404, json={"error": {"request": ["app.errors.notFound"]}})

    client = WfmClient(max_retries=0, transport=httpx.MockTransport(handler),
                       limiter=SharedRateLimiter(1000, tmp_path / "ratelimit.db"))
    return HttpMarket(client)


def with_market(tmp_path, work, routes=ROUTES):
    async def main():
        async with wire_market(tmp_path, routes) as market:
            return await work(market)

    return asyncio.run(main())


class FakeMarket:
    """In-memory Market adapter."""

    def __init__(self, details=None, events=()):
        self.details = details or {}
        self.events = list(events)

    async def item(self, slug):
        return self.details[slug]

    async def new_orders(self, stop):
        for event in self.events:
            yield event


class Notes(Reporter):
    """A Reporter that remembers what it was told."""

    def __init__(self):
        self.done, self.notes, self.current = 0, {}, None

    def advance(self, *, ok=True):
        self.done += 1

    def working_on(self, label):
        self.current = label

    def note(self, **detail):
        self.notes.update(detail)


def backfill(conn, tmp_path, slugs=("arcane_energize",)):
    return with_market(tmp_path, lambda market: backfill_stats(conn, market, list(slugs)))


def _job_row(db, name):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    try:
        return c.execute("SELECT * FROM jobs WHERE name = ?", (name,)).fetchone()
    finally:
        c.close()


def _seed_history(conn, *, trend_items=8, noise_items=8, days=100, seed=7):
    import math

    import numpy as np

    from wfm import prices

    rng = np.random.default_rng(seed)
    start = __import__("datetime").date(2026, 7, 1)

    def add(item_id, name, tags, log_prices):
        upsert_item(conn, {"id": item_id, "slug": item_id, "name": name, "tags": json.dumps(tags)}, NOW, detail=False)
        records = [{"source": "closed", "granularity": "day", "mod_rank": None, "subtype": "", "order_type": "",
                    "bucket": (start + __import__("datetime").timedelta(days=d)).isoformat() + "T00:00:00Z",
                    "median": round(math.exp(lp), 3), "volume": 20} for d, lp in enumerate(log_prices)]
        prices.store(conn, item_id, records)

    for i in range(trend_items):  # ~120p rising 0.6% a day, staying inside the 100–500p band
        lp = math.log(120 + 5 * i) + 0.006 * np.arange(days) + rng.normal(0, 0.003, days)
        add(f"trend_{i}", f"Trend {i}", ["arcane_enhancement"], lp)
    for i in range(noise_items):  # ~8p random walk: under 20p band
        lp = math.log(8) + np.cumsum(rng.normal(0, 0.05, days))
        add(f"noise_{i}", f"Noise {i}", ["mod"], lp)
    conn.commit()


def pd_timestamp(day: str):
    import pandas as pd

    return pd.Timestamp(day)


def pd_timedelta(days: int):
    import pandas as pd

    return pd.Timedelta(days=days)
