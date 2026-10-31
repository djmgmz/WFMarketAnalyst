"""Offline tests: a temporary SQLite database and canned warframe.market payloads. No network.

warframe.market is reached only through the Market module. Tests either drive the
real HttpMarket adapter with canned wire payloads (httpx.MockTransport), or use
FakeMarket, an in-memory adapter of the same interface.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from wfm import api
from wfm.catalog import categorize, sync_catalog, sync_item_details, upsert_item
from wfm.client import SharedRateLimiter, WfmClient
from wfm.db import connect, init_db
from wfm.jobs import Reporter
from wfm.market import COLLECTIONS, Connection, HttpMarket, MarketError, NewOrder, OnlineCount
from wfm.recorders import backfill_stats, record_new_orders, store_orders, top_summary

NOW = "2026-10-31T00:00:00Z"

# -- canned wire payloads (what warframe.market sends) -----------------------

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
        "90days": [{"datetime": "2026-10-30T00:00:00.000+00:00", "volume": 50, "median": 130,
                    "order_type": "sell", "mod_rank": 0}],
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


# -- fixtures ------------------------------------------------------------------

@pytest.fixture()
def conn(tmp_path) -> sqlite3.Connection:
    c = connect(tmp_path / "test.db")
    init_db(c)
    yield c
    c.close()


@pytest.fixture()
def client(conn: sqlite3.Connection, tmp_path) -> TestClient:
    with_market(tmp_path, lambda market: sync_catalog(conn, market))

    def override():
        yield conn

    api.app.dependency_overrides[api.get_db] = override
    yield TestClient(api.app)  # no context manager: skips the lifespan (no live market)
    api.app.dependency_overrides.clear()


def backfill(conn, tmp_path, slugs=("arcane_energize",)):
    return with_market(tmp_path, lambda market: backfill_stats(conn, market, list(slugs)))


# -- Market module (HTTP adapter) ---------------------------------------------

def test_http_market_turns_wire_payloads_into_records(tmp_path):
    async def read(market):
        return (await market.versions(), await market.items(), await market.item("arcane_energize"),
                await market.price_history("arcane_energize"), await market.collection("riven_weapon"),
                await market.best_orders("arcane_energize", 0))

    versions, items, detail, history, rivens, top = with_market(tmp_path, read)
    assert versions["items"] == "v1"
    assert [i["slug"] for i in items] == ["arcane_energize", "ash_prime_systems"]
    assert items[0]["trading_tax"] is None and detail["trading_tax"] == 2100000
    assert detail["description"] == "On Energy Pickup" and json.loads(detail["tags"]) == LIST_ITEM["tags"]
    assert len(history) == 4 and history[0]["bucket"] == "2026-10-29T06:00:00Z"
    live = next(r for r in history if r["source"] == "live")
    assert (live["order_type"], live["granularity"], live["mod_rank"]) == ("sell", "day", 0)
    assert rivens == [{"id": "r1", "slug": "braton", "name": "Braton", "data": json.dumps(ROUTES["/v2/riven/weapons"]["data"][0])}]
    assert top["sell"][0]["platinum"] == 8 and top["buy"] == []


def test_http_market_raises_market_error(tmp_path):
    with pytest.raises(MarketError):
        with_market(tmp_path, lambda market: market.item("no_such_item"))


def test_websocket_messages_become_events():
    from wfm.market import _ws_event  # the adapter's own parsing, tested at its internal seam

    event = _ws_event({"route": "@wfm|event/subscriptions/newOrder", "payload": WIRE_ORDER})
    assert isinstance(event, NewOrder)
    assert (event.order["item_id"], event.order["user_name"], event.order["crossplay"]) == ("i1", "Tenno", 1)
    assert _ws_event({"route": "@wfm|event/reports/online", "payload": {"connections": 5, "authorizedUsers": 2}}) \
        == OnlineCount(5, 2)
    assert _ws_event({"route": "@wfm|event/subscriptions/newOrder", "payload": {"type": "sell"}}) is None


# -- Catalog -------------------------------------------------------------------

def test_categorize_priority():
    assert categorize(["prime", "mod"]) == "Mod"
    assert categorize(["prime", "component"]) == "Prime"
    assert categorize(["relic", "prime"]) == "Relic"
    assert categorize(["something_new"]) == "Other"


def test_catalog_sync_stores_items_details_and_collections(client, conn):
    row = conn.execute("SELECT * FROM items WHERE id = 'i1'").fetchone()
    assert (row["category"], row["trading_tax"], row["description"]) == ("Arcane", 2100000, "On Energy Pickup")
    assert row["detail_synced_at"] is not None
    tags = {r["tag"] for r in conn.execute("SELECT tag FROM item_tags WHERE item_id = 'i1'")}
    assert tags == {"legendary", "arcane_enhancement"}
    assert conn.execute("SELECT name FROM aux_catalog WHERE kind = 'riven_weapon'").fetchone()[0] == "Braton"


def test_list_refresh_keeps_detail_fields(client, conn, tmp_path):
    from wfm.catalog import sync_item_list

    with_market(tmp_path, lambda market: sync_item_list(market, conn))
    row = conn.execute("SELECT * FROM items WHERE id = 'i1'").fetchone()
    assert row["trading_tax"] == 2100000 and row["description"] == "On Energy Pickup"


def test_item_details_commit_before_next_request(tmp_path):
    """No write lock may be held while waiting on warframe.market (the 'database is locked' bug)."""
    db = tmp_path / "lock.db"
    conn = connect(db)
    init_db(conn)
    records = with_market(tmp_path, lambda market: market.items())
    for record in records:
        upsert_item(conn, record, NOW, detail=False)
    conn.commit()

    class ProbingMarket(FakeMarket):
        async def item(self, slug):
            # Another process must be able to write while this request is "in flight".
            other = sqlite3.connect(db, timeout=0.1)
            other.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('probe', 'ok')")
            other.commit()
            other.close()
            return next(r for r in records if r["slug"] == slug)

    assert asyncio.run(sync_item_details(ProbingMarket(), conn, concurrency=1)) == (2, 0)
    conn.close()


def test_retry_locked_waits_out_another_writer(tmp_path):
    from wfm.db import retry_locked

    db = tmp_path / "busy.db"
    conn = sqlite3.connect(db, timeout=0.05)
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    other = sqlite3.connect(db)
    other.execute("BEGIN IMMEDIATE")  # another process holds the write lock

    attempts = []

    def write():
        attempts.append(1)
        if len(attempts) == 2:
            other.rollback()  # the other writer finishes while we pause
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        return "saved"

    assert asyncio.run(retry_locked(conn, write, pause=0)) == "saved" and len(attempts) == 2

    other.execute("BEGIN IMMEDIATE")  # held for good: the last attempt's error is raised
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        asyncio.run(retry_locked(conn, lambda: conn.execute("INSERT INTO t VALUES (2)"), attempts=3, pause=0))
    other.rollback()

    calls = []

    def broken():
        calls.append(1)
        conn.execute("INSERT INTO missing VALUES (1)")

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        asyncio.run(retry_locked(conn, broken, pause=0))
    assert calls == [1]  # only "locked" is retried
    conn.close()
    other.close()


# -- Item images -----------------------------------------------------------------

COMMUNITY_ITEMS = [  # the shape of the WFCD item list
    {"uniqueName": "/Lotus/Arcane/Energize", "name": "Arcane Energize", "imageName": "Energize.png"},
    {"uniqueName": "/Lotus/Projections/T1G", "name": "Lith G1 Intact", "imageName": "Unused.png"},
    {"uniqueName": "/Lotus/Projections/T1GBronze", "name": "Lith G1 Intact", "imageName": "RelicLith.png"},
    {"uniqueName": "/Lotus/Warframes/Ash", "name": "Ash Prime", "imageName": "AshPrime.png", "components": [
        {"uniqueName": "/Lotus/Recipes/AshPrimeHelmetComponent", "name": "Neuroptics", "imageName": "PrimeHelmet.png"},
        {"uniqueName": "/Lotus/Recipes/AshPrimeBlueprint", "name": "Blueprint", "imageName": "blueprint.png"},
    ]},
    {"uniqueName": "/Lotus/Mods/QuickThinkingBeginner", "name": "Quick Thinking", "imageName": "QuickThinking.jpg"},
    {"uniqueName": "/Lotus/Skins/A", "name": "Shared Name", "imageName": "A.png"},
    {"uniqueName": "/Lotus/Skins/B", "name": "Shared Name", "imageName": "B.png"},
    {"uniqueName": "/Lotus/Evil", "name": "Evil", "imageName": "../../etc/passwd"},
]


def test_images_match_by_game_id_relic_part_and_unique_name():
    from wfm.images import match_images

    catalog = [
        ("a", "Arcane Energize", "/Lotus/Arcane/Energize"),         # exact in-game id
        ("r", "Lith G1 Relic", "/Lotus/Projections/T1G"),           # the exact id wins over the intact version
        ("r2", "Lith G2 Relic", "/Lotus/Projections/T1GX"),         # no match at all
        ("n", "Ash Prime Neuroptics Blueprint", "/Lotus/Recipes/AshPrimeHelmetBlueprint"),  # part -> component
        ("b", "Ash Prime Blueprint", "/Lotus/Recipes/AshPrimeBlueprint"),                   # nested component id
        ("q", "Quick Thinking", "/Lotus/Mods/QuickThinking"),       # unique name
        ("s", "Shared Name", "/Lotus/Other"),                       # ambiguous name: not guessed
        ("e", "Evil", "/Lotus/Evil"),                               # unsafe file name: ignored
    ]
    assert match_images(COMMUNITY_ITEMS, catalog) == {
        "a": "Energize.png", "r": "Unused.png", "n": "PrimeHelmet.png", "b": "blueprint.png", "q": "QuickThinking.jpg"}
    relic_only = [("r", "Lith G1 Relic", "/Lotus/Projections/T1G")]
    assert match_images(COMMUNITY_ITEMS[2:3], relic_only) == {"r": "RelicLith.png"}  # base relic -> intact version


class FakeImageSource:
    def __init__(self, community, broken=()):
        self.community, self.broken, self.downloads = community, set(broken), []

    async def items(self):
        return self.community

    async def image(self, name):
        self.downloads.append(name)
        if name in self.broken:
            raise httpx.HTTPStatusError("404", request=httpx.Request("GET", name), response=httpx.Response(404))
        return f"bytes of {name}".encode()


def test_sync_images_downloads_each_file_once_and_resumes(conn, tmp_path, monkeypatch):
    from wfm import images

    monkeypatch.setattr(images, "PAUSE_SECONDS", 0)
    monkeypatch.setattr(images, "RETRIES", 0)
    for item_id, name, ref in [("a", "Arcane Energize", "/Lotus/Arcane/Energize"),
                               ("b1", "Ash Prime Blueprint", "/Lotus/Recipes/AshPrimeBlueprint"),
                               ("b2", "Other Prime Blueprint", "/Lotus/Recipes/OtherPrimeBlueprint"),
                               ("q", "Quick Thinking", "/Lotus/Mods/QuickThinking")]:
        upsert_item(conn, {"id": item_id, "slug": item_id, "name": name, "game_ref": ref, "tags": "[]"}, NOW, detail=False)
    conn.commit()
    community = [*COMMUNITY_ITEMS, {"uniqueName": "/Lotus/Recipes/OtherPrimeBlueprint", "name": "Blueprint",
                                    "imageName": "blueprint.png"}]
    folder = tmp_path / "images"

    first = FakeImageSource(community, broken={"QuickThinking.jpg"})
    notes = Notes()
    summary = asyncio.run(images.sync_images(conn, first, folder, notes))
    assert sorted(first.downloads) == ["Energize.png", "QuickThinking.jpg", "blueprint.png"]  # the shared file once
    assert summary._asdict() == {"items": 4, "matched": 4, "files": 3, "downloaded": 2, "already_had": 0, "failed": 1}
    stored = dict(conn.execute("SELECT id, image FROM items"))
    assert stored == {"a": "Energize.png", "b1": "blueprint.png", "b2": "blueprint.png", "q": None}  # failed: unset
    assert (folder / "blueprint.png").read_bytes() == b"bytes of blueprint.png" and not list(folder.glob(".*.part"))
    assert notes.done == 3

    # A stale value (the file was deleted) is cleared, and the missing files are fetched again.
    (folder / "Energize.png").unlink()
    again = FakeImageSource(community, broken={"Energize.png"})
    summary = asyncio.run(images.sync_images(conn, again, folder))
    assert sorted(again.downloads) == ["Energize.png", "QuickThinking.jpg"]  # resumes: only missing files
    assert (summary.already_had, summary.downloaded, summary.failed) == (1, 1, 1)
    stored = dict(conn.execute("SELECT id, image FROM items"))
    assert stored == {"a": None, "b1": "blueprint.png", "b2": "blueprint.png", "q": "QuickThinking.jpg"}


def test_http_image_source_identifies_itself_and_follows_the_cdn_redirect():
    from wfm.config import settings
    from wfm.images import HttpImageSource

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.url.path, request.headers["user-agent"]))
        if request.url.host == "api.warframestat.us":
            assert request.url.params["only"] == "uniqueName,name,imageName,components"
            return httpx.Response(200, json=COMMUNITY_ITEMS[:1])
        if request.url.host == "cdn.warframestat.us":
            return httpx.Response(301, headers={"location": "https://raw.githubusercontent.com" + request.url.path})
        if request.url.path.endswith("Energize.png"):
            return httpx.Response(200, content=b"png", headers={"content-type": "image/png"})
        return httpx.Response(200, text="<html>", headers={"content-type": "text/html"})

    async def run():
        async with HttpImageSource(transport=httpx.MockTransport(handler)) as source:
            items = await source.items()
            data = await source.image("Energize.png")
            with pytest.raises(ValueError):
                await source.image("Page.png")  # a web page instead of an image is refused
            return items, data

    items, data = asyncio.run(run())
    assert items == COMMUNITY_ITEMS[:1] and data == b"png"
    assert {ua for *_, ua in seen} == {settings.user_agent}


def test_item_images_are_served_and_listed(client, conn, tmp_path, monkeypatch):
    from wfm.config import Settings

    monkeypatch.setattr(api, "settings", Settings(db_path=tmp_path / "wfm.db"))
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "Energize.png").write_bytes(b"png bytes")
    conn.execute("UPDATE items SET image = 'Energize.png' WHERE id = 'i1'")
    conn.commit()

    items = {i["slug"]: i["image"] for i in client.get("/api/items").json()["items"]}
    assert items == {"arcane_energize": "Energize.png", "ash_prime_systems": None}
    resp = client.get("/api/images/Energize.png")
    assert resp.status_code == 200 and resp.content == b"png bytes" and "max-age" in resp.headers["cache-control"]
    assert client.get("/api/images/Missing.png").status_code == 404
    assert client.get("/api/images/..%2Fwfm.db").status_code == 404


# -- Recorders -----------------------------------------------------------------

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


# -- Dashboard API ---------------------------------------------------------------

def test_items_endpoint_filters_and_prices(client, conn, tmp_path):
    backfill(conn, tmp_path)
    body = client.get("/api/items", params={"category": "Arcane"}).json()
    assert body["total"] == 1
    assert body["items"][0]["price"] == 125.0
    assert body["items"][0]["tags"] == ["legendary", "arcane_enhancement"]
    assert client.get("/api/items", params={"q": "ash"}).json()["total"] == 1


def test_history_and_movers(client, conn, tmp_path):
    backfill(conn, tmp_path)
    hist = client.get("/api/items/arcane_energize/history").json()
    assert hist["rank"] == 0 and [p["median"] for p in hist["closed"]] == [100.0, 125.0]
    assert hist["live"]["sell"][0]["median"] == 130
    movers = client.get("/api/movers", params={"min_volume": 0}).json()
    assert movers["gainers"][0]["change"] == 0.25
    assert movers["losers"] == []  # a rise never shows up as a loser


def test_watchlist_roundtrip(client):
    assert client.put("/api/watchlist/ash_prime_systems", json={"target_price": 10}).status_code == 200
    assert [w["slug"] for w in client.get("/api/watchlist").json()] == ["ash_prime_systems"]
    client.delete("/api/watchlist/ash_prime_systems")
    assert client.get("/api/watchlist").json() == []


def test_unknown_item_404(client):
    assert client.get("/api/items/nope").status_code == 404


def test_overview_counts(client):
    body = client.get("/api/overview").json()
    assert body["catalog"]["items"] == 2
    assert body["catalog"]["primeParts"] == 1
    assert body["recording"]["ordersTotal"] == 0


def test_item_detail_includes_latest_price(client, conn, tmp_path):
    backfill(conn, tmp_path)
    body = client.get("/api/items/arcane_energize").json()
    assert body["price"] == 125.0
    assert body["historyRanks"] == [0, 5]


def test_ducats_excludes_sets(client, conn, tmp_path):
    routes = {**ROUTES, "/v2/items": {"data": [LIST_ITEM, PRIME_PART, PRIME_SET]}}
    records = with_market(tmp_path, lambda market: market.items(), routes)
    upsert_item(conn, next(r for r in records if r["slug"] == "ash_prime_set"), NOW, detail=False)
    conn.commit()
    assert [d["slug"] for d in client.get("/api/ducats").json()] == ["ash_prime_systems"]


def test_shared_rate_limiter_spaces_slots_across_instances(tmp_path):
    path = tmp_path / "ratelimit.db"
    a, b = SharedRateLimiter(2.0, path), SharedRateLimiter(2.0, path)  # two "processes"
    delays = [a.reserve(), b.reserve(), a.reserve(), b.reserve()]
    # Each reservation lands one interval (0.5s) after the previous, whoever made it.
    assert delays[0] == pytest.approx(0.0, abs=0.05)
    for earlier, later in zip(delays, delays[1:]):
        assert later - earlier == pytest.approx(0.5, abs=0.05)


# ---------------------------------------------------------------------------
# Job module: everything below goes through its interface
# (Job.run inside a job; status / start / stop from the dashboard).
# ---------------------------------------------------------------------------

def _job_row(db, name):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    try:
        return c.execute("SELECT * FROM jobs WHERE name = ?", (name,)).fetchone()
    finally:
        c.close()


@pytest.fixture()
def job_db(tmp_path):
    db = tmp_path / "jobs.db"
    c = connect(db)
    init_db(c)
    c.close()
    return db


def test_job_reports_progress_and_finishes(job_db):
    import time

    from wfm.jobs import Job

    with Job.run("backfill", total=3, scope="Arcane", db_path=job_db, flush_seconds=0.05, terminal=False) as job:
        job.working_on("arcane_energize")
        job.advance()
        job.advance(ok=False)
        time.sleep(0.3)  # let the heartbeat flush
        row = _job_row(job_db, "backfill")
        assert (row["status"], row["total"], row["done"], row["failed"], row["current"]) == \
            ("running", 3, 2, 1, "arcane_energize")
        assert json.loads(row["detail"]) == {"scope": "Arcane"}
        job.message = "2 items"
    row = _job_row(job_db, "backfill")
    assert job.status == "finished" and (row["status"], row["message"]) == ("finished", "2 items")
    assert row["finished_at"] and row["current"] is None


def test_a_second_run_of_a_live_job_is_refused(job_db, monkeypatch):
    from wfm import jobs
    from wfm.jobs import Job, JobConflict

    with Job.run("sync-images", db_path=job_db, flush_seconds=0.05, terminal=False):
        with pytest.raises(JobConflict):
            with Job.run("sync-images", db_path=job_db, terminal=False):
                pass  # e.g. the command line while the dashboard's run is going
        with Job.run("backfill", db_path=job_db, terminal=False):
            pass  # other jobs are unaffected

    # A process the dashboard launched takes over the 'running' row start() wrote for it.
    monkeypatch.setattr(jobs, "_spawn", lambda name, args: None)
    c = connect(job_db)
    jobs.start(c, "sync-images")
    monkeypatch.setenv(jobs.LAUNCHED_BY_DASHBOARD, "sync-images")
    with Job.run("sync-images", db_path=job_db, terminal=False):
        pass
    monkeypatch.delenv(jobs.LAUNCHED_BY_DASHBOARD)

    # A run whose process died (stalled) does not block a new one.
    c.execute("UPDATE jobs SET status = 'running', heartbeat_at = '2026-01-01T00:00:00Z' WHERE name = 'sync-images'")
    c.commit()
    c.close()
    with Job.run("sync-images", db_path=job_db, terminal=False):
        pass
    assert _job_row(job_db, "sync-images")["status"] == "finished"


def test_ctrl_c_stops_and_errors_fail(job_db):
    from wfm.jobs import Job

    with Job.run("record", db_path=job_db, terminal=False) as job:
        raise KeyboardInterrupt  # Ctrl+C ends the block cleanly
    assert job.status == "stopped" and _job_row(job_db, "record")["status"] == "stopped"

    with pytest.raises(RuntimeError):
        with Job.run("record", db_path=job_db, terminal=False):
            raise RuntimeError("socket exploded")
    row = _job_row(job_db, "record")
    assert (row["status"], row["message"]) == ("failed", "socket exploded")


def test_dashboard_stop_interrupts_the_job_like_ctrl_c(job_db):
    """End to end: Stop sets the flag, the heartbeat raises KeyboardInterrupt, the job ends 'stopped'."""
    import time

    from wfm import jobs
    from wfm.jobs import Job

    with Job.run("record", db_path=job_db, flush_seconds=0.05, terminal=False) as job:
        c = connect(job_db)
        assert jobs.stop(c, "record") == "stopping"
        c.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:  # the interrupt lands in here
            time.sleep(0.02)
        pytest.fail("the job was not interrupted")
    assert job.status == "stopped"
    assert _job_row(job_db, "record")["stop_requested"] == 0  # cleared once stopped


def test_start_writes_the_row_before_launching_and_refuses_duplicates(conn, monkeypatch, tmp_path):
    from wfm import jobs

    launched = []
    monkeypatch.setattr(jobs, "_spawn", lambda name, args: launched.append((name, args)))
    upsert_item(conn, with_market(tmp_path, lambda market: market.item("arcane_energize")), NOW, detail=True)
    conn.commit()
    jobs.start(conn, "backfill", jobs.Scope(category="Arcane"))
    assert launched == [("backfill", ["backfill", "--category", "Arcane"])]
    row = conn.execute("SELECT * FROM jobs WHERE name = 'backfill'").fetchone()
    assert (row["status"], row["current"], json.loads(row["detail"])) == ("running", "starting", {"scope": "Arcane"})
    with pytest.raises(jobs.JobConflict):  # second click before the process even started
        jobs.start(conn, "backfill", jobs.Scope(category="Arcane"))
    assert len(launched) == 1


def test_launched_job_keeps_a_stop_clicked_during_startup(job_db, monkeypatch):
    import threading

    from wfm import jobs
    from wfm.jobs import Job

    monkeypatch.setattr(jobs, "_spawn", lambda name, args: None)
    c = connect(job_db)
    jobs.start(c, "sync-catalog")
    jobs.stop(c, "sync-catalog")  # clicked before the process wrote its first heartbeat
    c.close()
    stopped = threading.Event()
    monkeypatch.setenv(jobs.LAUNCHED_BY_DASHBOARD, "sync-catalog")  # as _spawn sets it for the process
    with Job.run("sync-catalog", db_path=job_db, flush_seconds=0.05, on_stop=stopped.set, terminal=False):
        assert stopped.wait(2), "the pending stop was lost when the job took over its row"


def test_job_endpoints(client, conn, monkeypatch):
    from wfm import jobs
    from wfm.db import utc_now

    monkeypatch.setattr(jobs, "_spawn", lambda name, args: None)
    body = client.get("/api/jobs").json()
    assert [j["name"] for j in body] == ["record", "backfill", "sync-catalog", "train-forecast", "sync-images"]
    backfill = body[1]
    assert (backfill["kind"], backfill["scoped"], backfill["run"]) == ("progress", True, None)
    assert backfill["command"] == "python -m wfm backfill --category Arcane"

    assert client.post("/api/jobs/nope/start").status_code == 404
    assert client.post("/api/jobs/backfill/start", json={"category": "Nope"}).status_code == 422
    assert client.post("/api/jobs/backfill/start", json={"slugs": ["missing_item"]}).status_code == 422
    assert client.post("/api/jobs/backfill/start", json={"watchlist": True}).status_code == 422  # empty
    assert client.post("/api/jobs/backfill/start", json={}).status_code == 422  # no scope
    assert client.post("/api/jobs/backfill/start", json={"category": "Arcane"}).status_code == 202
    assert client.post("/api/jobs/backfill/start", json={"category": "Arcane"}).status_code == 409

    assert client.post("/api/jobs/record/stop").status_code == 409  # never ran
    conn.execute("""INSERT INTO jobs (name, status, started_at, heartbeat_at, detail)
                    VALUES ('record', 'running', ?, '2026-10-31T00:00:00Z', '{}')""", (utc_now(),))
    conn.commit()
    runs = {j["name"]: j["run"] for j in client.get("/api/jobs").json()}
    assert runs["record"]["state"] == "stalled" and runs["backfill"]["state"] == "running"
    assert client.post("/api/jobs/backfill/stop").json()["outcome"] == "stopping"
    assert client.post("/api/jobs/record/stop").json()["outcome"] == "cleared"  # its process is gone


def test_terminal_lines_render_job_snapshots():
    import io

    from wfm.db import utc_now
    from wfm.terminal import TerminalLine, progress_line, recorder_line

    snap = {"total": 4, "done": 2, "failed": 1, "current": "arcane_grace", "started_at": utc_now(),
            "detail": {"connection": "connected", "last_order_at": utc_now(), "snapshots": 3}}
    rich, plain = progress_line("Backfill")(snap)
    assert rich.startswith("█") and "Backfill 2/4 (50%)" in rich and "ETA" in rich and "arcane_grace" in rich
    assert plain.startswith("Backfill 2/4 (50%) | ") and "1 failed" in plain and plain.isascii()
    resumed = {**snap, "started_at": "2026-10-31T00:00:00Z", "detail": {"skipped": 2}}  # nothing fetched yet
    assert "ETA" not in progress_line("Backfill")(resumed)[1]
    rich, plain = recorder_line({**snap, "done": 7})
    assert "connected" in rich and "7 new orders" in rich and "3 watchlist prices" in rich and plain.isascii()

    stream = io.StringIO()  # not a terminal: plain lines, no carriage returns
    line = TerminalLine(lambda: progress_line("Backfill")(snap), stream=stream)
    line.close()
    assert stream.getvalue().startswith("Backfill 2/4") and "\r" not in stream.getvalue()


def test_migration_adds_columns_to_existing_jobs_table(tmp_path):
    db = tmp_path / "old.db"
    old = sqlite3.connect(db)
    old.execute("CREATE TABLE jobs (name TEXT PRIMARY KEY, status TEXT NOT NULL, started_at TEXT NOT NULL, "
                "heartbeat_at TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}')")
    old.commit()
    old.close()
    c = connect(db)
    init_db(c)
    assert "stop_requested" in {r[1] for r in c.execute("PRAGMA table_info(jobs)")}
    c.close()


# -- Price history module ---------------------------------------------------------

def test_unranked_items_price_and_history_without_a_rank(client, conn, tmp_path):
    from wfm import prices

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


# -- Catalog freshness --------------------------------------------------------------

def test_sync_plan_follows_stored_versions(client, conn):
    from wfm.catalog import plan_sync

    versions = ROUTES["/v2/versions"]["data"]["collections"]
    up_to_date = plan_sync(conn, versions)
    assert (up_to_date.item_list, up_to_date.kinds) == (False, ())  # empty kinds are not refetched forever

    plan = plan_sync(conn, {**versions, "items": "v2", "liches": "v2"})
    assert plan.item_list is True
    assert plan.kinds == ("lich_weapon", "lich_ephemera", "lich_quirk")
    assert plan_sync(conn, versions, force=True).kinds == tuple(COLLECTIONS)


def test_new_items_version_does_not_refetch_existing_details(client, conn, tmp_path):
    routes = {**ROUTES, "/v2/versions": {"data": {"collections": {
        **ROUTES["/v2/versions"]["data"]["collections"], "items": "v2"}}}}
    summary = with_market(tmp_path, lambda market: sync_catalog(conn, market), routes)
    assert summary["plan"]["item_list"] is True
    assert summary["details"] == (0, 0)  # both items already have details
    assert conn.execute("SELECT value FROM meta WHERE key = 'version:items'").fetchone()[0] == "v2"


def test_interrupted_detail_refresh_resumes_instead_of_being_lost(client, conn, tmp_path):
    """The old bug: a refresh stopped halfway was forgotten. Now it is just items lacking details."""
    first = with_market(tmp_path, lambda market: sync_catalog(conn, market, refresh_details=True, detail_limit=1))
    assert first["details_forgotten"] == 2 and first["details"] == (1, 0)  # "stopped" after one item
    resumed = with_market(tmp_path, lambda market: sync_catalog(conn, market))
    assert resumed["details"] == (1, 0)  # picks up the remaining item
    assert conn.execute("SELECT COUNT(*) FROM items WHERE detail_synced_at IS NULL").fetchone()[0] == 0


def test_collection_version_waits_until_every_kind_synced(conn, tmp_path):
    from wfm.catalog import sync_aux

    attributes_fail = {**ROUTES, "/v2/riven/attributes": {"error": {"request": ["boom"]}}}
    results = with_market(
        tmp_path, lambda market: sync_aux(market, conn, ["riven_weapon", "riven_attribute"], {"rivens": "v9"}),
        attributes_fail)
    assert results == {"riven_weapon": 1, "riven_attribute": "failed"}
    assert conn.execute("SELECT value FROM meta WHERE key = 'version:rivens'").fetchone() is None


# -- Forecast module -------------------------------------------------------------
# Synthetic history: "Arcane" items trend steadily (predictable), "Mod" items are a
# random walk (unpredictable). They sit in different price bands, so both segment
# kinds separate the predictable items from the unpredictable ones.

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


def test_forecast_features_never_look_ahead(conn):
    from wfm import forecast

    _seed_history(conn, trend_items=1, noise_items=0, days=40)
    before = forecast._dataset(conn)
    conn.execute("UPDATE price_stats SET median = median * 3 WHERE bucket >= '2026-07-31'")  # change the future
    after = forecast._dataset(conn)
    day = pd_timestamp("2026-07-25")
    feature_cols = forecast.NUMERIC + ["band"]
    assert before.loc[before["date"] == day, feature_cols].equals(after.loc[after["date"] == day, feature_cols])
    assert not before.loc[before["date"] == day, "y_7"].equals(after.loc[after["date"] == day, "y_7"])  # only the target moves


def test_forecast_target_is_the_average_over_the_next_h_days(conn):
    import math

    from wfm import forecast

    _seed_history(conn, trend_items=1, noise_items=0, days=40)
    data = forecast._dataset(conn).set_index("date")
    day = pd_timestamp("2026-07-20")
    week = [math.log(data.loc[day + pd_timedelta(k), "price"]) for k in range(1, 8)]
    assert data.loc[day, "y_7"] == pytest.approx(sum(week) / 7 - math.log(data.loc[day, "price"]))
    tomorrow = data.loc[day + pd_timedelta(1), "price"]
    assert data.loc[day, "y_1"] == pytest.approx(math.log(tomorrow / data.loc[day, "price"]))  # h = 1: tomorrow

    # A day without trades carries the last price forward; a week without any trades has no target.
    conn.execute("DELETE FROM price_stats WHERE bucket LIKE '2026-07-27%'")
    data = forecast._dataset(conn).set_index("date")
    assert not math.isnan(data.loc[day, "y_7"]) and math.isnan(data.loc[pd_timestamp("2026-07-26"), "y_1"])
    conn.execute("DELETE FROM price_stats WHERE bucket >= '2026-07-21' AND bucket < '2026-07-28'")
    assert math.isnan(forecast._dataset(conn).set_index("date").loc[day, "y_7"])


def test_forecast_split_never_trains_on_labels_from_the_test_window(conn):
    import pandas as pd

    from wfm import forecast

    _seed_history(conn, trend_items=2, noise_items=0, days=60)
    data = forecast._dataset(conn)
    cutoff = data["date"].max() - pd.Timedelta(days=forecast.TEST_DAYS)
    for h in forecast.HORIZONS:
        train_rows, test_rows = forecast._split(data[data[f"y_{h}"].notna()], cutoff, h)
        assert (train_rows["date"] + pd.Timedelta(days=h)).max() <= cutoff < test_rows["date"].min()


def test_forecast_uses_the_model_only_where_it_beats_no_change(conn):
    from wfm import forecast

    _seed_history(conn)
    report = forecast.train(conn)
    week = report["horizons"]["7"]
    assert week["status"] == "tested"
    assert week["segments"]["category"]["Arcane"]["skill"] > 0.3  # the trend is learnable
    assert week["uses_model"] == {"category": {"Arcane": True, "Mod": False}, "band": {"100–500p": True, "under 20p": False}}
    assert week["segments"]["category"]["Mod"]["skill"] <= 0  # never claims skill on a random walk

    methods = {(r["item_id"].split("_")[0], r["method"])
               for r in conn.execute("SELECT item_id, method FROM forecasts WHERE horizon = 7")}
    assert methods == {("trend", "model"), ("noise", "baseline")}
    trend = forecast.forecast_for(conn, "trend_0", None)
    assert [f["horizon"] for f in trend] == [1, 7]
    week_ahead = trend[1]
    assert week_ahead["low"] <= week_ahead["predicted"] <= week_ahead["high"]
    assert week_ahead["predicted"] > week_ahead["base_price"] * 1.015  # follows the trend: ~2.4% over days 1-7 on average
    noise = forecast.forecast_for(conn, "noise_0", None)[1]
    assert noise["predicted"] == noise["base_price"] and noise["low"] < noise["base_price"] < noise["high"]
    assert forecast.latest_report(conn)["made_at"] == report["made_at"]
    assert [m["item_id"] for m in forecast.predicted_movers(conn, 7, 3)["gainers"]][:1] != []


def test_forecast_falls_back_on_small_segments_and_too_little_data(conn):
    from wfm import forecast

    _seed_history(conn, trend_items=3, noise_items=0, days=60)  # 3 series x 7 test days < 30 rows
    report = forecast.train(conn)
    assert report["horizons"]["7"]["uses_model"]["category"].get("Arcane") is False
    assert {r[0] for r in conn.execute("SELECT method FROM forecasts")} == {"baseline"}

    empty = connect(":memory:")
    init_db(empty)
    with pytest.raises(ValueError):
        forecast.train(empty)  # nothing to learn from


def test_forecast_job_and_endpoints(conn, monkeypatch):
    from wfm import jobs

    _seed_history(conn)
    spec = jobs.JOBS["train-forecast"]
    assert (spec.kind, spec.command) == ("progress", "python -m wfm forecast")

    def override():
        yield conn

    api.app.dependency_overrides[api.get_db] = override
    try:
        client = TestClient(api.app)
        assert client.get("/api/forecast/report").json() is None  # never trained
        from wfm import forecast
        forecast.train(conn)
        body = client.get("/api/items/trend_0/forecast").json()
        assert [f["horizon"] for f in body] == [1, 7] and body[1]["method"] == "model"
        movers = client.get("/api/forecast/movers", params={"horizon": 7}).json()
        assert movers["gainers"] and movers["gainers"][0]["item"]["slug"].startswith("trend_")
        assert client.get("/api/forecast/report").json()["horizons"]["7"]["status"] == "tested"
        assert client.get("/api/forecast/movers", params={"horizon": 3}).status_code == 422
        # Ranked by platinum gained by default, or by percentage; moves under 1p are left out.
        for by in ("gain", "change"):
            rises = client.get("/api/forecast/movers", params={"horizon": 7, "limit": 50, "by": by}).json()["gainers"]
            assert rises and all(r["gain"] >= 1 for r in rises)
            assert [r[by] for r in rises] == sorted((r[by] for r in rises), reverse=True)
        assert client.get("/api/forecast/movers", params={"by": "volume"}).status_code == 422

        found = client.get("/api/forecast/search", params={"q": "trend", "limit": 3}).json()
        assert found["total"] == 8 and [f["item"]["name"] for f in found["forecasts"]] == ["Trend 0", "Trend 1", "Trend 2"]
        assert found["forecasts"][0]["method"] == "model" and found["forecasts"][0]["rank"] is None
        noise = client.get("/api/forecast/search", params={"q": "Noise 3", "horizon": 1}).json()["forecasts"]
        assert [(f["method"], f["change"]) for f in noise] == [("baseline", 0.0)]
        assert client.get("/api/forecast/search", params={"q": "nothing like this"}).json() == {"total": 0, "forecasts": []}
        assert client.get("/api/forecast/search", params={"q": "trend", "horizon": 3}).status_code == 422
    finally:
        api.app.dependency_overrides.clear()


def test_backfill_scope_with_history(conn):
    from wfm.jobs import Scope, backfill_targets

    with pytest.raises(ValueError):
        backfill_targets(conn, Scope(with_history=True))
    _seed_history(conn, trend_items=2, noise_items=1, days=10)
    slugs, label = backfill_targets(conn, Scope(with_history=True))
    assert sorted(slugs) == ["noise_0", "trend_0", "trend_1"] and label == "items with history"


def pd_timestamp(day: str):
    import pandas as pd

    return pd.Timestamp(day)


def pd_timedelta(days: int):
    import pandas as pd

    return pd.Timedelta(days=days)
