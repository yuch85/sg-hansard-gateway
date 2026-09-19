"""Facet index routes (Phase 27.1 wave 2, spec §4.3) — split from
nav_routes.py for the 300-LOC rule.

Six index-only routes (quota-exempt, token-gated by the shared protected
router): /years, /year/{yyyy}, /members, /members/{letter}, /bills,
/bills/{letter}. Each serves html/json/text and carries the max-age=300
header; every link is an abs_*_url anchor + the R2 visible-URL twin; every
page has R8 escape-hatch links (parent level + launcher).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from typing import Optional

from fastapi import APIRouter, Depends, Response
from fastapi.responses import HTMLResponse, PlainTextResponse, Response as _Resp

from hansard_gateway import error_responses, render
from hansard_gateway.auth import AuthContext, require_capability_token
from hansard_gateway.config import settings
from hansard_gateway.index.loader import IndexService
from hansard_gateway.render.recovery_links import corrected_form_for_422
from hansard_gateway.render.urls import (
    abs_bills_url,
    abs_date_url,
    abs_format_sibling,
    abs_launcher_url,
    abs_members_url,
    abs_search_url,
    abs_year_url,
    abs_years_url,
)

#: Allowed ``format`` query values (spec §12); HTML is the default.
_FORMATS: frozenset[str] = frozenset({"html", "json", "text"})

#: A facet year: exactly 4 digits (spec §4.3).
_YEAR_RE = re.compile(r"^[0-9]{4}$")

#: A facet letter: exactly one lowercase letter (spec §4.3).
_LETTER_RE = re.compile(r"^[a-z]$")

#: The 26 member/bill letters (a-z only — digits are not member names).
_FACET_LETTERS: tuple[str, ...] = tuple(
    "abcdefghijklmnopqrstuvwxyz"
)

#: Response headers for the deterministic index-only pages (OQ1 split).
_INDEX_HEADERS: dict[str, str] = {
    "Cache-Control": "private, max-age=300",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow, noarchive",
}


@dataclass(frozen=True)
class _FacetData:
    """One facet page's data (shared by the six facet handlers)."""

    heading: str
    page_url: str
    entries: list[tuple]  # (label, url, count, count_label)
    nav: list[tuple]  # (label, url) escape-hatch links


def _validate_format(fmt: str) -> bool:
    """True when ``fmt`` is one of the allowed format values (R9)."""
    return fmt in _FORMATS


def _bad_request(
    *, fmt: str, token: str,
    correction_url: Optional[str] = None,
    detail: Optional[str] = None,
) -> Response:
    """422 for invalid facet parameters (spec §6.3): the nav bar renders
    because the token is valid, plus the plain-language line and the
    inferable corrected-form link."""
    return error_responses.build_error_response(
        status=settings.http_unprocessable,
        code=error_responses.CODE_INVALID_PARAMETER,
        fmt="html" if fmt not in _FORMATS else fmt,
        token=token,
        correction_url=correction_url,
        detail=detail,
    )


def _facet_json(data: _FacetData) -> _Resp:
    """?format=json for a facet page: {heading, entries:[{label,url,count}]}
    — every url absolute and token-bearing (built by abs_* helpers)."""
    import json

    body = {
        "heading": data.heading,
        "entries": [
            {"label": e[0], "url": e[1], "count": e[2]}
            for e in data.entries
        ],
    }
    return _Resp(
        content=json.dumps(body, indent=2),
        media_type="application/json",
        headers=_INDEX_HEADERS,
    )


def _facet_text(data: _FacetData) -> _Resp:
    """?format=text for a facet page: one line per entry with its url."""
    lines = [data.heading, ""]
    for label, url, count, count_label in data.entries:
        suffix = f" — {count} {count_label}" if count is not None else ""
        lines.append(f"- {label}{suffix}")
        lines.append(f"  url: {url}")
    return PlainTextResponse("\n".join(lines), headers=_INDEX_HEADERS)


def _facet_html(data: _FacetData, *, token: str) -> _Resp:
    """HTML for a facet page via the shared facets.html template."""
    entries_ctx = [
        {"label": e[0], "url": e[1], "count": e[2], "count_label": e[3]}
        for e in data.entries
    ]
    nav_ctx = [{"label": n[0], "url": n[1]} for n in data.nav]
    body = render.render_facet(
        token=token, heading=data.heading, page_url=data.page_url,
        entries=entries_ctx, nav=nav_ctx,
    )
    return HTMLResponse(body, headers=_INDEX_HEADERS)


def _dispatch(fmt: str, *, data: _FacetData, token: str) -> Response:
    """Format dispatch for a facet page (html/json/text)."""
    if fmt == "json":
        return _facet_json(data)
    if fmt == "text":
        return _facet_text(data)
    return _facet_html(data, token=token)


def _letter_counts(index: IndexService, *, kind: str) -> dict[str, int]:
    """Per-letter counts (a-z) for a members/bills facet grid."""
    fn = index.members_letter if kind == "member" else index.bills_letter
    return {letter: len(fn(letter)) for letter in _FACET_LETTERS}


