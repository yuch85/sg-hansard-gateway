"""In-process read-only index service with build-id-keyed query caching.

:class:`IndexService` opens the local term index read-only and serves the
ladder/facet queries (spec §4). Two invariants (the live-retrieval property,
spec §3 / prohibition 2):

* It NEVER raises at construction and NEVER blocks app boot — a missing or
  unreadable index file degrades to an EMPTY state (build_id None, all
  queries return empty lists). The ladder simply shows nothing.
* It caches QUERY RESULTS (token-independent, per RESEARCH anti-pattern), not
  rendered HTML, keyed on (query_name, args). The cache is invalidated when
  ``build_id`` changes (an index swap), so a fresh crawl is served without a
  process restart.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Optional

from hansard_gateway.config import Settings, settings as _settings
from hansard_gateway.index import queries
from hansard_gateway.index.schema import BUILD_ID_KEY

logger = logging.getLogger(__name__)

#: Upper bound on cached query-result entries (bounded dict; the cache is
#: invalidated wholesale on build_id change, so TTL is not the policy).
_CACHE_MAXSIZE = 512

#: SQLite open mode for the read-only connection (URI form).
_RO_MODE = "ro"


class IndexService:
    """Read-only term index with build-id-keyed in-process query caching."""

    def __init__(
        self,
        *,
        path: Path,
        settings: Optional[Settings] = None,
    ) -> None:
        self._path = path
        self._settings = settings or _settings
        self._conn: Optional[sqlite3.Connection] = None
        self._build_id: Optional[str] = None
        self._cache: dict[tuple, Any] = {}
        self._open()

    def _open(self) -> None:
        """Open the index read-only; degrade to empty state on any failure.

        Never raises (the live-retrieval invariant): a missing/unreadable/corrupt
        file leaves the service in the empty state, and the app still boots and
        still serves transcript routes.
        """
        try:
            if not self._path.exists():
                logger.info("index absent at %s — serving empty state", self._path)
                return
            conn = sqlite3.connect(
                f"file:{self._path}?mode={_RO_MODE}", uri=True,
                check_same_thread=False,
            )
            # A corrupt/non-SQLite file raises here; treat as empty.
            conn.execute("SELECT count(*) FROM sqlite_master")
            self._conn = conn
            self._build_id = self._read_build_id()
            logger.info(
                "index opened read-only: %s build_id=%s",
                self._path,
                self._build_id,
            )
        except sqlite3.Error as exc:
            logger.warning(
                "index unreadable at %s (%s) — serving empty state",
                self._path,
                exc,
            )
            self._conn = None
            self._build_id = None

    def _read_build_id(self) -> Optional[str]:
        """Read meta.build_id from the open connection (None when absent)."""
        assert self._conn is not None
        cur = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (BUILD_ID_KEY,)
        )
        row = cur.fetchone()
        return row[0] if row else None

    @property
    def build_id(self) -> Optional[str]:
        """The current index build stamp (None in the empty state)."""
        return self._build_id

    @property
    def is_empty(self) -> bool:
        """True when the index file is absent/unreadable (empty state)."""
        return self._conn is None

    def refresh(self) -> None:
        """Force a swap check now (re-read the on-disk build_id).

        Queries do this lazily; expose it so callers (and tests) can detect a
        swap without issuing a query.
        """
        self._maybe_reopen()

    def _maybe_reopen(self) -> None:
        """Re-open the index if the on-disk build_id changed (swap detection).

        The file's build_id is re-read cheaply on each call; when it differs
        from the cached one the connection is re-opened and the query cache
        cleared. A file that disappeared since boot degrades to empty state.
        """
        try:
            if not self._path.exists():
                if self._conn is not None:
                    self._close_conn()
                    self._build_id = None
                    self._cache.clear()
                return
            probe = sqlite3.connect(
                f"file:{self._path}?mode={_RO_MODE}", uri=True,
                check_same_thread=False,
            )
            try:
                cur = probe.execute(
                    "SELECT value FROM meta WHERE key = ?", (BUILD_ID_KEY,)
                )
                row = cur.fetchone()
            finally:
                probe.close()
            disk_build_id = row[0] if row else None
        except sqlite3.Error:
            return
        if disk_build_id != self._build_id:
            self._close_conn()
            self._build_id = None
            self._cache.clear()
            self._open()

    def _close_conn(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def close(self) -> None:
        """Release the read-only connection (idempotent)."""
        self._close_conn()
        self._build_id = None
        self._cache.clear()

    def _query(
        self, name: str, fn, *args: Any, default: Any
    ) -> Any:
        """Run ``fn(*args)`` on the live conn, caching by (name, args).

        On the empty state (no conn) returns ``default`` without touching
        SQLite. The build_id is checked first so a swap invalidates the cache.
        """
        if self._conn is None:
            return default
        self._maybe_reopen()
        if self._conn is None:
            return default
        key = (name, args)
        if key in self._cache:
            return self._cache[key]
        result = fn(self._conn, *args)
        if len(self._cache) >= _CACHE_MAXSIZE:
            self._cache.clear()
        self._cache[key] = result
        return result

    # --- Public query surface (delegates to queries.py) ---

    def terms_for_prefix(self, prefix: str) -> list:
        """Terms reachable from ``prefix`` (spec §4.2 Block A data)."""
        return self._query(
            "terms_for_prefix",
            queries.terms_for_prefix,
            prefix,
            default=[],
        )

    def children_for_prefix(self, prefix: str) -> list:
        """Child fan-out for ``prefix`` (spec §4.2 Block B data)."""
        return self._query(
            "children_for_prefix",
            queries.children_for_prefix,
            prefix,
            default=[],
        )

    def letter_counts(self) -> dict:
        """The 36 single-char counts (launcher Find-a-topic block)."""
        return self._query("letter_counts", queries.letter_counts, default={})

    def common_terms(self, n: int) -> list:
        """Top-``n`` terms by doc_count (launcher Common topics)."""
        return self._query("common_terms", queries.common_terms, n, default=[])

    def recent_sittings(self, n: int) -> list:
        """The last ``n`` sitting dates (launcher Recent sittings)."""
        return self._query(
            "recent_sittings", queries.recent_sittings, n, default=[]
        )

    def years(self) -> list:
        """(year, n_sittings) pairs (facet: /years)."""
        return self._query("years", queries.years, default=[])

    def sittings_in_year(self, year: str) -> list:
        """Sitting dates in a year (facet: /year/{yyyy})."""
        return self._query(
            "sittings_in_year", queries.sittings_in_year, year, default=[]
        )

    def sittings_around(self, day: str) -> tuple[Optional[str], Optional[str]]:
        """(previous, next) sitting dates relative to ``day`` (spec §4.4)."""
        return self._query(
            "sittings_around", queries.sittings_around, day,
            default=(None, None),
        )

    def reports_for_sitting(self, day: str) -> list:
        """(link_id, title) rows for one sitting, in index order."""
        return self._query(
            "reports_for_sitting", queries.reports_for_sitting, day, default=[]
        )

    def terms_by_norm(self) -> list:
        """All terms as (surface, norm, doc_count) — related-terms source."""
        return self._query("terms_by_norm", queries.terms_by_norm, default=[])

    def term_by_norm(self, norm: str) -> Optional[tuple[str, int]]:
        """(surface, doc_count) for the indexed term with norm ``norm`` (F-4)."""
        return self._query(
            "term_by_norm", queries.term_by_norm, norm, default=None
        )

    def reports_for_norm(self, norm: str) -> list:
        """(link_id, title, date, report_type, speaker) rows for term ``norm`` (F-4)."""
        return self._query(
            "reports_for_norm", queries.reports_for_norm, norm, default=[]
        )

    def members_letter(self, letter: str) -> list:
        """Speakers starting with ``letter`` (facet: /members/{letter})."""
        return self._query(
            "members_letter", queries.members_letter, letter, default=[]
        )

    def bills_letter(self, letter: str) -> list:
        """Bills starting with ``letter`` (facet: /bills/{letter})."""
        return self._query("bills_letter", queries.bills_letter, letter, default=[])
