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
