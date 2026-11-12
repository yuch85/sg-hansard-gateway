"""Phase 27.5 plan 01: crawl loop, single-writer lock, first-run guard.

Offline only: fake clients stand in for the network, fake sleeps drive the
loop. Written RED first (T1), then the implementation lands in this phase.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from crawl import crawl_db
from crawl.crawl_loop import run_loop
from crawl.crawl_client import CrawlTransientError
from crawl.crawl_main import run_crawl
from crawl.crawl_seed import seed_state
from hansard_gateway.config import Settings
from hansard_gateway.index.build import build_index

_STAMP = "%Y-%m-%dT%H:%M:%SZ"


def _stamp(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime(_STAMP)


def _cfg(tmp_path: Path, **kw: Any) -> Settings:
    base: dict[str, Any] = dict(
        index_db_path=tmp_path / "index.db",
        crawl_start_date="2026-08-01",
        retry_attempts=1,
        retry_backoff_s=0.0,
    )
    base.update(kw)
    return Settings(**base)


def _raw(report_id: str, title: str) -> dict:
    return {
        "reportId": report_id,
        "title": title,
        "reportType": "oral-answer",
        "mpNames": None,
        "htmlFileName": None,
    }


def _idx_row(report_id: str, day: str, title: str) -> dict:
    return {
        "report_id": report_id,
        "link_id": report_id.rstrip("#"),
        "sitting_date": day,
        "title": title,
        "report_type": "oral-answer",
        "speaker": None,
    }


class FakeClient:
    """search_sitting stand-in: per-date rows, optional transient failures."""

    def __init__(self, by_date=None, fail=()):
        self.by_date = by_date or {}
        self.fail = set(fail)
        self.calls: list[str] = []

    def search_sitting(self, *, sitting: date):
        iso = sitting.isoformat()
        self.calls.append(iso)
        if iso in self.fail:
            raise CrawlTransientError(sitting=sitting, status=503, detail="down")
        return list(self.by_date.get(iso, []))

    def close(self) -> None:
        pass


class AlwaysFailClient(FakeClient):
    """Every fetch fails transiently (state must stay empty)."""

    def search_sitting(self, *, sitting: date):
        self.calls.append(sitting.isoformat())
        raise CrawlTransientError(sitting=sitting, status=503, detail="down")


def _seed(cfg: Settings, rows: list[dict], state: list[tuple[str, int, str]]) -> None:
    """Write a live index with the given reports and crawl_state rows."""
    build_index(rows, cfg.index_db_path, settings=cfg)
    conn = sqlite3.connect(cfg.index_db_path)
    try:
        conn.executemany("INSERT INTO crawl_state VALUES (?, ?, ?)", state)
        conn.commit()
    finally:
        conn.close()


def _q(cfg: Settings, sql: str) -> list[tuple]:
    conn = sqlite3.connect(cfg.index_db_path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _state(cfg: Settings) -> dict[str, int]:
    return {r[0]: r[1] for r in _q(cfg, "SELECT sitting_date, had_reports FROM crawl_state")}


def _meta(cfg: Settings, key: str) -> str | None:
    rows = _q(cfg, f"SELECT value FROM meta WHERE key = '{key}'")
    return rows[0][0] if rows else None


def _seeded_index(cfg: Settings) -> None:
    """Index with reports on two dates, empty crawl_state, build_id stamped."""
    build_index(
        [_idx_row("a1#", "2026-08-01", "Alpha"), _idx_row("a3#", "2026-08-03", "Gamma")],
        cfg.index_db_path, settings=cfg,
    )
    conn = sqlite3.connect(cfg.index_db_path)
    recent = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y%m%dT%H%M%SZ")
    conn.execute("UPDATE meta SET value = ? WHERE key='build_id'", ("abc" + recent,))
    conn.commit()
    conn.close()


# --- T1.2: the single-writer lock -----------------------------------------


def test_lock_second_run_skipped(tmp_path: Path) -> None:
    """A second writer on a locked index exits 3 and touches nothing."""
    cfg = _cfg(tmp_path)
    run_crawl(
        settings=cfg,
        client=FakeClient({"2026-08-01": [_raw("a1#", "Alpha")]}),
        today=date(2026, 8, 2),
    )
    before = cfg.index_db_path.read_bytes()
    fake = FakeClient({"2026-08-03": [_raw("n#", "New")]})
    with crawl_db.index_lock(cfg.index_db_path):
        assert run_crawl(settings=cfg, client=fake, today=date(2026, 8, 3)) == 3
    assert fake.calls == [], "a lock-skipped run makes zero upstream requests"
    assert cfg.index_db_path.read_bytes() == before


# --- T1.3/T1.4/T1.5/T1.6/T1.14/T1.15: the first-run guard ------------------


def test_first_run_guard_clamps_window(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Fresh index: the first guarded run fetches only the recent window."""
    cfg = _cfg(tmp_path)  # crawl_start_date 2026-08-01, first_run_days 31
    today = date(2026, 10, 15)
    floor = (today - timedelta(days=31)).isoformat()  # 2026-09-14
    fake = FakeClient()
    with caplog.at_level(logging.WARNING):
        assert run_crawl(settings=cfg, client=fake, today=today) == 0
    assert fake.calls, "the guarded window still crawls"
    assert min(fake.calls) == floor
    assert max(fake.calls) <= today.isoformat()
    assert _meta(cfg, "crawl_floor") == floor
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(
        floor in m and "HANSARD_CRAWL_FULL_HISTORY" in m for m in warnings
    ), "the guard warning names the window and the full-history escape"


