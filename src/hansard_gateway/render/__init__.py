"""Jinja2 rendering layer — server-rendered HTML, autoescaping ON.

The transcript is verbatim upstream markup crossing into the response, so
``autoescape=True`` is mandatory (T-27-13 XSS mitigation). No third-party page
resources are loaded (lock: no third-party resources) — only an inline critical
CSS block.
Protected pages carry a ``noindex,nofollow,noarchive`` robots meta; public pages
do not.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

from hansard_gateway.models import HansardReport, SearchHit, SearchPage
from hansard_gateway.config import settings
# Absolute token-bearing URL builders live in :mod:`.urls` (spec R1/R3/R5);
# re-exported here so existing imports keep working (plan 27.1-02).
from hansard_gateway.render.urls import (  # noqa: F401
    abs_bills_url,
    abs_date_url,
    abs_format_sibling,
    abs_launcher_url,
    abs_members_url,
    abs_nav_url,
    abs_report_url,
    abs_search_url,
    abs_year_url,
    abs_years_url,
)
# Navigation page renderers (launcher/nav/facets) live in :mod:`.pages`;
# re-exported here so existing imports keep working (plan 27.1-02, 300-LOC split).
from hansard_gateway.render.pages import (  # noqa: F401
    render_facet,
    render_launcher,
    render_nav,
)

#: Directory holding the Jinja2 templates (sibling of this package file).
_TEMPLATE_DIR = Path(__file__).parent / "templates"

#: robots meta value for protected (token-bearing) pages (addendum §13).
_PROTECTED_ROBOTS = "noindex,nofollow,noarchive"

#: referrer meta value for every page (addendum §11).
_REFERRER_POLICY = "no-referrer"

#: The ISO timestamp format for the provenance "Retrieved" line (spec §13).
_RETRIEVED_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

#: The long form used for the report page's human-readable date.
_DATE_LONG = "%d %B %Y"


def _nav_context(token: Optional[str]) -> dict[str, Any]:
    """Global-nav-bar context (spec §5.1): five absolute token-bearing links.

    Empty for a tokenless page (the invalid-token 404 branch) — the include is
    additionally gated on ``token`` in base.html, so nothing leaks into the
    byte-identical 404 body (T-27.1-11).
    """
    if not token:
        return {}
    return {
        "nav_home_url": abs_launcher_url(token=token),
        # F-3 (wave 6): "Find a topic A-Z" points at the launcher — it IS the
        # letter index (the 36-letter block). The bare /a/{t}/nav path does
        # not exist (the ladder route is /nav/{prefix}, 1-5 chars only), so
        # the old target 404'd.
        "nav_index_url": abs_launcher_url(token=token),
        "nav_years_url": abs_years_url(token=token),
        "nav_members_url": abs_members_url(token=token),
        "nav_bills_url": abs_bills_url(token=token),
    }


def _env() -> Environment:
    """Build the Jinja2 environment (autoescaping on, file loader)."""
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=select_autoescape(
            enabled_extensions=("html", "htm", "xml"),
            default_for_string=True,
        ),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 Zulu string (provenance Retrieved)."""
    return datetime.now(timezone.utc).strftime(_RETRIEVED_FORMAT)


def date_long(day: date) -> str:
    """Human-readable long date (e.g. '19 October 2004')."""
    return day.strftime(_DATE_LONG)


def parse_iso_date(value: Optional[str]) -> Optional[date]:
    """Parse a yyyy-mm-dd query value (None when absent or malformed)."""
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _render(template: str, protected: bool, token: Optional[str] = None,
            **context: object) -> str:
    """Render a template with the shared base context injected."""
    env = _env()
    tmpl = env.get_template(template)
    return tmpl.render(
        robots=_PROTECTED_ROBOTS if protected else None,
        referrer=_REFERRER_POLICY,
        base=settings.public_base_url.rstrip("/"),
        token=token,
        **_nav_context(token),
        **context,
    )


def render_index() -> str:
    """Render the public home page (placeholder links only, no auth bypass)."""
    return _render("index.html", protected=False)


def render_about() -> str:
    """Render the public about page (provenance / not-affiliated, spec §25)."""
    return _render("about.html", protected=False)


