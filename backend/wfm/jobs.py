"""Job module: the life of a long-running job, from launch to final status.

Inside the job's own process::

    with Job.run("backfill", total=len(slugs), scope="Arcane") as job:
        for slug in slugs:
            job.working_on(slug)
            ...
            job.advance(ok=True)
        job.message = "170 items, 1234 rows"
    job.status          # "finished" | "stopped" | "failed"

Ctrl+C and the dashboard's Stop button both end the block as "stopped" without
an exception; any other exception marks the job "failed" and propagates. Code
that only reports progress (catalog sync, recorders) takes a `Reporter`, which
defaults to NO_REPORT, so it runs the same with or without a job.

From the dashboard's API process::

    status(conn)              every job definition with its latest run
    start(conn, name, scope)  launches the job detached; raises JobConflict or ValueError
    stop(conn, name)          "stopping" (job stops itself within seconds) or "cleared"

Behind the interface: the `jobs` table, a heartbeat thread that flushes progress
every couple of seconds (the API reports a running job with a silent heartbeat
as "stalled"), cooperative stop requests turned into a clean interrupt, the
terminal line and log routing, and launching jobs as detached processes.
"""

from __future__ import annotations

import _thread
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .config import PROJECT_ROOT, settings
from .db import seconds_since, utc_now
from .terminal import TerminalLine, progress_line, recorder_line, route_logs

FLUSH_SECONDS = 2.0
# A running job whose heartbeat is older than this has crashed or been killed.
STALE_SECONDS = 30


class JobConflict(RuntimeError):
    """The job is already running."""


# Set on processes the dashboard launches: they take over the 'running' row start() wrote for them.
LAUNCHED_BY_DASHBOARD = "WFM_LAUNCHED_JOB"


# ---------------------------------------------------------------------------
# Job definitions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Scope:
    """What a backfill covers: slugs, a category, the watchlist, or every item with history."""

    category: str | None = None
    slugs: tuple[str, ...] = ()
    watchlist: bool = False
    with_history: bool = False  # the daily refresh: extend what is already recorded


def backfill_targets(conn: sqlite3.Connection, scope: Scope) -> tuple[list[str], str]:
    """Resolve a backfill scope to item slugs and a short label. Raises ValueError."""
    if scope.slugs:
        marks = ",".join("?" * len(scope.slugs))
        known = {r[0] for r in conn.execute(f"SELECT slug FROM items WHERE slug IN ({marks})", scope.slugs)}
        if missing := [s for s in scope.slugs if s not in known]:
            raise ValueError(f"Unknown items: {', '.join(missing[:5])}")
        return list(scope.slugs), ", ".join(scope.slugs[:3]) + ("…" if len(scope.slugs) > 3 else "")
    if scope.watchlist:
        slugs = [r[0] for r in conn.execute("SELECT i.slug FROM watchlist w JOIN items i ON i.id = w.item_id")]
        if not slugs:
            raise ValueError("The watchlist is empty")
        return slugs, "watchlist"
    if scope.with_history:
        slugs = [r[0] for r in conn.execute(
            "SELECT slug FROM items WHERE id IN (SELECT DISTINCT item_id FROM price_stats) ORDER BY name")]
        if not slugs:
            raise ValueError("No item has price history yet: backfill a category first")
        return slugs, "items with history"
    if scope.category:
        from .catalog import CATEGORIES  # catalog reports progress through this module

        if scope.category not in CATEGORIES:
            raise ValueError(f"Unknown category '{scope.category}'")
        slugs = [r[0] for r in conn.execute(
            "SELECT slug FROM items WHERE category = ? ORDER BY name", (scope.category,))]
        if not slugs:
            raise ValueError(f"No items in {scope.category}")
        return slugs, scope.category
    raise ValueError("Choose what to backfill: item slugs, a category, the watchlist or items with history")


def _backfill_args(scope: Scope) -> list[str]:
    if scope.slugs:
        return ["backfill", *scope.slugs]
    if scope.with_history:
        return ["backfill", "--with-history"]
    return ["backfill", "--watchlist"] if scope.watchlist else ["backfill", "--category", scope.category or ""]


