"""Zero-result recovery + 422 corrected-form link builders (spec §6,
Phase 27.1 wave 4).

Split from :mod:`links` for the 300-LOC rule. Same contract: pure,
keyword-only, absolute token-bearing URLs via the abs_*_url helpers (R1/R3/
R5); the caller passes index data so no new upstream calls are introduced
(T-27.1-13). No summarisation — every suggested surface is a stored term.
"""

from __future__ import annotations

import re
from typing import Optional

from hansard_gateway.index.extract import STOPWORDS, norm
from hansard_gateway.render.links import _abs_search_paged, _link
from hansard_gateway.render.urls import (
    abs_bills_url,
    abs_launcher_url,
    abs_members_url,
    abs_nav_url,
    abs_search_url,
    abs_years_url,
)

#: Zero-result ladder entry: the /nav/{prefix} depth (spec §6.1).
_LADDER_ENTRY_PREFIX_LEN = 3

#: The finished limit= links offered on every paginated /search (spec §6.5).
_LIMIT_OFFERS: tuple[int, ...] = (50, 100, 200)

#: The 36-way facet alphabet (a-z, 0-9) — a letter outside it is a 422.
_FACET_ALPHABET: frozenset[str] = frozenset(
    "abcdefghijklmnopqrstuvwxyz0123456789"
)

#: A facet year: exactly 4 digits (mirrors facet_routes._YEAR_RE).
_FACET_YEAR_RE = re.compile(r"^[0-9]{4}$")

#: A 4-digit year in the sane corpus range (Parliament sits 1955–present).
_FACET_YEAR_MIN = 1955
_FACET_YEAR_MAX = 2100


def build_zero_result_recovery(
    *,
    token: str,
    query: str,
    index,
    date_from: str = "",
    date_to: str = "",
    speaker: str = "",
) -> dict[str, list[dict[str, str]]]:
    """Zero-result recovery sections (spec §6.1) — all finished links:

    - ``did_you_mean``: up to ``settings.did_you_mean_n`` stored-term surfaces
      nearest to ``q`` (render/did_you_mean ranking), each a search link.
    - ``broader``: for a multi-word ``q``, one search link per individual
      word that exists in the term index.
    - ``ladder``: a /nav/{first 3 chars} link per word in ``q``.

    Reads only the stored term table (no upstream, no summarisation)."""
    from hansard_gateway.render.did_you_mean import suggest_terms
    from hansard_gateway.config import settings

    words = [w for w in norm(query).split(" ") if w and w not in STOPWORDS]
    did_you_mean = [
        _link(
            _abs_search_paged(
                token=token, query=surface, date_from=date_from,
                date_to=date_to, speaker=speaker,
            ),
            label=surface,
        )
        for surface, _doc in suggest_terms(
            query=query, index=index, limit=settings.did_you_mean_n
        )
    ]
    broader: list[dict[str, str]] = []
    if len(words) > 1:
        # A query word "exists in the term index" when it is a WORD OF any
        # stored term (not necessarily the whole term).
        index_words: set[str] = set()
        for _s, tnorm, _c in index.terms_by_norm():
            index_words.update(tnorm.split(" "))
        for word in words:
            if word in index_words:
                broader.append(_link(
                    _abs_search_paged(
                        token=token, query=word, date_from=date_from,
                        date_to=date_to, speaker=speaker,
                    ),
                    label=word,
                ))
    ladder = [
        _link(abs_nav_url(token=token, prefix=word[:_LADDER_ENTRY_PREFIX_LEN]),
              label=word)
        for word in words
    ]
    return {
        "did_you_mean": did_you_mean,
        "broader": broader,
        "ladder": ladder,
    }


