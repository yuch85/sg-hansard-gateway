"""Crawl entrypoint: date enumeration, incremental logic, index rebuild.

Run via ``uv run python -m crawl.crawl_main`` (systemd --user timer, weekly).
Default = incremental: dates already in ``crawl_state`` are skipped unless
their crawl is older than ``crawl_recrawl_age_days``. ``--cold`` forces a
full rebuild from ``crawl_start_date`` (the one-off RUNBOOK step).

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
from crawl.crawl_db import atomic_swap, build_staging_index, staging_path
from hansard_gateway.config import Settings, settings as _settings
from hansard_gateway.index.schema import BUILD_ID_KEY, init_db

logger = logging.getLogger(__name__)

#: UTC timestamp format for crawl_state.crawled_at.
_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

#: Table name for crawl bookkeeping (schema.py).
_CRAWL_STATE = "crawl_state"


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 Zulu string."""
    return datetime.now(timezone.utc).strftime(_STAMP_FORMAT)


def _state_conn(index_db: Path) -> sqlite3.Connection:
    """Open the live index (or a fresh empty one) for crawl_state reads.

    A missing, empty, or corrupt (non-SQLite) file is replaced with a fresh
    schema-only DB so the crawl can start clean; a valid live index is left
    untouched (the app may be reading it).
    """
    if not index_db.exists() or index_db.stat().st_size == 0:
        _reinit(index_db)
        return sqlite3.connect(str(index_db))
    probe = sqlite3.connect(str(index_db))
    try:
        probe.execute("SELECT count(*) FROM sqlite_master")
    except sqlite3.DatabaseError:
        probe.close()
        index_db.unlink(missing_ok=True)
        _reinit(index_db)
        logger.warning("live index was not a valid DB — reinitialised (%s)", index_db)
        return sqlite3.connect(str(index_db))
    # Ensure the schema exists even if the file pre-dates it (defensive).
    try:
        probe.execute("SELECT count(*) FROM crawl_state")
    except sqlite3.OperationalError:
        init_db(probe)
        probe.commit()
    probe.close()
    return sqlite3.connect(str(index_db))


def _reinit(index_db: Path) -> None:
    """Create a fresh empty index DB at ``index_db`` (schema only)."""
    conn = sqlite3.connect(str(index_db))
    try:
        init_db(conn)
    finally:
        conn.close()


def _dates_to_fetch(
    conn: sqlite3.Connection,
    *,
    start: date,
    end: date,
    recrawl_age_days: int,
    cold: bool,
) -> list[date]:
    """Enumerate sitting dates needing a fetch (incremental or cold)."""
    state: dict[str, tuple[int, str]] = {
        row[0]: (row[1], row[2])
        for row in conn.execute(
            "SELECT sitting_date, had_reports, crawled_at FROM crawl_state"
        )
    }
    now = datetime.now(timezone.utc)
    out: list[date] = []
    day = start
    while day <= end:
        iso = day.isoformat()
        entry = state.get(iso)
        if not cold and entry is not None:
            had_reports, crawled_at = entry
            if had_reports == 0:
                day += timedelta(days=1)
                continue
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


def run_crawl(
    *,
    settings: Optional[Settings] = None,
    client: Optional[CrawlClient] = None,
    cold: bool = False,
    today: Optional[date] = None,
) -> int:
    """Execute one crawl run; return a process exit code (0 = success)."""
    cfg = settings or _settings
    index_db = cfg.index_db_path
    end = today or date.today()
    start = datetime.strptime(cfg.crawl_start_date, "%Y-%m-%d").date()
    own_client = client is None
    crawl_client = client or CrawlClient(settings=cfg)
    try:
        conn = _state_conn(index_db)
        try:
            dates = _dates_to_fetch(
                conn,
                start=start,
                end=end,
                recrawl_age_days=cfg.crawl_recrawl_age_days,
                cold=cold,
            )
            logger.info(
                "crawl run: %d dates to fetch (%s -> %s, cold=%s)",
                len(dates),
                start.isoformat(),
                end.isoformat(),
                cold,
            )
            rows: list[dict] = []
            skipped = 0
            for day in dates:
                try:
                    fetched = crawl_client.search_sitting(sitting=day)
                except CrawlTransientError as exc:
                    # Skip (not empty): the date retries on the next run.
                    logger.error(
                        "skipping %s — transient upstream failure (will retry)",
                        day.isoformat(),
                        extra={"detail": exc.detail, "status": exc.status},
                    )
                    skipped += 1
                    continue
                stamped_at = _now_iso()
                conn.execute(
                    "INSERT OR REPLACE INTO crawl_state "
                    "(sitting_date, had_reports, crawled_at) VALUES (?, ?, ?)",
                    (day.isoformat(), int(bool(fetched)), stamped_at),
                )
                for row in fetched:
                    rows.append(_to_report_row(row, day=day, stamped_at=stamped_at))
            conn.commit()
        finally:
            conn.close()

        staging = staging_path(index_db)
        try:
            build_staging_index(rows, index_db, settings=cfg)
        except Exception:
            logger.exception(
                "index build failed — live index untouched (%s)", index_db
            )
            return 1
        build_id = atomic_swap(live=index_db, settings=cfg)
        logger.info(
            "crawl run complete: build_id=%s rows=%d dates_skipped=%d",
            build_id,
            len(rows),
            skipped,
        )
        return 0
    finally:
        if own_client:
            crawl_client.close()


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
    """CLI entrypoint: ``python -m crawl.crawl_main [--cold]``."""
    parser = argparse.ArgumentParser(
        description="Crawl SPRS sitting TOCs into the local term index."
    )
    parser.add_argument(
        "--cold",
        action="store_true",
        help="full rebuild from crawl_start_date (one-off RUNBOOK step)",
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
        return run_crawl(cold=args.cold)
    except Exception:
        logger.exception("crawl run failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