@dataclass(frozen=True)
class JobSpec:
    name: str
    title: str
    kind: Literal["progress", "stream"]  # stream: open-ended, counts events (the recorder)
    resumable: bool                       # a restart skips work already done
    args: Callable[[Scope], list[str]]    # `python -m wfm` arguments
    render: Callable[[dict[str, Any]], tuple[str, str]]
    scoped: bool = False                  # needs a Scope (validated by backfill_targets)
    unit: str = "items"                   # what `done` counts
    example: Scope = field(default_factory=Scope)

    @property
    def command(self) -> str:
        return " ".join(["python", "-m", "wfm", *self.args(self.example)])


JOBS: dict[str, JobSpec] = {spec.name: spec for spec in (
    JobSpec("record", "Order recorder", "stream", resumable=False,
            args=lambda _: ["record"], render=recorder_line, unit="orders"),
    JobSpec("backfill", "Price history backfill", "progress", resumable=True,
            args=_backfill_args, render=progress_line("Backfill"), scoped=True, example=Scope(category="Arcane")),
    JobSpec("sync-catalog", "Catalog sync", "progress", resumable=True,
            args=lambda _: ["sync-catalog"], render=progress_line("Catalog sync")),
    JobSpec("train-forecast", "Price forecast", "progress", resumable=False,
            args=lambda _: ["forecast"], render=progress_line("Forecast"), unit="steps"),
    JobSpec("sync-images", "Item images", "progress", resumable=True,
            args=lambda _: ["sync-images"], render=progress_line("Images"), unit="images"),
)}


# ---------------------------------------------------------------------------
# Reporting progress (inside the job's process)
# ---------------------------------------------------------------------------

class Reporter:
    """What job code reports progress to. This base class ignores everything (NO_REPORT)."""

    message: str | None = None

    def set_total(self, total: int) -> None: ...

    def working_on(self, label: str | None) -> None: ...

    def advance(self, *, ok: bool = True) -> None: ...

    def note(self, **detail: Any) -> None: ...


NO_REPORT = Reporter()


