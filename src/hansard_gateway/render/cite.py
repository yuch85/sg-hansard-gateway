"""Citation cap rule + Cite-line context (Phase 27.3 Wave 1, G-A5-3/G-A6-2).

Every ``article.speech`` on a report page gains ``id="speech-{sequence}"`` and,
subject to the cap rule, one Cite line: a descriptive anchor
``speech N — {speaker}`` whose href is the absolute report URL + ``#speech-N``,
plus its verbatim ``.u`` twin (R2). Opening the Cite URL gold-highlights the
speech via CSS ``:target`` — per NAVIGATION, not sticky (G-A5-5: v1 sells
per-navigation; persistence would need JS, which R4 forbids).

THE CAP RULE (planner decision, state it so the gate can challenge it):
Cite lines render for the first N speeches where N = min(speech_count,
max(0, (PAGE_BUDGET_LINKS - B) // 2), max(0, (PAGE_BUDGET_BYTES - base) //
est_bytes_per_cite)), B = the page's measured non-Cite absolute-link count.
The division by 2 is deliberate headroom: each Cite adds ONE anchor AND one
~200-byte ``.u`` URL text, and the byte growth is the binding constraint on
long sittings, not the anchor count. A simpler "+1 per Cite" rule is rejected
for that reason. The rule is a PURE function of (speech_count, B, base,
budgets) — numerically deterministic over the WHOLE machine surface per
G-A6-2 (no randomness, no per-page judgment); the conformance assertion
recomputes the SAME function on real renders.

``speech-N`` is an ORDINAL, not a durable id (G-A6-3): acceptable for v1
because report content is immutable; noted here so a future re-parse never
silently renumbers citations.
"""

from __future__ import annotations

from typing import Optional

from hansard_gateway.models import HansardReport, Speech
from hansard_gateway.render.urls import abs_report_url

#: spec §7.1 per-page budgets (the caps the Cite lines must respect).
CITE_PAGE_BUDGET_LINKS: int = 400
CITE_PAGE_BUDGET_BYTES: int = 100 * 1024

#: CONSERVATIVE UPPER BOUND for the emitted Cite anchor + its .u echo
#: material (anchor text + href + a .u twin of a long token URL, calibrated
#: from the wireframe Cite line). It is headroom for the PRE-RENDER
#: arithmetic, not a measured per-line value: the conformance assertion
#: re-measures on real renders, and the final rendered-page <=100 KB linter
#: is the actual safety check that must hold regardless of the estimate.
CITE_EST_BYTES_PER_LINE: int = 220

#: Headroom divisor (each Cite = 1 anchor + 1 .u twin; the //2 keeps anchor
#: count AND twin-text byte growth inside both caps with margin).
CITE_CAP_DIVISOR: int = 2

#: The anchor text for a speech with no speaker name (procedural turn).
PROCEDURAL_LABEL: str = "[procedural]"


def cite_speech_limit(
    *,
    speech_count: int,
    non_cite_links: int,
    base_bytes: int,
    page_budget_links: int = CITE_PAGE_BUDGET_LINKS,
    page_budget_bytes: int = CITE_PAGE_BUDGET_BYTES,
    est_bytes_per_cite: int = CITE_EST_BYTES_PER_LINE,
) -> int:
    """How many speeches (the FIRST N in sequence order) get a Cite line.

    Pure, total, no I/O — the conformance assertion imports THIS function,
    never a re-implementation (G-A6-2 determinism).
    """
    link_branch = max(0, (page_budget_links - non_cite_links) // CITE_CAP_DIVISOR)
    byte_branch = max(0, (page_budget_bytes - base_bytes) // est_bytes_per_cite)
    return min(speech_count, link_branch, byte_branch)


def cite_label(*, sequence: int, speech: Speech) -> str:
    """The descriptive anchor text: ``speech N — {speaker}`` (R6-safe)."""
    speaker = speech.speaker_original or PROCEDURAL_LABEL
    return f"speech {sequence} — {speaker}"


def build_cite_context(
    *,
    report: HansardReport,
    token: str,
    non_cite_links: int,
    base_bytes: int,
) -> tuple[list[Optional[str]], list[Optional[str]]]:
    """Per-speech (cite_url, cite_label) pairs in sequence order.

    ``cite_url`` is the full absolute Cite URL (request token, R3) or ``None``
    for speeches past the cap — those keep their ``id="speech-N"`` but get no
    Cite line (the cap limits rendered Cite LINKS only; ids add zero links and
    keep every speech targetable by a human who knows the ordinal).
    """
    speeches = sorted(report.speeches, key=lambda s: s.sequence)
    limit = cite_speech_limit(
        speech_count=len(speeches),
        non_cite_links=non_cite_links,
        base_bytes=base_bytes,
    )
    urls: list[Optional[str]] = []
    labels: list[Optional[str]] = []
    for i, speech in enumerate(speeches):
        if i < limit:
            urls.append(
                abs_report_url(
                    token=token, link_id=report.report_id,
                    fragment=f"speech-{speech.sequence}",
                )
            )
            labels.append(cite_label(sequence=speech.sequence, speech=speech))
        else:
            urls.append(None)
            labels.append(None)
    return urls, labels
