"""Incremental crawl keeps earlier data, keeps state, rechecks recent empties.

Phase 27.4 plan 01. Offline only: a fake client stands in for the network.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from crawl import crawl_db
from crawl.crawl_client import CrawlTransientError
from crawl.crawl_main import run_crawl
from hansard_gateway.config import Settings
from hansard_gateway.index.build import build_index

_STAMP = "%Y-%m-%dT%H:%M:%SZ"


def _stamp(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime(_STAMP)


def _cfg(tmp_path: Path, **kw) -> Settings:
    base = dict(
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


def _ids(cfg: Settings) -> set[str]:
    return {r[0] for r in _q(cfg, "SELECT report_id FROM report")}


def _state(cfg: Settings) -> dict[str, int]:
    return {r[0]: r[1] for r in _q(cfg, "SELECT sitting_date, had_reports FROM crawl_state")}


def test_incremental_keeps_existing_reports(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    first = FakeClient({
        "2026-08-01": [_raw("a1#", "Alpha Budget")],
        "2026-08-02": [_raw("a2#", "Beta Housing")],
    })
    assert run_crawl(settings=cfg, client=first, today=date(2026, 8, 2)) == 0
    assert _ids(cfg) == {"a1#", "a2#"}
    second = FakeClient({"2026-08-04": [_raw("a4#", "Gamma Transport")]})
    assert run_crawl(settings=cfg, client=second, today=date(2026, 8, 4)) == 0
    assert _ids(cfg) == {"a1#", "a2#", "a4#"}, "earlier reports must survive an incremental swap"


def test_incremental_keeps_crawl_state(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    run_crawl(settings=cfg, client=FakeClient({"2026-08-01": [_raw("a1#", "Alpha")]}),
              today=date(2026, 8, 2))
    run_crawl(settings=cfg, client=FakeClient({"2026-08-04": [_raw("a4#", "Gamma")]}),
              today=date(2026, 8, 4))
    st = _state(cfg)
    assert {"2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04"} <= set(st)
    assert st["2026-08-01"] == 1 and st["2026-08-04"] == 1


def test_second_run_does_not_refetch_old_dates(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    run_crawl(settings=cfg, client=FakeClient({
        "2026-08-01": [_raw("a1#", "Alpha")], "2026-08-02": [_raw("a2#", "Beta")],
    }), today=date(2026, 8, 2))
    again = FakeClient()
    run_crawl(settings=cfg, client=again, today=date(2026, 8, 10))
    assert "2026-08-01" not in again.calls and "2026-08-02" not in again.calls
    assert "2026-08-10" in again.calls


def test_refetched_date_replaces_its_rows_only(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed(cfg,
          [_idx_row("a1#", "2026-08-01", "Old One"), _idx_row("b1#", "2026-08-01", "Old Two"),
           _idx_row("c1#", "2026-08-02", "Keep Me")],
          [("2026-08-01", 1, _stamp(100)), ("2026-08-02", 1, _stamp(1))])
    fake = FakeClient({"2026-08-01": [_raw("a2#", "New One")]})
    assert run_crawl(settings=cfg, client=fake, today=date(2026, 8, 2)) == 0
    assert fake.calls == ["2026-08-01"]
    assert _ids(cfg) == {"a2#", "c1#"}


def test_recent_empty_date_is_rechecked_and_picked_up(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed(cfg, [_idx_row("z#", "2026-07-30", "Existing")],
          [("2026-08-01", 0, _stamp(3))])
    fake = FakeClient({"2026-08-01": [_raw("late#", "Posted Late")]})
    assert run_crawl(settings=cfg, client=fake, today=date(2026, 8, 5)) == 0
    assert "2026-08-01" in fake.calls
    assert {"late#", "z#"} <= _ids(cfg)
    assert _state(cfg)["2026-08-01"] == 1


def test_old_empty_date_stays_skipped(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, crawl_start_date="2026-06-01")
    _seed(cfg, [], [("2026-06-10", 0, _stamp(60))])
    fake = FakeClient()
    run_crawl(settings=cfg, client=fake, today=date(2026, 8, 5))
    assert "2026-06-10" not in fake.calls


def test_recheck_window_zero_restores_skip(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, crawl_empty_recheck_days=0)
    _seed(cfg, [], [("2026-08-01", 0, _stamp(3))])
    fake = FakeClient()
    run_crawl(settings=cfg, client=fake, today=date(2026, 8, 5))
    assert "2026-08-01" not in fake.calls


def test_failed_build_leaves_live_and_state_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    _seed(cfg, [_idx_row("a1#", "2026-08-01", "Alpha")], [("2026-08-01", 1, _stamp(1))])
    before_ids, before_state = _ids(cfg), _q(cfg, "SELECT * FROM crawl_state")

    def boom(rows, target, **kw):
        target.write_bytes(b"partial")
        raise RuntimeError("mid-build crash")

    monkeypatch.setattr(crawl_db, "build_index", boom)
    fake = FakeClient({"2026-08-03": [_raw("n#", "New")]})
    assert run_crawl(settings=cfg, client=fake, today=date(2026, 8, 3)) == 1
    assert _ids(cfg) == before_ids
    assert _q(cfg, "SELECT * FROM crawl_state") == before_state
    assert not crawl_db.staging_path(cfg.index_db_path).exists()


def test_transient_error_keeps_old_rows_and_state(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    old = _stamp(100)
    _seed(cfg, [_idx_row("a1#", "2026-08-01", "Alpha")], [("2026-08-01", 1, old)])
    fake = FakeClient(fail={"2026-08-01"})
    assert run_crawl(settings=cfg, client=fake, today=date(2026, 8, 2)) == 0
    assert "a1#" in _ids(cfg)
    assert _q(cfg, "SELECT crawled_at FROM crawl_state WHERE sitting_date='2026-08-01'") == [(old,)]


@pytest.mark.parametrize("cold", [False, True])
def test_first_run_without_live_index_works(tmp_path: Path, cold: bool) -> None:
    cfg = _cfg(tmp_path)
    fake = FakeClient({"2026-08-01": [_raw("a1#", "Alpha")]})
    assert run_crawl(settings=cfg, client=fake, cold=cold, today=date(2026, 8, 2)) == 0
    assert _ids(cfg) == {"a1#"}


def test_empty_answer_for_date_with_reports_keeps_rows(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    old = _stamp(100)
    _seed(cfg, [_idx_row("a1#", "2026-08-01", "Alpha")], [("2026-08-01", 1, old)])
    fake = FakeClient()  # upstream now answers "no results" for 2026-08-01
    assert run_crawl(settings=cfg, client=fake, today=date(2026, 8, 2)) == 0
    assert "a1#" in _ids(cfg)
    assert _state(cfg)["2026-08-01"] == 1


# --- T5: --seed-state (no network) ---------------------------------------


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


def test_seed_state_builds_state_from_index_without_network(tmp_path: Path) -> None:
    from crawl.crawl_seed import seed_state

    cfg = _cfg(tmp_path)
    _seeded_index(cfg)
    assert seed_state(settings=cfg) == 0
    assert _state(cfg) == {"2026-08-01": 1, "2026-08-02": 0, "2026-08-03": 1}
    assert _ids(cfg) == {"a1#", "a3#"}
    stamps = _q(cfg, "SELECT DISTINCT crawled_at FROM crawl_state")
    assert len(stamps) == 1 and stamps[0][0].endswith("Z")


def test_seed_state_refuses_when_state_exists(tmp_path: Path) -> None:
    from crawl.crawl_seed import seed_state

    cfg = _cfg(tmp_path)
    _seed(cfg, [_idx_row("a1#", "2026-08-01", "Alpha")], [("2026-08-01", 1, _stamp(1))])
    before = cfg.index_db_path.read_bytes()
    assert seed_state(settings=cfg) == 2
    assert cfg.index_db_path.read_bytes() == before


def test_seed_state_refuses_empty_index(tmp_path: Path) -> None:
    from crawl.crawl_seed import seed_state

    cfg = _cfg(tmp_path)
    build_index([], cfg.index_db_path, settings=cfg)
    assert seed_state(settings=cfg) == 2


def test_after_seed_incremental_fetches_only_new_dates(tmp_path: Path) -> None:
    from crawl.crawl_seed import seed_state

    cfg = _cfg(tmp_path)
    _seeded_index(cfg)
    seed_state(settings=cfg)
    fake = FakeClient({"2026-08-06": [_raw("n#", "New")]})
    assert run_crawl(settings=cfg, client=fake, today=date(2026, 8, 6)) == 0
    # 08-02 is a seeded empty date inside the 14-day recheck window.
    assert sorted(fake.calls) == ["2026-08-02", "2026-08-04", "2026-08-05", "2026-08-06"]
    assert {"a1#", "a3#", "n#"} <= _ids(cfg)


# --- review notes: unreadable-live guard and recheck boundary -------------


def test_state_with_reports_but_no_report_rows_refetches_all(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed(cfg, [], [("2026-08-01", 1, _stamp(1)), ("2026-08-02", 0, _stamp(1))])
    fake = FakeClient({"2026-08-01": [_raw("a1#", "Alpha")]})
    assert run_crawl(settings=cfg, client=fake, today=date(2026, 8, 3)) == 0
    assert "2026-08-01" in fake.calls, "fresh state must not be trusted without rows"
    assert "a1#" in _ids(cfg)


def test_recheck_default_is_14_days() -> None:
    assert Settings().crawl_empty_recheck_days == 14


def test_empty_recheck_boundary_exactly_n_days_is_skipped(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)  # default window: 14 days
    _seed(cfg, [_idx_row("z#", "2026-07-30", "Existing")],
          [("2026-08-06", 0, _stamp(20)), ("2026-08-07", 0, _stamp(20))])
    fake = FakeClient()
    run_crawl(settings=cfg, client=fake, today=date(2026, 8, 20))
    assert "2026-08-06" not in fake.calls, "exactly 14 days old: skipped"
    assert "2026-08-07" in fake.calls, "13 days old: rechecked"


@pytest.mark.parametrize("n", [1, 3])
def test_empty_recheck_boundary_follows_setting(tmp_path: Path, n: int) -> None:
    cfg = _cfg(tmp_path, crawl_empty_recheck_days=n)
    end = date(2026, 8, 20)
    edge = (end - timedelta(days=n)).isoformat()
    inside = (end - timedelta(days=n - 1)).isoformat()
    _seed(cfg, [_idx_row("z#", "2026-07-30", "Existing")],
          [(edge, 0, _stamp(30)), (inside, 0, _stamp(30))])
    fake = FakeClient()
    run_crawl(settings=cfg, client=fake, today=end)
    assert edge not in fake.calls
    assert inside in fake.calls
