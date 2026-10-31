"""Item images module: pictures for catalog items, from the Warframe community's item database (WFCD).

warframe.market's own image server sits behind a bot check, so images come from WFCD instead
(https://github.com/WFCD/warframe-items), matched to our catalog by in-game id. Files are stored
once under data/images/ and served locally; none of this touches warframe.market.

Interface::

    sync_images(conn, source, images_dir, job=NO_REPORT)
                     match every catalog item to a community image and download the files it lacks;
                     returns ImagesSynced. Resumable: files already on disk are not downloaded again.
    ImageSource      the seam: items() -> community item list, image(name) -> file bytes
    HttpImageSource  the real adapter (public WFCD API and image CDN), polite and identified

Behind the interface:

* Matching (first rule that applies): the exact in-game id; a relic's intact version (the market
  lists the base relic, the community each refinement); a part blueprint's component (the market's
  `...HelmetBlueprint` is the community's `...HelmetComponent`); and finally the item's name, only
  when exactly one community item has it.
* items.image is exactly the matched file when it is on disk (NULL otherwise), so an interrupted
  run simply resumes and a stale value never outlives the next run.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, NamedTuple, Protocol

import httpx

from .config import settings
from .db import retry_locked
from .jobs import NO_REPORT, Reporter

log = logging.getLogger(__name__)

ITEMS_URL = "https://api.warframestat.us/items/"
IMAGE_URL = "https://cdn.warframestat.us/img/{name}"
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.(png|jpe?g|webp)$")
CONCURRENCY = 3
PAUSE_SECONDS = 0.3  # per worker between downloads: at most about 10 requests a second
RETRIES = 2


class ImageSource(Protocol):
    async def items(self) -> list[dict[str, Any]]: ...

    async def image(self, name: str) -> bytes: ...


class HttpImageSource:
    """The public WFCD item API and image CDN, with our identifying User-Agent."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self._http = httpx.AsyncClient(transport=transport, follow_redirects=True, timeout=60.0,
                                       headers={"User-Agent": settings.user_agent})

    async def __aenter__(self) -> HttpImageSource:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._http.aclose()

    async def items(self) -> list[dict[str, Any]]:
        resp = await self._http.get(ITEMS_URL, params={"only": "uniqueName,name,imageName,components"})
        resp.raise_for_status()
        return resp.json()

    async def image(self, name: str) -> bytes:
        resp = await self._http.get(IMAGE_URL.format(name=name))
        resp.raise_for_status()
        if not resp.headers.get("content-type", "").startswith("image/"):
            raise ValueError(f"{name}: not an image ({resp.headers.get('content-type')})")
        return resp.content


def match_images(community: list[dict[str, Any]], catalog: list[tuple[str, str, str | None]]) -> dict[str, str]:
    """{item id: image file name} for every catalog row (id, name, game_ref) that matches."""
    by_ref: dict[str, str] = {}
    by_name: defaultdict[str, set[str]] = defaultdict(set)
    for item in community:
        for entry in (item, *(item.get("components") or [])):
            ref, image = entry.get("uniqueName"), entry.get("imageName")
            if ref and image and SAFE_NAME.match(image):
                by_ref.setdefault(ref, image)
        if item.get("name") and item.get("imageName") and SAFE_NAME.match(item["imageName"]):
            by_name[item["name"].lower()].add(item["imageName"])

    def find(name: str, ref: str | None) -> str | None:
        if ref:
            candidates = [ref, ref + "Bronze"]
            if ref.endswith("Blueprint"):
                candidates.append(ref.removesuffix("Blueprint") + "Component")
            for candidate in candidates:
                if candidate in by_ref:
                    return by_ref[candidate]
        named = by_name.get(name.lower(), set())
        return next(iter(named)) if len(named) == 1 else None

    return {item_id: image for item_id, name, ref in catalog if (image := find(name, ref))}


class ImagesSynced(NamedTuple):
    items: int        # catalog items
    matched: int      # items matched to a community image
    files: int        # distinct files those items need (items often share one)
    downloaded: int   # files fetched this run
    already_had: int  # files already on disk
    failed: int       # files that could not be fetched; the next run retries them


async def sync_images(conn: sqlite3.Connection, source: ImageSource, images_dir: Path,
                      job: Reporter = NO_REPORT) -> ImagesSynced:
    """Match the catalog to community images and download the files not yet on disk.

    items.image ends up exactly "the matched file, if it is on disk": one transaction first
    records the files already there and clears every other item (including stale values),
    then each download is recorded as it lands, so the dashboard fills in while it runs.
    """
    job.working_on("community item list")
    catalog = [tuple(r) for r in conn.execute("SELECT id, name, game_ref FROM items")]
    matched = match_images(await source.items(), catalog)
    items_by_file: defaultdict[str, list[str]] = defaultdict(list)
    for item_id, image in matched.items():
        items_by_file[image].append(item_id)

    images_dir.mkdir(parents=True, exist_ok=True)
    present = {name for name in items_by_file if _saved(images_dir / name)}
    await retry_locked(conn, lambda: _reconcile(conn, {i: f for i, f in matched.items() if f in present}))
    job.set_total(len(items_by_file))
    if present:
        job.note(skipped=len(present))
        for _ in present:
            job.advance()

    slots = asyncio.Semaphore(CONCURRENCY)

    async def fetch(name: str) -> bool:
        async with slots:
            job.working_on(name)
            data = await _download(source, name)
            if data is not None:
                part = images_dir / f".{name}.{os.getpid()}.part"  # never shared with another process
                part.write_bytes(data)
                part.replace(images_dir / name)
                await retry_locked(conn, lambda: _record(conn, name, items_by_file[name]))
            job.advance(ok=data is not None)
            await asyncio.sleep(PAUSE_SECONDS)
            return data is not None

    fetched = await asyncio.gather(*(fetch(name) for name in sorted(items_by_file.keys() - present)))
    failed = fetched.count(False)
    return ImagesSynced(items=len(catalog), matched=len(matched), files=len(items_by_file),
                        downloaded=len(fetched) - failed, already_had=len(present), failed=failed)


async def _download(source: ImageSource, name: str) -> bytes | None:
    for attempt in range(RETRIES + 1):
        try:
            return await source.image(name)
        except (httpx.HTTPError, ValueError) as exc:
            if attempt == RETRIES:
                log.warning("Image %s failed: %s", name, exc)
                return None
            await asyncio.sleep(2 * (attempt + 1))
    return None  # pragma: no cover


def _saved(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def _reconcile(conn: sqlite3.Connection, images: dict[str, str]) -> None:
    """Set items.image to exactly `images` ({item id: file}) in one transaction."""
    conn.execute("UPDATE items SET image = NULL WHERE image IS NOT NULL")
    conn.executemany("UPDATE items SET image = ? WHERE id = ?", [(f, i) for i, f in images.items()])
    conn.commit()


def _record(conn: sqlite3.Connection, name: str, item_ids: list[str]) -> None:
    marks = ",".join("?" * len(item_ids))
    conn.execute(f"UPDATE items SET image = ? WHERE id IN ({marks})", [name, *item_ids])
    conn.commit()
