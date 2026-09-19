"""Build the full term index into a target SQLite path (spec §3.2–3.4).

Takes raw sitting-TOC row dicts (already normalised to the index's report
shape by the caller) and materialises: report rows, the term table, the
cross-word prefix table (prefix lengths 1..ladder_depth, stopwords excluded),
the prefix_children fan-out (with fat-branch grandchild skip-levels, YC
addition 2026-09-18), and the ``meta.build_id`` stamp.

Logging, never silent: a depth-max prefix whose term list exceeds
``ladder_term_cap`` is logged (spec §4.2 / RESEARCH pitfall 5).
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from hansard_gateway.config import Settings, settings as _settings
from hansard_gateway.index.extract import STOPWORDS, extract_terms
from hansard_gateway.index.schema import BUILD_ID_KEY, init_db

logger = logging.getLogger(__name__)

#: Column names of the report table (metadata-only — no body/transcript).
_REPORT_COLUMNS = (
    "report_id",
    "link_id",
    "sitting_date",
    "title",
    "report_type",
    "speaker",
    "crawled_at",
)

#: Row keys the builder reads from a report-shaped dict.
_ROW_REPORT_ID = "report_id"
_ROW_LINK_ID = "link_id"
_ROW_SEEN = "sitting_date"
_ROW_TITLE = "title"
_ROW_TYPE = "report_type"
_ROW_SPEAKER = "speaker"
_ROW_CRAWLED_AT = "crawled_at"

#: UTC timestamp format for build_id and crawled_at.
_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 Zulu string."""
    return datetime.now(timezone.utc).strftime(_STAMP_FORMAT)


def _build_id() -> str:
    """A fresh unique build stamp (uuid4 + timestamp) for meta.build_id.

    The uuid4 guarantees uniqueness even for two builds in the same second
    (the swap-detection test relies on a distinct build_id per build).
    """
    return uuid.uuid4().hex + _now_iso().replace("-", "").replace(":", "")


def _report_values(row: dict[str, Any], *, crawled_at: str) -> tuple[Any, ...]:
    """Map one report-shaped row onto the report table column order."""
    return (
        str(row.get(_ROW_REPORT_ID) or ""),
        str(row.get(_ROW_LINK_ID) or ""),
        str(row.get(_ROW_SEEN) or ""),
        str(row.get(_ROW_TITLE) or ""),
        row.get(_ROW_TYPE),
        row.get(_ROW_SPEAKER),
        crawled_at,
    )


def _insert_reports(conn: sqlite3.Connection, rows: list[dict[str, Any]], *, crawled_at: str) -> None:
    """Insert report rows (replacing any prior rows for the same report_id)."""
    for row in rows:
        conn.execute(
            "INSERT OR REPLACE INTO report "
            f"({', '.join(_REPORT_COLUMNS)}) VALUES ({', '.join('?' * len(_REPORT_COLUMNS))})",
            _report_values(row, crawled_at=crawled_at),
        )


def _insert_terms(conn: sqlite3.Connection, terms: list[Any]) -> dict[str, int]:
    """Insert the term table and return {norm: term_id} for prefix building."""
    by_norm: dict[str, int] = {}
    for term in terms:
        cur = conn.execute(
            "INSERT INTO term (surface, norm, kind, doc_count, first_date, last_date) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (term.surface, term.norm, term.kind, term.doc_count,
             term.first_date, term.last_date),
        )
        by_norm[term.norm] = int(cur.lastrowid)
    return by_norm


def _insert_prefixes(
    conn: sqlite3.Connection,
    terms: list[Any],
    term_ids: dict[str, int],
    *,
    depth: int,
) -> None:
    """Materialise the cross-word prefix table (spec §3.4).

    For every non-stopword word of a term's norm, insert (word[:n], term_id)
    for n in 1..depth. Stopwords get no prefix rows of their own, but a term
    is still reachable via its other words.
    """
    rows: list[tuple[str, int]] = []
    for term in terms:
        tid = term_ids[term.norm]
        for word in term.norm.split(" "):
            if not word or word in STOPWORDS:
                continue
            for n in range(1, min(depth, len(word)) + 1):
                rows.append((word[:n], tid))
    conn.executemany("INSERT OR IGNORE INTO term_prefix VALUES (?, ?)", rows)