def render_report(*, report: HansardReport, token: str, retrieved: str,
                  report_nav: Optional[dict[str, Any]] = None) -> str:
    """Render a report page (spec §5 contract) with token-preserving links."""
    self_url = abs_report_url(token=token, link_id=report.report_id)
    context: dict[str, Any] = {
        "report": report,
        "retrieved": retrieved,
        "date_long": date_long(report.date),
        "report_nav": report_nav or {},
        "sitting_url": abs_date_url(token=token, day_iso=report.date.isoformat()),
        "home_url": abs_launcher_url(token=token),
        "report_nav": report_nav or {},
        "json_url": abs_format_sibling(url=self_url, fmt="json"),
        "text_url": abs_format_sibling(url=self_url, fmt="text"),
    }
    return _render("report.html", protected=True, token=token, **context)


def results_line(*, page: SearchPage, hits: list[SearchHit]) -> str:
    """The honest results header (F-4).

    ``page.total`` is the upstream-observed total — an ESTIMATE (SPRS's
    load-balanced nodes disagree on totals; it is the max of per-page
    probes, not a true count). The exact number rendered is
    ``len(hits)``. The header therefore reads
    "Showing N of ~T estimated results" whenever N differs from T, and
    "Results: N" when they agree — no bare misleading number.
    """
    shown = len(hits)
    if page.total and page.total != shown:
        return f"Showing {shown} of ~{page.total:,} estimated results"
    return f"Results: {shown}"


def render_search(*, page: SearchPage, token: str, page_note: Optional[str] = None,
                  refine: Optional[dict[str, Any]] = None) -> str:
    """Render search results (spec §6) with token-preserving links."""
    from hansard_gateway.render.links import self_search_url

    self_url = self_search_url(
        token=token, query=page.query, date_from="", date_to="",
        speaker="", page=page.page, limit=page.limit,
    )
    context: dict[str, Any] = {
        "query": page.query,
        "total": page.total,
        "results_line": results_line(page=page, hits=page.hits),
        "hits": page.hits,
        "page_note": page_note,
        "refine": refine or {},
        "home_url": abs_launcher_url(token=token),
        "report_url": lambda link_id: abs_report_url(token=token, link_id=link_id),
        "self_url": self_url,
        "json_url": abs_format_sibling(url=self_url, fmt="json"),
        "text_url": abs_format_sibling(url=self_url, fmt="text"),
    }
    return _render("search.html", protected=True, token=token, **context)


def render_date(*, hits: list[SearchHit], date_long: str, token: str,
                query: str = "", day_iso: str = "",
                toc_nav: Optional[dict[str, Any]] = None) -> str:
    """Render a sitting TOC (spec §7) with token-preserving report links."""
    self_url = abs_date_url(token=token, day_iso=day_iso)
    context: dict[str, Any] = {
        "hits": hits,
        "date_long": date_long,
        "day_iso": day_iso,
        "query": query,
        "toc_nav": toc_nav or {},
        "home_url": abs_launcher_url(token=token),
        "report_url": lambda link_id: abs_report_url(token=token, link_id=link_id),
        "self_url": self_url,
        "json_url": abs_format_sibling(url=self_url, fmt="json"),
        "text_url": abs_format_sibling(url=self_url, fmt="text"),
    }
    return _render("date.html", protected=True, token=token, **context)


def render_error(*, report_id: str, token: Optional[str],
                 correction_url: Optional[str] = None,
                 detail: Optional[str] = None,
                 status: int = 0) -> str:
    """Render the two-branch error page (spec §6.2 — no fabrication).

    ``token=None`` renders the byte-identical production body (the
    no-enumeration 404). ``token=<real>`` renders the nav-bar branch with the
    plain-language ``detail`` line (422, spec §6.3), retry guidance
    (429/502/503/504, spec §6.4), and the ``correction_url`` finished link
    where inferable — all only for VALID-token errors."""
    return _render(
        "error.html",
        protected=token is not None,
        token=token,
        report_id=report_id,
        correction_url=correction_url,
        detail=detail,
        status=status,
    )


# The spec §12 format serializers (report / search / date JSON + text) live in
# :mod:`.formats` (300-LOC split, Phase 27.1 wave 6); re-exported here so
# existing ``from hansard_gateway.render import render_*_format`` keeps working.
from hansard_gateway.render.formats import (  # noqa: F401
    render_date_format,
    render_report_format,
    render_search_format,
)
