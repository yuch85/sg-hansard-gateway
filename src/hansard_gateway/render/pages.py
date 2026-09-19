"""Navigation page renderers (Phase 27.1 wave 2) — split from render/__init__
for the 300-LOC rule.

Launcher (spec §4.1), prefix ladder (spec §4.2) and facet (spec §4.3) pages.
Every href is built by the abs_*_url helpers with the REQUEST token (R1/R3);
the templates never construct a URL. The R2 visible-URL twin (``<span class
="u">``) is emitted by the templates for every anchor.
"""

from __future__ import annotations

from hansard_gateway.index.queries import ALPHABET
from hansard_gateway.render.urls import (
    abs_bills_url,
    abs_date_url,
    abs_format_sibling,
    abs_launcher_url,
    abs_members_url,
    abs_nav_url,
    abs_search_url,
    abs_years_url,
)


def render_launcher(
    *,
    token: str,
    letter_counts: dict[str, int],
    common_terms: list[tuple[str, int]],
    recent_sittings: list[str],
) -> str:
    """Render the launcher departure board (spec §4.1) — form-free, all 8
    sections in order, every link absolute + token-bearing with the R2
    visible-URL twin."""
    launcher_url = abs_launcher_url(token=token)
    context = {
        "token": token,
        "letter_counts": letter_counts,
        "common_terms": common_terms,
        "recent_sittings": recent_sittings,
        "nav_url": lambda c: abs_nav_url(token=token, prefix=c),
        "search_url": lambda s: abs_search_url(token=token, query=s),
        "date_url": lambda d: abs_date_url(token=token, day_iso=d),
        "years_url": abs_years_url(token=token),
        "members_url": abs_members_url(token=token),
        "bills_url": abs_bills_url(token=token),
        "launcher_json_url": abs_format_sibling(url=launcher_url, fmt="json"),
        "alphabet": "".join(ALPHABET),
    }
    return _pkg_render("launcher.html", protected=True, **context)


def render_nav(*, page, token: str) -> str:
    """Render one prefix-ladder page (spec §4.2) — Blocks A/B/C with the
    depth-5 / cap rules applied by the route, every href built by the
    abs_*_url helpers with the request token (R1/R3) + the R2 visible-URL
    twin."""
    prefix = page.prefix
    page_url = abs_nav_url(token=token, prefix=prefix)
    json_url = abs_format_sibling(url=page_url, fmt="json")
    text_url = abs_format_sibling(url=page_url, fmt="text")
    context = {
        "token": token,
        "page": page,
        "terms": [
            {"surface": t[0], "kind": t[1], "doc_count": t[2],
             "first_date": t[3], "last_date": t[4]}
            for t in page.terms
        ],
        "children": [
            {"child": c[0], "n_terms": c[1], "grandchild": c[2],
             "gc_n_terms": c[3]}
            for c in page.children
        ],
        "truncated": page.truncated,
        "nav_url": lambda p: abs_nav_url(token=token, prefix=p),
        "search_url": lambda s: abs_search_url(token=token, query=s),
        "up_url": (
            abs_nav_url(token=token, prefix=prefix[:-1])
            if len(prefix) > 1 else None
        ),
        # F-3 (wave 6): the "letter index" escape hatch points at the
        # launcher (it IS the letter index); bare /a/{t}/nav does not exist.
        "letter_index_url": abs_launcher_url(token=token),
        "launcher_url": abs_launcher_url(token=token),
        "json_url": json_url,
        "text_url": text_url,
    }
    return _pkg_render("nav.html", protected=True, **context)


def render_facet(
    *,
    token: str,
    heading: str,
    page_url: str,
    entries: list[dict],
    nav: list[dict],
) -> str:
    """Render one facet index page (spec §4.3) via the shared facets.html —
    every entry is an abs_*_url anchor + the R2 visible-URL twin; ``nav``
    carries the R8 escape-hatch links (parent level + launcher)."""
    context = {
        "token": token,
        "heading": heading,
        "entries": entries,
        "nav": nav,
        "json_url": abs_format_sibling(url=page_url, fmt="json"),
        "text_url": abs_format_sibling(url=page_url, fmt="text"),
    }
    return _pkg_render("facets.html", protected=True, **context)

def _pkg_render(template: str, protected: bool, **context: object) -> str:
    """Defer to the package-level ``_render`` (avoids a circular import at
    module load; the package is fully initialised by call time)."""
    from hansard_gateway.render import _render

    return _render(template, protected, **context)
