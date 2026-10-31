"""Database helpers: lock retries and migrations."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from wfm.db import connect, init_db


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


def test_migration_adds_columns_to_existing_jobs_table(tmp_path):
    db = tmp_path / "old.db"
    old = sqlite3.connect(db)
    old.execute("CREATE TABLE jobs (name TEXT PRIMARY KEY, status TEXT NOT NULL, started_at TEXT NOT NULL, "
                "heartbeat_at TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}')")
    # A price table from before the suspect column, which the item_prices view now reads.
    old.execute("CREATE TABLE price_stats (item_id TEXT NOT NULL, source TEXT NOT NULL, granularity TEXT NOT NULL, "
                "bucket TEXT NOT NULL, mod_rank INTEGER NOT NULL DEFAULT -1, subtype TEXT NOT NULL DEFAULT '', "
                "order_type TEXT NOT NULL DEFAULT '', volume INTEGER, median REAL, fetched_at TEXT NOT NULL)")
    old.commit()
    old.close()
    c = connect(db)
    init_db(c)
    assert "stop_requested" in {r[1] for r in c.execute("PRAGMA table_info(jobs)")}
    assert "suspect" in {r[1] for r in c.execute("PRAGMA table_info(price_stats)")}
    assert c.execute("SELECT COUNT(*) FROM item_prices").fetchone()[0] == 0  # the view works on the old table
    c.close()
