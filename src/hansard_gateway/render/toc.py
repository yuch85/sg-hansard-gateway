"""Speaker-index TOC builder (Phase 27.3 Wave 2 — the report restyle).

Every RETAINED speech gains one in-page anchor (``#speech-N``) in the sticky
side TOC. The TOC is a MACHINE-SURFACE CHANGE (a6 amendment 5): it adds N
same-page ``#speech-N`` hrefs per report, where N = min(speech_count,
TOC_max) and TOC_max is the combined link+byte cap (the conformance test in
``tests/test_link_conformance.py`` proves it — G-A6-2 determinism).

In-page fragment hrefs carry no token (R3 applies to token-bearing links;
the TOC links are same-page navigation, the same class as any ordinary
anchor) and no ``.u`` twin is emitted for them (R2's echo requirement
applies to absolute token-bearing URLs — the full absolute Cite URL for the
same target already carries its ``.u`` twin on the Cite line).

The TOC entries are COPIES of source text, not new content (G-A8-2/G-A8-3):
the preview is a verbatim prefix of the speech's first paragraph, and the
speaker label is the speech's own ``speaker_original`` — the tag-strip delta
accounting classifies them as copies, and the persistent-speaker structural
invariant (the sticky bar IS the speech's real header row) proves zero
duplicated speaker text.
"""

from __future__ import annotations

from dataclasses import dataclass

from hansard_gateway.models import HansardReport, Speech
from hansard_gateway.render.cite import PROCEDURAL_LABEL

#: Preview length (chars) for a TOC entry's first-word line (G-A8-2: the
#: preview is a verbatim prefix of the first paragraph, truncated to ~60
#: chars at a word boundary).
TOC_PREVIEW_CHARS: int = 60

#: The Speaker-classification bound (a VISUAL heuristic, not a semantic
#: claim): a non-empty ``speaker_original`` that ends with "Speaker" and is
#: at most this long is the Speaker's procedural name (e.g. "Mr Speaker",
#: "Mr Deputy Speaker"); anything else is an MP turn.
TOC_MAX_SPEAKER_NAME_LEN: int = 12

#: CONSERVATIVE UPPER BOUND for one rendered TOC ``<li>`` element (turn
#: number + speaker label + preview + markup). Measurement source: the 14
#: real TOC li elements in
#: ~/alice/missions/010-hansard-gateway-ui/wireframe/report-wireframe.html
#: range 163-208 bytes (max 208), rounded UP to 260 to cover longer speaker
#: names, 2+ digit turn numbers, and full 60-char previews at production
#: markup. It is headroom for the PRE-RENDER allocation arithmetic, NOT a
#: measured per-entry value — the conformance test runs the allocation
#: function in-test and the final rendered-page <=100 KB linter assertion
#: is the actual fail-safe that must hold regardless of the estimate.
EST_BYTES_PER_TOC_ENTRY: int = 260

#: The "Speaker" word the procedural-name heuristic keys on.
_SPEAKER_WORD: str = "Speaker"

#: Speaker-class values (a VISUAL class — the text is never changed by it).
TOC_SPEAKER_CLASS_PROCEDURAL: str = "procedural"
TOC_SPEAKER_CLASS_SPEAKER: str = "speaker"
TOC_SPEAKER_CLASS_MP: str = "mp"


@dataclass(frozen=True)
class TocEntry:
    """One TOC row: an in-page anchor to a retained speech."""

    anchor: str
    sequence: int
    speaker_label: str
    preview: str
    speaker_class: str


def speaker_label(*, speech: Speech) -> str:
    """The ONE source for a speech's display name (TOC label AND the speech
    h3 — the two can never drift): the verbatim ``speaker_original`` or the
    procedural placeholder."""
    return speech.speaker_original or PROCEDURAL_LABEL


