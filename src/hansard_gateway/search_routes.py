"""Search + date TOC routes (Plan 04) — kept out of main.py for the 300-LOC limit.

Both routes are mounted on the SAME protected router as /report (Wave 3
carry-forward): auth gate -> rate gate -> cache -> providers -> render.
SPRS is authoritative; Pair augments discovery (spec §6). The date route is
TOC-only — one searchResult sweep, no inline topic fetches (D-05).

Provider/client/refine/response-helper bodies live in
:mod:`hansard_gateway.search.providers` (300-LOC split, Phase 27.1 wave 4).
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse

from hansard_gateway.auth import AuthContext, require_capability_token
from hansard_gateway import render
from hansard_gateway.config import Settings, settings
from hansard_gateway.openapi_schemas import date_responses, search_responses
from hansard_gateway.rate_limit import RateGate
from hansard_gateway.render import links
from hansard_gateway.render.recovery_links import corrected_form_for_422
from hansard_gateway.render import parse_iso_date
from hansard_gateway.search.providers import (
    _PAIR_NOTE,
    _PROTECTED_HEADERS,
    admission_response,
    bad_request,
    collected_end_note,
    format_response,
    merge_pair,
    run_search,
    search_refine,
    select_page,
    sweep_sitting,
    truncated_note,
    upstream_failed,
)
from hansard_gateway.search.boost import apply_exact_term_boost
from hansard_gateway.sprs.cache import ReportCache

#: Allowed ``format`` query values (spec §12); HTML is the default.
_FORMATS: frozenset[str] = frozenset({"html", "json", "text"})


def register_search_routes(
    protected: APIRouter, *, cfg: Settings, gate: RateGate,
    cache: ReportCache, index,
) -> None:
    """Mount /search and /date/{day} on the shared protected router."""

    @protected.get("/search", responses=search_responses())
    async def search(
        request: Request,
        q: str,
        from_: Optional[str] = None,
        to: Optional[str] = None,
        speaker: Optional[str] = None,
        page: int = settings.search_min_page,
        limit: int = settings.search_max_limit,
        format: str = "html",
        ctx: AuthContext = Depends(require_capability_token),
    ) -> Response:
        """Search Hansard for ``q``; filters ``from_``/``to``/``speaker``/
        ``page``/``limit``. Every result link is token-preserving."""
        limit = max(cfg.search_min_page, min(limit, cfg.search_max_limit))
        date_from, date_to = parse_iso_date(from_), parse_iso_date(to)
        if len(q) > cfg.search_max_query_len:
            return bad_request(fmt=format, token=ctx.token)
        if page < cfg.search_min_page:
            correction, detail = corrected_form_for_422(
                token=ctx.token, route="search", param="page", value=str(page),
                query=q, date_from=from_ or "", date_to=to or "",
                speaker=speaker or "")
            return bad_request(fmt=format, token=ctx.token,
                               correction_url=correction, detail=detail)
        bad_format = format not in _FORMATS
        if bad_format:
            correction, detail = corrected_form_for_422(
                token=ctx.token, route="search", param="format", value=format,
                query=q)
            return bad_request(fmt="html", token=ctx.token,
                               correction_url=correction, detail=detail)
        if (from_ and date_from is None) or (to and date_to is None):
            correction = detail = None
            if from_ and date_from is None:
                correction, detail = corrected_form_for_422(
                    token=ctx.token, route="search", param="from_",
                    value=from_, query=q, date_from=from_ or "",
                    date_to=to or "", speaker=speaker or "")
            elif to and date_to is None:
                correction, detail = corrected_form_for_422(
                    token=ctx.token, route="search", param="to",
                    value=to, query=q, date_from=from_ or "",
                    date_to=to or "", speaker=speaker or "")
            return bad_request(fmt=format, token=ctx.token,
                               correction_url=correction, detail=detail)

        admission = await gate.admit(token_label=ctx.token_label)
        if not admission.admitted:
            return admission_response(admission, fmt=format, token=ctx.token)

        # The cache key addresses the SWEEP, not the page (27.1-search-hop-
        # rectify): page selection is a render-time slice of the cached rows,
        # so a truncated cold sweep cached once answers every page of the
        # collected rows — a clicked ?page=N is a fast cache hit that never
        # re-sweeps (the click-only pagination invariant). A per-page memo
        # (search_page_key) skips the re-slice + re-render for repeats.
        key = cache.search_key(
            keyword=q,
            date_from=date_from.isoformat() if date_from else "",
            date_to=date_to.isoformat() if date_to else "",
            limit=limit)
        page_key = cache.search_page_key(sweep_key=key, page=page)
        cached_page = cache.get(key=page_key)
        rows_total = None
        if cached_page is not None:
            # Per-page memo hit: the (boosted, merged) page + its note are
            # pre-rendered — re-serve byte-identically (no boost recompute,
            # no upstream POSTs).
            sprs_page, _pair_page, page_note = cached_page
            request.scope["state"]["cache_hit"] = True
            if format in ("json", "text"):
                return format_response(
                    render.render_search_format(page=sprs_page, fmt=format,
                                                token=ctx.token), fmt=format)
            refine = search_refine(
                index=index, token=ctx.token, page=sprs_page,
                date_from=from_ or "", date_to=to or "", speaker=speaker or "")
            return HTMLResponse(
                render.render_search(page=sprs_page, token=ctx.token,
                                     page_note=page_note, refine=refine),
                headers=_PROTECTED_HEADERS)
        rows_total = cache.get(key=key)
        if rows_total is not None:
            rows, total, trunc = rows_total
            # WARM sweep: slice the cached rows for this page (no upstream
            # POSTs) and fall through to boost/merge/note + memo refresh.
            sprs_page = select_page(
                query=q, total=total, page=page, limit=limit, rows=rows,
                budget_truncated=trunc)
            pair_page = None
            request.scope["state"]["cache_hit"] = True
        else:
            # COLD path: the sweep is bounded to cfg.search_cold_page_budget
            # pages when the probed result set is large, so page 1 lands
            # inside the ~5s fetch budget of click-only LLM web tools (the
            # live HIB query cost 51-126 POSTs / 16-34s on a full sweep).
            # Small queries (probed total within the budget) sweep to
            # completion; the /date TOC sweep is unaffected (it does not use
            # run_search).
            sprs_page, pair_page, rows_total = await run_search(
                gate=gate, ctx=ctx, query=q, date_from=date_from,
                date_to=date_to, speaker=speaker, page=page, limit=limit,
                cold_page_budget=cfg.search_cold_page_budget)
            if sprs_page is None:
                return upstream_failed(format, ctx.token,
                                       "search upstream failed")
            cache.set(key=key, value=rows_total)
            request.scope["state"]["cache_hit"] = False

        # The raw (pre-boost, pre-pair) slice — the boost prepends index rows
        # and pair merges discovery hits, so the rendered page can be
        # non-empty even when the sweep collected nothing for this page.
        raw_page = sprs_page
        page_note = None
        if pair_page and pair_page.hits:
            sprs_page = merge_pair(sprs_page, pair_page)
            page_note = _PAIR_NOTE
        # F-4: pin the reports of an exact indexed term (e.g. "Health
        # Information Bill") to the top of the rendered page — the index is
        # metadata-only, so no transcript fetch is added.
        sprs_page, boost_note = apply_exact_term_boost(
            page=sprs_page, index=index, query=q)
        if boost_note:
            page_note = (
                f"{page_note} {boost_note}".strip() if page_note else boost_note)
        # Cold budget (27.1-search-hop-rectify): the honest note for the two
        # truncated-sweep shapes — page 1 built from a bounded sweep, or a
        # continuation page past the collected rows (empty raw slice).
        # `collected` = rows the sweep actually swept (the cached sweep is
        # (rows, probed_maxResult)).
        collected = len(rows_total[0]) if rows_total is not None else 0
        if raw_page.truncated and collected:
            if page == 1:
                note = truncated_note(collected=collected, total=sprs_page.total)
            elif not raw_page.hits:
                note = collected_end_note()
            else:
                note = None
            if note:
                page_note = (
                    f"{page_note} {note}".strip() if page_note else note)
        # Refresh the per-page memo AFTER boost/merge/note so a repeat of the
        # same page re-renders byte-identically from cache (the note is NOT
        # recomputed on the warm path — boost is a no-op on an already-boosted
        # page and the pair page is not re-fetched).
        cache.set(key=page_key, value=(sprs_page, pair_page, page_note))

        if format in ("json", "text"):
            return format_response(
                render.render_search_format(page=sprs_page, fmt=format,
                                            token=ctx.token), fmt=format)
        refine = search_refine(
            index=index, token=ctx.token, page=sprs_page,
            date_from=from_ or "", date_to=to or "", speaker=speaker or "")
        return HTMLResponse(
            render.render_search(page=sprs_page, token=ctx.token,
                                 page_note=page_note, refine=refine),
            headers=_PROTECTED_HEADERS)

    @protected.get("/date/{day}", responses=date_responses())
    async def date_toc(
        request: Request,
        day: str,
        format: str = "html",
        ctx: AuthContext = Depends(require_capability_token),
    ) -> Response:
        """The sitting TOC (``yyyy-mm-dd``): every report for that day."""
        parsed = parse_iso_date(day)
        bad_format = format not in _FORMATS
        if parsed is None or bad_format:
            correction = detail = None
            if bad_format:
                correction, detail = corrected_form_for_422(
                    token=ctx.token, route="date", param="format", value=format)
            else:
                correction, detail = corrected_form_for_422(
                    token=ctx.token, route="date", param="day", value=day)
            return bad_request(fmt="html" if bad_format else format,
                               token=ctx.token,
                               correction_url=correction, detail=detail)
        admission = await gate.admit(token_label=ctx.token_label)
        if not admission.admitted:
            return admission_response(admission, fmt=format, token=ctx.token)
        day_iso = parsed.isoformat()
        key = cache.search_key(
            keyword="", date_from=day_iso, date_to=day_iso,
            limit=settings.cache_maxsize)
        hits = cache.get(key=key)
        request.scope["state"]["cache_hit"] = hits is not None
        if hits is None:
            hits = await sweep_sitting(gate=gate, ctx=ctx, sitting=parsed)
            if hits is None:
                return upstream_failed(format, ctx.token, "sitting sweep failed")
            cache.set(key=key, value=hits)

        if format in ("json", "text"):
            return format_response(
                render.render_date_format(hits=hits, date_iso=day_iso,
                                          fmt=format, token=ctx.token),
                fmt=format)
        toc_nav = links.build_prev_next_sittings(
            token=ctx.token, day_iso=day_iso,
            sittings_around=index.sittings_around(day_iso))
        toc_nav.update(links.build_year_link(token=ctx.token, day_iso=day_iso))
        return HTMLResponse(
            render.render_date(hits=hits,
                               date_long=render.date_long(parsed),
                               token=ctx.token, day_iso=day_iso,
                               toc_nav=toc_nav),
            headers=_PROTECTED_HEADERS)
