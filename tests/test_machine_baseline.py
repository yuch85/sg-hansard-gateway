"""Offline machine-surface regression gate (Phase 27.3).

Every later wave (Wave 1 Cite links, Wave 2 TOC/restyle, Wave 3/4 restyle) must
keep this green. It re-renders each corpus page with the in-process TestClient
(the committed fixture index + the same respx stubs as
``test_link_conformance.py``) and asserts the rendered machine surface against
the **IMMUTABLE Wave-0 baseline** at ``tests/fixtures/baselines/wave0/<entry>``.

The shared extraction is imported from ``scripts/machine_surface.py`` (ONE
parser for capture AND regression — 27.3-01 key_link), never re-implemented.

TWO-TIER SCOPE (the decision recorded in the wave0/README): the offline index
and the live index are DIFFERENT indexes, so the assertion scope splits per page
class:

* **6 offline entries** (launcher, nav/h, years, members, bills, date): the
  offline index DOES drive them → FULL machine-surface equality against the
  wave0 baseline + ``?format=text`` byte-equality against the text fixture.
  (The date entry is respx-stubbed with the committed searchResult fixture, the
  same stub the capture used.)
* **2 live entries** (report, search): the offline index CANNOT reproduce the
  live content → STRUCTURAL invariants only (same schema keys, nav_strip==2,
  every absolute token-bearing href has a `.u` twin, byte_size non-empty and
  under the 100 KB page budget, cap headroom: absolute + expected-wave-delta
  ≤ 400). Full equality for these two is asserted LIVE by
  ``scripts/machine_baseline.py --verify`` (not in the default pytest run — no
  live marker, no real network).

No live network in the default run: report/search are rendered offline under
respx stubs; only the structural invariants are checked, never a real fetch.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient

# The shared extraction lives in scripts/ (standalone, not a package). Put it
# on the path so the regression test uses the SAME parser as the capture tool
# (27.3-01 key_link: one parser, not two).
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from baseline_corpus import CORPUS, CorpusEntry, entry_path, text_path  # noqa: E402
from machine_surface import (  # noqa: E402
    extract_surface,
    redact_surface,
    surface_to_dict,
)

from hansard_gateway.auth import TEST_TOKEN  # noqa: E402

UPSTREAM_BASE = "https://sprs.parl.gov.sg/search"
PAIR_BASE = "https://search.pair.gov.sg"
E2E_REPORT_ID = "037_20041019_S0004_T0023"

#: The wave0 baseline dir (the IMMUTABLE cumulative reference — this test
#: always diffs against it, never baselines/wave<N>/).
WAVE0_DIR = Path(__file__).parent / "fixtures" / "baselines" / "wave0"
TEXT_DIR = Path(__file__).parent / "fixtures" / "text_format_baseline"

#: spec §7.1 link budget (cap arithmetic for the structural-invariant check).
PAGE_BUDGET_LINKS = 400

#: The Wave-1 expected delta on report pages (capped Cite lines) — the cap
#: headroom check asserts absolute + this stays under the 400-link budget.
WAVE1_REPORT_ABS_DELTA = 60

#: Wave-1 CSS delta (bytes of inline CSS added to base.html by the :target
#: rules + the production .u rule + .cite/.cite-note rules — the wave1
#: citation feature). Every page type inherits base.html, so the offline
#: byte_size equality accounts for this fixed additive delta; the per-page
#: Cite lines are report-only (the other offline entries have no speeches).
#: Measured: launcher 19144-18540 = nav 6626-6022 = ... = 604 bytes on every
#: offline page (identical, because the delta is the shared base <style>).
WAVE1_BASE_CSS_DELTA = 604

#: Wave-2 CSS delta (bytes of inline CSS added to base.html by the a8
#: wireframe system — the 27.3-03 restyle: palette variables, the 17rem
#: sticky-TOC grid, the sticky .sp-head persistent-speaker row, speaker
#: classes, the provenance block, the print block). Every page type
#: inherits base.html, so the offline byte_size equality accounts for this
#: fixed additive delta ON TOP OF the Wave-1 delta. Measured on the
#: launcher (the smallest offline page): rendered 22596 - wave0 18540 =
#: 4056 total (Wave-1 604 + Wave-2 3452). The Wave-2 delta is the rendered
#: CSS growth: 4927 (wave2 rendered CSS) - 1485 (wave1 rendered CSS) = 3442,
#: plus 10 bytes of template whitespace from the restructure.
WAVE2_BASE_CSS_DELTA = 3452

#: The surface fields compared for FULL equality on offline entries.
_EQUALITY_KEYS: tuple[str, ...] = (
    "hrefs", "u_texts", "anchor_texts", "correspondence",
    "counts", "nav_strip", "format_links", "byte_size",
)


def _search_fixture_rows() -> list[dict[str, Any]]:
    """The committed 2004 searchResult rows (offline ground truth)."""
    path = Path(__file__).parent / "fixtures" / "searchresult_20041019_p1.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _topic_fixture() -> dict[str, Any]:
    """The committed 2004 sprs2 topic payload (the offline E2E report)."""
    path = Path(__file__).parent / "fixtures" / "topic_20041019_saf.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _stub_search(mock: respx.MockRouter) -> None:
    mock.post("/searchResult").respond(json=_search_fixture_rows())


def _render(client: TestClient, entry: CorpusEntry) -> tuple[str, bytes]:
    """Render one corpus entry offline; return (html, text_bytes).

    The date TOC + search hit the upstream (respx-stubbed); the report hits
    ``getHansardTopic`` (respx-stubbed with the committed topic fixture).
    Purely index pages (launcher/nav/facet) need no stub.
    """
    route = f"/a/{TEST_TOKEN}{entry_path(entry)}"
    text_route = f"/a/{TEST_TOKEN}{text_path(entry)}"
    needs_search_stub = entry.name in ("search_hib_p1", "date_2026-01-12")
    needs_topic_stub = entry.name == "report_bill-774"
    if not (needs_search_stub or needs_topic_stub):
        return client.get(route).text, client.get(text_route).content

    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        if needs_search_stub:
            _stub_search(mock)
        if needs_topic_stub:
            mock.post("/getHansardTopic").respond(json=_topic_fixture())
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False,
                          assert_all_mocked=False)
        pair.start()
        try:
            html = client.get(route).text
            text_bytes = client.get(text_route).content
        finally:
            pair.stop()
    return html, text_bytes


def _load_baseline(entry: CorpusEntry) -> dict[str, Any]:
    return json.loads((WAVE0_DIR / f"{entry.name}.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Parametrized gate over the 8-entry corpus
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("entry", CORPUS, ids=[e.name for e in CORPUS])
def test_machine_surface_matches_wave0(
    client_with_index: TestClient, entry: CorpusEntry
) -> None:
    """The offline-rendered machine surface conforms to the Wave-0 baseline.

    Offline entries: FULL equality. Live entries: structural invariants (the
    offline index cannot reproduce their content; full equality is asserted
    live by ``machine_baseline.py --verify``).
    """
    html, _ = _render(client_with_index, entry)
    # The wave0 baselines store redacted URLs (hg_…); redact the rendered
    # surface the same way before comparing (offline render carries the real
    # offline token).
    surface = redact_surface(surface_to_dict(extract_surface(html)))
    baseline = _load_baseline(entry)

    if entry.source == "offline":
        for key in _EQUALITY_KEYS:
            if key == "byte_size":
                # Wave-1 + Wave-2 base.html CSS deltas (the :target + .u +
                # .cite rules + the a8 wireframe system) are inherited by
                # EVERY page type — the machine surface fields
                # (hrefs/.u/counts/correspondence) stay byte-identical to
                # wave0; only the shared <style> block grew. Account for
                # both explicitly.
                assert surface[key] == (
                    baseline[key] + WAVE1_BASE_CSS_DELTA + WAVE2_BASE_CSS_DELTA
                ), (
                    f"{entry.name}: byte_size {surface[key]} != wave0 "
                    f"{baseline[key]} + Wave-1 {WAVE1_BASE_CSS_DELTA} + "
                    f"Wave-2 {WAVE2_BASE_CSS_DELTA} CSS deltas"
                )
            else:
                assert surface[key] == baseline[key], (
                    f"{entry.name}: surface[{key}] != wave0 baseline"
                )
        return

    # Live-source entry: structural invariants (not full equality).
    assert set(surface.keys()) >= set(baseline.keys()) - {"meta"}, (
        f"{entry.name}: schema keys missing vs wave0"
    )
    assert surface["nav_strip"] == 2, (
        f"{entry.name}: nav_strip must be 2 (top+bottom, R8), got "
        f"{surface['nav_strip']}"
    )
    # .u <-> href correspondence: every absolute token-bearing href has a
    # visible .u twin (R2) — the invariant a restyle must not break.
    for corr in surface["correspondence"]:
        assert corr["u_twin"] and corr["u_index"] >= 0, (
            f"{entry.name}: absolute token href lost its .u twin: {corr['href']}"
        )
    # The offline render of a live entry uses a DIFFERENT (committed) fixture,
    # so byte_size is not comparable to the live baseline; assert only that it
    # rendered non-empty and stays under the §7.1 page budget (a restyle must
    # not blow up the page).
    assert 0 < surface["byte_size"] <= 100 * 1024, (
        f"{entry.name}: byte_size {surface['byte_size']} empty or over the "
        f"100 KB page budget"
    )
    # cap headroom: absolute anchors + the Wave-1 report delta stay under the
    # 400-link budget (R7).
    headroom = surface["counts"]["absolute_anchors"]
    if entry.name == "report_bill-774":
        headroom += WAVE1_REPORT_ABS_DELTA
    assert headroom <= PAGE_BUDGET_LINKS, (
        f"{entry.name}: cap headroom {headroom} exceeds "
        f"{PAGE_BUDGET_LINKS}-link budget"
    )


@pytest.mark.parametrize("entry", CORPUS, ids=[e.name for e in CORPUS])
def test_text_format_bytes_match_fixture(
    client_with_index: TestClient, entry: CorpusEntry
) -> None:
    """The ?format=text bytes match the committed text fixture.

    Offline entries: FULL byte-equality (the extraction-contract freeze — must
    stay byte-identical through the restyle). Live entries: the text fixture
    is a redacted LIVE reference the offline index can't reproduce, so assert
    only that the offline ?format=text renders non-empty (no crash) — the live
    byte-equality is asserted by ``machine_baseline.py --verify``.
    """
    _, text_bytes = _render(client_with_index, entry)
    fixture = (TEXT_DIR / f"{entry.name}.txt").read_bytes()

    if entry.source == "offline":
        assert text_bytes == fixture, (
            f"{entry.name}: ?format=text bytes drifted from the committed "
            f"fixture (extraction-contract freeze broken)"
        )
        return

    # Live-source entry: offline render is non-empty + non-trivial.
    assert len(text_bytes) > 0, f"{entry.name}: offline ?format=text empty"


# --------------------------------------------------------------------------- #
# The gate is LIVE (not vacuous): a template that adds a random link must fail
# --------------------------------------------------------------------------- #


def test_gate_is_live_not_vacuous() -> None:
    """Sanity: the extractor sees a synthetic extra link (proves the parser is
    not vacuously matching an empty surface)."""
    html = (
        '<a href="https://x.example/a/hg_t/other">Other</a>'
        '<span class="u">https://x.example/a/hg_t/other</span>'
        "Navigate: Home\nNavigate: Home"
    )
    surface = surface_to_dict(extract_surface(html))
    assert surface["counts"]["absolute_anchors"] == 1
    assert surface["correspondence"][0]["u_twin"] is True
    assert surface["nav_strip"] == 2
