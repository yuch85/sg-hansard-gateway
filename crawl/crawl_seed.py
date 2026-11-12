"""One-off ``--seed-state``: rebuild ``crawl_state`` from the live index.

No network. For an index whose ``crawl_state`` was lost (or never kept), mark
every date from ``crawl_start_date`` up to the newest report date: dates that
have reports -> had_reports=1, all others -> had_reports=0, both stamped with
the index build time. Dates after the newest report date stay unknown, so the
next incremental run fetches them. Assumes the index came from a complete
crawl up to its newest report date (not verifiable from the index alone).
Refuses (exit 2, live untouched) if state already exists or the index is empty.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from crawl import crawl_db
from hansard_gateway.config import Settings, settings as _settings
from hansard_gateway.index.schema import BUILD_ID_KEY

logger = logging.getLogger(__name__)

_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
#: build_id ends with a compact UTC stamp such as 20260917T233632Z.
_BUILD_STAMP_LEN = 16
_BUILD_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"


def _build_time(live) -> Optional[datetime]:
    """Build time parsed from ``meta.build_id``; None when unreadable."""
    try:
        conn = sqlite3.connect(f"file:{live}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (BUILD_ID_KEY,)
            ).fetchone()
        finally:
            conn.close()
        return datetime.strptime(row[0][-_BUILD_STAMP_LEN:], _BUILD_STAMP_FORMAT).replace(
            tzinfo=timezone.utc
        )
    except (sqlite3.DatabaseError, TypeError, ValueError):
        return None


def seed_state(*, settings: Optional[Settings] = None) -> int:
    """Seed ``crawl_state`` from the live index; return an exit code.

    0 = seeded, 1 = build failure, 2 = refused (state exists / empty index),
    3 = another crawl holds the single-writer lock (skipped; live untouched).
    """
    cfg = settings or _settings
    live = cfg.index_db_path
    try:
        with crawl_db.index_lock(live):
            return _seed_state_locked(cfg, live)
    except crawl_db.CrawlLockedError:
        logger.warning(
            "another crawl holds the lock on %s — skipping seed-state", live
        )
        return 3


def _seed_state_locked(cfg: Settings, live) -> int:
    """The seed body, run under ``index_lock`` (see :func:`seed_state`)."""
    rows, state, _distrusted = crawl_db.read_live(live)
    if state:
        logger.error("seed-state refused: crawl_state already has %d rows", len(state))
        return 2
    report_dates = {r["sitting_date"] for r in rows if r.get("sitting_date")}
    built = _build_time(live) if rows else None
    if not report_dates or built is None:
        logger.error("seed-state refused: index has no reports or no readable build_id")
        return 2
    stamp = built.strftime(_STAMP_FORMAT)
    day = date.fromisoformat(cfg.crawl_start_date)
    last = date.fromisoformat(max(report_dates))
    seeded: dict[str, tuple[int, str]] = {}
    while day <= last:
        iso = day.isoformat()
        seeded[iso] = (1 if iso in report_dates else 0, stamp)
        day += timedelta(days=1)
    staging = crawl_db.staging_path(live)
    try:
        crawl_db.build_staging_index(rows, live, settings=cfg)
        crawl_db.write_state(staging, seeded)
    except Exception:
        logger.exception("seed-state build failed - live index untouched (%s)", live)
        staging.unlink(missing_ok=True)
        return 1
    build_id = crawl_db.atomic_swap(live=live, settings=cfg)
    logger.info(
        "seed-state complete: build_id=%s reports=%d dates_seeded=%d through=%s",
        build_id, len(rows), len(seeded), last.isoformat(),
    )
    return 0
