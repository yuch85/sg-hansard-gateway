"""Crawl entrypoint: date enumeration, incremental logic, index rebuild.

Run via ``uv run python -m crawl.crawl_main`` — directly, on a host timer
(cron / systemd), or as the in-image ``loop`` action (Phase 27.5). Every run
takes the single-writer flock (``crawl_db.index_lock``; a contender exits 3)
and honors the first-run guard: on a fresh deployment only the recent
``crawl_first_run_days`` window is crawled and persisted as
``meta.crawl_floor``, unless ``--cold`` / ``crawl_full_history`` asks for
full history. Default = incremental: dates already in ``crawl_state`` are
skipped unless their crawl is older than ``crawl_recrawl_age_days`` or they
had no reports and are newer than ``crawl_empty_recheck_days`` (late-posted
reports). ``--cold`` forces a full rebuild from ``crawl_start_date`` (the
one-off RUNBOOK step).

A date whose fetch raises :class:`~crawl.crawl_client.CrawlTransientError`
is logged and skipped — NOT recorded as empty — so the next run retries it
(spec §3.1; T-27.1-04 accept: a down week never touches the live index).
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from crawl.crawl_client import CrawlClient, CrawlTransientError
from crawl import crawl_db
from crawl.crawl_db import atomic_swap, build_staging_index, staging_path
from hansard_gateway.config import Settings, settings as _settings

logger = logging.getLogger(__name__)

#: UTC timestamp format for crawl_state.crawled_at.
_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

#: Table name for crawl bookkeeping (schema.py).
_CRAWL_STATE = "crawl_state"


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 Zulu string."""
    return datetime.now(timezone.utc).strftime(_STAMP_FORMAT)


def _dates_from_state(
    state: dict[str, tuple[int, str]],
    *,
    start: date,
    end: date,
    recrawl_age_days: int,
    empty_recheck_days: int,
    cold: bool,
) -> list[date]:
    """Enumerate sitting dates needing a fetch (incremental or cold).

    Incremental skips a date unless it is unknown, had reports and was last
    crawled at least ``recrawl_age_days`` ago, or had no reports but is
    newer than ``empty_recheck_days`` (late-posted reports).
    """
    now = datetime.now(timezone.utc)
    out: list[date] = []
    day = start
    while day <= end:
        entry = state.get(day.isoformat())
        if not cold and entry is not None:
            had_reports, crawled_at = entry
            if had_reports == 0:
                if (end - day).days >= empty_recheck_days:
                    day += timedelta(days=1)
                    continue
            else:
                try:
                    age = now - datetime.fromisoformat(crawled_at.replace("Z", "+00:00"))
                except ValueError:
                    age = timedelta(days=recrawl_age_days + 1)
                if age.days < recrawl_age_days:
                    day += timedelta(days=1)
                    continue
        out.append(day)
        day += timedelta(days=1)
    return out


def _dates_to_fetch(
    conn: sqlite3.Connection,
    *,
    start: date,
    end: date,
    recrawl_age_days: int,
    cold: bool,
    empty_recheck_days: int = 0,
) -> list[date]:
    """Enumerate sitting dates needing a fetch, reading state from ``conn``."""
    state = {
        row[0]: (row[1], row[2])
        for row in conn.execute(
            "SELECT sitting_date, had_reports, crawled_at FROM crawl_state"
        )
    }
    return _dates_from_state(
        state,
        start=start,
        end=end,
        recrawl_age_days=recrawl_age_days,
        empty_recheck_days=empty_recheck_days,
        cold=cold,
    )


def run_crawl(
    *,
    settings: Optional[Settings] = None,
    client: Optional[CrawlClient] = None,
    cold: bool = False,
    today: Optional[date] = None,
) -> int:
    """Execute one crawl run; return a process exit code.

    0 = success, 1 = failure, 3 = skipped because another crawl holds the
    single-writer lock on the index (T-27.5-01: a skip is neither success
    nor failure). The new index is built in a staging file from the live
    index's existing reports (minus the dates re-fetched this run) plus the
    newly fetched ones, and carries the merged ``crawl_state``. Nothing is
    written to the live file until the atomic swap, so any failure leaves
    it untouched.
    """
    cfg = settings or _settings
    index_db = cfg.index_db_path
    end = today or date.today()
    own_client = client is None
    crawl_client = client or CrawlClient(settings=cfg)
    try:
        try:
            with crawl_db.index_lock(index_db):
                return _run_crawl_locked(cfg, crawl_client, cold=cold, end=end)
        except crawl_db.CrawlLockedError:
            logger.warning(
                "another crawl holds the lock on %s — skipping this run", index_db
            )
            return 3
        except OSError:
            # e.g. the index directory does not exist: the pre-27.5 contract
            # was a plain failed run (rc 1), not a traceback out of run_crawl.
            logger.exception("crawl run failed — cannot take the lock on %s", index_db)
            return 1
    finally:
        if own_client:
            crawl_client.close()


