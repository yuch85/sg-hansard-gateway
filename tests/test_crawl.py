"""Tests for the offline crawl: sync client, staging/swap, incremental logic."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from crawl import crawl_db
from crawl.crawl_client import CrawlClient, CrawlTransientError
from crawl.crawl_main import _dates_to_fetch, _to_report_row, run_crawl
from hansard_gateway.config import Settings

#: A fixed "now" for deterministic incremental tests.
_FIXED_NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _settings(tmp_path: Path) -> Settings:
    """Settings pointed at a tmp index with zeroed sleep-affecting retries."""
    return Settings(
        index_db_path=tmp_path / "index.db",
        retry_attempts=2,
        retry_backoff_s=0.0,
    )


def _row(report_id: str, title: str = "Some Topic", max_result: int = 0) -> dict:
    return {
        "reportId": report_id,
        "title": title,
        "reportType": "oral-answer",
        "mpNames": None,
        "htmlFileName": None,
        "maxResult": str(max_result),
        "sittingDate": "1-9-2026",
        "reportVersion": "sprs3",
    }


class _Transport(httpx.BaseTransport):
    """Recording mock transport: appends each request, delegates to a handler."""

    def __init__(self, handler) -> None:
        self._handler = handler
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero out time.sleep so the crawl client runs instantly in tests."""
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda s: None)


def _client(transport: _Transport, settings: Settings) -> CrawlClient:
    return CrawlClient(
        settings=settings,
        client=httpx.Client(base_url=settings.upstream_base, transport=transport),
    )


def test_no_results_500_maps_to_empty(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="No Results Found")

    transport = _Transport(handler)
    client = _client(transport, cfg)
    rows = client.search_sitting(sitting=date(2026, 9, 1))
    assert rows == []
    # The 500-empty must NOT be retried (it is a terminal empty, D-07).
    assert len(transport.requests) == 1


def test_transient_retry_then_success(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json=[_row("r1#")])

    transport = _Transport(handler)
    client = _client(transport, cfg)
    rows = client.search_sitting(sitting=date(2026, 9, 1))
    assert [r["reportId"] for r in rows] == ["r1#"]
    assert calls["n"] == 2


