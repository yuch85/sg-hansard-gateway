"""sprs2 (pre-2012) topic parser — verbatim transcript extraction.

The pre-2012 upstream payload is a FULL HTML document in
``resultHTML.htmlContent`` (RESEARCH Finding 3, VERIFIED on the 2004 SAF
fixture). Speakers are delimited by ``<!--MP_NAME:...-->`` HTML comments; an
EMPTY comment means procedural text (``speaker_original=None``). Metadata comes
from the ``<meta>`` tags (``Parl_No``, ``Sess_No``, ``Vol_No``, ``Sit_Date``
(YYYY-MM-DD here), ``Sect_Name``, ``Title``, ``MP_Speak``, ``Start_Col``,
``End_Col``). The metadata table's ``Sitting No`` is read from the body table.

Verbatim integrity (spec §14, Pitfall 8): ``Column: N`` marks are KEPT in the
paragraph text — only whitespace is collapsed. A bold fallback covers very old
documents that lack MP_NAME comments.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date
from html.parser import HTMLParser
from typing import Any, Optional, Sequence

from bs4 import BeautifulSoup

from hansard_gateway.models import HansardReport, Speech
from hansard_gateway.sprs.payload import TopicPayload

#: Speaker-comment marker. ``HTMLParser.handle_comment`` delivers the comment
#: TEXT with the ``<!-- -->`` delimiters already stripped, so the regex anchors
#: on the plain marker text; an empty group means procedural text.
MP_NAME_RE = re.compile(r"^\s*MP_NAME:\s*([^<]*?)\s*$")

#: Meta-tag keys carrying report metadata (VERIFIED present, Finding 3).
META_TITLE = "Title"
META_SIT_DATE = "Sit_Date"
META_PARL_NO = "Parl_No"
META_SESS_NO = "Sess_No"
META_VOL_NO = "Vol_No"
META_SECT_NAME = "Sect_Name"
META_SITTING_NO = "Sitting No"
META_SITTING_NO_LABEL = "Sitting No:"

#: Bold/strong tags used by the fallback speaker walk for pre-comment docs.
_BOLD_TAGS = ("b", "strong")

#: Minimum segment length before a walk is considered substantive.
_MIN_SEGMENT_CHARS = 3


class _CommentSplitter(HTMLParser):
    """Walk a raw HTML fragment and collect MP_NAME-comment delimiters.

    ``handle_comment`` receives the comment TEXT (the ``<!-- -->`` delimiters
    are stripped), so ``MP_NAME_RE`` matches the plain marker text. This keeps
    the split deterministic on the raw bytes without re-serialising the parse
    tree (lxml can merge/drop comments on ``str(soup.body)``).
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._markers: list[tuple[int, int, str]] = []

    def handle_comment(self, data: str) -> None:
        """Record one MP_NAME marker with its position and speaker text."""
        match = MP_NAME_RE.match(data)
        if match is None:
            return
        line, col = self.getpos()
        self._markers.append((line, col, match.group(1).strip()))

    def markers(self) -> Sequence[tuple[int, int, str]]:
        """Return the recorded ``(line, speaker)`` markers in document order."""
        return self._markers


def _split_segments(html: str) -> list[tuple[Optional[str], str]]:
    """Split raw HTML into ``(speaker, segment_html)`` runs.

    Segments run from just after one MP_NAME comment to the start of the next.
    An empty speaker (``None``) is procedural text.
    """
    parser = _CommentSplitter()
    parser.feed(html)
    parser.close()
    markers = parser.markers()
    if not markers:
        return [(None, html)]

    # Rebuild absolute char offsets from (line, col) positions. HTMLParser's
    # getpos() line index is 1-based and the col points at the comment text
    # start (just after ``<!--``); the segment begins there (the ``MP_NAME:...``
    # text itself is not transcript).
    lines = html.splitlines(keepends=True)
    line_starts = [0]
    for line in lines[:-1]:
        line_starts.append(line_starts[-1] + len(line))

    def _offset(line: int, col: int) -> int:
        return line_starts[line - 1] + col

    segments: list[tuple[Optional[str], str]] = []
    for i, (line, col, speaker) in enumerate(markers):
        seg_start = _offset(line, col)
        next_line, next_col = (
            (markers[i + 1][0], markers[i + 1][1])
            if i + 1 < len(markers)
            else (None, None)
        )
        if next_line is not None:
            seg_end = _offset(next_line, next_col)
        else:
            seg_end = len(html)
        segments.append((speaker or None, html[seg_start:seg_end]))
    return segments


