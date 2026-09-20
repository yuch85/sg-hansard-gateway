"""Machine-surface extraction for the Hansard gateway (Phase 27.3).

ONE parser + extraction function, shared by the baseline capture tool
(``scripts/machine_baseline.py``) AND the offline regression test
(``tests/test_machine_baseline.py``). Extracting the rendered-HTML machine
surface is a single contract, so capture and regression must never diverge into
two parallel parsers (27.3-01 key_link).

The surface is the DOM-contract freeze: every ``href`` in document order, every
``span.u`` text, the href-to-``.u`` correspondence (R2), anchor texts, counts,
the nav-strip marker count (spec §5.1), the ``?format=json``/``?format=text``
sibling links (R9), and the page byte size (for the §7.1 cap arithmetic in
later waves).

Stdlib-only (``html.parser``) — mirrors
``tests/test_link_conformance.py:_AnchorParser`` but returns a richer,
ordered structure suitable for byte-stable JSON fixtures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Optional
from urllib.parse import urlsplit

#: The nav-strip marker label (spec §5.1, must appear twice: top + bottom).
NAV_MARKER: str = "Navigate:"

#: The two format-sibling query values that may appear as links (R9).
FORMAT_SIBLINGS: tuple[str, ...] = ("json", "text")


class _SurfaceParser(HTMLParser):
    """Collects every anchor (href + text + rel), every ``span.u`` text, in
    document order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[dict[str, str]] = []
        self._in_a = False
        self._a_href = ""
        self._a_rel = ""
        self._a_text = ""
        self.u_texts: list[str] = []
        self._in_u = False
        self._u_text = ""

    def handle_starttag(self, tag: str,
                        attrs: list[tuple[str, Optional[str]]]) -> None:
        d = dict(attrs)
        if tag == "a":
            self._in_a = True
            self._a_href = d.get("href", "")
            self._a_rel = d.get("rel", "")
            self._a_text = ""
        elif tag == "span" and d.get("class") == "u":
            self._in_u = True
            self._u_text = ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_a:
            self.anchors.append(
                {"href": self._a_href, "text": self._a_text.strip(),
                 "rel": self._a_rel}
            )
            self._in_a = False
        elif tag == "span" and self._in_u:
            self.u_texts.append(self._u_text.strip())
            self._in_u = False

    def handle_startendtag(self, tag: str,
                           attrs: list[tuple[str, Optional[str]]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._in_a:
            self._a_text += data
        if self._in_u:
            self._u_text += data


@dataclass
class MachineSurface:
    """The extracted machine surface of one rendered page.

    All list fields are in DOCUMENT ORDER (the order the DOM reader / an AI
    sees them), which is what the byte-stable baseline fixtures pin.
    """

    hrefs: list[str] = field(default_factory=list)
    anchor_texts: list[str] = field(default_factory=list)
    u_texts: list[str] = field(default_factory=list)
    correspondence: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    nav_strip: int = 0
    format_links: dict[str, Any] = field(default_factory=dict)
    byte_size: int = 0


def extract_surface(html: str) -> MachineSurface:
    """Parse rendered HTML into a :class:`MachineSurface`.

    ``correspondence`` lists, for each absolute (https) token-bearing href,
    whether a ``.u`` twin with the same URL exists and its index (``-1`` if
    absent). This is the R2 href<->.u correspondence the restyle must keep.
    In-page fragment hrefs (``#speech-N``) are captured in ``hrefs`` too; at
    freeze time there are none, but the schema holds them for later waves.
    """
    parser = _SurfaceParser()
    parser.feed(html)

    hrefs: list[str] = []
    anchor_texts: list[str] = []
    for a in parser.anchors:
        hrefs.append(a["href"])
        anchor_texts.append(a["text"])

    u_texts = list(parser.u_texts)
    u_index = {u: i for i, u in enumerate(u_texts)}

    correspondence: list[dict[str, Any]] = []
    abs_token_hrefs: list[str] = []
    for href in hrefs:
        parts = urlsplit(href)
        is_abs_https = parts.scheme == "https" and bool(parts.netloc)
        token_bearing = "/a/hg_" in href
        if is_abs_https and token_bearing:
            abs_token_hrefs.append(href)
            correspondence.append(
                {"href": href, "u_twin": href in u_index,
                 "u_index": u_index.get(href, -1)}
            )

    nav_strip = html.count(NAV_MARKER)

    format_links: dict[str, Any] = {fmt: [] for fmt in FORMAT_SIBLINGS}
    for href in hrefs:
        for fmt in FORMAT_SIBLINGS:
            if f"format={fmt}" in href:
                format_links[fmt].append(href)

    counts = {
        "total_anchors": len(hrefs),
        "absolute_anchors": len(abs_token_hrefs),
        "u_spans": len(u_texts),
    }

    return MachineSurface(
        hrefs=hrefs,
        anchor_texts=anchor_texts,
        u_texts=u_texts,
        correspondence=correspondence,
        counts=counts,
        nav_strip=nav_strip,
        format_links=format_links,
        byte_size=len(html.encode("utf-8")),
    )


def surface_to_dict(surface: MachineSurface) -> dict[str, Any]:
    """Serialize a :class:`MachineSurface` to a stable-ordered dict (for JSON).

    Key insertion order is fixed here (not sorted) so the JSON is stable and
    human-readable; the fields are exactly those a later wave diffs against.
    """
    return {
        "hrefs": list(surface.hrefs),
        "anchor_texts": list(surface.anchor_texts),
        "u_texts": list(surface.u_texts),
        "correspondence": list(surface.correspondence),
        "counts": dict(surface.counts),
        "nav_strip": surface.nav_strip,
        "format_links": {k: list(v) for k, v in surface.format_links.items()},
        "byte_size": surface.byte_size,
    }


def redact_url(url: str) -> str:
    """Redact a single URL's token segment to the ``hg_…`` shape.

    Only the path segment following ``/a/`` is touched; the token is
    ``hg_`` + 43 chars, so it is replaced by the literal ``hg_…`` (never the
    plaintext). URLs without a token segment are returned unchanged.
    """
    if "/a/hg_" not in url:
        return url
    idx = url.index("/a/hg_")
    after = url[idx + len("/a/"):]
    _token, sep, tail = after.partition("/")
    return f"{url[:idx]}/a/hg_…{sep}{tail}" if sep else f"{url[:idx]}/a/hg_…"
