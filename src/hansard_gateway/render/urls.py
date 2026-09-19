"""Absolute token-bearing URL builders for the render layer (spec R1/R3/R5).

Every href on a gateway page MUST be built here — templates never interpolate
stored data into URLs. The base comes from ``settings.public_base_url`` and
the token from the REQUEST (never a stored default), which is what makes the
link-only ingress work: a page reached with token X contains only token-X
links (R3), and every link is fully qualified (R1) with values percent-
encoded (R5).
"""

from __future__ import annotations

from urllib.parse import quote

from hansard_gateway.config import settings


def _base() -> str:
    """The public base URL without a trailing slash (single source)."""
    return settings.public_base_url.rstrip("/")


def abs_report_url(*, token: str, link_id: str) -> str:
    """Absolute, token-bearing URL for one report (self-navigating link)."""
    return f"{_base()}/a/{token}/report/{quote(link_id, safe='')}"


def abs_search_url(
    *, token: str, query: str, date_from: str = "", date_to: str = "",
    speaker: str = "", fmt: str = "",
) -> str:
    """Absolute, token-bearing URL for a search (deterministic encoding)."""
    params = [("q", query)]
    if date_from:
        params.append(("from_", date_from))
    if date_to:
        params.append(("to", date_to))
    if speaker:
        params.append(("speaker", speaker))
    if fmt:
        params.append(("format", fmt))
    qs = "&".join(f"{k}={quote(v, safe='')}" for k, v in params)
    return f"{_base()}/a/{token}/search?{qs}"


def abs_date_url(*, token: str, day_iso: str, fmt: str = "") -> str:
    """Absolute, token-bearing URL for a sitting TOC."""
    suffix = f"?format={fmt}" if fmt else ""
    return f"{_base()}/a/{token}/date/{quote(day_iso, safe='')}{suffix}"


def abs_nav_url(*, token: str, prefix: str = "") -> str:
    """Absolute, token-bearing URL for a prefix-ladder page (spec §4.2).

    Empty prefix is the letter index (``.../nav``); otherwise the prefix is
    appended percent-encoded (``.../nav/he``).
    """
    if prefix:
        return f"{_base()}/a/{token}/nav/{quote(prefix, safe='')}"
    return f"{_base()}/a/{token}/nav"


def abs_years_url(*, token: str) -> str:
    """Absolute, token-bearing URL for the /years facet (spec §4.3)."""
    return f"{_base()}/a/{token}/years"


def abs_year_url(*, token: str, year: str) -> str:
    """Absolute, token-bearing URL for one /year/{yyyy} facet page."""
    return f"{_base()}/a/{token}/year/{quote(year, safe='')}"


def abs_members_url(*, token: str, letter: str = "") -> str:
    """Absolute, token-bearing URL for /members or /members/{letter}."""
    if letter:
        return f"{_base()}/a/{token}/members/{quote(letter, safe='')}"
    return f"{_base()}/a/{token}/members"


def abs_bills_url(*, token: str, letter: str = "") -> str:
    """Absolute, token-bearing URL for /bills or /bills/{letter}."""
    if letter:
        return f"{_base()}/a/{token}/bills/{quote(letter, safe='')}"
    return f"{_base()}/a/{token}/bills"


def abs_launcher_url(*, token: str, fmt: str = "") -> str:
    """Absolute, token-bearing URL for the launcher (``.../a/{t}/``)."""
    suffix = f"?format={fmt}" if fmt else ""
    return f"{_base()}/a/{token}/{suffix}"


def abs_format_sibling(*, url: str, fmt: str) -> str:
    """R9 format sibling: ``url`` re-rendered as ``?format={fmt}``.

    Appends ``?format=`` (or ``&format=`` when the url already carries a
    query) so the client never has to append the parameter itself.
    """
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}format={fmt}"
