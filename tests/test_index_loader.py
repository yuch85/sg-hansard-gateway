"""Tests for IndexService: empty-state, build-id cache invalidation, wiring."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hansard_gateway.index.build import build_index
from hansard_gateway.index.loader import IndexService
from hansard_gateway.index.schema import BUILD_ID_KEY


def _rows() -> list[dict]:
    """A few report rows (metadata-only) for building a tmp index."""
    return [
        {"report_id": "b1", "link_id": "b1", "sitting_date": "2023-05-10",
         "title": "Health Information Bill", "report_type": "bill",
         "speaker": None},
        {"report_id": "t1", "link_id": "t1", "sitting_date": "2025-01-08",
         "title": "Data Protection and Cybersecurity", "report_type": "oral-answer",
         "speaker": "Tan Chun Seng"},
    ]


def test_missing_path_empty_state(tmp_path: Path) -> None:
    """Invariant: an absent index file degrades to empty state, never raises."""
    svc = IndexService(path=tmp_path / "nope.db")
    try:
        assert svc.is_empty
        assert svc.build_id is None
        assert svc.terms_for_prefix("heal") == []
        assert svc.children_for_prefix("h") == []
        assert svc.letter_counts() == {}
        assert svc.common_terms(10) == []
        assert svc.recent_sittings(10) == []
        assert svc.years() == []
        assert svc.sittings_in_year("2025") == []
        assert svc.members_letter("t") == []
        assert svc.bills_letter("h") == []
    finally:
        svc.close()


def test_serves_queries(tmp_path: Path) -> None:
    target = tmp_path / "index.db"
    build_index(_rows(), target)
    svc = IndexService(path=target)
    try:
        assert not svc.is_empty
        assert svc.build_id is not None
        terms = svc.terms_for_prefix("heal")
        assert any(t[0] == "Health Information Bill" for t in terms)
        counts = svc.letter_counts()
        assert counts.get("h", 0) >= 1
        assert svc.common_terms(5)
        assert svc.recent_sittings(5) == ["2025-01-08", "2023-05-10"]
    finally:
        svc.close()


def test_build_id_swap_invalidates_cache(tmp_path: Path) -> None:
    """A swap (new build_id) clears the cache; a fresh query sees new data."""
    target = tmp_path / "index.db"
    build_index(_rows(), target)
    svc = IndexService(path=target)
    try:
        first_bid = svc.build_id
        # Prime the cache with a query.
        svc.terms_for_prefix("data")
        # Simulate a swap: rebuild the DB with an extra term + new build_id.
        build_index(
            _rows()
            + [
                {"report_id": "x9", "link_id": "x9", "sitting_date": "2026-01-01",
                 "title": "Xylophone Performance Arts", "report_type": "bill",
                 "speaker": None}
            ],
            target,
        )
        svc.refresh()  # force swap detection
        assert svc.build_id != first_bid, "build_id must change after rebuild"
        # The cached query must now reflect the new index (cache invalidated).
        terms = svc.terms_for_prefix("xylo")
        assert any(t[0] == "Xylophone Performance Arts" for t in terms)
    finally:
        svc.close()


def test_cached_query_does_not_rerun_sql(tmp_path: Path) -> None:
    """A second identical query is served from cache (no SQL re-execution)."""
    target = tmp_path / "index.db"
    build_index(_rows(), target)
    svc = IndexService(path=target)
    conn = svc._conn
    assert conn is not None
    calls = {"execute": 0}
    orig_execute = conn.execute

    def counting_execute(*a, **kw):
        # Only count the data query (terms_for_prefix SQL), not the
        # build_id probe that _maybe_reopen runs each call.
        sql = a[0] if a else kw.get("sql", "")
        if isinstance(sql, str) and "term_prefix" in sql and "SELECT" in sql:
            calls["execute"] += 1
        return orig_execute(*a, **kw)

    # Wrap the connection's execute via a proxy object (Connection.execute is
    # read-only on the C type, so we swap the whole connection for a wrapper).
    class _ConnProxy:
        def __init__(self, inner) -> None:
            self._inner = inner

        def execute(self, *a, **kw):
            return counting_execute(*a, **kw)

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

    svc._conn = _ConnProxy(conn)
    try:
        svc.terms_for_prefix("data")
        after_first = calls["execute"]
        assert after_first >= 1
        svc.terms_for_prefix("data")
        after_second = calls["execute"]
        assert after_second == after_first, "second call must hit the cache"
    finally:
        svc.close()


def test_app_exposes_index_state(client_with_index) -> None:
    """create_app() exposes app.state.index; /health still works (no boot regression)."""
    app = client_with_index.app
    assert hasattr(app.state, "index")
    assert isinstance(app.state.index, IndexService)
    assert client_with_index.get("/health").json() == {"status": "ok"}


def test_report_route_works_with_index(client_with_index, token_store) -> None:
    """A protected route serves with the fixture index present (boot + auth OK)."""
    from hansard_gateway.auth import TEST_TOKEN

    # The launcher is a protected route; with a valid token + the fixture index
    # present it must return 200 (the index is wired but not yet consumed by
    # routes until wave 2 — the point is boot + auth + index coexist).
    resp = client_with_index.get(f"/a/{TEST_TOKEN}/")
    assert resp.status_code == 200
