"""27.2-REQ-05: robots.txt operator override (HANSARD_ROBOTS_PATH).

Default is the byte-identical 148-byte constant; an existing override file is
served verbatim; a missing override file falls back to the default. The route
closes over the per-app settings (the app_settings seam), never the singleton.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from hansard_gateway.config import settings
from hansard_gateway.index.loader import IndexService
from hansard_gateway.main import create_app
from tests.test_hygiene import ROBOTS_BODY


def _client(index: IndexService, robots_path: Path | None = None) -> TestClient:
    """Build a TestClient over an app with the given robots_path override."""
    cfg = replace(settings, robots_path=robots_path) if robots_path else settings
    return TestClient(create_app(app_settings=cfg, index_override=index))


def test_robots_default_is_byte_identical(client_with_index: TestClient) -> None:
    """No override -> the 148-byte default constant, byte-for-byte."""
    r = client_with_index.get("/robots.txt")
    assert r.status_code == 200
    assert r.text == ROBOTS_BODY
    assert len(r.content) == len(ROBOTS_BODY.encode())


def test_robots_serves_override_file_bytes(index: IndexService, tmp_path: Path) -> None:
    """An existing override file is served verbatim (retrieval policy)."""
    p = tmp_path / "robots_override.txt"
    body = "User-agent: *\nDisallow: /\n"
    p.write_text(body, encoding="utf-8")

    r = _client(index, robots_path=p).get("/robots.txt")

    assert r.status_code == 200
    assert r.text == body
    assert r.text != ROBOTS_BODY


def test_robots_missing_file_falls_back_to_default(
    index: IndexService, tmp_path: Path
) -> None:
    """robots_path pointing at a missing file -> the default constant."""
    missing = tmp_path / "does_not_exist.txt"

    r = _client(index, robots_path=missing).get("/robots.txt")

    assert r.status_code == 200
    assert r.text == ROBOTS_BODY