class Job(Reporter):
    def __init__(self, spec: JobSpec, *, total: int | None, scope: str | None, db_path: Path,
                 flush_seconds: float, on_stop: Callable[[], None]):
        self.spec = spec
        self._db_path = db_path
        self._on_stop = on_stop
        self._stop_signalled = False
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._state: dict[str, Any] = {
            "status": "running", "total": total, "done": 0, "failed": 0, "current": None,
            "detail": {"scope": scope} if scope else {}, "message": None,
            "started_at": utc_now(), "finished_at": None,
        }
        self._claim()
        self._thread = threading.Thread(target=self._heartbeat, args=(flush_seconds,), daemon=True)
        self._thread.start()

    @classmethod
    @contextmanager
    def run(cls, name: str, *, total: int | None = None, scope: str | None = None,
            db_path: Path | None = None, flush_seconds: float = FLUSH_SECONDS,
            on_stop: Callable[[], None] = _thread.interrupt_main, terminal: bool = True) -> Iterator[Job]:
        spec = JOBS[name]
        job = cls(spec, total=total, scope=scope, db_path=Path(db_path or settings.db_path),
                  flush_seconds=flush_seconds, on_stop=on_stop)
        line = TerminalLine(lambda: spec.render(job.snapshot())).start() if terminal else None
        try:
            with route_logs(line) if line else _nothing():
                try:
                    yield job
                except KeyboardInterrupt:
                    job._finish("stopped")
                except BaseException as exc:
                    job._finish("failed", str(exc)[:300])
                    raise
                else:
                    job._finish("finished")
        finally:
            if line:
                line.close()

    # -- Reporter -------------------------------------------------------------

    def set_total(self, total: int) -> None:
        self._update(total=total, done=0, failed=0)

    def working_on(self, label: str | None) -> None:
        self._update(current=label)

    def advance(self, *, ok: bool = True) -> None:
        with self._lock:
            self._state["done"] += 1
            self._state["failed"] += 0 if ok else 1

    def note(self, **detail: Any) -> None:
        with self._lock:
            self._state["detail"].update(detail)

    @property
    def message(self) -> str | None:  # type: ignore[override]
        return self._state["message"]

    @message.setter
    def message(self, value: str | None) -> None:
        self._update(message=value)

    # -- reading --------------------------------------------------------------

    @property
    def status(self) -> str:
        return self._state["status"]

    @property
    def done(self) -> int:
        return self._state["done"]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {**self._state, "detail": dict(self._state["detail"])}

    # -- lifecycle and persistence -------------------------------------------

    def _update(self, **fields: Any) -> None:
        with self._lock:
            self._state.update(fields)

    def _finish(self, status: str, message: str | None = None) -> None:
        self._update(status=status, current=None, finished_at=utc_now(),
                     **({"message": message} if message else {}))
        self._stopped.set()
        self._thread.join(timeout=5)
        self._flush()

    def _heartbeat(self, interval: float) -> None:
        while not self._stopped.wait(interval):
            try:
                if self._flush() and not self._stop_signalled:
                    self._stop_signalled = True
                    self._on_stop()  # same clean path as Ctrl+C
            except sqlite3.Error:
                pass  # a busy database must never take the job down; retry next tick

    def _values(self) -> tuple[Any, ...]:
        """The state as jobs-table values, in the column order of _claim and _flush."""
        s = self.snapshot()
        return (s["status"], s["total"], s["done"], s["failed"], s["current"], json.dumps(s["detail"]),
                s["message"], s["started_at"], utc_now(), s["finished_at"], self.spec.name)

    def _claim(self) -> None:
        """Take over this job's row, refusing (JobConflict) while a live run holds it.

        Check and write share one immediate transaction, as in start(), so two runs can never
        both claim the row. The process the dashboard launched takes over the 'running' row
        start() wrote for it, keeping a Stop clicked during startup; a stalled run's row is
        reclaimed, with any Stop it left cleared.
        """
        launched = os.environ.get(LAUNCHED_BY_DASHBOARD) == self.spec.name
        conn = sqlite3.connect(self._db_path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM jobs WHERE name = ?", (self.spec.name,)).fetchone()
            if row and _state(row) == "running" and not launched:
                raise JobConflict(f"'{self.spec.name}' is already running (started {row['started_at']}). "
                                  "Wait for it to finish, or stop it from the dashboard first.")
            conn.execute(
                """INSERT INTO jobs (status, total, done, failed, current, detail, message,
                                     started_at, heartbeat_at, finished_at, name)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (name) DO UPDATE SET
                     status = excluded.status, total = excluded.total, done = excluded.done,
                     failed = excluded.failed, current = excluded.current, detail = excluded.detail,
                     message = excluded.message, started_at = excluded.started_at,
                     heartbeat_at = excluded.heartbeat_at, finished_at = excluded.finished_at,
                     stop_requested = CASE WHEN jobs.status = 'running' THEN jobs.stop_requested ELSE 0 END""",
                self._values(),
            )
            conn.execute("COMMIT")
        finally:
            conn.close()  # rolls back an unfinished transaction (a refused claim)

    def _flush(self) -> bool:
        """Write the state; return True when the dashboard has asked the job to stop."""
        values = self._values()
        status = values[0]
        conn = sqlite3.connect(self._db_path, timeout=10)
        try:
            conn.execute(
                """UPDATE jobs SET status = ?, total = ?, done = ?, failed = ?, current = ?, detail = ?,
                                   message = ?, started_at = ?, heartbeat_at = ?, finished_at = ?,
                                   stop_requested = CASE WHEN ? = 'running' THEN stop_requested ELSE 0 END
                   WHERE name = ?""",
                (*values[:-1], status, self.spec.name),
            )
            conn.commit()
            row = conn.execute("SELECT stop_requested FROM jobs WHERE name = ?", (self.spec.name,)).fetchone()
            return bool(row and row[0]) and status == "running"
        finally:
            conn.close()


@contextmanager
def _nothing() -> Iterator[None]:
    yield


# ---------------------------------------------------------------------------
# Controlling jobs (from the dashboard's API process)
# ---------------------------------------------------------------------------

def _state(row: sqlite3.Row) -> str:
    """The stored status, except a 'running' job with a silent heartbeat is 'stalled'."""
    if row["status"] == "running" and seconds_since(row["heartbeat_at"]) > STALE_SECONDS:
        return "stalled"
    return row["status"]


def _spec(name: str) -> JobSpec:
    if name not in JOBS:
        raise KeyError(name)
    return JOBS[name]


def status(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every job definition with its latest run (None if it never ran)."""
    rows = {r["name"]: r for r in conn.execute("SELECT * FROM jobs")}
    out = []
    for spec in JOBS.values():
        run = None
        if row := rows.get(spec.name):
            run = {k: row[k] for k in ("status", "total", "done", "failed", "current", "message",
                                       "started_at", "heartbeat_at", "finished_at")}
            run.update(detail=json.loads(row["detail"] or "{}"), stop_requested=bool(row["stop_requested"]),
                       state=_state(row))
        out.append({"name": spec.name, "title": spec.title, "kind": spec.kind, "resumable": spec.resumable,
                    "scoped": spec.scoped, "unit": spec.unit, "command": spec.command, "run": run})
    return out


def start(conn: sqlite3.Connection, name: str, scope: Scope = Scope()) -> None:
    """Launch a job as a detached process. Raises KeyError, ValueError (bad scope) or JobConflict.

    The 'running' row is written in the same transaction as the already-running
    check, before the process exists, so two clicks can never start two jobs.
    """
    spec = _spec(name)
    label = backfill_targets(conn, scope)[1] if spec.scoped else None
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM jobs WHERE name = ?", (name,)).fetchone()
        if row and _state(row) == "running":
            raise JobConflict(f"'{name}' is already running")
        now = utc_now()
        conn.execute(
            """INSERT INTO jobs (name, status, total, done, failed, current, detail, message,
                                 started_at, heartbeat_at, finished_at, stop_requested)
               VALUES (?, 'running', NULL, 0, 0, 'starting', ?, NULL, ?, ?, NULL, 0)
               ON CONFLICT (name) DO UPDATE SET
                 status = 'running', total = NULL, done = 0, failed = 0, current = 'starting',
                 detail = excluded.detail, message = NULL, started_at = excluded.started_at,
                 heartbeat_at = excluded.heartbeat_at, finished_at = NULL, stop_requested = 0""",
            (name, json.dumps({"scope": label} if label else {}), now, now),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    try:
        _spawn(name, spec.args(scope))
    except OSError as exc:
        conn.execute("UPDATE jobs SET status = 'failed', finished_at = ?, message = ? WHERE name = ?",
                     (utc_now(), f"Could not start: {exc}", name))
        conn.commit()
        raise


def stop(conn: sqlite3.Connection, name: str) -> Literal["stopping", "cleared"]:
    """Ask a running job to stop, or close the record of one whose process died.

    Raises KeyError for an unknown job and JobConflict when it is not running.
    """
    _spec(name)
    row = conn.execute("SELECT * FROM jobs WHERE name = ?", (name,)).fetchone()
    if not row or row["status"] != "running":
        raise JobConflict(f"'{name}' is not running")
    if _state(row) == "stalled":
        conn.execute(
            """UPDATE jobs SET status = 'stopped', finished_at = ?, current = NULL, stop_requested = 0,
                               message = 'Marked as stopped: the process was not responding'
               WHERE name = ?""", (utc_now(), name))
        conn.commit()
        return "cleared"
    conn.execute("UPDATE jobs SET stop_requested = 1 WHERE name = ?", (name,))
    conn.commit()
    return "stopping"


def _spawn(name: str, args: list[str]) -> None:
    """Run `python -m wfm <args>` detached from the API, so it survives reloads.

    Output goes to data/logs/<name>.log.
    """
    log_dir = Path(settings.db_path).parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    with open(log_dir / f"{name}.log", "a", encoding="utf-8") as log:
        log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} started from the dashboard: wfm {' '.join(args)}\n")
        log.flush()
        detach: dict[str, Any] = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
            if sys.platform == "win32" else {"start_new_session": True}
        )
        subprocess.Popen(
            [sys.executable, "-m", "wfm", *args], cwd=PROJECT_ROOT / "backend",
            stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            env={**os.environ, "WFM_DB_PATH": str(settings.db_path), "PYTHONUNBUFFERED": "1",
                 "PYTHONIOENCODING": "utf-8", LAUNCHED_BY_DASHBOARD: name},
            **detach,
        )
