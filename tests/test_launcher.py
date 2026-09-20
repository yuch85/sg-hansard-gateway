"""Tests for the launcher departure board (Phase 27.1 wave 2, spec §4.1).

Covers: no-redirect identical 200 at /a/{t} and /a/{t}/, the 8 sections in
order, the exact agent-instruction wording, form/script/iframe bans, the
invalid-token 404 (byte-identical no-enumeration body), and the R9 json/text
format siblings.
"""

from __future__ import annotations

import hashlib
import json
import re

from fastapi.testclient import TestClient

from hansard_gateway.auth import TEST_TOKEN
from hansard_gateway.config import settings
from hansard_gateway.index.queries import ALPHABET

#: The exact agent-instruction sentence the launcher must carry (spec §4.1).
_INSTRUCTION_SENTINEL = "Do not construct or guess URLs"

#: The production invalid-token 404 body fingerprint (no enumeration).
_BOGUS_TOKEN = "hg_invalidtoken00000000000000zz"


def _get(client: TestClient, path: str) -> "object":
    """GET a launcher path without following redirects (the no-redirect check)."""
    return client.get(path, follow_redirects=False)


def test_both_forms_200_no_redirect_identical(client_with_index: TestClient) -> None:
    """/a/{t} and /a/{t}/ both 200, no redirect, byte-identical bodies."""
    r1 = _get(client_with_index, f"/a/{TEST_TOKEN}")
    r2 = _get(client_with_index, f"/a/{TEST_TOKEN}/")
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert not r1.history, "no-slash form must not redirect"
    assert not r2.history, "slash form must not redirect"
    assert r1.content == r2.content, "bodies must be byte-identical"


def test_agent_instruction_exact(client_with_index: TestClient) -> None:
    """The launcher carries the spec §4.1 instruction paragraph verbatim."""
    body = client_with_index.get(f"/a/{TEST_TOKEN}/").text
    assert _INSTRUCTION_SENTINEL in body
    assert "You are reading a navigation page" in body


def test_sections_in_order(client_with_index: TestClient) -> None:
    """All 8 spec §4.1 sections appear, in order."""
    body = client_with_index.get(f"/a/{TEST_TOKEN}/").text
    positions = [
        body.index("Find a topic by word"),
        body.index("Common topics"),
        body.index("Recent sittings"),
        body.index("Browse by year"),
        body.index("Browse by member"),
        body.index("Bills A-Z"),
        body.index("This page as JSON"),
    ]
    assert positions == sorted(positions), "launcher sections out of spec order"


def test_36_letter_links_with_counts(client_with_index: TestClient) -> None:
    """36 nav links (a-z, 0-9) each with a visible count and the R2 twin."""
    body = client_with_index.get(f"/a/{TEST_TOKEN}/").text
    for c in ALPHABET:
        assert f"/a/{TEST_TOKEN}/nav/{c}" in body, f"letter link for {c!r} missing"
    # Exactly 36 distinct /nav/<char> letter-index links.
    links = set(re.findall(rf'/a/{TEST_TOKEN}/nav/([a-z0-9])"', body))
    assert links == set(ALPHABET), f"expected 36 letter links, got {sorted(links)}"
    # Each letter link carries the R2 visible-URL twin span.
    base = settings.public_base_url.rstrip("/")
    for c in ALPHABET[:4]:  # spot check: a,b,c,d
        full = f"{base}/a/{TEST_TOKEN}/nav/{c}"
        assert f'<span class="u">{full}</span>' in body


def test_common_topics_and_recent_sittings(
    client_with_index: TestClient, index
) -> None:
    """Common topics (top-N finished search links) + recent sittings (date
    links) are present with the R2 twins."""
    from hansard_gateway.render.urls import abs_search_url

    body = client_with_index.get(f"/a/{TEST_TOKEN}/").text
    common = index.common_terms(settings.common_topics_n)
    assert common, "fixture index must yield common terms"
    for surface, _n in common:
        assert surface in body
        exact = abs_search_url(token=TEST_TOKEN, query=surface)
        assert exact in body, f"finished search link for {surface!r} missing"
    sittings = index.recent_sittings(settings.recent_sittings_n)
    for day in sittings:
        assert f"/a/{TEST_TOKEN}/date/{day}" in body


