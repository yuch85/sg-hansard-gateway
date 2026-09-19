"""Search providers + response helpers (split from search_routes.py,
300-LOC rule).

The SPRS (authoritative) + Pair (discovery) provider pair, the pair-client
builder, the format wrapper, the 422/429/upstream-failed response builders,
and the refine/recovery link-group assembly — everything the /search and
/date/{day} handlers delegate to. No route registration here;
:mod:`search_routes` keeps the handler bodies.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Optional

import httpx
from fastapi import Response
from fastapi.responses import PlainTextResponse

from hansard_gateway import error_responses
from hansard_gateway.auth import AuthContext
from hansard_gateway.config import settings
from hansard_gateway.models import SearchHit, SearchPage
from hansard_gateway.rate_limit import AdmissionResult, RateGate
from hansard_gateway.render import links
from hansard_gateway.render.recovery_links import (
    build_limit_links,
    build_zero_result_recovery,
)
from hansard_gateway.search.pair import PairSearchProvider
from hansard_gateway.search.sprs import SprsSearchProvider, normalize_row
from hansard_gateway.sprs.client import SprsClient, UpstreamError

#: The module logger (upstream failures are logged, never silent).
_LOG = logging.getLogger(__name__)

#: Response headers for protected (token-bearing) responses (addendum §11-13).
_PROTECTED_HEADERS: dict[str, str] = {
    "Cache-Control": "private, no-store",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow, noarchive",
}

#: Note shown when Pair discovery hits are merged into the SPRS page.
_PAIR_NOTE = "Pair discovery results included where available."

#: Honest note when the sweep stopped at the cold page budget (27.1-search-
#: hop-rectify): the result set is incomplete, and the rendered continuation
#: link paginates ONLY the rows collected — never a re-sweep.
_TRUNCATED_NOTE = (
    "Showing the first {n:,} of ~{total:,} estimated results — this result "
    "set is large, so the sweep stopped after the first page budget. "
    "Use the page links below to browse the rest of what was collected; "
    "refine the query to narrow it."
)

#: Honest note when a continuation page runs past the collected rows (the
#: partial sweep is cached under the same search key — see run_search).
_COLLECTED_END_NOTE = (
    "End of the collected results for this query — the upstream sweep was "
    "bounded to keep the first page fast. Return to the first page and "
    "refine the query to narrow the result set."
)

def format_response(body: str, *, fmt: str) -> Response:
    """Wrap a serialized body with the correct media type (spec §12)."""
    if fmt == "json":
        return Response(content=body, media_type="application/json",
                        headers=_PROTECTED_HEADERS)
    return PlainTextResponse(body, headers=_PROTECTED_HEADERS)


def merge_pair(sprs_page: SearchPage, pair_page: SearchPage) -> SearchPage:
    """Append Pair discovery hits to the authoritative SPRS page (deduped)."""
    merged = list(sprs_page.hits)
    seen = {h.link_id for h in merged}
    for hit in pair_page.hits:
        if hit.link_id not in seen:
            merged.append(hit)
            seen.add(hit.link_id)
    return SearchPage(
        query=sprs_page.query,
        total=sprs_page.total + len(merged) - len(sprs_page.hits),
        page=sprs_page.page,
        limit=sprs_page.limit,
        hits=merged,
        provider=sprs_page.provider,
        rendered_total=sprs_page.rendered_total,
    )


def select_page(
    *,
    query: str,
    total: int,
    page: int,
    limit: int,
    rows: list[dict[str, Any]],
    budget_truncated: bool = False,
) -> SearchPage:
    """Slice a cached sweep's rows into one SearchPage (render-time).

    The cache key is page-agnostic (27.1-search-hop-rectify): ``rendered_
    total`` = rows the sweep collected (F-4 honest count), ``total`` stays
    the probed estimate. A page past the collected rows yields empty hits
    (the route renders the "end of collected results" note). ``budget_
    truncated`` is only True when the cold page budget stopped the sweep
    early (a no-gain short-sweep keeps the legacy pagination)."""
    start = (page - 1) * limit
    hits = [normalize_row(row) for row in rows[start : start + limit]]
    return SearchPage(
        query=query, total=total, page=page, limit=limit, hits=hits,
        provider="sprs", rendered_total=len(rows),
        truncated=budget_truncated,
    )


def truncated_note(*, collected: int, total: int) -> str:
    """The F-4 honest note for a cold page built from a BOUNDED sweep.

    ``collected`` = rows actually swept (the honest N); ``total`` = the
    probed maxResult estimate (the honest ~T). Mirrors the
    "Showing N of ~T estimated" header semantics."""
    return _TRUNCATED_NOTE.format(n=collected, total=total)


def collected_end_note() -> str:
    """The note for a continuation page past the collected rows (no re-sweep)."""
    return _COLLECTED_END_NOTE


def pair_client() -> httpx.AsyncClient:
    """The Pair discovery client (browser-ID header, bounded timeouts).

    F-5 (wave 6): read/write capped at the per-attempt timeout — a stalled
    Pair degrades to an empty discovery page (T-27-19), never a hang."""
    headers = {"Content-Type": settings.upstream_content_type,
               "X-Browser-ID": settings.pair_browser_id}
    timeout = httpx.Timeout(
        connect=settings.connect_timeout_s,
        read=settings.upstream_attempt_timeout_s,
        write=settings.upstream_attempt_timeout_s,
        pool=settings.connect_timeout_s)
    return httpx.AsyncClient(headers=headers, timeout=timeout)


async def run_search(
    *, gate: RateGate, ctx: AuthContext, query: str, date_from: Optional[date],
    date_to: Optional[date], speaker: Optional[str], page: int, limit: int,
    cold_page_budget: Optional[int] = None,
) -> tuple[Optional[SearchPage], Optional[SearchPage],
           Optional[tuple[list[dict[str, Any]], int, bool]]]:
    """Run SPRS (authoritative) + Pair (discovery); returns (sprs_page,
    pair_page, sweep) where sweep = (deduped rows, probed maxResult,
    budget_truncated).

    The route caches the sweep under the page-agnostic search key so a
    clicked ?page=N is a fast cache hit that never re-sweeps (27.1-search-
    hop-rectify click-only pagination invariant). sprs_page is None on
    upstream failure (then sweep is None too); Pair never aborts the route
    (T-27-19); both clients close in ``finally``. ``cold_page_budget``:
    when set, a large result set (probed max > budget*page_size) stops
    after the budget pages; small queries sweep to completion; ``None`` =
    sweep to completion unconditionally (the /date TOC path).
    """
    sprs_client = SprsClient(settings=settings, gate=gate, token_label=ctx.token_label)
    pair_http = pair_client()
    try:
        sprs = SprsSearchProvider(settings=settings, client=sprs_client)
        pair = PairSearchProvider(settings=settings, client=pair_http)
        sprs_rows, sprs_total, sprs_trunc = await sprs.sweep_for(
            query=query, date_from=date_from, date_to=date_to,
            speaker=speaker, cold_budget=cold_page_budget)
        sprs_page = select_page(
            query=query, total=sprs_total, page=page, limit=limit,
            rows=sprs_rows, budget_truncated=sprs_trunc)
        pair_page = await pair.search(
            query=query, date_from=date_from, date_to=date_to,
            speaker=None, page=page, limit=limit)
        return sprs_page, pair_page, (sprs_rows, sprs_total, sprs_trunc)
    except UpstreamError as exc:
        _LOG.warning("search upstream failure", extra={"err": exc.detail})
        return None, None, None
    finally:
        await sprs_client.aclose()
        await pair_http.aclose()


async def sweep_sitting(
    *, gate: RateGate, ctx: AuthContext, sitting: date
) -> Optional[list[SearchHit]]:
    """One full dedupe sweep of a single sitting (D-05 TOC source)."""
    sprs_client = SprsClient(settings=settings, gate=gate, token_label=ctx.token_label)
    try:
        provider = SprsSearchProvider(settings=settings, client=sprs_client)
        page = await provider.search(query="", date_from=sitting,
                                     date_to=sitting, speaker=None,
                                     page=1, limit=settings.cache_maxsize)
        return page.hits
    except UpstreamError as exc:
        _LOG.warning("sitting sweep upstream failure", extra={"err": exc.detail})
        return None
    finally:
        await sprs_client.aclose()


def _speaker_counts(hits: list[SearchHit]) -> list[str]:
    """Distinct speakers among the hits, frequency-sorted (spec §4.4)."""
    counts: dict[str, int] = {}
    for hit in hits:
        if hit.speaker:
            counts[hit.speaker] = counts.get(hit.speaker, 0) + 1
    return [n for n, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def search_refine(
    *, index, token: str, page: SearchPage,
    date_from: str, date_to: str, speaker: str,
) -> dict[str, list[dict[str, str]]]:
    """The spec §4.4 link groups for /search (all from already-fetched data).

    Zero hits swap the refine groups for the spec §6.1 recovery sections
    (did-you-mean / broader / ladder entry) — never a bare 'no results'."""
    kw = dict(token=token, query=page.query, date_from=date_from,
              date_to=date_to, speaker=speaker)
    if not page.hits:
        recovery = build_zero_result_recovery(index=index, **kw)
        return {
            "did_you_mean": recovery["did_you_mean"],
            "broader": recovery["broader"],
            "ladder": recovery["ladder"],
            "limits": build_limit_links(
                page=1, current_limit=page.limit, **kw),
        }
    years = {h.date.isoformat()[:4] for h in page.hits if h.date}
    years |= {y for y, _n in index.years()}
    # Cold budget (27.1-search-hop-rectify): a TRUNCATED page's continuation
    # links must span only the rows actually collected (the partial sweep is
    # cached under the same key, so page N > collected/limit hits the honest
    # "end of collected results" note). A complete sweep (truncated=False)
    # paginates over the probed estimate as before — small queries and warm
    # rebuilds are unchanged.
    pagination_total = (
        page.rendered_total
        if page.truncated and page.rendered_total
        else page.total
    )
    return {
        "refine_date": links.build_refine_by_date(
            years_with_hits=sorted(years, reverse=True),
            today_year=datetime.now(timezone.utc).year, **kw),
        "refine_speaker": links.build_refine_by_speaker(
            speakers=_speaker_counts(page.hits), **kw),
        "related": links.build_related_terms(
            terms=index.terms_by_norm(), token=token, query=page.query),
        "pagination": links.build_pagination(
            page=page.page, limit=page.limit, total=pagination_total, **kw),
        "limits": build_limit_links(
            page=page.page, current_limit=page.limit, **kw),
    }


def bad_request(
    *, fmt: str = "html", token: str,
    correction_url: Optional[str] = None, detail: Optional[str] = None,
) -> Response:
    """422 for invalid search parameters (addendum §18), format-aware (R-2a).

    ``token`` is the REQUEST token (R3): the error page's nav links must be
    built from it, never a stored default. ``correction_url`` + ``detail``
    (spec §6.3) carry the inferable corrected form + plain-language line."""
    return error_responses.build_error_response(
        status=settings.http_unprocessable,
        code=error_responses.CODE_INVALID_PARAMETER, fmt=fmt, token=token,
        correction_url=correction_url, detail=detail)


def admission_response(
    admission: AdmissionResult, *, fmt: str = "html", token: str
) -> Response:
    """Rate-gate denial → 429/503 (spec §16/§17), format-aware (R-2a)."""
    return error_responses.build_error_response(
        status=admission.status,
        code=error_responses.CODE_RATE_LIMITED, fmt=fmt, token=token)


def upstream_failed(fmt: str, token: str, detail: str) -> Response:
    """The 502 for a failed search/sitting sweep (delegated to main)."""
    from hansard_gateway.main import _error_response

    exc = UpstreamError(status=settings.http_bad_gateway, detail=detail)
    return _error_response("", token, exc, fmt=fmt)



