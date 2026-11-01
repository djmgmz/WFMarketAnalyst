"""Command line entry point: ``python -m wfm <command>``."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")

from .config import settings
from .db import session
from .jobs import Job, JobConflict, Scope, backfill_targets
from .market import HttpMarket, Market


def _with_market(work: Callable[[Market], Awaitable[T]]) -> T:
    """Run async `work` against the real warframe.market adapter."""

    async def main() -> T:
        async with HttpMarket() as market:
            return await work(market)

    return asyncio.run(main())


def _cmd_init(_: argparse.Namespace) -> None:
    with session():
        print(f"Database ready at {settings.db_path}")


def _cmd_sync_catalog(args: argparse.Namespace) -> None:
    from .catalog import sync_catalog

    with session() as conn, Job.run("sync-catalog") as job:
        summary = _with_market(lambda market: sync_catalog(
            conn,
            market,
            details=not args.no_details,
            refresh_details=args.refresh_details,
            force=args.force,
            detail_limit=args.limit,
            job=job,
        ))
        job.message = f"{job.done} item details fetched"
    if job.status == "stopped":
        print("Stopped. Everything fetched so far is saved; run sync-catalog again to resume.")
    else:
        print(json.dumps(summary, indent=2))


def _cmd_record(args: argparse.Namespace) -> None:
    from .recorders import run_recorders

    print(f"Recording new orders (and watchlist snapshots every {args.snapshot_interval // 60} min). Ctrl+C to stop.")
    with session() as conn, Job.run("record") as job:
        _with_market(lambda market: run_recorders(conn, market, snapshot_interval=args.snapshot_interval, job=job))
    print(f"Stopped recording. {job.done} new orders saved this session.")


def _cmd_backfill(args: argparse.Namespace) -> None:
    from .recorders import backfill_stats

    scope = Scope(category=args.category, slugs=tuple(args.slugs), watchlist=args.watchlist,
                  with_history=args.with_history)
    with session() as conn:
        try:
            slugs, label = backfill_targets(conn, scope)
        except ValueError as exc:
            raise SystemExit(str(exc)) from None
        print(f"Backfilling price history for {len(slugs)} items "
              f"(one request each, shared limit {settings.requests_per_second}/s). Ctrl+C to stop.")
        with Job.run("backfill", total=len(slugs), scope=label) as job:
            result = _with_market(lambda market: backfill_stats(conn, market, slugs, job, refetch=args.refetch))
            job.message = f"{result.done} items, {result.stored} statistic rows" + (
                f", {result.skipped} already fetched today" if result.skipped else "")
    if job.status == "stopped":
        # Each item commits as soon as it is fetched, so nothing finished is lost.
        print(f"Stopped after {job.done} of {len(slugs)} items. Everything fetched so far is saved; "
              "run the same backfill again to pick up where it stopped.")
    else:
        print(f"Backfilled {job.message}")


def _cmd_forecast(_: argparse.Namespace) -> None:
    from .forecast import train

    with session() as conn, Job.run("train-forecast") as job:
        try:
            report = train(conn, job)
        except ValueError as exc:
            raise SystemExit(str(exc)) from None
        job.message = _forecast_summary(report)
    print(f"Forecast trained: {job.message}" if job.status == "finished" else "Stopped. No forecasts were changed.")


def _cmd_sync_images(_: argparse.Namespace) -> None:
    from .images import HttpImageSource, ImagesSynced, sync_images

    async def run(conn, job: Job) -> ImagesSynced:
        async with HttpImageSource() as source:
            return await sync_images(conn, source, settings.images_dir, job)

    with session() as conn, Job.run("sync-images") as job:
        summary = asyncio.run(run(conn, job))
        job.message = (f"{summary.matched} of {summary.items} items have an image; "
                       f"{summary.downloaded} files downloaded, {summary.failed} failed")
    if job.status == "stopped":
        print("Stopped. Images saved so far are kept; run sync-images again to resume.")
    else:
        print(json.dumps(summary._asdict(), indent=2))


def _cmd_check_prices(_: argparse.Namespace) -> None:
    from .prices import flag_suspect

    with session() as conn:
        # Parts before sets (a set is checked against its parts); batches keep write locks short.
        ids = [r[0] for r in conn.execute(
            """SELECT DISTINCT ps.item_id FROM price_stats ps JOIN items i ON i.id = ps.item_id
               ORDER BY COALESCE(i.set_root, 0), ps.item_id""")]
        flagged = sum(flag_suspect(conn, ids[i:i + 200]) for i in range(0, len(ids), 200))
    print(f"{flagged} completed-trade rows flagged as likely faked, across {len(ids)} items with history.")


def _forecast_summary(report: dict) -> str:
    week = report["horizons"][str(max(int(h) for h in report["horizons"]))]
    skill = week["overall"].get("skill")
    used = sum(v for seg in week["uses_model"].values() for v in seg.values())
    total = sum(len(seg) for seg in week["uses_model"].values())
    verdict = "not enough data to test the model" if skill is None else f"7-day skill vs no-change {skill:+.0%}"
    return f"{verdict}; model used in {used} of {total} segments; {report['forecasts']} forecasts"


def _cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run("wfm.api:app", host=args.host, port=args.port, reload=args.reload)


def main() -> None:
    parser = argparse.ArgumentParser(prog="wfm", description="WFMarketAnalyst backend")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the SQLite database").set_defaults(func=_cmd_init)

    p = sub.add_parser("sync-catalog", help="download items and reference collections (no prices)")
    p.add_argument("--no-details", action="store_true", help="only the item list, skip per-item details")
    p.add_argument("--refresh-details", action="store_true", help="refetch details for every item (resumable)")
    p.add_argument("--force", action="store_true", help="ignore collection versions and resync")
    p.add_argument("--limit", type=int, help="fetch at most N item details (for testing)")
    p.set_defaults(func=_cmd_sync_catalog)

    p = sub.add_parser("record", help="RECORD market data: live new orders + watchlist snapshots")
    p.add_argument("--snapshot-interval", type=int, default=900, help="seconds between watchlist snapshots")
    p.set_defaults(func=_cmd_record)

    p = sub.add_parser("backfill", help="RECORD 90 days of price history from the v1 statistics endpoint")
    p.add_argument("slugs", nargs="*", help="item slugs, e.g. arcane_energize")
    p.add_argument("--watchlist", action="store_true", help="backfill every watchlist item")
    p.add_argument("--category", help="backfill a whole category, e.g. Arcane")
    p.add_argument("--with-history", action="store_true",
                   help="refresh every item that already has history (run daily to keep it growing)")
    p.add_argument("--refetch", action="store_true",
                   help="also fetch items already fetched today (normally skipped, so a rerun resumes)")
    p.set_defaults(func=_cmd_backfill)

    sub.add_parser("forecast", help="train the price forecast and store fresh forecasts (no network)") \
        .set_defaults(func=_cmd_forecast)

    sub.add_parser("sync-images", help="download item images from the Warframe community database (resumable)") \
        .set_defaults(func=_cmd_sync_images)

    sub.add_parser("check-prices", help="re-flag likely faked trades in all stored price history (offline)") \
        .set_defaults(func=_cmd_check_prices)

    p = sub.add_parser("serve", help="run the dashboard API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true")
    p.set_defaults(func=_cmd_serve)

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        args.func(args)
    except JobConflict as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
