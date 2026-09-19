"""Tests for the prefix-ladder route /a/{t}/nav/{prefix} (spec §4.2).

Covers: Blocks A/B/C, the 300-cap / 200-truncation / depth-5-all rules, the
fat-branch skip-level, 422-with-navigation (incl. the 5-char truncation
correction link), the ?format=json / ?format=text shapes, the max-age=300
header, and the invalid-token 404 (byte-identical, T-27.1-06/-09).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from hansard_gateway.auth import TEST_TOKEN
from hansard_gateway.config import Settings
from hansard_gateway.index.build import build_index
from hansard_gateway.index.loader import IndexService
from hansard_gateway.render.urls import (
    abs_launcher_url,
    abs_nav_url,
    abs_search_url,
)

#: The production invalid-token 404 fingerprint (no enumeration).
_BOGUS_TOKEN = "hg_invalidtoken00000000000000zz"

#: A surface that is unambiguously one term (not a substring of another).
_UNIQUE_SURFACE = "Health Information Bill (Amendment No. 2)"


def _base() -> str:
    from hansard_gateway.config import settings

    return settings.public_base_url.rstrip("/")


# --- Block A: terms -------------------------------------------------------


def test_nav_he_lists_health_information_bill(
    client_with_index: TestClient,
) -> None:
    """/nav/he lists the fixture bill as a finished search link (Block A)."""
    body = client_with_index.get(f"/a/{TEST_TOKEN}/nav/he").text
    assert "Health Information Bill (Amendment No. 2)" in body
    exact = abs_search_url(token=TEST_TOKEN, query=_UNIQUE_SURFACE)
    assert f'href="{exact}"' in body
    # R2: the URL is also printed as visible text.
    assert f'<span class="u">{exact}</span>' in body
    # Descriptive text (R6): kind + doc_count.
    assert "bill" in body and "reports" in body


def test_letter_index_is_a_valid_route(client_with_index: TestClient) -> None:
    """/nav (the letter index) is not a ladder page — the ladder route
    requires 1..5 chars. Starlette 404s the bare /nav (no such route)."""
    resp = client_with_index.get(f"/a/{TEST_TOKEN}/nav", follow_redirects=False)
    assert resp.status_code == 404


def test_nav_bar_az_link_is_live(client_with_index: TestClient) -> None:
    """F-3 (wave 6): the global nav bar's "Find a topic A-Z" anchor targets a
    live route. It points at the launcher (the letter index) — NOT the
    nonexistent bare /a/{t}/nav, which the pre-fix link 404'd on."""
    body = client_with_index.get(f"/a/{TEST_TOKEN}/nav/a").text
    launcher = abs_launcher_url(token=TEST_TOKEN)
    assert f'href="{launcher}">Find a topic A-Z</a>' in body
    # The anchor's target is a live 200 (the launcher).
    assert client_with_index.get(f"/a/{TEST_TOKEN}/").status_code == 200
    # The old broken target still does not exist as a route — the fix
    # re-pointed the link, it did not create a bare /nav page.
    assert client_with_index.get(f"/a/{TEST_TOKEN}/nav").status_code == 404


def test_depth5_renders_all_no_block_b(client_with_index: TestClient) -> None:
    """/nav/healt (depth 5): ALL terms render, no truncation line, no Block B."""
    body = client_with_index.get(f"/a/{TEST_TOKEN}/nav/healt").text
    assert "Narrow further" not in body, "Block B must be omitted at depth 5"
    assert "truncated" not in body.lower()
    assert _UNIQUE_SURFACE in body


def test_block_c_links(client_with_index: TestClient) -> None:
    """Block C: /nav/he has up/letter-index/launcher links; /nav/h has NO
    up-one-level link."""
    body_he = client_with_index.get(f"/a/{TEST_TOKEN}/nav/he").text
    assert f'href="{_base()}/a/{TEST_TOKEN}/nav/h"' in body_he
    # F-3 (wave 6): the letter-index escape hatch is the launcher (bare
    # /a/{t}/nav does not exist and 404s).
    assert f'href="{_base()}/a/{TEST_TOKEN}/" class' not in body_he
    assert f'href="{_base()}/a/{TEST_TOKEN}/">Back to the letter index</a>' in body_he
    assert f'href="{_base()}/a/{TEST_TOKEN}/"' in body_he

    body_h = client_with_index.get(f"/a/{TEST_TOKEN}/nav/h").text
    assert "Up one level" not in body_h, "depth 1 must omit the up link"


def test_block_b_child_links(client_with_index: TestClient) -> None:
    """Block B: /nav/h links to /nav/he with its term count."""
    body = client_with_index.get(f"/a/{TEST_TOKEN}/nav/h").text
    assert "Narrow further" in body
    assert f'href="{_base()}/a/{TEST_TOKEN}/nav/he"' in body
    # Count label: per-word child fan-out — 'he' covers the 4 Health* terms
    # (Health Information, Health Information Bill, its Amendment No. 2, and
    # the 'heal' prefix of the bill surface).
    assert re.search(r"he</a>\s*<span class=\"u\">[^<]*</span> — 4 topics", body)


