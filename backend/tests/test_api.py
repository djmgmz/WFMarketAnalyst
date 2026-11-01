"""Dashboard API routes."""

from __future__ import annotations

from wfm.catalog import upsert_item

from helpers import LIST_ITEM, NOW, PRIME_PART, PRIME_SET, ROUTES, backfill, with_market


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
    # The asking price is the cheapest listing, not the median one.
    assert hist["live"]["sell"][0]["median"] == 130 and hist["live"]["sell"][0]["ask"] == 118
    # Two trading days are too thin to rank.
    assert client.get("/api/movers", params={"min_volume": 0}).json()["gainers"] == []

    from wfm import prices  # ten genuine trading days make it liquid enough to rank
    prices.store(conn, "i1", [{"source": "closed", "granularity": "day", "mod_rank": 0, "subtype": "", "order_type": "",
                               "bucket": f"2026-10-{day:02d}T00:00:00Z", "median": 100.0, "volume": 10}
                              for day in range(13, 23)])
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
