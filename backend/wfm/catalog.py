"""Catalog sync: items, set/detail metadata and auxiliary collections.

This is reference data only. It does not record any prices or orders.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .db import get_meta, set_meta, utc_now
from .jobs import NO_REPORT, Reporter
from .market import COLLECTIONS, Market, MarketError, Record

log = logging.getLogger(__name__)

# First matching rule wins. Order matters: a prime mod is a Mod, a prime part is Prime.
CATEGORY_RULES: list[tuple[str, set[str]]] = [
    ("Relic", {"relic"}),
    ("Arcane", {"arcane_enhancement"}),
    ("Mod", {"mod"}),
    ("Prime", {"prime"}),
    ("Ayatan", {"ayatan_sculpture"}),
    ("Cosmetic", {"skin", "emote", "scene", "cosmetic"}),
    ("Resource", {"fish", "gem", "imprint", "lens", "focus"}),
    ("Gear", {"weapon", "warframe", "archwing", "sentinel", "companion", "necramech", "k_drive"}),
]

CATEGORIES = [name for name, _ in CATEGORY_RULES] + ["Other"]

def categorize(tags: Iterable[str]) -> str:
    tag_set = set(tags)
    for name, rule in CATEGORY_RULES:
        if tag_set & rule:
            return name
    return "Other"


def upsert_item(conn: sqlite3.Connection, record: Record, synced_at: str, *, detail: bool) -> None:
    """Insert or update one item from a Market item record.

    List records never overwrite fields that only the detail endpoint provides, so
    a list refresh does not wipe out data from an earlier detail sync.
    """
    row = {**record, "category": categorize(json.loads(record["tags"])), "list_synced_at": synced_at}
    if detail:
        row["detail_synced_at"] = synced_at
    cols = list(row)
    # COALESCE keeps detail-only values the list payload does not include.
    template = "{c} = excluded.{c}" if detail else "{c} = COALESCE(excluded.{c}, items.{c})"
    updates = ", ".join(template.format(c=c) for c in cols if c != "id")
    conn.execute(
        f"INSERT INTO items ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)}) "
        f"ON CONFLICT (id) DO UPDATE SET {updates}",
        row,
    )
    conn.execute("DELETE FROM item_tags WHERE item_id = ?", (row["id"],))
    conn.executemany(
        "INSERT OR IGNORE INTO item_tags (item_id, tag) VALUES (?, ?)",
        [(row["id"], t) for t in json.loads(row["tags"])],
    )


@dataclass(frozen=True)
class SyncPlan:
    """What a catalog sync has to fetch, decided from stored versions without the network."""

    item_list: bool
    kinds: tuple[str, ...]  # auxiliary collection kinds


def plan_sync(conn: sqlite3.Connection, versions: dict[str, str], *, force: bool = False) -> SyncPlan:
    """A collection is stale when its stored version differs from warframe.market's or is missing.

    Versions are stored only together with the data they describe (see sync_item_list,
    sync_aux), so an interrupted sync is simply still stale next time. Item details
    are not part of the plan: they are "whatever items lack details" (sync_item_details).
    """
    def stale(collection: str) -> bool:
        return force or get_meta(conn, f"version:{collection}") != versions.get(collection)

    has_items = conn.execute("SELECT EXISTS (SELECT 1 FROM items)").fetchone()[0]
    return SyncPlan(
        item_list=not has_items or stale("items"),
        kinds=tuple(kind for kind, collection in COLLECTIONS.items() if stale(collection)),
    )


async def sync_item_list(market: Market, conn: sqlite3.Connection, version: str | None = None) -> int:
    """Refresh the item list; `version` is stored in the same transaction as the items."""
    items = await market.items()
    now = utc_now()
    seen: set[str] = set()
    for record in items:
        upsert_item(conn, record, now, detail=False)
        seen.add(record["id"])
    # Items removed from the market disappear from the catalog (recorded data is kept).
    stale = [r["id"] for r in conn.execute("SELECT id FROM items") if r["id"] not in seen]
    conn.executemany("DELETE FROM items WHERE id = ?", [(i,) for i in stale])
    set_meta(conn, "items_list_synced_at", now)
    if version:
        set_meta(conn, "version:items", version)
    conn.commit()
    log.info("Item list: %d items stored, %d removed", len(items), len(stale))
    return len(items)


def forget_details(conn: sqlite3.Connection) -> int:
    """Mark every item as lacking details, so the next details pass refetches all (resumably)."""
    count = conn.execute("UPDATE items SET detail_synced_at = NULL WHERE detail_synced_at IS NOT NULL").rowcount
    conn.commit()
    return count


async def sync_item_details(
    market: Market,
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
    concurrency: int = 3,
    job: Reporter = NO_REPORT,
) -> tuple[int, int]:
    """Fetch full details for items that lack them. Resumable: each item commits on its own."""
    slugs = [r[0] for r in conn.execute("SELECT slug FROM items WHERE detail_synced_at IS NULL ORDER BY name")]
    if limit:
        slugs = slugs[:limit]
    total = len(slugs)
    if not total:
        log.info("Item details: nothing to fetch")
        return 0, 0

    job.set_total(total)
    job.note(phase="item details")
    queue: asyncio.Queue[str] = asyncio.Queue()
    for slug in slugs:
        queue.put_nowait(slug)
    done = failed = 0

    async def worker() -> None:
        nonlocal done, failed
        while True:
            try:
                slug = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            job.working_on(slug)
            ok = False
            try:
                upsert_item(conn, await market.item(slug), utc_now(), detail=True)
                conn.commit()
                done += 1
                ok = True
            except MarketError as exc:
                failed += 1
                log.warning("Item detail failed for %s: %s", slug, exc)
            job.advance(ok=ok)

    await asyncio.gather(*(worker() for _ in range(concurrency)))
    set_meta(conn, "items_detail_synced_at", utc_now())
    conn.commit()
    return done, failed


async def sync_aux(
    market: Market, conn: sqlite3.Connection, kinds: Iterable[str], versions: dict[str, str]
) -> dict[str, int | str]:
    """Replace the given auxiliary kinds; a collection's version is stored once all succeed."""
    results: dict[str, int | str] = {}
    for kind in kinds:
        try:
            rows = await market.collection(kind)
        except MarketError as exc:
            log.warning("Skipping %s for now: %s", kind, exc)
            results[kind] = "failed"
            continue
        now = utc_now()
        conn.execute("DELETE FROM aux_catalog WHERE kind = ?", (kind,))
        conn.executemany(
            "INSERT INTO aux_catalog (kind, id, slug, name, data, synced_at) VALUES (?, ?, ?, ?, ?, ?)",
            [(kind, r["id"], r["slug"], r["name"], r["data"], now) for r in rows],
        )
        conn.commit()
        results[kind] = len(rows)
    failed = {COLLECTIONS[k] for k, result in results.items() if result == "failed"}
    for collection in {COLLECTIONS[k] for k in results} - failed:
        if collection in versions:
            set_meta(conn, f"version:{collection}", versions[collection])
    set_meta(conn, "aux_synced_at", utc_now())
    conn.commit()
    log.info("Aux collections: %s", results)
    return results


async def sync_catalog(
    conn: sqlite3.Connection,
    market: Market,
    *,
    details: bool = True,
    refresh_details: bool = False,
    force: bool = False,
    detail_limit: int | None = None,
    job: Reporter = NO_REPORT,
) -> dict[str, Any]:
    """Bring the catalog up to date. Safe to stop at any point: the next run continues."""
    job.working_on("item list and collections")
    job.note(phase="catalog")
    versions = await market.versions()
    plan = plan_sync(conn, versions, force=force)
    summary: dict[str, Any] = {"plan": {"item_list": plan.item_list, "collections": list(plan.kinds)}}
    if plan.item_list:
        summary["items"] = await sync_item_list(market, conn, versions.get("items"))
    # Small collections first so the long per-item pass does not delay them.
    summary["aux"] = await sync_aux(market, conn, plan.kinds, versions)
    if refresh_details:
        summary["details_forgotten"] = forget_details(conn)
    if details:
        summary["details"] = await sync_item_details(market, conn, limit=detail_limit, job=job)
    return summary
