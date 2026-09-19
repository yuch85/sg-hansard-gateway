"""DDL for the term index (spec §3.2, extended for the skip-level).

Metadata-only by invariant: the ``report`` table carries NO body/transcript
column — verbatim transcripts stay live-retrieved from SPRS (spec §3).

The ``prefix_children`` table extends the spec's (prefix, child, n_terms)
shape with a nullable ``grandchild`` column (and its count) so a fat branch
(child holding more than ``fat_branch_threshold`` terms) can render its
grandchildren as skip-levels without a scan (skip-level addition).
"""

from __future__ import annotations

#: The index build stamp — the cache-invalidation key for the request path.
BUILD_ID_KEY = "build_id"

#: DDL statements, in creation order. Idempotent (IF NOT EXISTS).
SCHEMA_STATEMENTS: tuple[str, ...] = (
    # One row per crawled sitting TOC entry.
    """
    CREATE TABLE IF NOT EXISTS report (
      report_id    TEXT PRIMARY KEY,
      link_id      TEXT NOT NULL,
      sitting_date TEXT NOT NULL,
      title        TEXT NOT NULL,
      report_type  TEXT,
      speaker      TEXT,
      crawled_at   TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS report_by_date ON report(sitting_date)",
    # One row per distinct searchable phrase.
    """
    CREATE TABLE IF NOT EXISTS term (
      term_id    INTEGER PRIMARY KEY,
      surface    TEXT NOT NULL UNIQUE,
      norm       TEXT NOT NULL,
      kind       TEXT NOT NULL,
      doc_count  INTEGER NOT NULL,
      first_date TEXT,
      last_date  TEXT
    )
    """,
    # A term is reachable from every non-stopword word it contains,
    # at prefix lengths 1..ladder_depth (spec §3.4 cross-word prefixing).
    """
    CREATE TABLE IF NOT EXISTS term_prefix (
      prefix  TEXT NOT NULL,
      term_id INTEGER NOT NULL REFERENCES term(term_id),
      PRIMARY KEY (prefix, term_id)
    ) WITHOUT ROWID
    """,
    "CREATE INDEX IF NOT EXISTS term_prefix_by_prefix ON term_prefix(prefix)",
    # Materialised child fan-out per prefix; grandchild columns hold the
    # skip-level for fat branches only (NULL for ordinary children).
    """
    CREATE TABLE IF NOT EXISTS prefix_children (
      prefix      TEXT NOT NULL,
      child       TEXT NOT NULL,
      n_terms     INTEGER NOT NULL,
      grandchild  TEXT,
      gc_n_terms  INTEGER,
      PRIMARY KEY (prefix, child)
    ) WITHOUT ROWID
    """,
    # Crawl bookkeeping: which sitting dates have been fetched and when.
    """
    CREATE TABLE IF NOT EXISTS crawl_state (
      sitting_date TEXT PRIMARY KEY,
      had_reports  INTEGER NOT NULL,
      crawled_at   TEXT NOT NULL
    )
    """,
    # Build metadata (build_id drives in-process cache invalidation).
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
)


def init_db(conn) -> None:
    """Create all index tables on an open connection (idempotent)."""
    conn.executescript(";\n".join(SCHEMA_STATEMENTS))
    conn.commit()
