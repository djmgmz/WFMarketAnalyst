"""Jobs: progress, stopping, the single-run claim, the dashboard's controls and scopes."""

from __future__ import annotations

import json

import pytest

from wfm.catalog import upsert_item
from wfm.db import connect

from helpers import NOW, _job_row, _seed_history, backfill, with_market


def test_job_reports_progress_and_finishes(job_db):
    import time

    from wfm.jobs import Job

    with Job.run("backfill", total=3, scope="Arcane", db_path=job_db, flush_seconds=0.05, terminal=False) as job:
        job.working_on("arcane_energize")
        job.advance()
        job.advance(ok=False)
        time.sleep(0.3)  # let the heartbeat flush
        row = _job_row(job_db, "backfill")
        assert (row["status"], row["total"], row["done"], row["failed"], row["current"]) == \
            ("running", 3, 2, 1, "arcane_energize")
        assert json.loads(row["detail"]) == {"scope": "Arcane"}
        job.message = "2 items"
    row = _job_row(job_db, "backfill")
    assert job.status == "finished" and (row["status"], row["message"]) == ("finished", "2 items")
    assert row["finished_at"] and row["current"] is None


def test_a_second_run_of_a_live_job_is_refused(job_db, monkeypatch):
    from wfm import jobs
    from wfm.jobs import Job, JobConflict

    with Job.run("sync-images", db_path=job_db, flush_seconds=0.05, terminal=False):
        with pytest.raises(JobConflict):
            with Job.run("sync-images", db_path=job_db, terminal=False):
                pass  # e.g. the command line while the dashboard's run is going
        with Job.run("backfill", db_path=job_db, terminal=False):
            pass  # other jobs are unaffected

    # A process the dashboard launched takes over the 'running' row start() wrote for it.
    monkeypatch.setattr(jobs, "_spawn", lambda name, args: None)
    c = connect(job_db)
    jobs.start(c, "sync-images")
    monkeypatch.setenv(jobs.LAUNCHED_BY_DASHBOARD, "sync-images")
    with Job.run("sync-images", db_path=job_db, terminal=False):
        pass
    monkeypatch.delenv(jobs.LAUNCHED_BY_DASHBOARD)

    # A run whose process died (stalled) does not block a new one.
    c.execute("UPDATE jobs SET status = 'running', heartbeat_at = '2026-01-01T00:00:00Z' WHERE name = 'sync-images'")
    c.commit()
    c.close()
    with Job.run("sync-images", db_path=job_db, terminal=False):
        pass
    assert _job_row(job_db, "sync-images")["status"] == "finished"


def test_ctrl_c_stops_and_errors_fail(job_db):
    from wfm.jobs import Job

    with Job.run("record", db_path=job_db, terminal=False) as job:
        raise KeyboardInterrupt  # Ctrl+C ends the block cleanly
    assert job.status == "stopped" and _job_row(job_db, "record")["status"] == "stopped"

    with pytest.raises(RuntimeError):
        with Job.run("record", db_path=job_db, terminal=False):
            raise RuntimeError("socket exploded")
    row = _job_row(job_db, "record")
    assert (row["status"], row["message"]) == ("failed", "socket exploded")


def test_dashboard_stop_interrupts_the_job_like_ctrl_c(job_db):
    """End to end: Stop sets the flag, the heartbeat raises KeyboardInterrupt, the job ends 'stopped'."""
    import time

    from wfm import jobs
    from wfm.jobs import Job

    with Job.run("record", db_path=job_db, flush_seconds=0.05, terminal=False) as job:
        c = connect(job_db)
        assert jobs.stop(c, "record") == "stopping"
        c.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:  # the interrupt lands in here
            time.sleep(0.02)
        pytest.fail("the job was not interrupted")
    assert job.status == "stopped"
    assert _job_row(job_db, "record")["stop_requested"] == 0  # cleared once stopped


def test_start_writes_the_row_before_launching_and_refuses_duplicates(conn, monkeypatch, tmp_path):
    from wfm import jobs

    launched = []
    monkeypatch.setattr(jobs, "_spawn", lambda name, args: launched.append((name, args)))
    upsert_item(conn, with_market(tmp_path, lambda market: market.item("arcane_energize")), NOW, detail=True)
    conn.commit()
    jobs.start(conn, "backfill", jobs.Scope(category="Arcane"))
    assert launched == [("backfill", ["backfill", "--category", "Arcane"])]
    row = conn.execute("SELECT * FROM jobs WHERE name = 'backfill'").fetchone()
    assert (row["status"], row["current"], json.loads(row["detail"])) == ("running", "starting", {"scope": "Arcane"})
    with pytest.raises(jobs.JobConflict):  # second click before the process even started
        jobs.start(conn, "backfill", jobs.Scope(category="Arcane"))
    assert len(launched) == 1


