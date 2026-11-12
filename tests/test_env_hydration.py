"""27.2-REQ-02: HANSARD_* env-var hydration of the Settings singleton.

Every deployment-relevant field is overridable via env; a fresh clone with no
env behaves identically to the pre-27.2 deployment (all locked defaults).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from hansard_gateway.config import (
    ENV_APP_PORT,
    ENV_CRAWL_EMPTY_RECHECK_DAYS,
    ENV_CRAWL_ENABLED,
    ENV_CRAWL_FIRST_RUN_DAYS,
    ENV_CRAWL_FULL_HISTORY,
    ENV_CRAWL_INTERVAL_HOURS,
    ENV_CRAWL_RECRAWL_AGE_DAYS,
    ENV_CRAWL_START_DATE,
    ENV_INDEX_DB_PATH,
    ENV_LOG_FILE,
    ENV_PUBLIC_BASE_URL,
    ENV_ROBOTS_PATH,
    ENV_TOKENS_PATH,
    Settings,
    _env_settings,
)

#: The thirteen overridable env vars: the six 27.2 deployment vars plus the
#: seven 27.5 crawl vars (the token-shape guard is deliberately excluded).
_ALL_ENV_VARS: tuple[str, ...] = (
    ENV_APP_PORT,
    ENV_INDEX_DB_PATH,
    ENV_TOKENS_PATH,
    ENV_PUBLIC_BASE_URL,
    ENV_LOG_FILE,
    ENV_ROBOTS_PATH,
    ENV_CRAWL_START_DATE,
    ENV_CRAWL_RECRAWL_AGE_DAYS,
    ENV_CRAWL_EMPTY_RECHECK_DAYS,
    ENV_CRAWL_ENABLED,
    ENV_CRAWL_INTERVAL_HOURS,
    ENV_CRAWL_FULL_HISTORY,
    ENV_CRAWL_FIRST_RUN_DAYS,
)


def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every HANSARD_* var so _env_settings sees a pristine environment."""
    for var in _ALL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_env_settings_honors_all_six_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set HANSARD_* vars -> _env_settings returns the overridden Settings."""
    _clean_env(monkeypatch)
    monkeypatch.setenv(ENV_APP_PORT, "9999")
    monkeypatch.setenv(ENV_INDEX_DB_PATH, "/data/index.db")
    monkeypatch.setenv(ENV_TOKENS_PATH, "/data/tokens.yaml")
    monkeypatch.setenv(ENV_PUBLIC_BASE_URL, "https://gw.example.test")
    monkeypatch.setenv(ENV_LOG_FILE, "/var/log/gw.log")
    monkeypatch.setenv(ENV_ROBOTS_PATH, "/etc/gw/robots.txt")

    s = _env_settings()

    assert s.app_port == 9999
    assert s.index_db_path == Path("/data/index.db")
    assert s.tokens_path == Path("/data/tokens.yaml")
    assert s.public_base_url == "https://gw.example.test"
    assert s.log_file == "/var/log/gw.log"
    assert s.robots_path == Path("/etc/gw/robots.txt")


def test_env_settings_expands_user_in_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Path-typed env vars run through expanduser (~ resolves to the home dir)."""
    _clean_env(monkeypatch)
    monkeypatch.setenv(ENV_INDEX_DB_PATH, "~/gw/index.db")
    monkeypatch.setenv(ENV_ROBOTS_PATH, "~/gw/robots.txt")

    s = _env_settings()

    home = Path.home()
    assert s.index_db_path == (home / "gw" / "index.db")
    assert s.robots_path == (home / "gw" / "robots.txt")


