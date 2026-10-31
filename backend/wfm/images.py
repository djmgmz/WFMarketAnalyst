"""Item images module: pictures for catalog items, from the Warframe community's item database (WFCD).

warframe.market's own image server sits behind a bot check, so images come from WFCD instead
(https://github.com/WFCD/warframe-items), matched to our catalog by in-game id. Files are stored
once under data/images/ and served locally; none of this touches warframe.market.

Interface::

    sync_images(conn, source, images_dir, job=NO_REPORT)
                     match every catalog item to a community image and download the files it lacks;
                     returns a summary. Resumable: files already on disk are not downloaded again.
    ImageSource      the seam: items() -> community item list, image(name) -> file bytes
    HttpImageSource  the real adapter (public WFCD API and image CDN), polite and identified

Behind the interface:

* Matching (first rule that applies): the exact in-game id; a relic's intact version (the market
  lists the base relic, the community each refinement); a part blueprint's component (the market's
  `...HelmetBlueprint` is the community's `...HelmetComponent`); and finally the item's name, only
  when exactly one community item has it.
* items.image is set only after its file is saved, so an interrupted run simply resumes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Protocol

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


async def sync_images(conn: sqlite3.Connection, source: ImageSource, images_dir: Path,
                      job: Reporter = NO_REPORT) -> dict[str, int]:
    job.working_on("community item list")
    catalog = [tuple(r) for r in conn.execute("SELECT id, name, game_ref FROM items")]
    matched = match_images(await source.items(), catalog)

    items_by_file: defaultdict[str, list[str]] = defaultdict(list)
    for item_id, image in matched.items():
        items_by_file[image].append(item_id)
    images_dir.mkdir(parents=True, exist_ok=True)
    present = {name for name in items_by_file if (images_dir / name).is_file() and (images_dir / name).stat().st_size}
    todo = sorted(set(items_by_file) - present)

    job.set_total(len(items_by_file))
    if present:
        job.note(skipped=len(present))
        for name in present:
            await retry_locked(conn, lambda: _record(conn, name, items_by_file[name]))
            job.advance()

    failed = 0
    queue: asyncio.Queue[str] = asyncio.Queue()
    for name in todo:
        queue.put_nowait(name)

    async def worker() -> None:
        nonlocal failed
        while not queue.empty():
            name = queue.get_nowait()
            job.working_on(name)
            data = await _download(source, name)
            if data is None:
                failed += 1
            else:
                part = images_dir / f".{name}.{os.getpid()}.part"  # never shared with another process
                part.write_bytes(data)
                part.replace(images_dir / name)
                await retry_locked(conn, lambda: _record(conn, name, items_by_file[name]))
            job.advance(ok=data is not None)
            await asyncio.sleep(PAUSE_SECONDS)

    await asyncio.gather(*(worker() for _ in range(CONCURRENCY)))
    return {"items": len(catalog), "matched": len(matched), "files": len(items_by_file),
            "downloaded": len(todo) - failed, "already_had": len(present), "failed": failed}


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


def _record(conn: sqlite3.Connection, name: str, item_ids: list[str]) -> None:
    marks = ",".join("?" * len(item_ids))
    conn.execute(f"UPDATE items SET image = ? WHERE id IN ({marks})", [name, *item_ids])
    conn.commit()
