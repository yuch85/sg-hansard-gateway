"""Finished-URL link builders for the existing content routes (spec §4.4).

Pure, keyword-only, typed functions that return lists of
``{"url", "label"}`` dicts (or None-free nav dicts) of ABSOLUTE, token-
bearing URLs — every URL built by the abs_*_url helpers (R1/R3/R5) and
rendered with its R2 visible-URL twin by the templates. No route code, no
I/O: the caller passes index data (already fetched) and hit lists, so no new
upstream calls are introduced (T-27.1-13).

Bounded by design (T-27.1-14): related <= 20, refine speakers <= 20,
recent years <= 10, decades <= 4, pagination window <= 7 numbers — the
wave-3 conformance linter re-checks the page budgets per page.
"""

from __future__ import annotations

from typing import Any, Optional

from hansard_gateway.index.extract import STOPWORDS, norm
from hansard_gateway.render.urls import (
    abs_date_url,
    abs_report_url,
    abs_search_url,
    abs_year_url,
)

#: Refine-by-date: how many recent years (with hits) to offer (spec §4.4).
_RECENT_YEARS_N = 10

#: Refine-by-speaker: the top-N speakers among the current hits (spec §4.4).
_TOP_SPEAKERS_N = 20

#: Related topics: the maximum number of shared-word terms to offer.
_RELATED_TERMS_N = 20

#: Report title terms: the maximum number of finished search links (spec §4.4).
_TITLE_TERMS_N = 5

#: Pagination: the bounded numbered-link window centred on the current page.
_PAGE_WINDOW = 7

#: A decade label, e.g. '1990s'.
_DECADAL_LABEL = "{year}s"


def _link(url: str, label: str) -> dict[str, str]:
    """One finished link: absolute URL + descriptive label (R6)."""
    return {"url": url, "label": label}


def self_search_url(
    *, token: str, query: str, date_from: str = "", date_to: str = "",
    speaker: str = "", page: int = 1, limit: int = 50,
) -> str:
    """The absolute URL of the CURRENT search page (R9 format siblings)."""
    return _abs_search_paged(
        token=token, query=query, date_from=date_from, date_to=date_to,
        speaker=speaker, page=page, limit=limit,
    )


def _abs_search_paged(
    *, token: str, query: str, date_from: str = "", date_to: str = "",
    speaker: str = "", page: int = 1, limit: int = 0,
) -> str:
    """abs_search_url extended with page/limit (the client never bumps these
    itself — spec §6.5 — so the params must be in the rendered URL)."""
    url = abs_search_url(
        token=token, query=query, date_from=date_from, date_to=date_to,
        speaker=speaker,
    )
    params: list[tuple[str, str]] = []
    if page > 1:
        params.append(("page", str(page)))
    if limit:
        params.append(("limit", str(limit)))
    if not params:
        return url
    sep = "&" if "?" in url else "?"
    return url + sep + "&".join(f"{k}={v}" for k, v in params)