def _clean_text(fragment: str) -> str:
    """Collapse whitespace in a fragment but KEEP ``Column: N`` marks."""
    text = BeautifulSoup(fragment, "lxml").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


#: Tags whose text forms one transcript paragraph (F3 boundary restore).
#: Two eras: pre-2003 payloads use bare <br> line breaks (no block markup —
#: verified live 2026-09-21: the 1993 APPLICATION OF ENGLISH LAW BILL
#: payload is 22 KB, 0 <p> tags, 112 <br>); later payloads use <p> blocks
#: (the 2004 SAF fixture: 45 <p>, 0 <br>). <p> takes precedence when both
#: exist (the 2004-era docs never mix the two).
_LEGACY_PARA_TAGS: tuple[str, ...] = ("p", "br")


def _has_block_markup(fragment: str) -> bool:
    """True if the fragment carries <p> paragraph blocks (2004+ era)."""
    return "<p" in fragment


def _br_paragraph_blocks(fragment: str) -> list[str]:
    """Split a <br>-delimited fragment into cleaned paragraph blocks.

    Bare-<br> era payloads carry no block markup: each <br> is a paragraph
    boundary. The run of text between boundaries is cleaned with the same
    per-paragraph collapse as _clean_text; empty runs (&nbsp; spacers) are
    dropped. The leading ``MP_NAME:`` marker (an artifact of the segment
    slicing, not transcript) is stripped first — the same normalization
    _clean_text gets for free via the comment-based segment walk.
    """
    fragment = re.sub(r"^MP_NAME:\s*", "", fragment)
    soup = BeautifulSoup(fragment, "lxml")
    # The bare-<br> era wraps the transcript in full <html> markup (verified
    # live 2026-09-21: the 1993 payload carries <html><head>…<meta>…</head>
    # <body>…). Only the <body> subtree is transcript — walking the whole
    # soup would swallow <head> (and BeautifulSoup's get_text skips
    # <script>/<style> but not <meta>), so scope the walk to <body>.
    body = soup.body or soup
    blocks: list[str] = []
    current: list[str] = []

    def flush() -> None:
        text = re.sub(r"\s+", " ", " ".join(current)).strip()
        if text:
            blocks.append(text)
        current.clear()

    def walk(node: Any) -> None:
        for child in node.children:
            name = getattr(child, "name", None)
            if name == "br":
                flush()
            elif name is not None:
                walk(child)
            else:
                current.append(str(child))

    walk(body)
    flush()
    return blocks


def _paragraph_blocks(fragment: str) -> list[str]:
    """Split a segment into cleaned paragraph blocks (one per <p>).

    Each block is cleaned with the same per-paragraph collapse as
    ``_clean_text`` (strip entities, collapse whitespace). Empty blocks
    (&nbsp;-only spacers) are dropped. ``Column: N`` marks stay inside
    their paragraph (the <p align=left>Column: N</p> is its own block).
    """
    if _has_block_markup(fragment):
        soup = BeautifulSoup(fragment, "lxml")
        blocks: list[str] = []
        for tag_name in _LEGACY_PARA_TAGS:
            for block in soup.find_all(tag_name):
                text = re.sub(
                    r"\s+", " ", block.get_text(" ", strip=True)
                ).strip()
                if text:
                    blocks.append(text)
        return blocks
    return _br_paragraph_blocks(fragment)


