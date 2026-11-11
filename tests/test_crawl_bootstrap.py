"""Offline regression coverage for the production crawl client bootstrap."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx
import pytest

from crawl import crawl_main
from crawl.crawl_client import CrawlClient
from hansard_gateway.config import Settings


def test_real_settings_build_owned_client() -> None:
    """Production construction uses the current Settings timeout contract."""
    cfg = Settings(connect_timeout_s=2.5, upstream_attempt_timeout_s=7.5)
    with CrawlClient(settings=cfg) as client:
        assert client._client.timeout.connect == cfg.connect_timeout_s
        assert client._client.timeout.pool == cfg.connect_timeout_s
        assert client._client.timeout.read == cfg.upstream_attempt_timeout_s
        assert client._client.timeout.write == cfg.upstream_attempt_timeout_s
    assert client._client.is_closed


def test_entrypoint_builds_real_client_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI builds and closes its own real client, with all network forbidden."""
    cfg = Settings(index_db_path=tmp_path / "index.db", crawl_start_date="9999-01-01")
    monkeypatch.setattr(crawl_main, "_settings", cfg)

    def forbid_request(*args: object, **kwargs: object) -> None:
        pytest.fail("bootstrap smoke test must not send a network request")

    monkeypatch.setattr(httpx.Client, "send", forbid_request)
    assert crawl_main.main([]) == 0
    with sqlite3.connect(cfg.index_db_path) as conn:
        assert conn.execute("select count(*) from report").fetchone() == (0,)
        assert conn.execute("select count(*) from crawl_state").fetchone() == (0,)
