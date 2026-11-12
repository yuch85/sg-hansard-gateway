"""Staging-DB build + atomic swap for the index (spec §3.1, threat T-27.1-03).

The crawl writes the full index into ``<index_db_path>.staging`` and, only
after a clean build, ``os.replace``s it over the live file (atomic on POSIX —
same directory, same filesystem). Any exception deletes the staging file and
leaves the existing live index untouched: a failed crawl never serves a
half-built index. The live file is created 0600 (T-27.1-01).
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Optional

from hansard_gateway.config import Settings, settings as _settings
from hansard_gateway.index.build import build_index
from hansard_gateway.index.schema import BUILD_ID_KEY

logger = logging.getLogger(__name__)

#: File mode for the index DB: owner read/write only (T-27.1-01).
INDEX_FILE_MODE = 0o600
#: Suffix appended to the live path for the staging file.
STAGING_SUFFIX = ".db.staging"
#: Suffix appended to the live path for the single-writer lock file.
LOCK_SUFFIX = ".lock"
#: meta key holding the first-run guard's crawl floor (27.5, S7).
CRAWL_FLOOR_KEY = "crawl_floor"


class CrawlLockedError(RuntimeError):
    """Another crawl process holds the single-writer lock on the index."""


@contextlib.contextmanager
def index_lock(live: Path) -> Iterator[None]:
    """Non-blocking exclusive flock on ``<live>.lock`` (single crawl writer
    per index, T-27.5-01). POSIX-only (fcntl): the supported targets,
    container and host, are Linux.

    Raises :class:`CrawlLockedError` when the lock is already held; releases
    on exit. The lock file itself is never deleted — it carries no data.
    """
    lock_path = live.parent / (live.name + LOCK_SUFFIX)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, INDEX_FILE_MODE)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CrawlLockedError(f"another crawl holds the lock on {live}") from None
        yield
    finally:
        os.close(fd)


def read_crawl_floor(live: Path) -> Optional[str]:
    """The stored first-run-guard floor (meta key ``crawl_floor``), or None.

    Read-only; None on any failure (missing file, table, or key).
    """
    if not live.exists() or live.stat().st_size == 0:
        return None
    try:
        conn = sqlite3.connect(f"file:{live}?mode=ro", uri=True)
    except sqlite3.DatabaseError:
        return None
    try:
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (CRAWL_FLOOR_KEY,)
            ).fetchone()
        except sqlite3.DatabaseError:
            return None
        return str(row[0]) if row else None
    finally:
        conn.close()


def write_crawl_floor(staging: Path, floor: Optional[str]) -> None:
    """Set (or delete, when None) meta.crawl_floor on the staging index.

    Same one-shot transaction style as :func:`write_state`: the staging file
    is swapped over the live index only after a clean build.
    """
    conn = sqlite3.connect(staging)
    try:
        if floor is None:
            conn.execute("DELETE FROM meta WHERE key = ?", (CRAWL_FLOOR_KEY,))
        else:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (CRAWL_FLOOR_KEY, floor),
            )
        conn.commit()
    finally:
        conn.close()


def staging_path(live: Path) -> Path:
    """The staging file path for a live index path (``index.db.staging``)."""
    return live.parent / (live.name + STAGING_SUFFIX)


def _report_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Row counts per index table (RUNBOOK cold-crawl reporting step)."""
    counts: dict[str, int] = {}
    for (table,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ):
        counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return counts


def build_staging_index(
    rows: list[dict[str, Any]],
    live: Path,
    *,
    settings: Optional[Settings] = None,
) -> Path:
    """Build the full index into the staging file; return the staging path.

    The staging file is deleted if the build raises, so a failed build never
    leaves a partial file behind (T-27.1-03).
    """
    cfg = settings or _settings
    target = staging_path(live)
    try:
        build_index(rows, target, settings=cfg)
    except Exception:
        if target.exists():
            target.unlink()
        raise
    os.chmod(target, INDEX_FILE_MODE)
    return target


def atomic_swap(*, live: Path, settings: Optional[Settings] = None) -> str:
    """Swap the staging file over the live index; return the new build_id.

    ``os.replace`` is atomic on POSIX: readers see either the old or the new
    file, never a half-written one. After the swap, logs table counts +
    build_id (RESEARCH open question 4 — the cold-crawl RUNBOOK step).
    """
    staging = staging_path(live)
    os.replace(staging, live)
    conn = sqlite3.connect(live)
    try:
        counts = _report_counts(conn)
        build_id = (
            conn.execute(
                "SELECT value FROM meta WHERE key = ?", (BUILD_ID_KEY,)
            ).fetchone()
            or (None,)
        )[0]
    finally:
        conn.close()
    logger.info(
        "index swapped in: %s build_id=%s tables=%s",
        live,
        build_id,
        counts,
    )
    return build_id or ""