def _bold_speaker_segments(html: str) -> list[tuple[Optional[str], str]]:
    """Fallback walk for pre-comment docs: split on bold/strong speaker tags.

    Returns raw HTML fragments (not cleaned text) so that ``_paragraph_blocks``
    can extract per-<p> boundaries during the F3 boundary restore.
    """
    soup = BeautifulSoup(html, "lxml")
    segments: list[tuple[Optional[str], str]] = []
    current: Optional[str] = None
    buf: list[str] = []

    def _flush() -> None:
        raw = " ".join(buf)
        if len(_clean_text(raw)) >= _MIN_SEGMENT_CHARS:
            segments.append((current, raw))

    for node in soup.descendants:
        name = getattr(node, "name", None)
        if name in _BOLD_TAGS:
            _flush()
            buf.clear()
            current = node.get_text(" ", strip=True).rstrip(":").strip()
        else:
            buf.append(str(node))
    _flush()
    return segments


def _sitting_no_from_table(html: str) -> Optional[str]:
    """Read the ``Sitting No`` value from the metadata body table."""
    soup = BeautifulSoup(html, "lxml")
    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        label = cells[0].get_text(" ", strip=True)
        if label == META_SITTING_NO_LABEL:
            return cells[1].get_text(" ", strip=True)
    return None


def _meta_map(soup: BeautifulSoup) -> dict[str, str]:
    """Collect ``name``→``content`` from the document's meta tags."""
    return {
        m.get("name"): (m.get("content") or "").strip()
        for m in soup.find_all("meta")
        if m.get("name")
    }


def _parse_sit_date(raw: str) -> date:
    """Parse the sprs2 ``Sit_Date`` (YYYY-MM-DD) into a ``date``."""
    return date.fromisoformat(raw)


def _transcript_sha256(speeches: Sequence[Speech]) -> str:
    """SHA-256 of the normalized transcript (deterministic join order)."""
    lines = [
        paragraph
        for speech in speeches
        for paragraph in speech.paragraphs
    ]
    normalized = "\n".join(lines)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def parse(
    payload: TopicPayload, *, report_id: str
) -> HansardReport:
    """Parse an sprs2 ``htmlContent`` payload into a populated HansardReport.

    Computes the verbatim ``transcript_sha256`` fingerprint (D-16) and sets
    ``source_url`` to the official SPRS record URL.
    """
    html = payload.html_content
    if not html:
        raise ValueError("sprs2 payload has no htmlContent")

    soup = BeautifulSoup(html, "lxml")
    meta = _meta_map(soup)

    segments = _split_segments(html)
    if not any(speaker is not None for _, speaker in segments):
        # No MP_NAME comments → very old doc; fall back to bold speaker walk.
        segments = _bold_speaker_segments(html)

    speeches: list[Speech] = []
    for speaker, fragment in segments:
        text = _clean_text(fragment)
        if len(text) < _MIN_SEGMENT_CHARS:
            continue
        paragraphs = _paragraph_blocks(fragment)
        if not paragraphs:
            paragraphs = [text]
        speeches.append(
            Speech(
                sequence=len(speeches) + 1,
                speaker_original=speaker,
                speaker_name=None,
                speaker_role=None,
                paragraphs=paragraphs,
            )
        )

    sit_date_raw = meta.get(META_SIT_DATE, "")
    try:
        sit_date = _parse_sit_date(sit_date_raw)
    except ValueError:
        sit_date = date.min

    report = HansardReport(
        report_id=report_id,
        date=sit_date,
        title=meta.get(META_TITLE, "").strip(),
        topic_type=meta.get(META_SECT_NAME, "") or None,
        source_url=payload.source_url,
        volume=meta.get(META_VOL_NO, "") or None,
        parliament_no=meta.get(META_PARL_NO, "") or None,
        session_no=meta.get(META_SESS_NO, "") or None,
        sitting_no=_sitting_no_from_table(html),
        speeches=speeches,
        transcript_sha256=_transcript_sha256(speeches),
    )
    return report