def test_fat_branch_skip_level(tmp_path: Path, token_store) -> None:
    """A child over fat_branch_threshold renders its grandchild skip-link;
    an ordinary child does not. The threshold is a BUILD-time value
    (materialised into prefix_children), so two indexes are built: one at the
    default threshold (no skip) and one with a low threshold (skip renders)."""
    import hansard_gateway.auth as auth_mod
    from hansard_gateway.main import create_app

    auth_mod._store = token_store

    rows = [
        {"report_id": f"i{k}", "link_id": f"i{k}",
         "sitting_date": "2024-01-01",
         "title": f"Ivy Plant Care Topic {k}", "report_type": "oral-answer",
         "speaker": None}
        for k in range(60)
    ]

    def _app_with(index: IndexService) -> TestClient:
        return TestClient(create_app(index_override=index))

    target_default = tmp_path / "fat_default.db"
    build_index(rows, target_default)
    index_default = IndexService(path=target_default)
    try:
        tc = _app_with(index_default)
        body = tc.get(f"/a/{TEST_TOKEN}/nav/i").text
        # 'iv' holds 60 terms, far below the default 2000 -> NOT fat.
        assert "skip level" not in body
    finally:
        index_default.close()

    target_fat = tmp_path / "fat_low.db"
    build_index(rows, target_fat, settings=Settings(fat_branch_threshold=10))
    index_fat = IndexService(path=target_fat)
    try:
        tc = _app_with(index_fat)
        body = tc.get(f"/a/{TEST_TOKEN}/nav/i").text
        assert "skip level" in body, "fat child must render its grandchild"
        assert f'href="{_base()}/a/{TEST_TOKEN}/nav/ivy"' in body
    finally:
        index_fat.close()


# --- 422 with navigation --------------------------------------------------


def test_422_bad_prefix_with_nav(client_with_index: TestClient) -> None:
    """Uppercase / bad-char prefixes → 422 with the nav bar (valid token)."""
    for bad in ("HE", "he!a"):
        resp = client_with_index.get(f"/a/{TEST_TOKEN}/nav/{bad}")
        assert resp.status_code == 422, bad
        assert f"/a/{TEST_TOKEN}" in resp.text, "422 must keep navigation"


def test_422_six_chars_links_to_truncation(client_with_index: TestClient) -> None:
    """/nav/health (6 chars) → 422 + a finished link to /nav/healt."""
    resp = client_with_index.get(f"/a/{TEST_TOKEN}/nav/health")
    assert resp.status_code == 422
    trunc = abs_nav_url(token=TEST_TOKEN, prefix="healt")
    assert trunc in resp.text, "6-char 422 must carry the truncation link"
    # The truncation link is a real page.
    assert client_with_index.get(trunc).status_code == 200


def test_422_bad_format(client_with_index: TestClient) -> None:
    """Unknown format on a valid prefix → 422."""
    resp = client_with_index.get(f"/a/{TEST_TOKEN}/nav/he?format=bogus")
    assert resp.status_code == 422


# --- formats --------------------------------------------------------------


def test_nav_json_shape(client_with_index: TestClient) -> None:
    """?format=json: {prefix, total, terms, children} with absolute,
    token-bearing URLs."""
    resp = client_with_index.get(f"/a/{TEST_TOKEN}/nav/he?format=json")
    assert resp.status_code == 200
    data = json.loads(resp.text)
    assert set(data) == {"prefix", "total", "terms", "children"}
    assert data["prefix"] == "he"
    assert data["total"] == len(data["terms"])
    for term in data["terms"]:
        assert set(term) == {"surface", "kind", "doc_count", "search_url"}
        assert term["search_url"].startswith(_base())
        assert TEST_TOKEN in term["search_url"]
    for child in data["children"]:
        assert set(child) == {"prefix", "n_terms", "url"}
        assert child["url"].startswith(_base()) and TEST_TOKEN in child["url"]
    assert resp.headers["Cache-Control"] == "private, max-age=300"


def test_nav_text_shape(client_with_index: TestClient) -> None:
    """?format=text: a plain-text listing."""
    resp = client_with_index.get(f"/a/{TEST_TOKEN}/nav/he?format=text")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "Prefix: he" in resp.text
    assert "Narrow further:" in resp.text


# --- headers + no-enumeration --------------------------------------------


def test_ladder_headers(client_with_index: TestClient) -> None:
    """Ladder pages carry the OQ1 split headers (max-age=300, no-referrer,
    noindex)."""
    resp = client_with_index.get(f"/a/{TEST_TOKEN}/nav/he")
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "private, max-age=300"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert "noindex" in resp.headers["X-Robots-Tag"]


def test_ladder_no_form_script_iframe(client_with_index: TestClient) -> None:
    body = client_with_index.get(f"/a/{TEST_TOKEN}/nav/he").text.lower()
    assert "<form" not in body and "<script" not in body and "<iframe" not in body


def test_invalid_token_422_path_byte_identical(
    client_with_index: TestClient,
) -> None:
    """T-27.1-06/-09: an invalid token on /nav/he returns the byte-identical
    no-enumeration 404 (never the 422, which is valid-token-only)."""
    r = client_with_index.get(f"/a/{_BOGUS_TOKEN}/nav/he", follow_redirects=False)
    assert r.status_code == 404
    assert hashlib.md5(r.content).hexdigest() == "1a29cc1330d50031993c3cbcde2318d7"
    # Identical across paths.
    r2 = client_with_index.get(f"/a/{_BOGUS_TOKEN}/search?q=x", follow_redirects=False)
    assert r.content == r2.content