def build_limit_links(
    *,
    token: str,
    query: str,
    date_from: str = "",
    date_to: str = "",
    speaker: str = "",
    page: int = 1,
    current_limit: int = 0,
) -> list[dict[str, str]]:
    """The finished limit=50/100/200 links (spec §6.5) — the client never
    types a limit. The current limit is labelled (it still links, so a
    link-only client can re-confirm it)."""
    out: list[dict[str, str]] = []
    for n in _LIMIT_OFFERS:
        url = _abs_search_paged(
            token=token, query=query, date_from=date_from, date_to=date_to,
            speaker=speaker, page=1 if n == current_limit else page,
            limit=n,
        )
        label = (
            f"Limit {n} per page (current)" if n == current_limit
            else f"Limit {n} per page"
        )
        out.append(_link(url, label=label))
    return out


def corrected_form_for_422(
    *,
    token: str,
    route: str,
    param: str,
    value: str,
    query: str = "",
    date_from: str = "",
    date_to: str = "",
    speaker: str = "",
) -> tuple[Optional[str], str]:
    """The spec §6.3 corrected-form link + plain-language line for an
    inferable 422. Returns (None, "") when no correction is inferable —
    the caller then renders the nav bar + line only.

    - ``nav``: a prefix longer than the ladder depth → its 5-char
      truncation; other invalid prefix shapes have no inferable form.
    - ``year``: any /year/{x} that is not a sane 4-digit year → /years.
    - ``letter`` / ``bill``: a facet letter outside the a–z, 0–9 alphabet →
      the owning index (/members or /bills).
    - ``search``: a bad page/limit → the same search with the offending
      parameter dropped; a bad format → the same search as html; a bad
      from_/to date → the same search without it.
    - ``date``: a bad format → the launcher; a bad day → no inferable form.
    """
    if route == "nav":
        if re.fullmatch(r"[a-z0-9]{6,}", value):
            from hansard_gateway.config import settings

            trunc = value[: settings.ladder_depth]
            return (
                abs_nav_url(token=token, prefix=trunc),
                f"“{value}” is longer than the 5-character ladder depth; "
                f"this link narrows to its first 5 characters.",
            )
        return (
            None,
            f"“{value}” is not a valid topic prefix (a–z, 0–9, up to 5 "
            f"characters).",
        )
    if route == "year":
        if not (_FACET_YEAR_RE.match(value)
                and _FACET_YEAR_MIN <= int(value) <= _FACET_YEAR_MAX):
            return (
                abs_years_url(token=token),
                f"“{value}” is not a valid year; pick one from the year "
                f"index instead.",
            )
        return (None, "")
    if route in ("letter", "bill"):
        if value not in _FACET_ALPHABET:
            base = (
                abs_bills_url(token=token) if route == "bill"
                else abs_members_url(token=token)
            )
            return (
                base,
                f"“{value}” is not a valid letter (a–z, 0–9); start from "
                f"the index instead.",
            )
        return (None, "")
    if route == "search":
        if param in ("page", "limit"):
            url = _abs_search_paged(
                token=token, query=query, date_from=date_from, date_to=date_to,
                speaker=speaker, page=1 if param == "page" else 0, limit=0,
            )
            return (
                url,
                f"“{value}” is not a valid {param}; this link reruns the "
                f"search without it.",
            )
        if param == "format":
            return (
                abs_search_url(token=token, query=query),
                f"“{value}” is not a valid format (html, json, text); "
                f"this link shows the html page.",
            )
        if param in ("from_", "to"):
            return (
                _abs_search_paged(
                    token=token, query=query,
                    date_from="" if param == "from_" else date_from,
                    date_to="" if param == "to" else date_to,
                    speaker=speaker,
                ),
                f"“{value}” is not a valid date (yyyy-mm-dd); this link "
                f"reruns the search without it.",
            )
        return (None, "")
    if route == "date":
        if param == "format":
            return (
                abs_launcher_url(token=token),
                f"“{value}” is not a valid format (html, json, text); "
                f"start from your home page.",
            )
        return (None, "")
    return (None, "")