def test_launched_job_keeps_a_stop_clicked_during_startup(job_db, monkeypatch):
    import threading

    from wfm import jobs
    from wfm.jobs import Job

    monkeypatch.setattr(jobs, "_spawn", lambda name, args: None)
    c = connect(job_db)
    jobs.start(c, "sync-catalog")
    jobs.stop(c, "sync-catalog")  # clicked before the process wrote its first heartbeat
    c.close()
    stopped = threading.Event()
    monkeypatch.setenv(jobs.LAUNCHED_BY_DASHBOARD, "sync-catalog")  # as _spawn sets it for the process
    with Job.run("sync-catalog", db_path=job_db, flush_seconds=0.05, on_stop=stopped.set, terminal=False):
        assert stopped.wait(2), "the pending stop was lost when the job took over its row"


def test_job_endpoints(client, conn, monkeypatch):
    from wfm import jobs
    from wfm.db import utc_now

    monkeypatch.setattr(jobs, "_spawn", lambda name, args: None)
    body = client.get("/api/jobs").json()
    assert [j["name"] for j in body] == ["record", "backfill", "sync-catalog", "train-forecast", "sync-images"]
    backfill = body[1]
    assert (backfill["kind"], backfill["scoped"], backfill["run"]) == ("progress", True, None)
    assert backfill["command"] == "python -m wfm backfill --category Arcane"

    assert client.post("/api/jobs/nope/start").status_code == 404
    assert client.post("/api/jobs/backfill/start", json={"category": "Nope"}).status_code == 422
    assert client.post("/api/jobs/backfill/start", json={"slugs": ["missing_item"]}).status_code == 422
    assert client.post("/api/jobs/backfill/start", json={"watchlist": True}).status_code == 422  # empty
    assert client.post("/api/jobs/backfill/start", json={}).status_code == 422  # no scope
    assert client.post("/api/jobs/backfill/start", json={"category": "Arcane"}).status_code == 202
    assert client.post("/api/jobs/backfill/start", json={"category": "Arcane"}).status_code == 409

    assert client.post("/api/jobs/record/stop").status_code == 409  # never ran
    conn.execute("""INSERT INTO jobs (name, status, started_at, heartbeat_at, detail)
                    VALUES ('record', 'running', ?, '2026-10-31T00:00:00Z', '{}')""", (utc_now(),))
    conn.commit()
    runs = {j["name"]: j["run"] for j in client.get("/api/jobs").json()}
    assert runs["record"]["state"] == "stalled" and runs["backfill"]["state"] == "running"
    assert client.post("/api/jobs/backfill/stop").json()["outcome"] == "stopping"
    assert client.post("/api/jobs/record/stop").json()["outcome"] == "cleared"  # its process is gone


def test_terminal_lines_render_job_snapshots():
    import io

    from wfm.db import utc_now
    from wfm.terminal import TerminalLine, progress_line, recorder_line

    snap = {"total": 4, "done": 2, "failed": 1, "current": "arcane_grace", "started_at": utc_now(),
            "detail": {"connection": "connected", "last_order_at": utc_now(), "snapshots": 3}}
    rich, plain = progress_line("Backfill")(snap)
    assert rich.startswith("█") and "Backfill 2/4 (50%)" in rich and "ETA" in rich and "arcane_grace" in rich
    assert plain.startswith("Backfill 2/4 (50%) | ") and "1 failed" in plain and plain.isascii()
    resumed = {**snap, "started_at": "2026-10-31T00:00:00Z", "detail": {"skipped": 2}}  # nothing fetched yet
    assert "ETA" not in progress_line("Backfill")(resumed)[1]
    rich, plain = recorder_line({**snap, "done": 7})
    assert "connected" in rich and "7 new orders" in rich and "3 watchlist prices" in rich and plain.isascii()

    stream = io.StringIO()  # not a terminal: plain lines, no carriage returns
    line = TerminalLine(lambda: progress_line("Backfill")(snap), stream=stream)
    line.close()
    assert stream.getvalue().startswith("Backfill 2/4") and "\r" not in stream.getvalue()


def test_backfill_scope_with_history(conn):
    from wfm.jobs import Scope, backfill_targets

    with pytest.raises(ValueError):
        backfill_targets(conn, Scope(with_history=True))
    _seed_history(conn, trend_items=2, noise_items=1, days=10)
    slugs, label = backfill_targets(conn, Scope(with_history=True))
    assert sorted(slugs) == ["noise_0", "trend_0", "trend_1"] and label == "items with history"