def test_env_settings_clean_env_returns_locked_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No env -> the exact pre-27.2 locked defaults (byte-identical behavior)."""
    _clean_env(monkeypatch)

    s = _env_settings()

    assert s.app_port == 8765
    assert s.index_db_path == Path("~/.hansard/index.db").expanduser()
    assert s.tokens_path == Path("tokens.yaml")
    assert s.public_base_url == "https://hansard.example.org"
    assert s.log_file == ""
    assert s.robots_path is None


def test_env_settings_honors_crawl_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """27.5: all seven crawl vars hydrate Settings (dates, ints, floats, bools)."""
    _clean_env(monkeypatch)
    monkeypatch.setenv(ENV_CRAWL_START_DATE, "1960-04-01")
    monkeypatch.setenv(ENV_CRAWL_RECRAWL_AGE_DAYS, "30")
    monkeypatch.setenv(ENV_CRAWL_EMPTY_RECHECK_DAYS, "7")
    monkeypatch.setenv(ENV_CRAWL_ENABLED, "1")
    monkeypatch.setenv(ENV_CRAWL_INTERVAL_HOURS, "6.5")
    monkeypatch.setenv(ENV_CRAWL_FULL_HISTORY, "true")
    monkeypatch.setenv(ENV_CRAWL_FIRST_RUN_DAYS, "14")

    s = _env_settings()

    assert s.crawl_start_date == "1960-04-01"
    assert s.crawl_recrawl_age_days == 30
    assert s.crawl_empty_recheck_days == 7
    assert s.crawl_enabled is True
    assert s.crawl_interval_hours == 6.5
    assert s.crawl_full_history is True
    assert s.crawl_first_run_days == 14


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "Yes"])
def test_crawl_bool_true_words(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """27.5 bool parsing: 1/true/yes (any case) are true."""
    _clean_env(monkeypatch)
    monkeypatch.setenv(ENV_CRAWL_ENABLED, raw)
    assert _env_settings().crawl_enabled is True


@pytest.mark.parametrize("raw", ["0", "", "maybe"])
def test_crawl_bool_false_and_unrecognised(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """27.5 bool parsing: 0/empty/unrecognised are false (default)."""
    _clean_env(monkeypatch)
    monkeypatch.setenv(ENV_CRAWL_ENABLED, raw)
    assert _env_settings().crawl_enabled is False


def test_crawl_env_clean_env_returns_locked_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """27.5: pristine env -> locked crawl defaults (loop off, 24h, guard 31d)."""
    _clean_env(monkeypatch)

    s = _env_settings()

    assert s.crawl_enabled is False
    assert s.crawl_interval_hours == 24.0
    assert s.crawl_full_history is False
    assert s.crawl_first_run_days == 31
    # The three pre-existing crawl defaults stay untouched.
    assert s.crawl_start_date == "1955-01-01"
    assert s.crawl_recrawl_age_days == 90
    assert s.crawl_empty_recheck_days == 14


def test_malformed_crawl_env_falls_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """27.5 (L1): a malformed HANSARD_CRAWL_* value never raises out of
    _env_settings (the server imports config at boot): warn + locked default."""
    _clean_env(monkeypatch)
    monkeypatch.setenv(ENV_CRAWL_INTERVAL_HOURS, "abc")
    monkeypatch.setenv(ENV_CRAWL_FIRST_RUN_DAYS, "x")
    monkeypatch.setenv(ENV_CRAWL_START_DATE, "not-a-date")
    monkeypatch.setenv(ENV_CRAWL_ENABLED, "maybe")

    with caplog.at_level(logging.WARNING):
        s = _env_settings()

    assert s.crawl_interval_hours == 24.0
    assert s.crawl_first_run_days == 31
    assert s.crawl_start_date == "1955-01-01"
    assert s.crawl_enabled is False
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    for var in (
        ENV_CRAWL_INTERVAL_HOURS,
        ENV_CRAWL_FIRST_RUN_DAYS,
        ENV_CRAWL_START_DATE,
        ENV_CRAWL_ENABLED,
    ):
        assert any(var in m for m in warnings), var

    # Server import safety: re-running the import-time hydration under the
    # same env must succeed (config import must never crash server boot).
    import importlib

    import hansard_gateway.config as config_mod

    try:
        importlib.reload(config_mod)
    finally:
        monkeypatch.undo()
        importlib.reload(config_mod)


@pytest.mark.parametrize(
    "var, raw, field, default",
    [
        (ENV_CRAWL_FIRST_RUN_DAYS, "3_1", "crawl_first_run_days", 31),
        (ENV_CRAWL_RECRAWL_AGE_DAYS, "9_0", "crawl_recrawl_age_days", 90),
        (ENV_CRAWL_INTERVAL_HOURS, "2_4", "crawl_interval_hours", 24.0),
    ],
)
def test_underscore_literal_crawl_env_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    var: str,
    raw: str,
    field: str,
    default: float,
) -> None:
    """27.5 review 2: Python int()/float() accept "1_000" literals; an env var
    is not a Python literal — reject with a warning + locked default."""
    _clean_env(monkeypatch)
    monkeypatch.setenv(var, raw)

    with caplog.at_level(logging.WARNING):
        s = _env_settings()

    assert getattr(s, field) == default
    assert any(
        var in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
    )


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "NaN", "Infinity"])
def test_non_finite_crawl_interval_falls_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, raw: str
) -> None:
    """27.5 review: nan/inf parse as floats but are nonsense intervals:
    warn + locked default."""
    _clean_env(monkeypatch)
    monkeypatch.setenv(ENV_CRAWL_INTERVAL_HOURS, raw)

    with caplog.at_level(logging.WARNING):
        s = _env_settings()

    assert s.crawl_interval_hours == 24.0
    assert any(
        ENV_CRAWL_INTERVAL_HOURS in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING
    )


@pytest.mark.parametrize(
    "var, raw, field, default",
    [
        (ENV_CRAWL_INTERVAL_HOURS, "0", "crawl_interval_hours", 24.0),
        (ENV_CRAWL_INTERVAL_HOURS, "-3", "crawl_interval_hours", 24.0),
        (ENV_CRAWL_FIRST_RUN_DAYS, "0", "crawl_first_run_days", 31),
        (ENV_CRAWL_FIRST_RUN_DAYS, "-7", "crawl_first_run_days", 31),
    ],
)
def test_non_positive_crawl_env_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    var: str,
    raw: str,
    field: str,
    default: float,
) -> None:
    """27.5 (defect-4): a non-positive interval or guard window is nonsense:
    warn + locked default."""
    _clean_env(monkeypatch)
    monkeypatch.setenv(var, raw)

    with caplog.at_level(logging.WARNING):
        s = _env_settings()

    assert getattr(s, field) == default
    assert any(
        var in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
    )


def test_env_settings_ignores_token_shape_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """token_min_len / token_max_len are NOT env-overridable (auth regex is
    built from them at import) — a stray env var must not change them."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("HANSARD_TOKEN_MIN_LEN", "1")
    monkeypatch.setenv("HANSARD_TOKEN_MAX_LEN", "1")

    s = _env_settings()
    baseline = Settings()

    assert s.token_min_len == baseline.token_min_len == 23
    assert s.token_max_len == baseline.token_max_len == 123
