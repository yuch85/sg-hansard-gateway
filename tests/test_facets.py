"""Tests for the six facet index routes (Phase 27.1 wave 2, spec §4.3).

Covers: /years, /year/{yyyy}, /members, /members/{letter}, /bills,
/bills/{letter} — all served from the local index (no upstream), finished
token-bearing links, 422-with-navigation on bad input, the max-age=300
header, and the form/script/iframe ban.
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from hansard_gateway.auth import TEST_TOKEN
from hansard_gateway.config import settings
from hansard_gateway.index.queries import ALPHABET

#: The 26 member/bill letters (a-z).
_LETTERS = tuple("abcdefghijklmnopqrstuvwxyz")

_BOGUS_TOKEN = "hg_invalidtoken00000000000000zz"


def _base() -> str:
    return settings.public_base_url.rstrip("/")


# --- /years + /year/{yyyy} -------------------------------------------------


def test_years_lists_fixture_years(client_with_index: TestClient) -> None:
    """/years lists each fixture year with a /year/{yyyy} link + count."""
    body = client_with_index.get(f"/a/{TEST_TOKEN}/years").text
    for year in ("1988", "2001", "2020", "2023", "2024", "2025"):
        assert f"/a/{TEST_TOKEN}/year/{year}" in body
    # R2 twin + count label.
    url = f"{_base()}/a/{TEST_TOKEN}/year/2025"
    assert f'href="{url}"' in body
    assert f'<span class="u">{url}</span>' in body
    assert "sittings" in body


def test_year_lists_sitting_dates(client_with_index: TestClient) -> None:
    """/year/1988 lists the fixture sitting date with a /date/ link."""
    body = client_with_index.get(f"/a/{TEST_TOKEN}/year/1988").text
    assert f"/a/{TEST_TOKEN}/date/1988-11-15" in body
    url = f"{_base()}/a/{TEST_TOKEN}/date/1988-11-15"
    assert f'href="{url}"' in body
    assert f'<span class="u">{url}</span>' in body


def test_years_and_year_json(client_with_index: TestClient) -> None:
    """?format=json on the year facets returns structured token-bearing data."""
    resp = client_with_index.get(f"/a/{TEST_TOKEN}/years?format=json")
    assert resp.status_code == 200
    data = json.loads(resp.text)
    assert data["heading"] == "Browse by year"
    assert data["entries"]
    for e in data["entries"]:
        assert e["url"].startswith(_base()) and TEST_TOKEN in e["url"]

    resp = client_with_index.get(f"/a/{TEST_TOKEN}/year/1988?format=text")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "Sittings in 1988" in resp.text


# --- /members + /members/{letter} -----------------------------------------


def test_members_lists_26_letter_links(client_with_index: TestClient) -> None:
    """/members lists exactly 26 a-z letter links with counts."""
    body = client_with_index.get(f"/a/{TEST_TOKEN}/members").text
    for letter in _LETTERS:
        assert f"/a/{TEST_TOKEN}/members/{letter}" in body, letter
    # Exactly 26 distinct letter links.
    links = set(re.findall(rf'/a/{TEST_TOKEN}/members/([a-z])"', body))
    assert links == set(_LETTERS), f"expected 26 letter links, got {sorted(links)}"
    assert "members" in body  # count label


def test_member_letter_finished_speaker_links(client_with_index: TestClient) -> None:
    """/members/t lists fixture members as finished ?speaker= search URLs."""
    import html as _html

    body = client_with_index.get(f"/a/{TEST_TOKEN}/members/t").text
    assert "Tan Chun Seng" in body
    # The finished URL query-decodes to speaker=Tan Chun Seng (the href is
    # HTML-escaped: & -> &amp;).
    m = re.search(r'href="([^"]*?/search\?[^"]*?speaker=[^"]*)"', body)
    assert m, "no ?speaker= finished link found"
    parsed = urlparse(_html.unescape(m.group(1)))
    qs = parse_qs(parsed.query)
    assert qs.get("speaker") == ["Tan Chun Seng"]
    assert TEST_TOKEN in parsed.path


def test_members_and_bills_letter_counts(client_with_index: TestClient, index) -> None:
    """The letter-grid counts match the index's per-letter term counts."""
    body = client_with_index.get(f"/a/{TEST_TOKEN}/members").text
    # 't' has one fixture member (Tan Chun Seng).
    t_url = f"{_base()}/a/{TEST_TOKEN}/members/t"
    assert re.search(
        r'href="' + re.escape(t_url) + r'".*?— 1 member', body, re.S
    )
    # 'a' has zero fixture members — still renders with count 0.
    a_url = f"{_base()}/a/{TEST_TOKEN}/members/a"
    assert re.search(
        r'href="' + re.escape(a_url) + r'".*?— 0 member', body, re.S
    )


