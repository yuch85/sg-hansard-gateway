"""Pair provider tests: real backend-API normalization + degraded path.

The spike (27-PAIR-SPIKE) confirmed the route is plain-httpx-replayable, so
this provider is REAL (D-04): it must send the X-Browser-ID header, parse the
201 response into the shared SearchPage model, strip trailing # from post-2012
ids, and degrade to a logged (not silent) empty page on any failure.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from hansard_gateway.config import Settings
from hansard_gateway.search.pair import PairSearchProvider, parse_pair_date

PAIR_URL = "https://search.pair.gov.sg/api/v1/search"

#: Spike-confirmed sample response (truncated shape).
_PAIR_RESPONSE = {
    "id": "search_9d6fb4f8",
    "searchResults": [
        {
            "id": "006_19720323_S0002_T0013",
            "title": "SINGAPORE ARMED FORCES BILL",
            "url": "https://sprs.parl.gov.sg/search/#/topic?reportid=006_19720323_S0002_T0013",
            "snippet": "...",
            "date": "23 Mar 1972",
            "source": "hansard",
            "mpsSpeaking": ["Dr Ong Chit Chung (Jurong)"],
            "reportType": "Bills",
            "reportTypeEnum": "bills",
        },
        {
            "id": "bill-739#",
            "title": "WORKPLACE FAIRNESS BILL",
            "url": "https://sprs.parl.gov.sg/search/#/sprs3topic?reportid=bill-739#",
            "snippet": "second reading",
            "date": "7 Jan 2025",
            "source": "hansard",
            "mpsSpeaking": None,
            "reportType": "Bills",
            "reportTypeEnum": "bills",
        },
    ],
    "metadata": {"numberOfResults": 4262, "maxQueryWords": 32, "timing": 0.215},
}


def _make_provider(settings: Settings) -> tuple[PairSearchProvider, httpx.AsyncClient]:
    client = httpx.AsyncClient(
        headers={
            "Content-Type": settings.upstream_content_type,
            "X-Browser-ID": settings.pair_browser_id,
        },
    )
    return PairSearchProvider(settings=settings, client=client), client


async def test_pair_normalizes_backend_response() -> None:
    """A 201 Pair response normalizes into a SearchPage with stripped ids."""
    settings = Settings()
    provider, client = _make_provider(settings)
    try:
        with respx.mock(base_url="https://search.pair.gov.sg",
                        assert_all_called=False) as mock:
            route = mock.post("/api/v1/search").respond(json=_PAIR_RESPONSE, status_code=201)
            page = await provider.search(
                query="Singapore Armed Forces", date_from=None, date_to=None,
                speaker=None, page=1, limit=20,
            )
    finally:
        await client.aclose()
    assert route.called
    assert page.provider == "pair"
    assert page.total == 4262
    assert len(page.hits) == 2
    # post-2012 id loses its trailing # (27-PAIR-SPIKE §2.3 note).
    assert page.hits[1].report_id == "bill-739"
    assert page.hits[1].link_id == "bill-739"
    # pre-2012 id is spec-style and date parses from the human format.
    assert page.hits[0].link_id == "006_19720323_S0002_T0013"
    assert page.hits[0].date.isoformat() == "1972-03-23"
    assert page.hits[0].speaker == "Dr Ong Chit Chung (Jurong)"
    # X-Browser-ID header was sent (the spike's only auth gate).
    headers = {k.lower(): v for k, v in route.calls.last.request.headers.items()}
    assert "x-browser-id" in headers
    # No capability token anywhere upstream (addendum §19).
    body = route.calls.last.request.content.decode("utf-8")
    assert "hg_" not in body
    for value in headers.values():
        assert "hg_" not in value


async def test_pair_degrades_on_reject() -> None:
    """A 401 (header regression) yields a logged empty page, not a crash."""
    settings = Settings()
    provider, client = _make_provider(settings)
    try:
        with respx.mock(base_url="https://search.pair.gov.sg",
                        assert_all_called=False) as mock:
            mock.post("/api/v1/search").respond(
                json={"message": "Unauthorized", "statusCode": 401}, status_code=401
            )
            page = await provider.search(
                query="x", date_from=None, date_to=None,
                speaker=None, page=1, limit=20,
            )
    finally:
        await client.aclose()
    assert page.hits == []
    assert page.total == 0
    assert provider.unavailable_reason is not None


async def test_pair_degrades_on_transport_error() -> None:
    """A transport error yields a logged empty page (no silent failure)."""
    settings = Settings()
    provider, client = _make_provider(settings)
    try:
        with respx.mock(base_url="https://search.pair.gov.sg",
                        assert_all_called=False) as mock:
            route = mock.post("/api/v1/search")
            route.mock(side_effect=httpx.ConnectError("down"))
            page = await provider.search(
                query="x", date_from=None, date_to=None,
                speaker=None, page=1, limit=20,
            )
    finally:
        await client.aclose()
    assert page.hits == []
    assert provider.unavailable_reason is not None


async def test_pair_host_gated_by_allowlist() -> None:
    """With search.pair.gov.sg dropped from the allowlist, no request is sent."""
    settings = Settings(
        upstream_host_allowlist=("sprs.parl.gov.sg",),  # pair dropped
    )
    provider, client = _make_provider(settings)
    try:
        with respx.mock(base_url="https://search.pair.gov.sg",
                        assert_all_called=False) as mock:
            route = mock.post("/api/v1/search").respond(json=_PAIR_RESPONSE, status_code=201)
            page = await provider.search(
                query="x", date_from=None, date_to=None,
                speaker=None, page=1, limit=20,
            )
    finally:
        await client.aclose()
    assert route.call_count == 0
    assert page.hits == []
    assert provider.unavailable_reason is not None


def test_parse_pair_date_formats() -> None:
    """Both observed human date formats parse; garbage returns None."""
    assert parse_pair_date("23 Mar 1972").isoformat() == "1972-03-23"
    assert parse_pair_date("7 January 2025").isoformat() == "2025-01-07"
    assert parse_pair_date(None) is None
    assert parse_pair_date("not a date") is None


def test_pair_host_in_default_allowlist() -> None:
    """The default Settings allowlist still gates (not bypasses) the pair host."""
    settings = Settings()
    assert settings.pair_host in settings.upstream_host_allowlist
