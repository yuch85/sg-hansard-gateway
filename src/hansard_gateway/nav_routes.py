"""Launcher + prefix-ladder routes (Phase 27.1 wave 2, spec §4.1/§4.2).

Every route here reads ONLY ``app.state.index`` (the wave-1 IndexService) —
zero upstream calls, which is what lets the ladder survive SPRS 502/503
(spec §6.4). All routes are quota-exempt (no rate-gate admission call) but inherit
token gating from the shared protected router's dependency — the invalid-token
404 stays byte-identical because the gate rejects before any handler runs.

Index-only pages carry ``Cache-Control: private, max-age=300`` (the OQ1
header split; content routes keep no-store). The six facet routes live in
:mod:`facet_routes` (300-LOC split) and are mounted by the same main.py call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from fastapi import APIRouter, Depends, Response
from fastapi.responses import HTMLResponse, PlainTextResponse

from hansard_gateway import error_responses, render
from hansard_gateway.auth import AuthContext, require_capability_token
from hansard_gateway.render.recovery_links import corrected_form_for_422
from hansard_gateway.config import Settings, settings
from hansard_gateway.index.loader import IndexService
from hansard_gateway.render.urls import (
    abs_format_sibling,
    abs_launcher_url,
    abs_nav_url,
    abs_search_url,
)

#: Allowed ``format`` query values (spec §12); HTML is the default.
_FORMATS: frozenset[str] = frozenset({"html", "json", "text"})

#: A ladder prefix: 1..5 chars of [a-z0-9] (spec §4.2).
_PREFIX_RE = re.compile(r"^[a-z0-9]{1,5}$")

#: A prefix that is valid shape but longer than the ladder depth — the 422
#: page links to its 5-char truncation (spec §6.3).
_TRUNCATABLE_PREFIX_RE = re.compile(r"^[a-z0-9]{6,}$")

#: Response headers for the deterministic index-only pages (OQ1 split).
_INDEX_HEADERS: dict[str, str] = {
    "Cache-Control": "private, max-age=300",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow, noarchive",
}


@dataclass(frozen=True)
class _NavPage:
    """One ladder page's data, assembled per request (no page caching —
    links are token-bearing; query results are already cached by the index)."""

    prefix: str
    terms: list[tuple]
    children: list[tuple]
    truncated: bool


def _validate_format(fmt: str) -> bool:
    """True when ``fmt`` is one of the allowed format values (R9)."""
    return fmt in _FORMATS


def _bad_request(
    *, fmt: str, token: str,
    correction_url: Optional[str] = None,
    detail: Optional[str] = None,
) -> Response:
    """422 for invalid nav parameters (spec §6.3): the nav bar renders
    because the token is valid. ``correction_url`` (when inferable) is a
    finished link to the corrected form; ``detail`` the plain-language
    'what was wrong' line."""
    return error_responses.build_error_response(
        status=settings.http_unprocessable,
        code=error_responses.CODE_INVALID_PARAMETER,
        fmt="html" if fmt not in _FORMATS else fmt,
        token=token,
        correction_url=correction_url,
        detail=detail,
    )


def _assemble_nav(
    prefix: str, *, index: IndexService
) -> _NavPage:
    """Block A + B data for one ladder prefix, applying the cap rules:
    depth-5 renders ALL terms; otherwise cap 300 / truncate at 200."""
    terms = index.terms_for_prefix(prefix)
    depth_max = len(prefix) >= settings.ladder_depth
    # Block B is omitted at max depth (spec §4.2) — no children to show.
    children = [] if depth_max else index.children_for_prefix(prefix)
    truncated = (not depth_max) and len(terms) > settings.ladder_term_cap
    if truncated:
        terms = terms[: settings.ladder_truncate_at]
    return _NavPage(
        prefix=prefix, terms=list(terms),
        children=list(children), truncated=truncated,
    )


def _nav_json(page: _NavPage, *, token: str) -> str:
    """?format=json: {prefix, total, terms, children} — every *_url absolute
    and token-bearing (spec §4.2)."""
    import json

    data = {
        "prefix": page.prefix,
        "total": len(page.terms),
        "terms": [
            {
                "surface": t[0],
                "kind": t[1],
                "doc_count": t[2],
                "search_url": abs_search_url(token=token, query=t[0]),
            }
            for t in page.terms
        ],
        "children": [
            {
                "prefix": c[0],
                "n_terms": c[1],
                "url": abs_nav_url(token=token, prefix=c[0]),
            }
            for c in page.children
        ],
    }
    return json.dumps(data, indent=2)


def _nav_text(page: _NavPage, *, token: str) -> str:
    """?format=text: a plain-text listing (prefix, total, one line per term
    and child) with the finished URLs printed (R2 applies to HTML; text
    carries the same data)."""
    lines = [f"Prefix: {page.prefix}", f"Terms: {len(page.terms)}", ""]
    for surface, _kind, doc_count, _f, _l in page.terms:
        lines.append(f"- {surface} ({doc_count} reports)")
        lines.append(f"  url: {abs_search_url(token=token, query=surface)}")
    lines.append("")
    lines.append(f"Narrow further: {len(page.children)} children")
    for child, n_terms, _gc, _gc_n in page.children:
        lines.append(f"- {child} — {n_terms} topics")
        lines.append(f"  url: {abs_nav_url(token=token, prefix=child)}")
    return "\n".join(lines)


def register_nav_routes(
    protected: APIRouter, *, cfg: Settings, index: IndexService
) -> None:
    """Mount the launcher ("" and "/") and /nav/{prefix} on the shared
    protected router (auth gate already applied). The six facet routes are
    mounted separately by :func:`facet_routes.register_facet_routes`."""

    async def launcher_handler(
        format: str = "html",
        ctx: AuthContext = Depends(require_capability_token),
    ) -> Response:
        """The departure board (spec §4.1). Registered at BOTH "" and "/"
        so /a/{t} and /a/{t}/ return identical 200s with no redirect
        (RESEARCH OQ3 resolution)."""
        if not _validate_format(format):
            return _bad_request(fmt="html", token=ctx.token)
        letter_counts = index.letter_counts()
        common_terms = index.common_terms(cfg.common_topics_n)
        recent_sittings = index.recent_sittings(cfg.recent_sittings_n)
        if format == "json":
            return _launcher_json(
                ctx.token,
                letter_counts=letter_counts,
                common_terms=common_terms,
                recent_sittings=recent_sittings,
            )
        if format == "text":
            return _launcher_text(
                ctx.token,
                letter_counts=letter_counts,
                common_terms=common_terms,
                recent_sittings=recent_sittings,
            )
        return HTMLResponse(
            render.render_launcher(
                token=ctx.token,
                letter_counts=letter_counts,
                common_terms=common_terms,
                recent_sittings=recent_sittings,
            ),
            headers=_INDEX_HEADERS,
        )

    # Register the same handler at both path forms (no-redirect mechanism):
    # stacking the decorators would replace the function with a route object.
    protected.add_api_route(
        "", launcher_handler, methods=["GET"], response_class=HTMLResponse,
    )
    protected.add_api_route(
        "/", launcher_handler, methods=["GET"], response_class=HTMLResponse,
    )

    @protected.get("/nav/{prefix}")
    async def nav_ladder(
        prefix: str,
        format: str = "html",
        ctx: AuthContext = Depends(require_capability_token),
    ) -> Response:
        """The prefix ladder (spec §4.2) — Blocks A/B/C, depth-5 cap,
        fat-branch skip-level, 422-with-navigation. Index-only, no upstream."""
        if not _validate_format(format) or not _PREFIX_RE.match(prefix):
            correction, detail = corrected_form_for_422(
                token=ctx.token, route="nav", param="prefix", value=prefix)
            return _bad_request(fmt=format, token=ctx.token,
                                correction_url=correction, detail=detail)
        page = _assemble_nav(prefix, index=index)
        if format == "json":
            return Response(
                content=_nav_json(page, token=ctx.token),
                media_type="application/json",
                headers=_INDEX_HEADERS,
            )
        if format == "text":
            return PlainTextResponse(
                _nav_text(page, token=ctx.token), headers=_INDEX_HEADERS,
            )
        return HTMLResponse(
            render.render_nav(page=page, token=ctx.token),
            headers=_INDEX_HEADERS,
        )


def _launcher_json(
    token: str,
    *,
    letter_counts: dict[str, int],
    common_terms: list[tuple[str, int]],
    recent_sittings: list[str],
) -> Response:
    """The launcher as ?format=json (R9 sibling data, spec §4.1)."""
    import json

    from hansard_gateway.render.urls import abs_date_url

    data = {
        "url": abs_launcher_url(token=token),
        "letters": [
            {"prefix": c, "n_terms": letter_counts.get(c, 0),
             "url": abs_nav_url(token=token, prefix=c)}
            for c in letter_counts
        ],
        "common_topics": [
            {"surface": s, "doc_count": n,
             "search_url": abs_search_url(token=token, query=s)}
            for s, n in common_terms
        ],
        "recent_sittings": [
            {"date": d, "url": abs_date_url(token=token, day_iso=d)}
            for d in recent_sittings
        ],
        "facets": {
            "years": render.abs_years_url(token=token),
            "members": render.abs_members_url(token=token),
            "bills": render.abs_bills_url(token=token),
        },
    }
    return Response(
        content=json.dumps(data, indent=2),
        media_type="application/json",
        headers=_INDEX_HEADERS,
    )


def _launcher_text(
    token: str,
    *,
    letter_counts: dict[str, int],
    common_terms: list[tuple[str, int]],
    recent_sittings: list[str],
) -> Response:
    """The launcher as ?format=text (R9 sibling data, spec §4.1)."""
    lines = ["Singapore Hansard Gateway — navigation", ""]
    lines.append("Find a topic by word:")
    for c, n in letter_counts.items():
        lines.append(f"- {c} — {n} topics")
    lines.append("")
    lines.append("Common topics:")
    for surface, doc_count in common_terms:
        lines.append(f"- {surface} ({doc_count} reports)")
    lines.append("")
    lines.append("Recent sittings:")
    for day in recent_sittings:
        lines.append(f"- {day}")
    lines.append("")
    lines.append(
        f"Facets: years {render.abs_years_url(token=token)} "
        f"members {render.abs_members_url(token=token)} "
        f"bills {render.abs_bills_url(token=token)}"
    )
    return PlainTextResponse("\n".join(lines), headers=_INDEX_HEADERS)
