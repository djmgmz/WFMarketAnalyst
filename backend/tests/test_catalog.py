"""Catalog sync: items, details, collections and version-based freshness."""

from __future__ import annotations

import asyncio
import sqlite3

from wfm.catalog import categorize, sync_catalog, sync_item_details, upsert_item
from wfm.db import connect, init_db
from wfm.market import COLLECTIONS

from helpers import FakeMarket, NOW, ROUTES, with_market


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
    """No write lock is held while waiting on warframe.market.

    Otherwise every other process writing to the database fails with "database is locked".
    """
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


def test_sync_plan_follows_stored_versions(client, conn):
    from wfm.catalog import plan_sync

    versions = ROUTES["/v2/versions"]["data"]["collections"]
    up_to_date = plan_sync(conn, versions)
    # Empty kinds are not refetched forever.
    assert (up_to_date.item_list, up_to_date.kinds) == (False, ())

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
    """A details refresh stopped halfway resumes with the items still lacking details."""
    first = with_market(tmp_path, lambda market: sync_catalog(conn, market, refresh_details=True, detail_limit=1))
    # Stopped after one item.
    assert first["details_forgotten"] == 2 and first["details"] == (1, 0)
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
