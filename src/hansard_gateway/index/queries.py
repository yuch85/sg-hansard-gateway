"""SQL query functions over the term index (spec §4 routes' data source).

Every function takes an open read-only ``sqlite3.Connection`` and is
index-backed: the hot path (``terms_for_prefix`` / ``children_for_prefix``)
hits the ``term_prefix_by_prefix`` index, never a full scan. These are the
raw-data queries; :mod:`~hansard_gateway.index.loader.IndexService` wraps them
with build-id cache invalidation and empty-state degradation.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

#: The 36 single-char "letters" for the launcher's Find-a-topic block
#: (spec §4.1): a-z and 0-9.
ALPHABET: tuple[str, ...] = (
    "a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "m",
    "n", "o", "p", "q", "r", "s", "t", "u", "v", "w", "x", "y", "z",
    "0", "1", "2", "3", "4", "5", "6", "7", "8", "9",
)


def terms_for_prefix(
    conn: sqlite3.Connection, prefix: str
) -> list[tuple[str, str, int, Optional[str], Optional[str]]]:
    """Terms reachable from ``prefix``, ordered by doc_count desc, surface asc.

    Returns (surface, kind, doc_count, first_date, last_date) rows. The
    ``search_query`` for a finished link is the surface (the caller builds the
    abs_search_url).
    """
    cur = conn.execute(
        "SELECT t.surface, t.kind, t.doc_count, t.first_date, t.last_date "
        "FROM term_prefix tp JOIN term t ON t.term_id = tp.term_id "
        "WHERE tp.prefix = ? "
        "ORDER BY t.doc_count DESC, t.surface ASC",
        (prefix,),
    )
    return [
        (row[0], row[1], row[2], row[3], row[4]) for row in cur.fetchall()
    ]


def children_for_prefix(
    conn: sqlite3.Connection, prefix: str
) -> list[tuple[str, int, Optional[str], Optional[int]]]:
    """Child fan-out for ``prefix``: (child, n_terms, grandchild, gc_n_terms).

    Grandchild fields are NULL for ordinary children and populated only for
    fat branches (skip-levels, YC addition 2026-09-18).
    """
    cur = conn.execute(
        "SELECT child, n_terms, grandchild, gc_n_terms "
        "FROM prefix_children WHERE prefix = ? AND n_terms >= 1 "
        "ORDER BY n_terms DESC, child ASC",
        (prefix,),
    )
    return [
        (row[0], row[1], row[2], row[3]) for row in cur.fetchall()
    ]


def letter_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """The 36 single-char term counts for the launcher (spec §4.1).

    Returns a dict keyed by every letter in :data:`ALPHABET` (0 when absent)
    so the page renders a stable a-z, 0-9 grid.
    """
    cur = conn.execute(
        "SELECT substr(norm, 1, 1) AS c, COUNT(*) AS n "
        "FROM (SELECT DISTINCT norm FROM term) "
        "GROUP BY c"
    )
    counts = {letter: 0 for letter in ALPHABET}
    for c, n in cur.fetchall():
        if c in counts:
            counts[c] = n
    return counts


def common_terms(conn: sqlite3.Connection, n: int) -> list[tuple[str, int]]:
    """Top-``n`` terms by doc_count: (surface, doc_count)."""
    cur = conn.execute(
        "SELECT surface, doc_count FROM term ORDER BY doc_count DESC, surface ASC LIMIT ?",
        (n,),
    )
    return [(row[0], row[1]) for row in cur.fetchall()]


def recent_sittings(conn: sqlite3.Connection, n: int) -> list[str]:
    """The last ``n`` distinct sitting dates, most recent first."""
    cur = conn.execute(
        "SELECT DISTINCT sitting_date FROM report "
        "ORDER BY sitting_date DESC LIMIT ?",
        (n,),
    )
    return [row[0] for row in cur.fetchall()]


def years(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """(year, n_sittings) pairs, most recent year first."""
    cur = conn.execute(
        "SELECT substr(sitting_date, 1, 4) AS y, "
        "COUNT(DISTINCT sitting_date) AS n "
        "FROM report GROUP BY y ORDER BY y DESC"
    )
    return [(row[0], row[1]) for row in cur.fetchall()]


def sittings_in_year(conn: sqlite3.Connection, year: str) -> list[str]:
    """Distinct sitting dates in a year, most recent first."""
    cur = conn.execute(
        "SELECT DISTINCT sitting_date FROM report "
        "WHERE substr(sitting_date, 1, 4) = ? ORDER BY sitting_date DESC",
        (year,),
    )
    return [row[0] for row in cur.fetchall()]


def sittings_around(conn: sqlite3.Connection, day: str) -> tuple[Optional[str], Optional[str]]:
    """(previous, next) distinct sitting dates relative to ``day``.

    Powers the /date prev-next-sitting links (spec §4.4) from the index's
    sitting sequence; either side is None at the corpus edges.
    """
    prev = conn.execute(
        "SELECT MAX(sitting_date) FROM report WHERE sitting_date < ?",
        (day,),
    ).fetchone()
    nxt = conn.execute(
        "SELECT MIN(sitting_date) FROM report WHERE sitting_date > ?",
        (day,),
    ).fetchone()
    return (prev[0] if prev else None, nxt[0] if nxt else None)


def reports_for_sitting(
    conn: sqlite3.Connection, day: str
) -> list[tuple[str, str]]:
    """(link_id, title) for one sitting, in index order (report_id)."""
    cur = conn.execute(
        "SELECT link_id, title FROM report "
        "WHERE sitting_date = ? ORDER BY report_id",
        (day,),
    )
    return [(row[0], row[1]) for row in cur.fetchall()]


def reports_for_norm(
    conn: sqlite3.Connection, norm: str
) -> list[tuple[str, str, str, Optional[str], Optional[str]]]:
    """The reports indexed under the term with normalised form ``norm``.

    Returns (link_id, title, sitting_date, report_type, speaker) rows in
    index (report_id) order. The metadata join for the exact-term search
    boost (F-4): the report table is metadata-only, so no transcript is
    fetched — the gateway just pins the surfaced reports to the top of the
    rendered result page.
    """
    cur = conn.execute(
        "SELECT r.link_id, r.title, r.sitting_date, r.report_type, r.speaker "
        "FROM report r JOIN term t ON r.title = t.surface "
        "WHERE t.norm = ? ORDER BY r.report_id",
        (norm,),
    )
    return [
        (row[0], row[1], row[2], row[3], row[4]) for row in cur.fetchall()
    ]


def term_by_norm(conn: sqlite3.Connection, norm: str) -> Optional[tuple[str, int]]:
    """(surface, doc_count) for the single term with normalised form ``norm``.

    None when ``norm`` is not an indexed term. F-4: the exact-term check on
    the search path (one indexed lookup, cache-backed by the loader).
    """
    cur = conn.execute(
        "SELECT surface, doc_count FROM term WHERE norm = ?", (norm,)
    )
    row = cur.fetchone()
    return (row[0], row[1]) if row else None


def terms_by_norm(conn: sqlite3.Connection) -> list[tuple[str, str, int]]:
    """All terms as (surface, norm, doc_count) rows — small, cached.

    Source for related-term matching (spec §4.4); the match is computed in
    Python (word overlap on the normalised forms) so no LIKE/scan per request.
    """
    cur = conn.execute(
        "SELECT surface, norm, doc_count FROM term "
        "ORDER BY doc_count DESC, norm ASC"
    )
    return [(row[0], row[1], row[2]) for row in cur.fetchall()]


def members_letter(conn: sqlite3.Connection, letter: str) -> list[tuple[str, int]]:
    """Distinct speaker terms starting with ``letter``: (surface, doc_count)."""
    cur = conn.execute(
        "SELECT surface, doc_count FROM term "
        "WHERE kind = 'speaker' AND lower(substr(surface, 1, 1)) = lower(?) "
        "ORDER BY doc_count DESC, surface ASC",
        (letter,),
    )
    return [(row[0], row[1]) for row in cur.fetchall()]


def bills_letter(conn: sqlite3.Connection, letter: str) -> list[tuple[str, int]]:
    """Distinct bill terms starting with ``letter``: (surface, doc_count)."""
    cur = conn.execute(
        "SELECT surface, doc_count FROM term "
        "WHERE kind = 'bill' AND lower(substr(surface, 1, 1)) = lower(?) "
        "ORDER BY doc_count DESC, surface ASC",
        (letter,),
    )
    return [(row[0], row[1]) for row in cur.fetchall()]
