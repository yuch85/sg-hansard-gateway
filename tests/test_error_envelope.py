"""Consumer R-2a: the format-aware JSON/text error envelope.

Offline (TestClient + respx): upstream failures, 422/429, and 404 token
failures all honor the ``format`` query param — json → the
``{"error": {"code", "message", "retryable"}}`` envelope (application/json),
text → a one-line text/plain, html → the static error page. Status codes are
unchanged (spec §17). The 404 body is byte-identical across paths/tokens
(anti-enumeration, addendum §7).
"""

from __future__ import annotations

import json
import re

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import hansard_gateway.auth as auth_mod
from hansard_gateway.auth import TEST_TOKEN
from hansard_gateway.main import create_app

UPSTREAM_BASE = "https://sprs.parl.gov.sg/search"
PAIR_BASE = "https://search.pair.gov.sg"

#: The E2E target report id (spec §30-A).
E2E_REPORT_ID = "037_20041019_S0004_T0023"


@pytest.fixture()
def client(token_store) -> TestClient:
    """The full app wired to the fixture token store."""
    auth_mod._store = token_store
    return TestClient(create_app())


def _envelope(resp: httpx.Response) -> dict:
    """Parse and return the R-2a error envelope from a JSON error response."""
    return json.loads(resp.text)["error"]


# --- (a) upstream failure → 502 + JSON envelope, retryable -----------------


def test_report_upstream_502_json_envelope(client: TestClient) -> None:
    """format=json on an upstream timeout → 502, application/json, retryable."""
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/getHansardTopic").mock(
            side_effect=httpx.ReadTimeout("read timed out"))
        resp = client.get(f"/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}?format=json")
    assert resp.status_code == 502
    assert resp.headers["content-type"].startswith("application/json")
    err = _envelope(resp)
    assert err["code"] == "upstream_unavailable"
    assert err["retryable"] is True
    assert isinstance(err["message"], str) and err["message"]


def test_search_upstream_502_json_envelope(client: TestClient) -> None:
    """/search?format=json with a degraded SPRS → 502 JSON envelope."""
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/searchResult").respond(status_code=400, json={"error": "bad"})
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False,
                          assert_all_mocked=False)
        pair.start()
        try:
            resp = client.get(f"/a/{TEST_TOKEN}/search?q=probe&format=json")
        finally:
            pair.stop()
    assert resp.status_code == 502
    assert resp.headers["content-type"].startswith("application/json")
    err = _envelope(resp)
    assert err["code"] == "upstream_unavailable" and err["retryable"] is True


def test_date_upstream_502_json_envelope(client: TestClient) -> None:
    """/date?format=json with a degraded SPRS sweep → 502 JSON envelope."""
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/searchResult").respond(status_code=400, json={"error": "bad"})
        resp = client.get(f"/a/{TEST_TOKEN}/date/2004-10-19?format=json")
    assert resp.status_code == 502
    assert resp.headers["content-type"].startswith("application/json")
    assert _envelope(resp)["code"] == "upstream_unavailable"


def test_upstream_502_html_still_page(client: TestClient) -> None:
    """The default (html) format still renders the static error page."""
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/getHansardTopic").mock(
            side_effect=httpx.ReadTimeout("read timed out"))
        resp = client.get(f"/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}")
    assert resp.status_code == 502
    assert resp.headers["content-type"].startswith("text/html")
    assert "No transcript has been generated or substituted" in resp.text


# --- (b) 422 / 429 JSON envelope shape -------------------------------------


def test_report_bad_format_422_html_fallback(client: TestClient) -> None:
    """report?format=bogus → 422; the untrusted format falls back to html.

    We cannot honor a ``format`` value we don't recognize, so the error page
    is rendered as html (the safe default). A recognized format (json/text)
    IS honored — see test_422_json_envelope below.
    """
    resp = client.get(f"/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}?format=bogus")
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("text/html")


def test_search_bad_param_422_json_envelope(client: TestClient) -> None:
    """/search with a malformed date + format=json → 422 JSON envelope."""
    resp = client.get(f"/a/{TEST_TOKEN}/search?q=probe&from_=nope&format=json")
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/json")
    err = _envelope(resp)
    assert err["code"] == "invalid_parameter" and err["retryable"] is False


def test_date_bad_param_422_json_envelope(client: TestClient) -> None:
    """/date with a malformed day + format=json → 422 JSON envelope."""
    resp = client.get(f"/a/{TEST_TOKEN}/date/not-a-date?format=json")
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/json")
    assert _envelope(resp)["code"] == "invalid_parameter"


# --- (c) 404 token failure byte-identical across paths ---------------------


def test_404_token_failure_identical_across_paths(client: TestClient) -> None:
    """A bogus token yields a byte-identical 404 body on two different paths."""
    bogus = "hg_bogustoken0123456789abcdef"
    body_report = client.get(f"/a/{bogus}/report/{E2E_REPORT_ID}").content
    body_search = client.get(f"/a/{bogus}/search?q=probe").content
    assert body_report == body_search
    assert E2E_REPORT_ID not in body_report.decode("utf-8")  # no path echo
    assert "probe" not in body_search.decode("utf-8")


def test_404_invalid_vs_revoked_identical(client: TestClient) -> None:
    """Invalid and revoked (disabled) tokens → identical 404 bodies."""
    revoked = "hg_revokedtoken0123456789abcdef"
    invalid = "hg_notatoken0123456789abcdef"
    a = client.get(f"/a/{revoked}/report/{E2E_REPORT_ID}")
    b = client.get(f"/a/{invalid}/report/{E2E_REPORT_ID}")
    assert a.status_code == b.status_code == 404
    assert a.content == b.content


def test_404_token_failure_json_envelope(client: TestClient) -> None:
    """format=json on a token failure → 404 JSON envelope (not_found)."""
    bogus = "hg_bogustoken0123456789abcdef"
    resp = client.get(f"/a/{bogus}/report/{E2E_REPORT_ID}?format=json")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")
    err = _envelope(resp)
    assert err["code"] == "not_found" and err["retryable"] is False
    assert E2E_REPORT_ID not in resp.text


# --- (d) text-format error → text/plain ------------------------------------


def test_upstream_502_text_plain(client: TestClient) -> None:
    """report?format=text on an upstream timeout → 502 text/plain one-liner."""
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/getHansardTopic").mock(
            side_effect=httpx.ReadTimeout("read timed out"))
        resp = client.get(f"/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}?format=text")
    assert resp.status_code == 502
    assert resp.headers["content-type"].startswith("text/plain")
    assert "\n" not in resp.text.strip()
    assert "upstream_unavailable" in resp.text


def test_422_text_plain(client: TestClient) -> None:
    """report?format=text with a bad id → 422 text/plain one-liner."""
    resp = client.get(f"/a/{TEST_TOKEN}/report/bad id?format=text")
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("text/plain")
    assert "invalid_parameter" in resp.text
