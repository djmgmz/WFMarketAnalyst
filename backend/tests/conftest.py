"""Fixtures shared by every test module: a temporary database, an API client, a jobs database.

All tests run offline: a temporary SQLite database and canned payloads, no network. warframe.market
is reached only through the Market module, driven either as the real HttpMarket adapter over
canned wire payloads (httpx.MockTransport) or as FakeMarket, an in-memory adapter (helpers.py).
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from wfm import api
from wfm.catalog import sync_catalog
from wfm.db import connect, init_db

from helpers import with_market


@pytest.fixture()
def conn(tmp_path) -> sqlite3.Connection:
    c = connect(tmp_path / "test.db")
    init_db(c)
    yield c
    c.close()


@pytest.fixture()
def client(conn: sqlite3.Connection, tmp_path) -> TestClient:
    with_market(tmp_path, lambda market: sync_catalog(conn, market))

    def override():
        yield conn

    api.app.dependency_overrides[api.get_db] = override
    yield TestClient(api.app)  # no context manager: skips the lifespan (no live market)
    api.app.dependency_overrides.clear()


@pytest.fixture()
def job_db(tmp_path):
    db = tmp_path / "jobs.db"
    c = connect(db)
    init_db(c)
    c.close()
    return db
