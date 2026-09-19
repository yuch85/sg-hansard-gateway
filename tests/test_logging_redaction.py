"""RELEASE-BLOCKING: no plaintext capability token in any application log.

Drives /report, /search, and /date through the TestClient with the structured
JSON logger active (temp log file + caplog). Asserts:

1. The log file contains ZERO occurrences of the test token and ZERO
   ``hg_``-prefixed token strings (D-10 / addendum §29).
2. Each authenticated request produced a structured JSON line with
   ``token_label`` + the required fields, and NOT the token.
3. The uvicorn access log is off (no access-log line with ``/a/{token}``).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
import respx
from fastapi.testclient import TestClient

import hansard_gateway.auth as auth_mod
from hansard_gateway.auth import TEST_TOKEN, TEST_TOKEN_LABEL
from hansard_gateway.config import Settings
from hansard_gateway.logging_setup import (
    RequestLoggerMiddleware,
    configure_logging,
    redact,
)
from hansard_gateway.main import create_app

UPSTREAM_BASE = "https://sprs.parl.gov.sg/search"
PAIR_BASE = "https://search.pair.gov.sg"
E2E_REPORT_ID = "037_20041019_S0004_T0023"


@pytest.fixture()
def app(token_store, tmp_path: Path):
    """App with structured logging wired to a temp file."""
    auth_mod._store = token_store
    app = create_app()
    configure_logging(log_file=tmp_path / "hansard.log", settings=Settings())
    yield app
    logging.getLogger("hansard_gateway").handlers.clear()


@pytest.fixture()
def client(app) -> TestClient:
    """A TestClient over the logging app."""
    return TestClient(app)


def _saf_fixture(fixtures_dir: Path) -> dict[str, object]:
    path = fixtures_dir / "topic_20041019_saf.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _search_fixture(fixtures_dir: Path) -> list[dict]:
    path = fixtures_dir / "searchresult_20041019_p1.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_no_token_in_log_file(client: TestClient, fixtures_dir: Path, tmp_path: Path) -> None:
    """After /report + /search + /date, the log file has ZERO token strings."""
    topic = _saf_fixture(fixtures_dir)
    search_rows = _search_fixture(fixtures_dir)
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/getHansardTopic").respond(json={"resultHTML": topic})
        mock.post("/searchResult").respond(json=search_rows)
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False, assert_all_mocked=False)
        pair.start()
        try:
            r1 = client.get(f"/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}")
            r2 = client.get(f"/a/{TEST_TOKEN}/search?q=Pension%20Fund")
            r3 = client.get(f"/a/{TEST_TOKEN}/date/2004-10-19")
        finally:
            pair.stop()
    assert r1.status_code == 200, r1.text[:200]
    assert r2.status_code == 200, r2.text[:200]
    assert r3.status_code == 200, r3.text[:200]

    log_file = tmp_path / "hansard.log"
    content = log_file.read_text(encoding="utf-8")
    # RELEASE-BLOCKING: zero plaintext token, zero hg_-prefixed token string.
    assert TEST_TOKEN not in content
    assert "hg_testvalidtoken" not in content
    assert f"/a/{TEST_TOKEN}" not in content
    # The label IS present (sanitized structured lines were written).
    assert TEST_TOKEN_LABEL in content


def test_structured_line_fields(client: TestClient, fixtures_dir: Path, tmp_path: Path) -> None:
    """Each authenticated request emits one JSON line with the required fields."""
    topic = _saf_fixture(fixtures_dir)
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/getHansardTopic").respond(json={"resultHTML": topic})
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False, assert_all_mocked=False)
        pair.start()
        try:
            resp = client.get(f"/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}")
        finally:
            pair.stop()
    assert resp.status_code == 200

    content = (tmp_path / "hansard.log").read_text(encoding="utf-8")
    lines = [ln for ln in content.splitlines() if ln.strip()]
    request_lines = [ln for ln in lines if '"request_id"' in ln and '"route"' in ln]
    assert len(request_lines) >= 1
    record = json.loads(request_lines[-1])
    for field in ("timestamp", "request_id", "token_label", "route", "status", "duration_ms"):
        assert field in record, f"missing field {field}"
    assert record["token_label"] == TEST_TOKEN_LABEL
    assert record["route"] == "report"
    assert TEST_TOKEN not in json.dumps(record)


def test_uvicorn_access_log_off(client: TestClient, fixtures_dir: Path, tmp_path: Path, caplog) -> None:
    """No access-log line with /a/{token} appears (D-10, release-blocking)."""
    topic = _saf_fixture(fixtures_dir)
    with caplog.at_level(logging.DEBUG, logger="uvicorn.access"):
        with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
            mock.post("/getHansardTopic").respond(json={"resultHTML": topic})
            resp = client.get(f"/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}")
    assert resp.status_code == 200
    access_records = [r for r in caplog.records if r.name == "uvicorn.access"]
    for record in access_records:
        assert TEST_TOKEN not in record.getMessage()
        assert f"/a/{TEST_TOKEN}" not in record.getMessage()
    # The uvicorn.access logger is disabled by configure_logging.
    assert logging.getLogger("uvicorn.access").disabled


def test_redact_scrubs_token_shape() -> None:
    """The redaction filter scrubs hg_-shaped strings from any value."""
    assert redact(TEST_TOKEN) == "[REDACTED]"
    assert redact(f"prefix {TEST_TOKEN} suffix") == "prefix [REDACTED] suffix"
    assert redact({"token": TEST_TOKEN, "ok": 1}) == {"token": "[REDACTED]", "ok": 1}
    assert redact(["a", TEST_TOKEN]) == ["a", "[REDACTED]"]
    # Non-token strings pass through untouched.
    assert redact("hansard-team-1") == "hansard-team-1"


def test_no_access_log_path_in_file(client: TestClient, fixtures_dir: Path, tmp_path: Path) -> None:
    """The log file never carries the token-bearing path (access-log leak)."""
    search_rows = _search_fixture(fixtures_dir)
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/searchResult").respond(json=search_rows)
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False, assert_all_mocked=False)
        pair.start()
        try:
            resp = client.get(f"/a/{TEST_TOKEN}/search?q=Fund")
        finally:
            pair.stop()
    assert resp.status_code == 200
    content = (tmp_path / "hansard.log").read_text(encoding="utf-8")
    assert f"/a/{TEST_TOKEN}" not in content