def build_refine_by_date(
    *, token: str, query: str, date_from: str = "", date_to: str = "",
    speaker: str = "", years_with_hits: list[str],
    today_year: int,
) -> list[dict[str, str]]:
    """Refine by date (spec §4.4): the SAME q re-run per decade present in
    the index and per year that has hits (bounded to the most recent
    ``_RECENT_YEARS_N``), most useful first (R7: recent years before
    decades). ``years_with_hits`` are 4-digit year strings, most recent
    first."""
    years = [y for y in years_with_hits if len(y) == 4 and y.isdigit()]
    links: list[dict[str, str]] = []
    recent = [
        y for y in years
        if today_year - _RECENT_YEARS_N < int(y) <= today_year
    ][:_RECENT_YEARS_N]
    older = [y for y in years if y not in recent]
    for year in recent + older:
        links.append(_link(
            _abs_search_paged(
                token=token, query=query, date_from=f"{year}-01-01",
                date_to=f"{year}-12-31", speaker=speaker,
            ),
            label=year,
        ))
    decades = sorted(
        {int(y) // 10 * 10 for y in years}, reverse=True,
    )
    for decade in decades:
        links.append(_link(
            _abs_search_paged(
                token=token, query=query,
                date_from=f"{decade}-01-01", date_to=f"{decade + 9}-12-31",
                speaker=speaker,
            ),
            label=_DECADAL_LABEL.format(year=decade),
        ))
    return links


def build_refine_by_speaker(
    *, token: str, query: str, date_from: str = "", date_to: str = "",
    speaker: str = "", speakers: list[str],
) -> list[dict[str, str]]:
    """Refine by speaker (spec §4.4): top-20 speakers among the current
    hits (already frequency-sorted by the caller), finished speaker= links."""
    return [
        _link(
            _abs_search_paged(
                token=token, query=query, date_from=date_from,
                date_to=date_to, speaker=name,
            ),
            label=name,
        )
        for name in speakers[:_TOP_SPEAKERS_N]
    ]


def build_related_terms(
    *, token: str, query: str, terms: list[tuple[str, str, int]],
) -> list[dict[str, str]]:
    """Related topics (spec §4.4 + F-4 tie-break): up to 20 index terms
    sharing a normalised word with the query.

    Ranking: terms whose norm contains ALL query words come first (the
    "exact surface" tier — e.g. 'Health Information Bill' for
    q='Health Information'), then terms sharing only some words; within a
    tier, doc_count (the ``terms`` rows arrive pre-sorted). The all-words
    tier is what stops a single high-volume word like 'bill' (25k docs)
    from flooding the section and burying the 4-doc bill surface (F-4).
    """
    query_words = {w for w in norm(query).split(" ") if w and w not in STOPWORDS}
    if not query_words:
        return []
    full: list[dict[str, str]] = []
    partial: list[dict[str, str]] = []
    for surface, tnorm, _doc_count in terms:
        words = set(tnorm.split(" "))
        if words & query_words:
            link = _link(abs_search_url(token=token, query=surface), label=surface)
            if query_words <= words:
                full.append(link)
            else:
                partial.append(link)
    return (full + partial)[:_RELATED_TERMS_N]


def build_pagination(
    *, token: str, query: str, date_from: str = "", date_to: str = "",
    speaker: str = "", page: int, limit: int, total: int,
) -> list[dict[str, str]]:
    """Pagination (spec §4.4 / §6.5): Previous / numbered / Next as finished
    URLs over a bounded window (the client never bumps page itself)."""
    if limit <= 0:
        limit = 1
    pages = max(1, -(-total // limit))
    if pages <= 1:
        return []
    links: list[dict[str, str]] = []

    def _page_link(n: int, label: str) -> None:
        links.append(_link(
            _abs_search_paged(
                token=token, query=query, date_from=date_from,
                date_to=date_to, speaker=speaker, page=n, limit=limit,
            ),
            label=label,
        ))

    if page > 1:
        _page_link(page - 1, "Previous")
    start = max(1, page - _PAGE_WINDOW // 2)
    end = min(pages, start + _PAGE_WINDOW - 1)
    start = max(1, end - _PAGE_WINDOW + 1)
    for n in range(start, end + 1):
        _page_link(n, str(n))
    if page < pages:
        _page_link(page + 1, "Next")
    return links


def build_prev_next_sittings(
    *, token: str, day_iso: str,
    sittings_around: tuple[Optional[str], Optional[str]],
) -> dict[str, Optional[str]]:
    """Previous/next sitting links for a TOC page (spec §4.4)."""
    prev_day, next_day = sittings_around
    return {
        "prev_sitting_url": (
            abs_date_url(token=token, day_iso=prev_day) if prev_day else None
        ),
        "prev_sitting_label": f"Sitting of {prev_day}" if prev_day else None,
        "next_sitting_url": (
            abs_date_url(token=token, day_iso=next_day) if next_day else None
        ),
        "next_sitting_label": f"Sitting of {next_day}" if next_day else None,
    }


def build_year_link(*, token: str, day_iso: str) -> dict[str, Optional[str]]:
    """The /year/{yyyy} link for a TOC page (spec §4.4)."""
    year = day_iso[:4]
    return {
        "year_url": abs_year_url(token=token, year=year),
        "year_label": f"All sittings of {year}",
    }


def build_report_nav(
    *, token: str, report_id: str, title: str, day_iso: str,
    reports_in_sitting: list[tuple[str, str]],
    terms: list[tuple[str, str, int]],
) -> dict[str, Any]:
    """Footer/nav links for one report (spec §4.4): sitting TOC link,
    previous/next report within the sitting (index ordering), up to 5
    title-term search links, and the sitting's report count."""
    nav: dict[str, Any] = {
        "prev_report_url": None,
        "prev_report_label": None,
        "next_report_url": None,
        "next_report_label": None,
        "sitting_url": abs_date_url(token=token, day_iso=day_iso),
        "title_terms": [],
    }
    try:
        pos = next(
            i for i, (link_id, _t) in enumerate(reports_in_sitting)
            if link_id == report_id
        )
    except StopIteration:
        return nav
    if pos > 0:
        prev_id, prev_title = reports_in_sitting[pos - 1]
        nav["prev_report_url"] = abs_report_url(token=token, link_id=prev_id)
        nav["prev_report_label"] = f"Previous: {prev_title}"
    if pos < len(reports_in_sitting) - 1:
        next_id, next_title = reports_in_sitting[pos + 1]
        nav["next_report_url"] = abs_report_url(token=token, link_id=next_id)
        nav["next_report_label"] = f"Next: {next_title}"
    title_words = [
        w for w in norm(title).split(" ") if w and w not in STOPWORDS
    ]
    for word in title_words[:_TITLE_TERMS_N]:
        match = next(
            (s for s, t, _c in terms if word in t.split(" ")), None
        )
        surface = match or word
        nav["title_terms"].append(_link(
            abs_search_url(token=token, query=surface), label=surface
        ))
    return nav
