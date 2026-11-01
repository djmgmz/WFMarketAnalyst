"""Market module: everything the app reads from warframe.market, as domain records.

Interface (the `Market` protocol)::

    await market.versions()                 {collection: version} for change detection
    await market.items()                    every tradable item (list fields only)
    await market.item(slug)                 one item with all detail fields
    await market.collection(kind)           riven/lich/sister/mission/npc/location records
    await market.price_history(slug)        90-day daily + 48-hour hourly statistics rows
    await market.best_orders(slug, rank, subtype)
                                            {"sell": [...], "buy": [...]} from online players
    async for event in market.new_orders(stop):   Connection | NewOrder | OnlineCount, forever

Records use the database's column names (see schema.sql), so callers store them
without knowing the wire format. Failures raise MarketError.

Behind the seam (HttpMarket): URL paths and the v2 response envelope, the legacy
v1 statistics format, WebSocket routes and subscription, reconnect with backoff,
retries, and the rate limit shared by every wfm process. Tests use an in-memory
adapter that satisfies the same protocol.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import websockets
from websockets.asyncio.client import connect as ws_connect

from .client import ApiError, WfmClient
from .config import settings

log = logging.getLogger(__name__)

Record = dict[str, Any]

# Which versioned collection (as named by /v2/versions) each auxiliary kind belongs to.
COLLECTIONS: dict[str, str] = {
    "riven_weapon": "rivens", "riven_attribute": "rivens",
    "lich_weapon": "liches", "lich_ephemera": "liches", "lich_quirk": "liches",
    "sister_weapon": "sisters", "sister_ephemera": "sisters", "sister_quirk": "sisters",
    "mission": "missions", "npc": "npcs", "location": "locations",
}


class MarketError(RuntimeError):
    """warframe.market could not answer (network, HTTP error or bad response)."""


@dataclass(frozen=True)
class Connection:
    state: Literal["connecting", "connected", "reconnecting"]
    reconnects: int = 0


@dataclass(frozen=True)
class NewOrder:
    order: Record  # orders_seen columns except seen_at


@dataclass(frozen=True)
class OnlineCount:
    connections: int
    authorized_users: int


MarketEvent = Connection | NewOrder | OnlineCount


class Market(Protocol):
    async def versions(self) -> dict[str, str]: ...
    async def items(self) -> list[Record]: ...
    async def item(self, slug: str) -> Record: ...
    async def collection(self, kind: str) -> list[Record]: ...
    async def price_history(self, slug: str) -> list[Record]: ...
    async def best_orders(self, slug: str, rank: int | None = None,
                          subtype: str | None = None) -> dict[str, list[Record]]: ...
    def new_orders(self, stop: asyncio.Event) -> AsyncIterator[MarketEvent]: ...


# ---------------------------------------------------------------------------
# HTTP + WebSocket adapter
# ---------------------------------------------------------------------------

_COLLECTION_PATHS = {
    "riven_weapon": "/v2/riven/weapons", "riven_attribute": "/v2/riven/attributes",
    "lich_weapon": "/v2/lich/weapons", "lich_ephemera": "/v2/lich/ephemeras", "lich_quirk": "/v2/lich/quirks",
    "sister_weapon": "/v2/sister/weapons", "sister_ephemera": "/v2/sister/ephemeras",
    "sister_quirk": "/v2/sister/quirks",
    "mission": "/v2/missions", "npc": "/v2/npcs", "location": "/v2/locations",
}


class HttpMarket:
    def __init__(self, client: WfmClient | None = None):
        self._client = client or WfmClient()

    async def __aenter__(self) -> HttpMarket:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    async def _data(self, path: str, params: dict[str, Any] | None = None, *, retries: int | None = None) -> Any:
        try:
            return await self._client.get_data(path, params, retries=retries)
        except ApiError as exc:
            raise MarketError(str(exc)) from exc

    async def versions(self) -> dict[str, str]:
        return dict((await self._data("/v2/versions")).get("collections") or {})

    async def items(self) -> list[Record]:
        return [_item_record(raw) for raw in await self._data("/v2/items")]

    async def item(self, slug: str) -> Record:
        return _item_record(await self._data(f"/v2/item/{slug}"))

    async def collection(self, kind: str) -> list[Record]:
        # These endpoints can be very slow: one retry, then let the caller try next run.
        rows = await self._data(_COLLECTION_PATHS[kind], retries=1) or []
        return [{"id": r.get("id") or r.get("slug"), "slug": r.get("slug"), "name": _en(r).get("name"),
                 "data": json.dumps(r)} for r in rows]

    async def price_history(self, slug: str) -> list[Record]:
        try:
            body = await self._client.get_json(f"/v1/items/{slug}/statistics")
        except ApiError as exc:
            raise MarketError(str(exc)) from exc
        return _stat_records(body.get("payload") or {})

    async def best_orders(self, slug: str, rank: int | None = None,
                          subtype: str | None = None) -> dict[str, list[Record]]:
        params = {k: v for k, v in (("rank", rank), ("subtype", subtype)) if v is not None}
        data = await self._data(f"/v2/orders/item/{slug}/top", params or None)
        return {"sell": data.get("sell") or [], "buy": data.get("buy") or []}

    async def new_orders(self, stop: asyncio.Event) -> AsyncIterator[MarketEvent]:
        """Every newly posted order, reconnecting with backoff until `stop` is set."""
        payload = {"platform": settings.platform, "crossplay": settings.crossplay}
        backoff, reconnects = 2, 0
        while not stop.is_set():
            yield Connection("connecting", reconnects)
            try:
                async with ws_connect(settings.ws_url, subprotocols=["wfm"],
                                      user_agent_header=settings.user_agent, ping_interval=30) as ws:
                    await ws.send(json.dumps({"route": "@wfm|cmd/subscribe/newOrders",
                                              "id": "sub-new-orders", "payload": payload}))
                    log.info("Connected to %s, subscribed to new orders (%s)", settings.ws_url, payload)
                    yield Connection("connected", reconnects)
                    backoff = 2
                    async for raw in ws:
                        if stop.is_set():
                            return
                        if event := _ws_event(json.loads(raw)):
                            yield event
                reason = "server closed the connection"
            except (OSError, websockets.WebSocketException) as exc:
                reason = str(exc) or type(exc).__name__
            if stop.is_set():
                return
            reconnects += 1
            yield Connection("reconnecting", reconnects)
            log.warning("WebSocket disconnected (%s); reconnecting in %ds", reason, backoff)
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 120)


# ---------------------------------------------------------------------------
# Wire format -> records (implementation of the HTTP adapter)
# ---------------------------------------------------------------------------

def _en(raw: Record) -> Record:
    return (raw.get("i18n") or {}).get("en") or {}


def _bool(value: Any) -> int | None:
    return None if value is None else int(bool(value))


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value)


def _item_record(raw: Record) -> Record:
    """API Item model (list or full) -> items columns. Fields the payload lacks are None."""
    en = _en(raw)
    return {
        "id": raw["id"],
        "slug": raw["slug"],
        "name": en.get("name") or raw["slug"],
        "game_ref": raw.get("gameRef"),
        "tags": json.dumps(raw.get("tags") or []),
        "rarity": raw.get("rarity"),
        "max_rank": raw.get("maxRank"),
        "max_charges": raw.get("maxCharges"),
        "max_amber_stars": raw.get("maxAmberStars"),
        "max_cyan_stars": raw.get("maxCyanStars"),
        "base_endo": raw.get("baseEndo"),
        "endo_multiplier": raw.get("endoMultiplier"),
        "ducats": raw.get("ducats"),
        "vosfor": raw.get("vosfor"),
        "req_mastery_rank": raw.get("reqMasteryRank"),
        "trading_tax": raw.get("tradingTax"),
        "vaulted": _bool(raw.get("vaulted")),
        "tradable": _bool(raw.get("tradable")),
        "bulk_tradable": _bool(raw.get("bulkTradable")),
        "subtypes": _json(raw.get("subtypes")),
        "set_root": _bool(raw.get("setRoot")),
        "set_parts": _json(raw.get("setParts")),
        "quantity_in_set": raw.get("quantityInSet"),
        "description": en.get("description"),
        "wiki_link": en.get("wikiLink"),
        "icon": en.get("icon"),
        "thumb": en.get("thumb"),
        "sub_icon": en.get("subIcon"),
    }


def _order_record(order: Record) -> Record | None:
    if not order.get("itemId") or order.get("type") not in ("buy", "sell"):
        return None
    user = order.get("user") or {}
    return {
        "id": order["id"],
        "item_id": order["itemId"],
        "type": order["type"],
        "platinum": order["platinum"],
        "quantity": order["quantity"],
        "per_trade": order.get("perTrade"),
        "rank": order.get("rank"),
        "subtype": order.get("subtype"),
        "charges": order.get("charges"),
        "amber_stars": order.get("amberStars"),
        "cyan_stars": order.get("cyanStars"),
        "platform": user.get("platform"),
        "crossplay": _bool(user.get("crossplay")),
        "user_id": user.get("id"),
        "user_name": user.get("ingameName"),
        "user_reputation": user.get("reputation"),
        "user_status": user.get("status"),
        "created_at": order.get("createdAt"),
    }


def _ws_event(msg: Record) -> MarketEvent | None:
    route = msg.get("route", "")
    if route == "@wfm|event/subscriptions/newOrder":
        order = _order_record(msg.get("payload") or {})
        return NewOrder(order) if order else None
    if route == "@wfm|event/reports/online":
        p = msg.get("payload") or {}
        return OnlineCount(p.get("connections", 0), p.get("authorizedUsers", 0))
    if route.endswith(":error") or route.startswith("@ws:error"):
        log.warning("WebSocket error message: %s", msg)
    return None


_STAT_FIELDS = ["volume", "min_price", "max_price", "open_price", "closed_price", "avg_price",
                "wa_price", "median", "moving_avg", "donch_top", "donch_bot"]
_SECTIONS = {"statistics_closed": "closed", "statistics_live": "live"}
_PERIODS = {"48hours": "hour", "90days": "day"}


def _stat_records(payload: Record) -> list[Record]:
    """Legacy v1 statistics payload -> price_stats columns (without item_id / fetched_at).

    mod_rank is None for items without ranks; live rows carry order_type buy/sell.
    """
    rows = []
    for section, source in _SECTIONS.items():
        for period, granularity in _PERIODS.items():
            for entry in (payload.get(section) or {}).get(period) or []:
                row = {
                    "source": source,
                    "granularity": granularity,
                    "bucket": entry["datetime"][:19] + "Z",
                    "mod_rank": entry.get("mod_rank"),
                    "subtype": entry.get("subtype") or "",
                    "order_type": entry.get("order_type") or "",
                }
                row.update({f: entry.get(f) for f in _STAT_FIELDS})
                rows.append(row)
    return rows
