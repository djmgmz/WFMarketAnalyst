"""Rate-limited HTTP client for the warframe.market API.

Every request, from every running `wfm` process, reserves a start slot in one
shared limiter, so the API server, catalog sync, backfill and recorder together
stay under the public limit (3 requests/second). Every request also carries an
identifying User-Agent, as the API rules require.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx

from .config import Settings, settings as default_settings

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504, 509}


class ApiError(RuntimeError):
    def __init__(self, status: int, path: str, detail: Any = None):
        super().__init__(f"{status} for {path}: {detail}")
        self.status = status
        self.path = path
        self.detail = detail


class SharedRateLimiter:
    """Spaces request starts at most `rate` per second across all processes.

    The next free slot lives in a tiny SQLite file. Reserving a slot is one short
    BEGIN IMMEDIATE transaction, so concurrent processes serialize on it and each
    gets a distinct start time.
    """

    def __init__(self, rate: float, path: Path):
        self._interval = 1.0 / rate
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS slots (id INTEGER PRIMARY KEY CHECK (id = 1), next_at REAL NOT NULL)"
            )
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, timeout=30, isolation_level=None)

    def reserve(self) -> float:
        """Claim the next slot; return how many seconds to wait before using it."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT next_at FROM slots WHERE id = 1").fetchone()
            now = time.time()
            slot = max(now, row[0] if row else 0.0)
            conn.execute(
                "INSERT INTO slots (id, next_at) VALUES (1, ?) ON CONFLICT (id) DO UPDATE SET next_at = excluded.next_at",
                (slot + self._interval,),
            )
            conn.execute("COMMIT")
            return slot - now
        finally:
            conn.close()

    async def wait(self) -> None:
        delay = await asyncio.to_thread(self.reserve)
        if delay > 0:
            await asyncio.sleep(delay)


class WfmClient:
    def __init__(self, cfg: Settings = default_settings, max_retries: int = 4, *,
                 limiter: SharedRateLimiter | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self.max_retries = max_retries
        # limiter/transport are internal seams for tests; production always uses the shared limiter.
        self._limiter = limiter or SharedRateLimiter(cfg.requests_per_second, cfg.rate_limit_path)
        self._http = httpx.AsyncClient(
            base_url=cfg.api_base,
            transport=transport,
            timeout=httpx.Timeout(120.0, connect=15.0),
            headers={
                "User-Agent": cfg.user_agent,
                "Accept": "application/json",
                "Language": cfg.language,
                "Platform": cfg.platform,
                "Crossplay": "true" if cfg.crossplay else "false",
            },
        )

    async def __aenter__(self) -> "WfmClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get_json(
        self, path: str, params: dict[str, Any] | None = None, *, retries: int | None = None
    ) -> Any:
        """GET a path and return the parsed JSON body (not unwrapped)."""
        max_retries = self.max_retries if retries is None else retries
        for attempt in range(max_retries + 1):
            await self._limiter.wait()
            try:
                resp = await self._http.get(path, params=params)
            except httpx.TransportError as exc:
                if attempt == max_retries:
                    raise ApiError(0, path, repr(exc)) from exc
                await self._backoff(attempt, path, repr(exc))
                continue

            if resp.status_code in RETRY_STATUSES and attempt < max_retries:
                retry_after = resp.headers.get("Retry-After")
                await self._backoff(attempt, path, resp.status_code, retry_after)
                continue
            if resp.status_code >= 400:
                detail: Any
                try:
                    detail = resp.json().get("error")
                except ValueError:
                    detail = resp.text[:200]
                raise ApiError(resp.status_code, path, detail)
            return resp.json()
        raise ApiError(0, path, "retries exhausted")  # pragma: no cover

    async def get_data(
        self, path: str, params: dict[str, Any] | None = None, *, retries: int | None = None
    ) -> Any:
        """GET a v2 endpoint and unwrap the {apiVersion, data, error} envelope."""
        body = await self.get_json(path, params, retries=retries)
        if body.get("error"):
            raise ApiError(200, path, body["error"])
        return body.get("data")

    @staticmethod
    async def _backoff(attempt: int, path: str, reason: Any, retry_after: str | None = None) -> None:
        delay = float(retry_after) if retry_after and retry_after.isdigit() else min(2 ** attempt * 2, 30)
        log.warning("Retrying %s in %.0fs (%s)", path, delay, reason)
        await asyncio.sleep(delay)