def test_guard_floor_sticks_on_second_run(tmp_path: Path) -> None:
    """After a guarded first run, run 2 never walks dates before the floor."""
    cfg = _cfg(tmp_path)
    floor = "2026-09-14"
    run_crawl(settings=cfg, client=FakeClient(), today=date(2026, 10, 15))
    assert _meta(cfg, "crawl_floor") == floor
    second = FakeClient({"2026-10-18": [_raw("n#", "New")]})
    assert run_crawl(settings=cfg, client=second, today=date(2026, 10, 20)) == 0
    assert second.calls, "new dates are still fetched"
    assert min(second.calls) >= floor, "the floor sticks: no backlog walk"
    assert "2026-10-18" in second.calls
    assert _meta(cfg, "crawl_floor") == floor


@pytest.mark.parametrize("mode", ["full_history", "cold"])
def test_guard_off_with_full_history(tmp_path: Path, mode: str) -> None:
    """crawl_full_history / --cold fetches from crawl_start_date, clears floor."""
    cfg = _cfg(tmp_path)
    run_crawl(settings=cfg, client=FakeClient(), today=date(2026, 10, 15))
    assert _meta(cfg, "crawl_floor") == "2026-09-14"
    fake = FakeClient()
    if mode == "full_history":
        cfg = _cfg(tmp_path, crawl_full_history=True)
        assert run_crawl(settings=cfg, client=fake, today=date(2026, 10, 15)) == 0
    else:
        assert run_crawl(settings=cfg, client=fake, cold=True, today=date(2026, 10, 15)) == 0
    assert min(fake.calls) == "2026-08-01"
    assert _meta(cfg, "crawl_floor") is None, "the floor is cleared on a full-history run"


def test_guard_not_applied_when_state_present(tmp_path: Path) -> None:
    """A live index with non-empty crawl_state runs a normal incremental."""
    cfg = _cfg(tmp_path)
    _seed(cfg, [_idx_row("a1#", "2026-08-01", "Alpha")], [("2026-08-01", 1, _stamp(1))])
    fake = FakeClient()
    assert run_crawl(settings=cfg, client=fake, today=date(2026, 10, 15)) == 0
    assert min(fake.calls) == "2026-08-02", "state present: no clamp"
    assert _meta(cfg, "crawl_floor") is None, "no floor computed when state exists"


def test_stored_floor_always_wins(tmp_path: Path) -> None:
    """F1 + defect-1: the stored floor governs every run and survives swaps."""
    cfg = _cfg(tmp_path)
    floor = "2026-09-14"
    # Run 1: guarded first run, every fetch fails -> state stays EMPTY.
    assert run_crawl(settings=cfg, client=AlwaysFailClient(), today=date(2026, 10, 15)) == 0
    assert _meta(cfg, "crawl_floor") == floor
    assert _state(cfg) == {}
    # Run 2 (later today, state still empty): enumerates from the STORED
    # floor, not a recomputed one (recompute would give 2026-10-01).
    second = FakeClient()
    assert run_crawl(settings=cfg, client=second, today=date(2026, 11, 1)) == 0
    assert min(second.calls) == floor, "stored floor wins over recompute"
    assert _meta(cfg, "crawl_floor") == floor, "governing floor rewritten into staging"
    # Run 3 (state populated by run 2's successful fetches): still the
    # ORIGINAL floor, carried through run 2's swap rather than recomputed.
    third = FakeClient()
    assert run_crawl(settings=cfg, client=third, today=date(2026, 11, 3)) == 0
    assert third.calls and min(third.calls) >= floor
    assert _meta(cfg, "crawl_floor") == floor


def test_guard_not_applied_to_index_with_rows_but_no_state(tmp_path: Path) -> None:
    """Review: a pre-27.4 index (report rows, empty crawl_state) is an
    existing deployment, not a fresh one: no clamp, no floor computed."""
    cfg = _cfg(tmp_path)
    _seed(cfg, [_idx_row("a1#", "2026-08-01", "Alpha")], [])
    fake = FakeClient()
    assert run_crawl(settings=cfg, client=fake, today=date(2026, 10, 15)) == 0
    assert min(fake.calls) == "2026-08-01", "rows present: honest backfill, no guard clamp"
    assert _meta(cfg, "crawl_floor") is None


