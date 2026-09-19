"""27.2-REQ-02: HANSARD_* env-var hydration of the Settings singleton.

Every deployment-relevant field is overridable via env; a fresh clone with no
env behaves identically to the pre-27.2 deployment (all locked defaults).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hansard_gateway.config import (
    ENV_APP_PORT,
    ENV_INDEX_DB_PATH,
    ENV_LOG_FILE,
    ENV_PUBLIC_BASE_URL,
    ENV_ROBOTS_PATH,
    ENV_TOKENS_PATH,
    Settings,
    _env_settings,
)

#: The six overridable env vars (the token-shape guard is deliberately excluded).
_ALL_ENV_VARS: tuple[str, ...] = (
    ENV_APP_PORT,
    ENV_INDEX_DB_PATH,
    ENV_TOKENS_PATH,
    ENV_PUBLIC_BASE_URL,
    ENV_LOG_FILE,
    ENV_ROBOTS_PATH,
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
    assert s.public_base_url == "https://hansard.098020.xyz"
    assert s.log_file == ""
    assert s.robots_path is None


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
