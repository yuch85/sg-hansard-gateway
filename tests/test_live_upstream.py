"""Live E2E test — runs only when SPRS is reachable (``-m live``).

This is the spec §30-A / §23 acceptance target: ``GET /a/{token}/report/{id}``
against the REAL upstream returns 200 and the raw bytes contain the full report
title. Deselected by default (``addopts = -m 'not live'``); enable with
``uv run pytest -m live`` when SPRS is reachable.
"""

from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

import hansard_gateway.auth as auth_mod
from hansard_gateway.auth import TokenStore
from hansard_gateway.main import create_app

#: The E2E target report (spec §30-A).
E2E_REPORT_ID = "037_20041019_S0004_T0023"

#: The title the raw bytes must contain (spec §23).
EXPECTED_TITLE = "Singapore Armed Forces (Amendment No. 2) Bill"

#: A dedicated live token (distinct from the offline TEST_TOKEN) so a live run
#: never collides with the offline store.
LIVE_TOKEN_LABEL = "hansard-live"
LIVE_TOKEN = "hg_livee2etoken0123456789abcdef"


@pytest.fixture()
def live_client(tmp_path) -> TestClient:
    """A TestClient over the app with a single enabled live token."""
    tokens_path = tmp_path / "tokens.yaml"
    tokens_path.write_text(
        "tokens:\n"
        f"- label: {LIVE_TOKEN_LABEL}\n"
        f"  sha256: {hashlib.sha256(LIVE_TOKEN.encode('utf-8')).hexdigest()}\n"
        "  enabled: true\n"
        "  last4: cdef\n",
        encoding="utf-8",
    )
    auth_mod._store = TokenStore(path=tokens_path)
    return TestClient(create_app())


@pytest.mark.live
def test_live_report_e2e(live_client: TestClient) -> None:
    """The real upstream returns the full 2004 SAF report (spec §30-A/§23).

    F-2 (wave 6): case-INSENSITIVE title assert — the live upstream returns
    the title in ALL CAPS ('SINGAPORE ARMED FORCES ... BILL'), while
    EXPECTED_TITLE is sentence-case (T-8 class). The report renders 200
    fine; only the case of the title bytes differed."""
    resp = live_client.get(f"/a/{LIVE_TOKEN}/report/{E2E_REPORT_ID}")
    assert resp.status_code == 200
    assert EXPECTED_TITLE.casefold() in resp.text.casefold()


@pytest.mark.live
def test_live_report_html_no_js(live_client: TestClient) -> None:
    """Live §30-H: the raw bytes over the app carry no <script> (no JS gate)."""
    resp = live_client.get(f"/a/{LIVE_TOKEN}/report/{E2E_REPORT_ID}")
    assert resp.status_code == 200
    assert "<script" not in resp.text.lower()
    assert E2E_REPORT_ID in resp.text