# --- /bills + /bills/{letter} ---------------------------------------------


def test_bills_lists_26_letter_links(client_with_index: TestClient) -> None:
    """/bills lists exactly 26 a-z letter links with counts."""
    body = client_with_index.get(f"/a/{TEST_TOKEN}/bills").text
    for letter in _LETTERS:
        assert f"/a/{TEST_TOKEN}/bills/{letter}" in body, letter
    links = set(re.findall(rf'/a/{TEST_TOKEN}/bills/([a-z])"', body))
    assert links == set(_LETTERS)
    assert "bills" in body


def test_bill_letter_finished_q_links(client_with_index: TestClient) -> None:
    """/bills/h lists fixture bill terms as finished ?q= search URLs."""
    body = client_with_index.get(f"/a/{TEST_TOKEN}/bills/h").text
    assert "Health Information" in body
    m = re.search(r'href="([^"]*?/search\?q=[^"]*)"', body)
    assert m, "no ?q= finished link found"
    parsed = urlparse(m.group(1))
    qs = parse_qs(parsed.query)
    assert qs["q"][0] == "Health Information"
    assert TEST_TOKEN in parsed.path


# --- 422 with navigation ---------------------------------------------------


def test_422_bad_year(client_with_index: TestClient) -> None:
    """/year/99 (not 4 digits) → 422 with the nav bar (valid token)."""
    resp = client_with_index.get(f"/a/{TEST_TOKEN}/year/99")
    assert resp.status_code == 422
    assert f"/a/{TEST_TOKEN}" in resp.text


def test_422_bad_member_letter(client_with_index: TestClient) -> None:
    """/members/z1 (not a single letter) → 422 with the nav bar."""
    resp = client_with_index.get(f"/a/{TEST_TOKEN}/members/z1")
    assert resp.status_code == 422
    assert f"/a/{TEST_TOKEN}" in resp.text


def test_422_uppercase_bill_letter(client_with_index: TestClient) -> None:
    """/bills/H (uppercase) → 422 with the nav bar."""
    resp = client_with_index.get(f"/a/{TEST_TOKEN}/bills/H")
    assert resp.status_code == 422
    assert f"/a/{TEST_TOKEN}" in resp.text


def test_422_bad_format(client_with_index: TestClient) -> None:
    """Unknown format on a facet → 422."""
    resp = client_with_index.get(f"/a/{TEST_TOKEN}/years?format=bogus")
    assert resp.status_code == 422


# --- headers + R4 ban + escape hatch ---------------------------------------


def test_facet_headers_and_no_form(client_with_index: TestClient) -> None:
    """Every facet page carries max-age=300 + no form/script/iframe and has
    >=1 absolute token-bearing link (R8 escape hatch)."""
    paths = [
        f"/a/{TEST_TOKEN}/years",
        f"/a/{TEST_TOKEN}/year/2025",
        f"/a/{TEST_TOKEN}/members",
        f"/a/{TEST_TOKEN}/members/t",
        f"/a/{TEST_TOKEN}/bills",
        f"/a/{TEST_TOKEN}/bills/h",
    ]
    for path in paths:
        resp = client_with_index.get(path)
        assert resp.status_code == 200, path
        assert resp.headers["Cache-Control"] == "private, max-age=300", path
        assert resp.headers["Referrer-Policy"] == "no-referrer", path
        body = resp.text.lower()
        assert "<form" not in body, path
        assert "<script" not in body, path
        assert "<iframe" not in body, path
        # R8: at least one absolute token-bearing link.
        assert f'{_base()}/a/{TEST_TOKEN}' in resp.text, path


def test_invalid_token_404_byte_identical(client_with_index: TestClient) -> None:
    """An invalid token on a facet route → the byte-identical no-enumeration
    404 (T-27.1-06: routes inherit the router-level gate)."""
    import hashlib

    r = client_with_index.get(f"/a/{_BOGUS_TOKEN}/years", follow_redirects=False)
    assert r.status_code == 404
    assert hashlib.md5(r.content).hexdigest() == "d782a3a355cd3dee8a009e8b77e3d348"