#: Report columns carried forward from the live index into the next build.
_CARRY_COLUMNS = "report_id, link_id, sitting_date, title, report_type, speaker"


def crawl_window(
    cfg: Settings,
    state: dict[str, tuple[int, str]],
    distrusted: bool,
    *,
    cold: bool,
    end: date,
    has_rows: bool,
) -> tuple[date, Optional[str]]:
    """Resolve the enumeration start date and the governing crawl floor (S7).

    A STORED floor always wins (F1): it clamps the start even when
    ``crawl_state`` is empty (an all-fail first run leaves it empty). A floor
    is COMPUTED only for a genuinely fresh deployment: none stored AND the
    state is empty AND no report rows exist AND the live index is not
    signalling distrust (review: an index carrying rows but no state — e.g.
    pre-27.4 — is an existing deployment, not a fresh one; it backfills
    honestly instead of being clamped). The distrust path (F2) is never
    guarded — it refetches from ``crawl_start_date`` with no floor. A
    cold / full-history run clears the floor (returned None).
    """
    start = datetime.strptime(cfg.crawl_start_date, "%Y-%m-%d").date()
    if cold or cfg.crawl_full_history or distrusted:
        return start, None
    stored = read_crawl_floor(cfg.index_db_path)
    if stored:
        try:
            return max(start, date.fromisoformat(stored)), stored
        except ValueError:
            logger.warning("ignoring malformed stored crawl_floor=%r", stored)
    if not state and not has_rows:
        earliest = max(start, end - timedelta(days=cfg.crawl_first_run_days))
        if earliest > start:
            logger.warning(
                "first-run guard: fresh index — crawling only %s -> %s "
                "(~%d requests at ~1 req/s); set HANSARD_CRAWL_FULL_HISTORY=1 "
                "or run --cold for full history",
                earliest.isoformat(),
                end.isoformat(),
                (end - earliest).days + 1,
            )
            return earliest, earliest.isoformat()
    return start, None


def read_live(
    live: Path,
) -> tuple[list[dict[str, Any]], dict[str, tuple[int, str]], bool]:
    """Read the live index's reports and crawl_state without modifying it.

    Opened read-only. A missing, empty, or unreadable file (or table) yields
    nothing for that part: the crawl then rebuilds from what it fetches.
    The third element flags the DISTRUST case (27.5, F2): crawl_state claims
    reports exist but none were readable, so the index is not trustworthy —
    the crawl refetches everything and never applies the first-run guard.
    """
    rows: list[dict[str, Any]] = []
    state: dict[str, tuple[int, str]] = {}
    if not live.exists() or live.stat().st_size == 0:
        return rows, state, False
    try:
        conn = sqlite3.connect(f"file:{live}?mode=ro", uri=True)
    except sqlite3.DatabaseError:
        logger.warning("live index not readable - rebuilding from fetched data (%s)", live)
        return rows, state, False
    try:
        try:
            keys = [c.strip() for c in _CARRY_COLUMNS.split(",")]
            rows = [
                dict(zip(keys, rec))
                for rec in conn.execute(f"SELECT {_CARRY_COLUMNS} FROM report")
            ]
        except sqlite3.DatabaseError:
            logger.warning("live index has no readable report table (%s)", live)
        try:
            state = {
                r[0]: (r[1], r[2])
                for r in conn.execute(
                    "SELECT sitting_date, had_reports, crawled_at FROM crawl_state"
                )
            }
        except sqlite3.DatabaseError:
            logger.warning("live index has no readable crawl_state (%s)", live)
    finally:
        conn.close()
    if not rows and any(hr == 1 for hr, _ in state.values()):
        # State says reports exist but none were read: the index is not
        # trustworthy, so refetch everything instead of skipping those dates.
        logger.warning("live crawl_state lists reports but report table is empty - refetching all")
        return [], {}, True
    return rows, state, False


def write_state(staging: Path, state: dict[str, tuple[int, str]]) -> None:
    """Write the full crawl_state into the staging index (one transaction)."""
    conn = sqlite3.connect(staging)
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO crawl_state (sitting_date, had_reports, crawled_at)"
            " VALUES (?, ?, ?)",
            [(d, hr, at) for d, (hr, at) in sorted(state.items())],
        )
        conn.commit()
    finally:
        conn.close()
