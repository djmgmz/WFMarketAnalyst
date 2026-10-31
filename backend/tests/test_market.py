"""The Market module's HTTP adapter and the shared rate limiter."""

from __future__ import annotations

import json

import pytest

from wfm.client import SharedRateLimiter
from wfm.market import MarketError, NewOrder, OnlineCount

from helpers import LIST_ITEM, ROUTES, WIRE_ORDER, with_market


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


def test_shared_rate_limiter_spaces_slots_across_instances(tmp_path):
    path = tmp_path / "ratelimit.db"
    a, b = SharedRateLimiter(2.0, path), SharedRateLimiter(2.0, path)  # two "processes"
    delays = [a.reserve(), b.reserve(), a.reserve(), b.reserve()]
    # Each reservation lands one interval (0.5s) after the previous, whoever made it.
    assert delays[0] == pytest.approx(0.0, abs=0.05)
    for earlier, later in zip(delays, delays[1:]):
        assert later - earlier == pytest.approx(0.5, abs=0.05)