def _materialise_children(
    conn: sqlite3.Connection,
    *,
    fat_threshold: int,
    depth: int,
) -> None:
    """Fill prefix_children: per prefix, each per-word 1-char child + its count.

    Children are WORD children (spec §4.2 / §3.4), not whole-norm characters:
    for a stored prefix of length < ``depth``, the children are the distinct
    2-char word prefixes ``prefix || c`` (c a single char) that actually occur
    in ``term_prefix`` for terms reachable at ``prefix``. A term is counted for
    a child when ANY of its prefix rows starts with that child (a term is
    reachable from EVERY word it contains, not just its first).

    A child whose count exceeds ``fat_threshold`` additionally stores its
    top grandchild (2-char extension of the child) + the grandchild's term
    count, so Block B can render the skip-level without a scan (YC addition
    2026-09-18); ordinary children carry NULL grandchild fields. The queries
    run over the indexed ``term_prefix(prefix)`` — offline build-time only.
    """
    conn.execute("DELETE FROM prefix_children")
    conn.execute(
        """
        WITH parent_terms AS (
          -- Terms reachable at each stored prefix of length 1..depth-1.
          SELECT DISTINCT tp.prefix AS prefix, tp.term_id AS term_id
          FROM term_prefix tp
          WHERE length(tp.prefix) BETWEEN 1 AND ?
        ),
        child_sets AS (
          -- Per-word children (spec §3.4/§4.2): for a parent prefix, the
          -- child is prefix || c where c is one char and the child occurs as
          -- a word prefix of ANY term reachable at the parent — including a
          -- word the parent is not itself a prefix of (that is what makes a
          -- term reachable from EVERY word it contains, not just its first).
          SELECT DISTINCT pt.prefix AS prefix,
                         ct.prefix AS child,
                         pt.term_id AS term_id
          FROM parent_terms pt
          JOIN term_prefix ct
            ON ct.term_id = pt.term_id
           AND ct.prefix LIKE pt.prefix || '%'
          WHERE length(ct.prefix) = length(pt.prefix) + 1
        ),
        child_counts AS (
          SELECT prefix, child, COUNT(DISTINCT term_id) AS n
          FROM child_sets GROUP BY prefix, child
        ),
        gc_sets AS (
          -- Grandchildren of a child: the 1-char extensions of the child that
          -- occur as word prefixes among the child's own terms.
          SELECT DISTINCT cs.prefix AS prefix,
                         cs.child AS child,
                         ct.prefix AS gchild,
                         cs.term_id AS term_id
          FROM child_sets cs
          JOIN term_prefix ct
            ON ct.term_id = cs.term_id
           AND ct.prefix LIKE cs.child || '%'
          WHERE length(ct.prefix) = length(cs.child) + 1
        ),
        gc_counts AS (
          SELECT prefix, child, gchild, COUNT(DISTINCT term_id) AS n
          FROM gc_sets GROUP BY prefix, child, gchild
        ),
        top_gc AS (
          -- Top grandchild per child (count desc, gchild asc for ties), only
          -- for FAT children (n > threshold); ordinary children stay NULL.
          SELECT gc.prefix AS prefix, gc.child AS child,
                 gc.gchild AS gchild, gc.n AS n,
                 ROW_NUMBER() OVER (
                   PARTITION BY gc.prefix, gc.child
                   ORDER BY gc.n DESC, gc.gchild ASC
                 ) AS rn
          FROM gc_counts gc
          JOIN child_counts cc
            ON cc.prefix = gc.prefix AND cc.child = gc.child
          WHERE cc.n > ?
        )
        INSERT INTO prefix_children (prefix, child, n_terms, grandchild, gc_n_terms)
        SELECT cc.prefix, cc.child, cc.n, tg.gchild, tg.n
        FROM child_counts cc
        LEFT JOIN top_gc tg
          ON tg.prefix = cc.prefix AND tg.child = cc.child AND tg.rn = 1
        """,
        (depth, fat_threshold),
    )


def build_index(
    rows: list[dict[str, Any]],
    target_path: Path,
    *,
    settings: Optional[Settings] = None,
) -> str:
    """Build the full index into ``target_path``; return the new build_id.

    ``rows`` are report-shaped dicts (keys per :data:`_ROW_*`). The target
    file is created fresh (parent dirs included); the caller is responsible
    for staging + atomic swap (see crawl/crawl_db.py).
    """
    cfg = settings or _settings
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists():
        target_path.unlink()
    conn = sqlite3.connect(target_path)
    try:
        init_db(conn)
        crawled_at = _now_iso()
        _insert_reports(conn, rows, crawled_at=crawled_at)
        terms = extract_terms(
            [
                {
                    "title": row.get(_ROW_TITLE),
                    "reportType": row.get(_ROW_TYPE),
                    "mpNames": row.get(_ROW_SPEAKER),
                    "sittingDate": row.get(_ROW_SEEN),
                }
                for row in rows
            ]
        )
        term_ids = _insert_terms(conn, terms)
        _insert_prefixes(conn, terms, term_ids, depth=cfg.ladder_depth)
        _materialise_children(conn, fat_threshold=cfg.fat_branch_threshold,
                              depth=cfg.ladder_depth)
        _log_over_cap_prefixes(conn, cap=cfg.ladder_term_cap, depth=cfg.ladder_depth)
        build_id = _build_id()
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                     (BUILD_ID_KEY, build_id))
        conn.commit()
        logger.info(
            "index built: %d reports, %d terms -> %s (build_id=%s)",
            len(rows), len(terms), target_path, build_id,
        )
        return build_id
    finally:
        conn.close()


def _log_over_cap_prefixes(
    conn: sqlite3.Connection, *, cap: int, depth: int
) -> None:
    """Log (not silently skip) any max-depth prefix exceeding the term cap."""
    cur = conn.execute(
        "SELECT prefix, COUNT(*) AS n FROM term_prefix "
        f"WHERE length(prefix) = ? GROUP BY prefix HAVING n > ? ORDER BY n DESC",
        (depth, cap),
    )
    for prefix, n in cur.fetchall():
        logger.warning(
            "prefix at depth %d exceeds term cap: %r holds %d terms (> %d)",
            depth, prefix, n, cap,
        )