def test_distrust_path_not_guarded(tmp_path: Path) -> None:
    """F2: state claims reports, none readable -> full refetch, never guarded."""
    cfg = _cfg(tmp_path)
    _seed(cfg, [], [("2026-08-01", 1, _stamp(1)), ("2026-08-02", 0, _stamp(1))])
    fake = FakeClient()
    assert run_crawl(settings=cfg, client=fake, today=date(2026, 10, 15)) == 0
    assert min(fake.calls) == "2026-08-01", "distrust = full refetch from crawl_start_date"
    assert _meta(cfg, "crawl_floor") is None, "no floor persisted on the distrust path"


def test_run_crawl_missing_index_dir_returns_1(tmp_path: Path) -> None:
    """Review: an index path whose parent directory does not exist keeps the
    pre-27.5 contract — rc 1 with a logged error, not a traceback."""
    cfg = _cfg(tmp_path / "nonexistent-dir")
    fake = FakeClient()
    assert run_crawl(settings=cfg, client=fake, today=date(2026, 8, 2)) == 1
    assert fake.calls == []


# --- T1.12: seed-state takes the lock --------------------------------------


def test_seed_state_takes_lock(tmp_path: Path) -> None:
    """M1: --seed-state honors the single-writer lock (exit 3)."""
    cfg = _cfg(tmp_path)
    _seeded_index(cfg)
    before = cfg.index_db_path.read_bytes()
    with crawl_db.index_lock(cfg.index_db_path):
        assert seed_state(settings=cfg) == 3
    assert cfg.index_db_path.read_bytes() == before


# --- T1.7/T1.8/T1.9/T1.10/T1.16: the crawl loop ----------------------------


class _Sleeper:
    """Fake sleep: records durations; sets the stop event after N sleeps."""

    def __init__(self, stop: threading.Event, stop_after: int) -> None:
        self.sleeps: list[float] = []
        self._stop = stop
        self._stop_after = stop_after

    def __call__(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if len(self.sleeps) >= self._stop_after:
            self._stop.set()


def test_loop_ticks_immediately_then_interval() -> None:
    """The loop ticks before the first sleep, then every interval."""
    stop = threading.Event()
    sleeper = _Sleeper(stop, stop_after=2)
    ticks: list[int] = []

    def once_fn(**kw: Any) -> int:
        ticks.append(1)
        return 0

    cfg = Settings()
    assert run_loop(settings=cfg, once_fn=once_fn, sleep_fn=sleeper, stop_event=stop) == 0
    assert len(ticks) == 2, "tick before the first sleep, then after it"
    assert sleeper.sleeps == [cfg.crawl_interval_hours * 3600] * 2


def test_loop_failure_backoff(caplog: pytest.LogCaptureFixture) -> None:
    """Failures back off 60s doubling (cap 3600s); success resets to interval."""
    stop = threading.Event()
    sleeper = _Sleeper(stop, stop_after=3)
    results = iter([1, 1, 0])

    def once_fn(**kw: Any) -> int:
        return next(results, 0)

    cfg = Settings()
    with caplog.at_level(logging.ERROR):
        assert run_loop(settings=cfg, once_fn=once_fn, sleep_fn=sleeper, stop_event=stop) == 0
    assert sleeper.sleeps == [60, 120, cfg.crawl_interval_hours * 3600]
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("consecutive failures=1" in m for m in errors)
    assert any("consecutive failures=2" in m for m in errors)


def test_loop_lock_skip_short_retry() -> None:
    """rc 3 (lock held) retries after 900s and accrues no failure backoff."""
    stop = threading.Event()
    sleeper = _Sleeper(stop, stop_after=2)
    results = iter([3, 1])

    def once_fn(**kw: Any) -> int:
        return next(results, 0)

    assert run_loop(settings=Settings(), once_fn=once_fn, sleep_fn=sleeper, stop_event=stop) == 0
    assert sleeper.sleeps == [900, 60], "the failure after rc 3 backoffs from 60, not 120"


def test_loop_clean_shutdown_on_signal() -> None:
    """Setting the stop flag during a sleep exits the loop promptly with 0."""
    stop = threading.Event()
    sleeper = _Sleeper(stop, stop_after=1)  # the "signal" lands mid-sleep
    ticks: list[int] = []

    def once_fn(**kw: Any) -> int:
        ticks.append(1)
        return 0

    assert run_loop(settings=Settings(), once_fn=once_fn, sleep_fn=sleeper, stop_event=stop) == 0
    assert len(ticks) == 1
    assert len(sleeper.sleeps) == 1


def test_loop_exception_counts_as_failure(caplog: pytest.LogCaptureFixture) -> None:
    """F3: a raised tick logs ERROR with the count, backs off, never exits != 0."""
    stop = threading.Event()
    sleeper = _Sleeper(stop, stop_after=2)

    def once_fn(**kw: Any) -> int:
        raise RuntimeError("boom")

    with caplog.at_level(logging.ERROR):
        assert run_loop(settings=Settings(), once_fn=once_fn, sleep_fn=sleeper, stop_event=stop) == 0
    assert sleeper.sleeps == [60, 120], "a raised tick backs off exactly like rc 1"
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("consecutive failures=1" in m for m in errors)
    assert any("consecutive failures=2" in m for m in errors)