def register_facet_routes(
    protected: APIRouter, *, index: IndexService
) -> None:
    """Mount the six facet routes on the shared protected router (auth gate
    already applied; quota-exempt — no gate.admit)."""

    @protected.get("/years")
    async def facet_years(
        format: str = "html",
        ctx: AuthContext = Depends(require_capability_token),
    ) -> Response:
        """One link per year with sittings, newest first (spec §4.3)."""
        if not _validate_format(format):
            return _bad_request(fmt=format, token=ctx.token)
        entries = [
            (year, abs_year_url(token=ctx.token, year=year), n, "sittings")
            for year, n in index.years()
        ]
        data = _FacetData(
            heading="Browse by year",
            page_url=abs_years_url(token=ctx.token),
            entries=entries,
            nav=[("Back to the launcher", abs_launcher_url(token=ctx.token))],
        )
        return _dispatch(format, data=data, token=ctx.token)

    @protected.get("/year/{year}")
    async def facet_year(
        year: str,
        format: str = "html",
        ctx: AuthContext = Depends(require_capability_token),
    ) -> Response:
        """One link per sitting date in a year (spec §4.3). 4-digit year or
        422-with-navigation."""
        if not _validate_format(format) or not _YEAR_RE.match(year):
            correction, detail = corrected_form_for_422(
                token=ctx.token, route="year", param="year", value=year)
            return _bad_request(fmt=format, token=ctx.token,
                                correction_url=correction, detail=detail)
        entries = [
            (day, abs_date_url(token=ctx.token, day_iso=day), None, "")
            for day in index.sittings_in_year(year)
        ]
        data = _FacetData(
            heading=f"Sittings in {year}",
            page_url=abs_year_url(token=ctx.token, year=year),
            entries=entries,
            nav=[
                ("Back to years", abs_years_url(token=ctx.token)),
                ("Back to the launcher", abs_launcher_url(token=ctx.token)),
            ],
        )
        return _dispatch(format, data=data, token=ctx.token)

    @protected.get("/members")
    async def facet_members(
        format: str = "html",
        ctx: AuthContext = Depends(require_capability_token),
    ) -> Response:
        """A-Z letter links to /members/{letter} (spec §4.3)."""
        if not _validate_format(format):
            return _bad_request(fmt=format, token=ctx.token)
        counts = _letter_counts(index, kind="member")
        entries = [
            (letter, abs_members_url(token=ctx.token, letter=letter),
             counts[letter], "members")
            for letter in _FACET_LETTERS
        ]
        data = _FacetData(
            heading="Browse by member",
            page_url=abs_members_url(token=ctx.token),
            entries=entries,
            nav=[("Back to the launcher", abs_launcher_url(token=ctx.token))],
        )
        return _dispatch(format, data=data, token=ctx.token)

    @protected.get("/members/{letter}")
    async def facet_member(
        letter: str,
        format: str = "html",
        ctx: AuthContext = Depends(require_capability_token),
    ) -> Response:
        """Member links as finished ?speaker= search URLs (spec §4.3)."""
        if not _validate_format(format) or not _LETTER_RE.match(letter):
            correction, detail = corrected_form_for_422(
                token=ctx.token, route="letter", param="letter", value=letter)
            return _bad_request(fmt=format, token=ctx.token,
                                correction_url=correction, detail=detail)
        entries = [
            (name, abs_search_url(token=ctx.token, query="", speaker=name),
             n, "reports")
            for name, n in index.members_letter(letter)
        ]
        data = _FacetData(
            heading=f"Members starting with “{letter}”",
            page_url=abs_members_url(token=ctx.token, letter=letter),
            entries=entries,
            nav=[
                ("Back to members", abs_members_url(token=ctx.token)),
                ("Back to the launcher", abs_launcher_url(token=ctx.token)),
            ],
        )
        return _dispatch(format, data=data, token=ctx.token)

    @protected.get("/bills")
    async def facet_bills(
        format: str = "html",
        ctx: AuthContext = Depends(require_capability_token),
    ) -> Response:
        """A-Z letter links to /bills/{letter} (spec §4.3)."""
        if not _validate_format(format):
            return _bad_request(fmt=format, token=ctx.token)
        counts = _letter_counts(index, kind="bill")
        entries = [
            (letter, abs_bills_url(token=ctx.token, letter=letter),
             counts[letter], "bills")
            for letter in _FACET_LETTERS
        ]
        data = _FacetData(
            heading="Bills A-Z",
            page_url=abs_bills_url(token=ctx.token),
            entries=entries,
            nav=[("Back to the launcher", abs_launcher_url(token=ctx.token))],
        )
        return _dispatch(format, data=data, token=ctx.token)

    @protected.get("/bills/{letter}")
    async def facet_bill(
        letter: str,
        format: str = "html",
        ctx: AuthContext = Depends(require_capability_token),
    ) -> Response:
        """Bill links as finished ?q= search URLs (spec §4.3)."""
        if not _validate_format(format) or not _LETTER_RE.match(letter):
            correction, detail = corrected_form_for_422(
                token=ctx.token, route="bill", param="letter", value=letter)
            return _bad_request(fmt=format, token=ctx.token,
                                correction_url=correction, detail=detail)
        entries = [
            (surface, abs_search_url(token=ctx.token, query=surface),
             n, "reports")
            for surface, n in index.bills_letter(letter)
        ]
        data = _FacetData(
            heading=f"Bills starting with “{letter}”",
            page_url=abs_bills_url(token=ctx.token, letter=letter),
            entries=entries,
            nav=[
                ("Back to bills", abs_bills_url(token=ctx.token)),
                ("Back to the launcher", abs_launcher_url(token=ctx.token)),
            ],
        )
        return _dispatch(format, data=data, token=ctx.token)