def _run_crawl_locked(
    cfg: Settings, crawl_client: CrawlClient, *, cold: bool, end: date
) -> int:
    """The crawl body, run under ``index_lock`` (see :func:`run_crawl`)."""
    index_db = cfg.index_db_path
    old_rows, state, distrusted = crawl_db.read_live(index_db)
    start, floor = crawl_db.crawl_window(
        cfg, state, distrusted, cold=cold, end=end, has_rows=bool(old_rows)
    )
    dates = _dates_from_state(
        state,
        start=start,
        end=end,
        recrawl_age_days=cfg.crawl_recrawl_age_days,
        empty_recheck_days=cfg.crawl_empty_recheck_days,
        cold=cold,
    )
    logger.info(
        "crawl run: %d dates to fetch (%s -> %s, cold=%s, floor=%s), %d existing reports",
        len(dates),
        start.isoformat(),
        end.isoformat(),
        cold,
        floor,
        len(old_rows),
    )
    old_dates = {r["sitting_date"] for r in old_rows}
    new_rows: list[dict] = []
    replaced: set[str] = set()
    updates: dict[str, tuple[int, str]] = {}
    skipped = 0
    for day in dates:
        iso = day.isoformat()
        try:
            fetched = crawl_client.search_sitting(sitting=day)
        except CrawlTransientError as exc:
            # Skip (not empty): the date retries on the next run.
            logger.error(
                "skipping %s — transient upstream failure (will retry)",
                iso,
                extra={"detail": exc.detail, "status": exc.status},
            )
            skipped += 1
            continue
        if not fetched and iso in old_dates:
            # Reports were indexed for this date; an empty answer now is
            # not trusted to delete them (keep rows and state, retry later).
            logger.warning("empty answer for %s which has indexed reports — kept", iso)
            skipped += 1
            continue
        stamped_at = _now_iso()
        updates[iso] = (int(bool(fetched)), stamped_at)
        replaced.add(iso)
        for row in fetched:
            new_rows.append(_to_report_row(row, day=day, stamped_at=stamped_at))
    rows = [r for r in old_rows if r["sitting_date"] not in replaced] + new_rows

    staging = staging_path(index_db)
    try:
        build_staging_index(rows, index_db, settings=cfg)
        crawl_db.write_state(staging, {**state, **updates})
        # The governing floor must be re-written into EVERY staging build it
        # governs (checker 3, defect 1): build_index only writes meta.build_id,
        # so a floor not re-written here would vanish at the swap. A cold /
        # full-history / distrust run clears it (floor None -> delete).
        crawl_db.write_crawl_floor(staging, floor)
    except Exception:
        logger.exception(
            "index build failed — live index untouched (%s)", index_db
        )
        staging.unlink(missing_ok=True)
        return 1
    build_id = atomic_swap(live=index_db, settings=cfg)
    logger.info(
        "crawl run complete: build_id=%s rows=%d new_rows=%d dates_fetched=%d "
        "dates_skipped=%d max_sitting_date=%s",
        build_id,
        len(rows),
        len(new_rows),
        len(updates),
        skipped,
        max((r["sitting_date"] for r in rows), default="none"),
    )
    return 0


def _to_report_row(
    row: dict, *, day: date, stamped_at: str
) -> dict:
    """Map one raw searchResult row onto the index report shape."""
    report_id = str(row.get("reportId") or "")
    html_file = row.get("htmlFileName")
    link_id = str(html_file) if html_file else report_id.rstrip("#")
    return {
        "report_id": report_id,
        "link_id": link_id,
        "sitting_date": day.isoformat(),
        "title": str(row.get("title") or ""),
        "report_type": row.get("reportType"),
        "speaker": row.get("mpNames"),
        "crawled_at": stamped_at,
    }


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entrypoint: ``python -m crawl.crawl_main [loop] [--cold]``."""
    parser = argparse.ArgumentParser(
        description="Crawl SPRS sitting TOCs into the local term index."
    )
    parser.add_argument(
        "action",
        nargs="?",
        choices=("loop",),
        help="'loop': crawl immediately, then every crawl_interval_hours "
        "until signalled (27.5); other flags are ignored in loop mode",
    )
    parser.add_argument(
        "--cold",
        action="store_true",
        help="full rebuild from crawl_start_date (one-off RUNBOOK step)",
    )
    parser.add_argument(
        "--seed-state",
        action="store_true",
        help="one-off, no network: rebuild crawl_state from the live index",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="logging verbosity (default INFO)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    try:
        if args.action == "loop":
            from crawl.crawl_loop import run_loop

            return run_loop()
        if args.seed_state:
            from crawl.crawl_seed import seed_state

            return seed_state()
        return run_crawl(cold=args.cold)
    except Exception:
        logger.exception("crawl run failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
