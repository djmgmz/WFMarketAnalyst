"""Terminal display for jobs (internal to the Job module; no third-party dependency).

A job's terminal line is a pure rendering of its snapshot, redrawn every second
by TerminalLine, so the terminal and the dashboard can never disagree. While a
line is active, route_logs() prints log records above it instead of through it.
When output is redirected to a file, the line is printed as plain ASCII every
`plain_every` seconds instead.
"""

from __future__ import annotations

import logging
import shutil
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, TextIO

from .db import seconds_since

Snapshot = dict[str, Any]


def duration(seconds: float) -> str:
    seconds = int(max(0.0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


# -- renderers: snapshot -> (rich line, plain line) ----------------------------

def progress_line(label: str) -> Callable[[Snapshot], tuple[str, str]]:
    """Renderer for per-item jobs: bar, count, elapsed, ETA, failures, current item."""

    def render(s: Snapshot) -> tuple[str, str]:
        total, done = s["total"], s["done"]
        elapsed = seconds_since(s["started_at"])
        if not total:
            parts = [f"{label}: {s['current'] or 'starting'}", f"elapsed {duration(elapsed)}"]
            return " · ".join(parts), " | ".join(parts)
        pct = done / total
        parts = [f"{label} {done}/{total} ({pct:.0%})", f"elapsed {duration(elapsed)}"]
        worked = done - s["detail"].get("skipped", 0)  # skipped items (a resume) took no time
        if 0 < worked and done < total:
            parts.append(f"ETA {duration(elapsed / worked * (total - done))}")
        if s["failed"]:
            parts.append(f"{s['failed']} failed")
        filled = int(24 * pct)
        rich = "█" * filled + "░" * (24 - filled) + " " + " · ".join(parts)
        if s["current"]:
            rich += f" · {s['current']}"
        return rich, " | ".join(parts)

    return render


def recorder_line(s: Snapshot) -> tuple[str, str]:
    """Renderer for the order recorder: connection, orders, last order, uptime."""
    d = s["detail"]
    connection = d.get("connection", "connecting")
    icon = {"connected": "●", "connecting": "○", "reconnecting": "◌"}.get(connection, "○")
    parts = [connection, f"{s['done']} new orders"]
    if d.get("last_order_at"):
        parts.append(f"last {duration(seconds_since(d['last_order_at']))} ago ({s['current']})")
    parts.append(f"up {duration(seconds_since(s['started_at']))}")
    if d.get("snapshots"):
        parts.append(f"{d['snapshots']} watchlist prices")
    return f"{icon} Recording · " + " · ".join(parts), "Recording | " + " | ".join(parts)


# -- the redrawing line ---------------------------------------------------------

class TerminalLine:
    def __init__(self, render: Callable[[], tuple[str, str]], *, stream: TextIO | None = None,
                 interval: float = 1.0, plain_every: float = 30.0):
        self.render = render
        self.stream = stream or sys.stderr
        self.interactive = self.stream.isatty()
        self._interval = interval if self.interactive else plain_every
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._line_len = 0
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> TerminalLine:
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self.draw()

    def draw(self) -> None:
        with self._lock:
            rich, plain = self.render()
            if not self.interactive:
                self.stream.write(plain + "\n")
            else:
                line = rich[: shutil.get_terminal_size((100, 20)).columns - 1]
                self.stream.write("\r" + line.ljust(self._line_len))
                self._line_len = len(line)
            self.stream.flush()

    def clear(self) -> None:
        if self.interactive and self._line_len:
            with self._lock:
                self.stream.write("\r" + " " * self._line_len + "\r")
                self.stream.flush()
                self._line_len = 0

    def close(self) -> None:
        """Stop redrawing and leave the final state on screen."""
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)
        self.draw()
        if self.interactive:
            self.stream.write("\n")
            self.stream.flush()


class _AboveLineHandler(logging.Handler):
    def __init__(self, line: TerminalLine, formatter: logging.Formatter | None):
        super().__init__()
        self.line = line
        if formatter:
            self.setFormatter(formatter)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.line.clear()
            self.line.stream.write(self.format(record) + "\n")
            self.line.stream.flush()
            if self.line.interactive:
                self.line.draw()
        except Exception:  # pragma: no cover - never let logging crash the job
            self.handleError(record)


@contextmanager
def route_logs(line: TerminalLine) -> Iterator[None]:
    """Print log records above `line` (instead of through it) while active."""
    root = logging.getLogger()
    previous = root.handlers[:]
    formatter = previous[0].formatter if previous else None
    root.handlers = [_AboveLineHandler(line, formatter)]
    try:
        yield
    finally:
        root.handlers = previous
