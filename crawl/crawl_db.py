"""Staging-DB build + atomic swap for the index (spec §3.1, threat T-27.1-03).

The crawl writes the full index into ``<index_db_path>.staging`` and, only
after a clean build, ``os.replace``s it over the live file (atomic on POSIX —
same directory, same filesystem). Any exception deletes the staging file and
leaves the existing live index untouched: a failed crawl never serves a
half-built index. The live file is created 0600 (T-27.1-01).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Optional

from hansard_gateway.config import Settings, settings as _settings
from hansard_gateway.index.build import build_index
from hansard_gateway.index.schema import BUILD_ID_KEY

logger = logging.getLogger(__name__)

#: File mode for the index DB: owner read/write only (T-27.1-01).
INDEX_FILE_MODE = 0o600
#: Suffix appended to the live path for the staging file.
STAGING_SUFFIX = ".db.staging"


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


def read_live(live: Path) -> tuple[list[dict[str, Any]], dict[str, tuple[int, str]]]:
    """Read the live index's reports and crawl_state without modifying it.

    Opened read-only. A missing, empty, or unreadable file (or table) yields
    nothing for that part: the crawl then rebuilds from what it fetches.
    """
    rows: list[dict[str, Any]] = []
    state: dict[str, tuple[int, str]] = {}
    if not live.exists() or live.stat().st_size == 0:
        return rows, state
    try:
        conn = sqlite3.connect(f"file:{live}?mode=ro", uri=True)
    except sqlite3.DatabaseError:
        logger.warning("live index not readable - rebuilding from fetched data (%s)", live)
        return rows, state
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
        return [], {}
    return rows, state


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