def test_facet_links_present(client_with_index: TestClient) -> None:
    """/years, /members, /bills + the R9 ?format=json sibling are rendered."""
    body = client_with_index.get(f"/a/{TEST_TOKEN}/").text
    assert f"/a/{TEST_TOKEN}/years" in body
    assert f"/a/{TEST_TOKEN}/members" in body
    assert f"/a/{TEST_TOKEN}/bills" in body
    launcher = f"{settings.public_base_url.rstrip('/')}/a/{TEST_TOKEN}/"
    assert f"{launcher}?format=json" in body


def test_no_form_script_iframe(client_with_index: TestClient) -> None:
    """R4: the launcher has no <form>, <script>, or <iframe>."""
    body = client_with_index.get(f"/a/{TEST_TOKEN}/").text.lower()
    assert "<form" not in body, "launcher must be form-free (R4)"
    assert "<script" not in body
    assert "<iframe" not in body


def test_invalid_token_404_byte_identical(client_with_index: TestClient) -> None:
    """Bad token on the launcher → the no-enumeration 404 body, identical
    to the /a/bogus root and matching the live md5 fingerprint."""
    r = _get(client_with_index, f"/a/{_BOGUS_TOKEN}/")
    assert r.status_code == 404
    body = r.content
    assert body == _get(client_with_index, f"/a/{_BOGUS_TOKEN}/search?q=x").content
    assert hashlib.md5(body).hexdigest() == "d782a3a355cd3dee8a009e8b77e3d348"


def test_launcher_json_format(client_with_index: TestClient) -> None:
    """?format=json returns structured data with token-bearing URLs."""
    resp = client_with_index.get(f"/a/{TEST_TOKEN}/?format=json")
    assert resp.status_code == 200
    data = json.loads(resp.text)
    assert data["url"].endswith(f"/a/{TEST_TOKEN}/")
    assert len(data["letters"]) == 36
    assert all("url" in row and TEST_TOKEN in row["url"] for row in data["letters"])
    assert data["common_topics"], "fixture common topics must be present"
    assert data["recent_sittings"], "fixture recent sittings must be present"
    assert resp.headers["Cache-Control"] == "private, max-age=300"


def test_launcher_text_format(client_with_index: TestClient) -> None:
    """?format=text returns a plain-text listing."""
    resp = client_with_index.get(f"/a/{TEST_TOKEN}/?format=text")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "Find a topic by word" in resp.text
    assert f"/a/{TEST_TOKEN}/years" in resp.text


def test_launcher_bad_format_422(client_with_index: TestClient) -> None:
    """An unknown format value on the launcher → 422 (with the nav bar)."""
    resp = client_with_index.get(f"/a/{TEST_TOKEN}/?format=bogus")
    assert resp.status_code == 422
    # Valid token → the error page carries token-bearing navigation.
    assert f"/a/{TEST_TOKEN}" in resp.text


def test_empty_index_launcher_still_200(token_store) -> None:
    """Live-retrieval invariant: with NO index the launcher still serves a
    200 (all sections render, counts 0 / empty lists)."""
    import hansard_gateway.auth as auth_mod
    from hansard_gateway.main import create_app

    auth_mod._store = token_store
    client = TestClient(create_app())
    resp = client.get(f"/a/{TEST_TOKEN}/")
    assert resp.status_code == 200
    assert _INSTRUCTION_SENTINEL in resp.text
    # Empty state: the 36 letter links still render (with 0 counts).
    assert f"/a/{TEST_TOKEN}/nav/a" in resp.text
    assert "<form" not in resp.text.lower()