def classify_speaker(*, speaker_original: str | None) -> str:
    """The VISUAL speaker class (documented heuristic, not a semantic
    change to the text): no/blank name -> procedural; a short name ending in
    "Speaker" (the Speaker's procedural name) -> speaker; anything else ->
    mp."""
    name = (speaker_original or "").strip()
    if not name:
        return TOC_SPEAKER_CLASS_PROCEDURAL
    if (
        name.endswith(_SPEAKER_WORD)
        and len(name) <= TOC_MAX_SPEAKER_NAME_LEN
    ):
        return TOC_SPEAKER_CLASS_SPEAKER
    return TOC_SPEAKER_CLASS_MP


def _preview_of(paragraph: str) -> str:
    """Verbatim prefix of ``paragraph`` truncated to TOC_PREVIEW_CHARS at a
    word boundary (no mid-word cut; a short paragraph is used whole)."""
    if len(paragraph) <= TOC_PREVIEW_CHARS:
        return paragraph
    cut = paragraph.rfind(" ", 0, TOC_PREVIEW_CHARS + 1)
    if cut <= 0:
        cut = TOC_PREVIEW_CHARS
    return paragraph[:cut].rstrip()


def build_toc_entries(
    *,
    report: HansardReport,
    max_entries: int | None = None,
) -> list[TocEntry]:
    """The speaker-index TOC: one entry per RETAINED speech, in sequence
    order.

    ``max_entries`` (the CAPPED TOC, M3): when not None, returns the first
    ``min(speech_count, max_entries)`` entries in sequence order — the count
    is min(speech_count, TOC_max), NEVER an unconditional "one per speech"
    while a cap is in force. ``None`` returns one per speech.
    """
    speeches = sorted(report.speeches, key=lambda s: s.sequence)
    entries: list[TocEntry] = []
    for speech in speeches:
        if max_entries is not None and len(entries) >= max_entries:
            break
        first = speech.paragraphs[0] if speech.paragraphs else ""
        entries.append(
            TocEntry(
                anchor=f"speech-{speech.sequence}",
                sequence=speech.sequence,
                speaker_label=speaker_label(speech=speech),
                preview=_preview_of(first),
                speaker_class=classify_speaker(
                    speaker_original=speech.speaker_original
                ),
            )
        )
    return entries


def toc_max(
    *,
    speech_count: int,
    non_cite_links: int,
    base_bytes: int,
    cite_count: int,
    page_budget_links: int = 400,
    page_budget_bytes: int = 100 * 1024,
) -> int:
    """The combined LINK + BYTE cap on rendered TOC entries (M2/M3).

    The TOC is subject to BOTH caps — a page can pass the 400-link cap and
    still fail the 100 KB byte cap, so the allocation is the minimum of the
    two branches (plus the speech count itself):

    * link branch: TOC_max_links = max(0, PAGE_BUDGET_LINKS - B - Cite_count)
      where B = the page's measured non-Cite/TOC absolute-link count and
      Cite_count = the Wave-1 capped Cite line count (each Cite = one
      absolute anchor; each TOC entry = one in-page anchor; the linter's 400
      count covers ALL anchors).
    * byte branch: TOC_max_bytes = max(0, (PAGE_BUDGET_BYTES - base_bytes -
      Cite_count * CITE_EST_BYTES_PER_LINE) // EST_BYTES_PER_TOC_ENTRY) — the
      TOC entries add bytes the Wave-1 CITE estimate does not cover.

    Pure, total, no I/O — the conformance assertion imports THIS function
    (G-A6-2 determinism).
    """
    from hansard_gateway.render.cite import CITE_EST_BYTES_PER_LINE

    link_branch = max(0, page_budget_links - non_cite_links - cite_count)
    byte_headroom = (
        page_budget_bytes
        - base_bytes
        - cite_count * CITE_EST_BYTES_PER_LINE
    )
    byte_branch = max(0, byte_headroom // EST_BYTES_PER_TOC_ENTRY)
    return min(speech_count, link_branch, byte_branch)