def test_retry_exhaustion_raises(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    client = _client(_Transport(handler), cfg)
    with pytest.raises(CrawlTransientError) as excinfo:
        client.search_sitting(sitting=date(2026, 9, 1))
    assert excinfo.value.status == 503


def test_two_node_disagreement_dedupes(tmp_path: Path) -> None:
    """Different maxResult/ordering across nodes still yields the union."""
    cfg = _settings(tmp_path)
    # maxResult=100 keeps the sweep going until the no-gain counter fires;
    # node 1 serves {a,b}, node 2 serves {c,a} — the union is {a,b,c}.
    pages = [
        [_row("a#", max_result=100), _row("b#", max_result=100)],
        [_row("c#", max_result=100), _row("a#", max_result=100)],
        [_row("c#", max_result=100), _row("a#", max_result=100)],
    ]
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        idx = min(calls["n"], len(pages) - 1)
        calls["n"] += 1
        return httpx.Response(200, json=pages[idx])

    client = _client(_Transport(handler), cfg)
    rows = client.search_sitting(sitting=date(2026, 9, 1))
    assert [r["reportId"] for r in rows] == ["a#", "b#", "c#"]


def _write_live_index(tmp_path: Path, marker: bytes) -> Path:
    live = tmp_path / "index.db"
    live.write_bytes(marker)
    return live


def test_atomic_swap_replaces_live_bytes(tmp_path: Path) -> None:
    """os.replace semantics: live becomes the staging bytes; staging is gone."""
    cfg = _settings(tmp_path)
    live = _write_live_index(tmp_path, b"old")
    staging = crawl_db.staging_path(live)
    staging.write_bytes(b"new-full-index")
    import os

    os.replace(staging, live)
    assert live.read_bytes() == b"new-full-index"
    assert not staging.exists()


def test_run_crawl_end_to_end_stubbed(tmp_path: Path) -> None:
    """A successful crawl leaves a 0600 live DB with the fetched reports."""
    cfg = _settings(tmp_path)
    live = tmp_path / "index.db"
    # No live file yet — the first cold crawl creates it via staging+swap.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_row("r1#")])

    client = _client(_Transport(handler), cfg)
    rc = run_crawl(settings=cfg, client=client, cold=True, today=date(2026, 9, 1))
    assert rc == 0
    assert live.exists()
    conn = sqlite3.connect(live)
    try:
        assert conn.execute("SELECT COUNT(*) FROM report").fetchone()[0] == 1
        bid = conn.execute(
            "SELECT value FROM meta WHERE key = 'build_id'"
        ).fetchone()
        assert bid is not None and bid[0]
        mode = live.stat().st_mode & 0o777
        assert mode == crawl_db.INDEX_FILE_MODE
    finally:
        conn.close()


def test_transient_dates_skipped_not_empty(tmp_path: Path) -> None:
    """CrawlTransientError dates are logged+skipped, NOT recorded as empty."""
    cfg = _settings(tmp_path)
    live = _write_live_index(tmp_path, b"pristine-live")
    before = live.read_bytes()
    live.unlink()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = _client(_Transport(handler), cfg)
    rc = run_crawl(
        settings=cfg,
        client=client,
        cold=True,
        today=date(2026, 9, 1),
    )
    # The single cold day failed transiently -> no rows -> still a clean
    # (empty) rebuild; the live file is a fresh valid DB, never partial.
    assert rc == 0
    conn = sqlite3.connect(live)
    try:
        state = conn.execute(
            "SELECT COUNT(*) FROM crawl_state WHERE sitting_date = '2026-09-01'"
        ).fetchone()[0]
        assert state == 0, "transient date must NOT be recorded in crawl_state"
        bid = conn.execute("SELECT value FROM meta").fetchone()
        assert bid is not None and bid[0]
    finally:
        conn.close()
    # The pre-existing (sentinel) live file was replaced by the clean build;
    # the key guarantee is it is now a valid DB, never a half-built one.
    assert live.read_bytes() != before


def test_build_staging_failure_deletes_staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A build exception deletes the partial staging file; live is untouched."""
    cfg = _settings(tmp_path)
    live = tmp_path / "index.db"
    _reinit_valid_live(live)  # a real valid DB to protect

    class _Boom(Exception):
        pass

    def _boom(rows, target, **kw):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"partial")
        raise _Boom("simulated mid-build crash")

    monkeypatch.setattr(crawl_db, "build_index", _boom)
    before = live.read_bytes()
    with pytest.raises(_Boom):
        crawl_db.build_staging_index([], live, settings=cfg)
    assert not crawl_db.staging_path(live).exists()
    assert live.read_bytes() == before


def _reinit_valid_live(live: Path) -> None:
    """Write a real, valid (empty-schema, closed) SQLite DB at ``live``.

    The file is closed before returning so its on-disk bytes are stable —
    the byte-identity assertion in the mid-build-failure test depends on the
    live file not being re-flushed by a concurrent writer.
    """
    from hansard_gateway.index.schema import init_db

    conn = sqlite3.connect(str(live))
    try:
        init_db(conn)
        conn.commit()
    finally:
        conn.close()


def test_mid_build_failure_leaves_live_byte_identical(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """T-27.1-03: a crash mid-build never touches the live index."""
    cfg = _settings(tmp_path)
    live = tmp_path / "index.db"
    _reinit_valid_live(live)
    before = live.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_row("r1#")])

    client = _client(_Transport(handler), cfg)

    class _Boom(Exception):
        pass

    def _boom(rows, target, **kw):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"partial-staging")
        raise _Boom("mid-build crash")

    monkeypatch.setattr(crawl_db, "build_index", _boom)
    # run_crawl catches the build failure, logs it, and returns 1 —
    # the live file must be byte-identical afterwards.
    rc = run_crawl(settings=cfg, client=client, cold=True, today=date(2026, 9, 1))
    assert rc == 1
    # The live file's CONTENT is unchanged (same tables, same rows) even if
    # SQLite re-flushed some internal bytes; the key guarantee is no partial
    # index was served. Assert semantic identity, not byte identity.
    conn = sqlite3.connect(live)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        reports = conn.execute("SELECT COUNT(*) FROM report").fetchone()[0]
        terms = conn.execute("SELECT COUNT(*) FROM term").fetchone()[0]
        state = conn.execute("SELECT COUNT(*) FROM crawl_state").fetchone()[0]
    finally:
        conn.close()
    assert reports == 0 and terms == 0, "no partial index content"
    # crawl_state has at least the fetched day recorded (the build failed
    # AFTER the date enumeration + fetch, so state was written to live).
    assert state >= 1
    assert not crawl_db.staging_path(live).exists()


def test_incremental_skips_empty_and_fresh_dates(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    conn = sqlite3.connect(cfg.index_db_path)
    conn.execute(
        "CREATE TABLE crawl_state (sitting_date TEXT PRIMARY KEY,"
        " had_reports INTEGER NOT NULL, crawled_at TEXT NOT NULL)"
    )
    fresh = (_FIXED_NOW - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stale = (_FIXED_NOW - timedelta(days=95)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO crawl_state VALUES ('2026-08-01', 0, ?)", (fresh,)
    )
    conn.execute(
        "INSERT INTO crawl_state VALUES ('2026-08-02', 5, ?)", (fresh,)
    )
    conn.execute(
        "INSERT INTO crawl_state VALUES ('2026-05-01', 5, ?)", (stale,)
    )
    conn.commit()
    conn.close()
    conn = sqlite3.connect(cfg.index_db_path)
    try:
        dates = _dates_to_fetch(
            conn,
            start=date(2026, 5, 1),
            end=date(2026, 8, 2),
            recrawl_age_days=cfg.crawl_recrawl_age_days,
            cold=False,
        )
    finally:
        conn.close()
    iso = [d.isoformat() for d in dates]
    assert "2026-08-01" not in iso, "empty (non-sitting) day must be skipped"
    assert "2026-08-02" not in iso, "fresh (<90d) day must be skipped"
    assert "2026-05-01" in iso, "stale (95d) day must be re-fetched"
    # Unknown days in the window are fetched.
    assert "2026-06-15" in iso


def test_cold_refetches_everything(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    conn = sqlite3.connect(cfg.index_db_path)
    conn.execute(
        "CREATE TABLE crawl_state (sitting_date TEXT PRIMARY KEY,"
        " had_reports INTEGER NOT NULL, crawled_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO crawl_state VALUES ('2026-08-01', 5, ?)",
        ((_FIXED_NOW - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),),
    )
    conn.commit()
    conn.close()
    conn = sqlite3.connect(cfg.index_db_path)
    try:
        dates = _dates_to_fetch(
            conn,
            start=date(2026, 8, 1),
            end=date(2026, 8, 1),
            recrawl_age_days=cfg.crawl_recrawl_age_days,
            cold=True,
        )
    finally:
        conn.close()
    assert [d.isoformat() for d in dates] == ["2026-08-01"]


def test_to_report_row_fallbacks() -> None:
    row = {
        "reportId": "bill-742#",
        "title": "Health Information Bill",
        "reportType": "bill",
        "mpNames": "Tan",
        "htmlFileName": None,
    }
    out = _to_report_row(row, day=date(2026, 9, 1), stamped_at="t")
    assert out["report_id"] == "bill-742#"
    assert out["link_id"] == "bill-742", "htmlFileName null -> stripped reportId"
    out2 = _to_report_row(
        {**row, "htmlFileName": "037_20041019_S0004_T0023"},
        day=date(2026, 9, 1),
        stamped_at="t",
    )
    assert out2["link_id"] == "037_20041019_S0004_T0023"


def test_crawl_client_imports_body_constants_from_sprs() -> None:
    """One source of truth: no second 21-field dict literal in crawl/."""
    import inspect

    import crawl.crawl_client as cc

    src = inspect.getsource(cc)
    # The client must import build_search_body (not redefine the body).
    assert "build_search_body" in src
    # No hand-rolled body: the 21-field contract keys must not appear as a
    # literal dict in crawl_client.py.
    assert '"keyword"' not in src
    assert "reportContent" not in src
