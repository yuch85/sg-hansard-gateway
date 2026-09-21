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

import re
from typing import Any, Optional
from urllib.parse import urlsplit

from hansard_gateway.models import HansardReport, Speech
from hansard_gateway.render.urls import abs_report_url

#: spec §7.1 per-page budgets (the caps the Cite lines must respect).
CITE_PAGE_BUDGET_LINKS: int = 400
CITE_PAGE_BUDGET_BYTES: int = 100 * 1024

#: WORST-CASE UPPER BOUND for one rendered Cite block
#: ``<p class="cite">Cite: <a href="URL">LABEL</a><span class="u">URL</span></p>``
#: (UTF-8 bytes, escaping INSIDE the bound — :func:`cite_label` truncates the
#: label to :data:`CITE_LABEL_MAX_CHARS` by construction). Derived O(1) from
#: the MAXIMUM ALLOWED dimensions of every variable component (v0.1.7 plan,
#: C8-2 — a demonstrable bound, not a per-report measurement; the preferred
#: exact-render approach was rejected as O(speech_count) renders per request):
#:
#: * token length <= :attr:`settings.token_max_len` (123) — enforced by the
#:   auth shape guard;
#: * report id <= :data:`CITE_REPORT_ID_MAX` (64) — a generous ceiling over the
#:   ~12-24-char actual ids, asserted by the test that no stored/generated id
#:   exceeds it (the bound is enforced, not assumed);
#: * fragment "speech-N" <= 10 chars (N <= :data:`CITE_SPEECH_MAX`);
#: * speaker label <= :data:`CITE_LABEL_MAX_CHARS` chars POST-ESCAPE (the
#:   truncation makes the bound hold by construction);
#: * base URL = :attr:`settings.public_base_url` (fixed deployment value).
#:
#: Computed worst case = 617 B (base URL 30 + path 14 + token 123 + id 64 +
#: fragment 11 ("speech-9999") + label 80 + fixed markup); 700 carries
#: headroom for base-URL whitespace/attribute variation. The 80 B label
#: component assumes the post-escape rendered length <= 80 B, which holds for
#: the name-shaped speaker text the corpus carries (ASCII — no escape
#: expansion; the label bound test pins this on every committed baseline and
#: on an over-long synthetic). Consequence (accepted): for common tokens
#: (~29 chars, ~250 B/line) the cap is ~2-3x more conservative -> FEWER HTML
#: Cite lines on long reports. Universal addressability is carried by the JSON
#: ``cite_url`` (c9), so the conservatism costs the AI nothing. The conformance
#: assertion re-measures on real renders, and the final rendered-page <=100 KB
#: linter is the actual safety check regardless of this bound.
CITE_WORST_BYTES_PER_LINE: int = 700

#: Pre-change estimate, retained ONLY for the machine-baseline byte_size delta
#: accounting (the historical Cite lines rendered under the 220 estimate are
#: pinned in the wave0 baselines). Do NOT use for new cap arithmetic —
#: :data:`CITE_WORST_BYTES_PER_LINE` replaces it.
CITE_EST_BYTES_PER_LINE: int = 220

#: The bound's component ceilings (documented so the in-test recomputation and
#: the code derive from ONE source each).
CITE_REPORT_ID_MAX: int = 64
CITE_SPEECH_MAX: int = 9999
CITE_LABEL_MAX_CHARS: int = 80

#: Headroom divisor (each Cite = 1 anchor + 1 .u twin; the //2 keeps anchor
#: count AND twin-text byte growth inside both caps with margin).
CITE_CAP_DIVISOR: int = 2

# The procedural label + the shared label/class helpers now live in
# :mod:`.toc` (the ONE source for the TOC label AND the speech h3 — they
# can never drift); re-exported here for existing imports.
from hansard_gateway.render.toc import (  # noqa: E402,F401
    PROCEDURAL_LABEL,
    classify_speaker,
    speaker_label,
)


def cite_speech_limit(
    *,
    speech_count: int,
    non_cite_links: int,
    base_bytes: int,
    page_budget_links: int = CITE_PAGE_BUDGET_LINKS,
    page_budget_bytes: int = CITE_PAGE_BUDGET_BYTES,
    est_bytes_per_cite: int = CITE_WORST_BYTES_PER_LINE,
) -> int:
    """How many speeches (the FIRST N in sequence order) get a Cite line.

    Pure, total, no I/O — the conformance assertion imports THIS function,
    never a re-implementation (G-A6-2 determinism).
    """
    link_branch = max(0, (page_budget_links - non_cite_links) // CITE_CAP_DIVISOR)
    byte_branch = max(0, (page_budget_bytes - base_bytes) // est_bytes_per_cite)
    return min(speech_count, link_branch, byte_branch)


def measure_non_cite_page(
    *,
    template: str,
    token: str,
    context: dict[str, Any],
) -> tuple[int, int]:
    """Render a report template WITHOUT Cite lines and measure it.

    Returns ``(non_cite_absolute_link_count, byte_size)`` — the two inputs the
    cap rule needs (G-A6-2: the cap is computed from the WHOLE page's measured
    surface, not just the speech count). Rendered through the same Jinja
    environment the production render uses (``render._env``/shared base
    context), so the measurement is of the real pre-Cite page.
    """
    from hansard_gateway.render import _env, _nav_context, _PROTECTED_ROBOTS, _REFERRER_POLICY
    from hansard_gateway.config import settings

    env = _env()
    context = dict(context)
    # The pre-Cite measurement render must see the SAME shared context the
    # final render does (Wave-2 restyle defaults: TOC context + the label
    # helpers report.html consumes) — otherwise the measured base page is not
    # the real pre-Cite page.
    context.setdefault("toc_entries", [])
    context.setdefault("toc_dropped_count", 0)
    context.setdefault("speaker_label", speaker_label)
    context.setdefault("classify_speaker", classify_speaker)
    html = env.get_template(template).render(
        robots=_PROTECTED_ROBOTS,
        referrer=_REFERRER_POLICY,
        base=settings.public_base_url.rstrip("/"),
        token=token,
        **_nav_context(token),
        speech_cites=[],
        cite_note=False,
        **context,
    )
    count = 0
    for match in re.finditer(r"<a [^>]*href=[\"']([^\"']+)[\"']", html):
        href = match.group(1)
        parts = urlsplit(href)
        if (
            parts.scheme == "https"
            and parts.netloc
            and "/a/hg_" in href
        ):
            count += 1
    return count, len(html.encode("utf-8"))


def cite_label(*, sequence: int, speech: Speech) -> str:
    """The descriptive anchor text: ``speech N — {speaker}`` (R6-safe).

    The speaker part is truncated to :data:`CITE_LABEL_MAX_CHARS` (ellipsis,
    total INCLUDING the ellipsis) so the rendered label's post-escape UTF-8
    length is bounded BY CONSTRUCTION — :data:`CITE_WORST_BYTES_PER_LINE`
    assumes it and the test asserts it. The h3 / TOC label
    (:func:`toc.speaker_label`) is untouched: the bound applies to the Cite
    anchor text only.
    """
    speaker = speech.speaker_original or PROCEDURAL_LABEL
    if len(speaker) > CITE_LABEL_MAX_CHARS:
        speaker = speaker[: CITE_LABEL_MAX_CHARS - 1] + "…"
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
