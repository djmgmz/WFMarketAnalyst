"""SQLite connection handling."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

from .config import settings

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
BUSY_TIMEOUT_SECONDS = 30
LOCK_ATTEMPTS = 4
LOCK_PAUSE_SECONDS = 15.0

T = TypeVar("T")
log = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_day_start() -> str:
    """Midnight UTC today, in the stored timestamp format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")


def parse_utc(value: str) -> datetime:
    """Parse a stored UTC timestamp ('...Z', '+00:00' or naive) as an aware datetime."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def seconds_since(value: str) -> float:
    return (datetime.now(timezone.utc) - parse_utc(value)).total_seconds()


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or settings.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL lets the recorders write while the API reads.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Columns added after a table first shipped: (table, column, definition).
# CREATE TABLE IF NOT EXISTS does not touch existing tables, so add them here.
MIGRATIONS: list[tuple[str, str, str]] = [
    ("jobs", "stop_requested", "INTEGER NOT NULL DEFAULT 0"),
    ("items", "image", "TEXT"),
    ("price_stats", "suspect", "INTEGER NOT NULL DEFAULT 0"),
]


def init_db(conn: sqlite3.Connection) -> None:
    # Migrate existing tables first: the schema script recreates views that may use new columns.
    for table, column, definition in MIGRATIONS:
        columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        # No columns: the table is new and the schema creates it whole
        if columns and column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    conn.commit()
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


async def retry_locked(conn: sqlite3.Connection, write: Callable[[], T], *,
                       attempts: int = LOCK_ATTEMPTS, pause: float = LOCK_PAUSE_SECONDS) -> T:
    """Run `write` (which commits itself), retrying while another process holds the write lock.

    When the lock is held past the busy timeout, roll back, wait `pause` seconds and try again, so
    one slow writer elsewhere does not end a long-running job. Other errors, and the last failed
    attempt, are raised.
    """
    for attempt in range(1, attempts + 1):
        try:
            return write()
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or attempt == attempts:
                raise
            conn.rollback()
            log.warning("Database locked by another process (attempt %d of %d); retrying in %.0fs",
                        attempt, attempts, pause)
            await asyncio.sleep(pause)
    raise AssertionError("unreachable")  # pragma: no cover


@contextmanager
def session(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        init_db(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
        (key, value, utc_now()),
    )
