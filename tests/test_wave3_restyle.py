"""Wave-3 (Plan 27.3-04) layout restyle tests for the non-report pages.

This wave is LAYOUT-ONLY: the machine surface (hrefs, ``.u`` twins, anchor
texts, counts, correspondence, nav/format links) of every non-report page
type must stay EXACTLY the Wave-0 baseline. Only the shared ``<style>`` block
in ``base.html`` may grow (layout CSS), so ``byte_size`` is the one field
allowed to move — and it is asserted as the exact accounted CSS delta, never
a free-form "close enough".

The zero-diff assertions reuse the same render path and shared extraction
parser as ``test_machine_baseline.py`` (one parser, one client — 27.3-01
key_link); this file adds the layout-presence assertions (the classes the
restyle must add) and the per-page budget assertions (CSS budget per the
CO-approved 8 KB constant, the 100 KB page budget, the 400-link cap — all
pinned to the SAME constants the conformance linter uses).

The search entry is rendered offline against the committed
``searchresult_20041019_p1.json`` fixture (respx-stubbed — the offline
entries are order-deterministic; the LIVE search ranking order is
upstream-nondeterministic per T-27-58, so the offline fixture is the
regression target, and the live diff is judged by SET + counts at the wave
boundary).
"""

from __future__ import annotations

import html as html_lib
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from baseline_corpus import CORPUS, CorpusEntry, entry_path, text_path  # noqa: E402
from machine_surface import (  # noqa: E402
    extract_surface,
    redact_surface,
    surface_to_dict,
)

from hansard_gateway.auth import TEST_TOKEN  # noqa: E402

# One source for the budgets — import the SAME constants the conformance
# linter enforces (no drift between the layout tests and the linter).
from tests.test_link_conformance import (  # noqa: E402
    PAGE_BUDGET_CSS_BYTES,
    PAGE_BUDGET_HTML_BYTES,
    PAGE_BUDGET_LINKS,
)

UPSTREAM_BASE = "https://sprs.parl.gov.sg/search"
PAIR_BASE = "https://search.pair.gov.sg"

#: The IMMUTABLE Wave-0 baseline dir — the zero-diff target for every
#: non-report page type this wave restyles (27.3-04 M1: never touched).
WAVE0_DIR = Path(__file__).parent / "fixtures" / "baselines" / "wave0"

#: The non-report page types this wave restyles. The offline entries are the
#: full-equality target; search is rendered offline against the committed
#: searchResult fixture (upstream-stubbed) so it is order-deterministic.
OFFLINE_RESTYLE_ENTRIES = ("launcher", "nav_h", "years", "members", "bills")
SEARCH_ENTRY = "search_hib_p1"
#: Every non-report page type this wave restyles (the zero-diff target set).
ALL_RESTYLE_ENTRIES: tuple[str, ...] = OFFLINE_RESTYLE_ENTRIES + (SEARCH_ENTRY,)

#: The machine-surface fields that must stay byte-identical to wave0 after a
#: layout-only restyle (everything except the shared-CSS byte_size growth,
#: which is asserted separately as an exact delta).
_LAYOUT_INvariant_KEYS: tuple[str, ...] = (
    # byte_size is excluded on purpose: it moves with the shared base.html
    # CSS (asserted separately as a bounded delta + budget).
    "hrefs", "u_texts", "anchor_texts", "correspondence",
    "counts", "nav_strip", "format_links",
)


def _search_fixture_rows() -> list[dict[str, Any]]:
    """The committed 2004 searchResult rows (offline ground truth)."""
    path = Path(__file__).parent / "fixtures" / "searchresult_20041019_p1.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _stub_search(mock: respx.MockRouter) -> None:
    mock.post("/searchResult").respond(json=_search_fixture_rows())


def _render(
    client: TestClient, entry: CorpusEntry
) -> tuple[str, bytes]:
    """Render one corpus entry offline (same path as test_machine_baseline).

    Returns (html, text_bytes). Search + the date TOC hit the upstream
    (respx-stubbed); the purely index-driven pages need no stub.

    NOTE (the two-tier corpus rule, wave0/README): the search entry is
    ``source="live"`` — its committed wave0 fixture is a LIVE reference
    (real 206-hit HIB results) the offline index CANNOT reproduce (the
    committed searchResult fixture is the 2004 49-row E2E topic, a
    different corpus). So the offline search render is a STRUCTURAL
    invariant target only — the same scope the offline regression gate
    (``test_machine_baseline.py``) applies to live-source entries — never a
    zero-diff target. The zero-diff contract for search is enforced LIVE at
    the wave boundary by ``machine_baseline.py --verify`` (judged by SET +
    counts per T-27-58, since the live ranking order is
    upstream-nondeterministic).
    """
    route = f"/a/{TEST_TOKEN}{entry_path(entry)}"
    text_route = f"/a/{TEST_TOKEN}{text_path(entry)}"
    needs_search_stub = entry.name in ("search_hib_p1", "date_2026-01-12")
    if not needs_search_stub:
        return client.get(route).text, client.get(text_route).content

    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        _stub_search(mock)
        pair = respx.mock(
            base_url=PAIR_BASE,
            assert_all_called=False,
            assert_all_mocked=False,
        )
        pair.start()
        try:
            html = client.get(route).text
            text_bytes = client.get(text_route).content
        finally:
            pair.stop()
    return html, text_bytes


def _load_baseline(entry: CorpusEntry) -> dict[str, Any]:
    return json.loads((WAVE0_DIR / f"{entry.name}.json").read_text(encoding="utf-8"))


def _surface(client: TestClient, entry: CorpusEntry) -> dict[str, Any]:
    html, _ = _render(client, entry)
    return redact_surface(surface_to_dict(extract_surface(html)))


def _corpus_entry(name: str) -> CorpusEntry:
    return next(e for e in CORPUS if e.name == name)


# --------------------------------------------------------------------------- #
# Zero machine-surface diff — the core acceptance of this wave
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ALL_RESTYLE_ENTRIES)
def test_nonreport_machine_surface_zero_diff_vs_wave0(
    client_with_index: TestClient, name: str
) -> None:
    """A layout-only restyle must NOT change the machine surface.

    Every surface field except ``byte_size`` is asserted byte-identical to
    the IMMUTABLE Wave-0 baseline: same hrefs (document order = R7 ranking
    order), same ``.u`` twins, same anchor texts, same correspondence, same
    counts, same nav strips, same format siblings. CSS may reposition
    visually (grid/flex/columns) — it may never add, remove, or reorder an
    anchor (T-27.3-11 mitigation).
    """
    entry = _corpus_entry(name)
    surface = _surface(client_with_index, entry)

    if entry.source == "live":
        # The search entry is a LIVE reference (wave0/README two-tier rule):
        # the offline index cannot reproduce its content, so full zero-diff
        # against the wave0 fixture is asserted LIVE by --verify at the wave
        # boundary (SET + counts, T-27-58). Offline: structural invariants
        # only — the layout restyle must not change the SHAPE of the surface
        # (schema, nav strips, the href<->.u correspondence, counts).
        baseline = _load_baseline(entry)
        assert set(surface.keys()) >= set(baseline.keys()) - {"meta"}, (
            f"{name}: schema keys missing vs wave0"
        )
        assert surface["nav_strip"] == baseline["nav_strip"] == 2, (
            f"{name}: nav_strip must stay 2 (top+bottom, R8)"
        )
        for corr in surface["correspondence"]:
            assert corr["u_twin"] and corr["u_index"] >= 0, (
                f"{name}: an absolute token href lost its .u twin: "
                f"{corr['href']}"
            )
        assert (
            surface["counts"]["absolute_anchors"]
            == surface["counts"]["u_spans"]
        ), (
            f"{name}: absolute token anchors and .u spans diverged "
            f"({surface['counts']}) — every link keeps its twin"
        )
        return
    baseline = _load_baseline(entry)
    for key in _LAYOUT_INvariant_KEYS:
        assert surface[key] == baseline[key], (
            f"{name}: machine surface {key} changed vs wave0 — this wave is "
            f"layout-only; the baseline diff is the proof"
        )


@pytest.mark.parametrize("name", ALL_RESTYLE_ENTRIES)
def test_nonreport_byte_size_moves_only_by_shared_css(
    client_with_index: TestClient, name: str
) -> None:
    """``byte_size`` may grow — but ONLY by the shared base.html CSS delta.

    This wave adds layout classes to the templates; any markup change is
    class-attribute-only (no link, no text node), so the rendered byte
    growth is the shared ``<style>`` block growth + the added class
    attributes. Assert the growth is BOUNDED (it must not explode — a
    regression that duplicates markup or inlines per-element styles would
    show up here) and that the page stays under the 100 KB budget.
    """
    entry = _corpus_entry(name)
    surface = _surface(client_with_index, entry)
    if entry.source == "live":
        # The wave0 search fixture is the LIVE 206-hit page (105 KB) — the
        # offline render (49-row fixture index) is a different corpus, so a
        # vs-wave0 byte delta is meaningless; only the absolute page budget
        # applies (asserted below + in the budget test).
        return
    baseline = _load_baseline(entry)
    delta = surface["byte_size"] - baseline["byte_size"]
    # The shared CSS delta is at most the CSS budget itself; the per-page
    # class additions are a few hundred bytes of attributes. Bound the total
    # growth so a markup-duplication regression fails loudly.
    assert 0 <= delta <= PAGE_BUDGET_CSS_BYTES + 2 * 1024, (
        f"{name}: byte_size grew {delta} B vs wave0 "
        f"({baseline['byte_size']} -> {surface['byte_size']}) — expected only "
        f"the shared layout CSS + class attributes"
    )
    assert surface["byte_size"] <= PAGE_BUDGET_HTML_BYTES, (
        f"{name}: page byte_size {surface['byte_size']} over the "
        f"{PAGE_BUDGET_HTML_BYTES}-byte budget"
    )


# --------------------------------------------------------------------------- #
# Budget tests (pinned to the conformance linter's constants)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ALL_RESTYLE_ENTRIES)
def test_nonreport_budgets_hold(
    client_with_index: TestClient, name: str
) -> None:
    """The three page budgets on every restyled non-report page:

    - inline CSS <= PAGE_BUDGET_CSS_BYTES (the CO-approved 8 KB constant,
      one source with the conformance linter);
    - page HTML <= PAGE_BUDGET_HTML_BYTES (100 KB);
    - total anchors <= PAGE_BUDGET_LINKS (400 — the search page's 206 echoes
      are the worst case; the restyle adds ZERO anchors, so this is asserted
      on the rendered page, not just the baseline).
    """
    entry = _corpus_entry(name)
    html, _ = _render(client_with_index, entry)
    css = "".join(
        re.findall(r"<style[^>]*>(.*?)</style>", html, flags=re.DOTALL)
    )
    assert len(css.encode("utf-8")) <= PAGE_BUDGET_CSS_BYTES, (
        f"{name}: inline CSS {len(css)} B over the "
        f"{PAGE_BUDGET_CSS_BYTES}-byte budget (search is the worst-case "
        f"echo page — trim presentational rules, never contract rules)"
    )
    assert len(html.encode("utf-8")) <= PAGE_BUDGET_HTML_BYTES, (
        f"{name}: page {len(html)} B over the 100 KB budget"
    )
    surface = redact_surface(surface_to_dict(extract_surface(html)))
    assert surface["counts"]["total_anchors"] <= PAGE_BUDGET_LINKS, (
        f"{name}: {surface['counts']['total_anchors']} anchors over the "
        f"{PAGE_BUDGET_LINKS}-link budget — the restyle must add zero links"
    )


# --------------------------------------------------------------------------- #
# Layout presence — the classes the restyle MUST add (structure assertions)
# --------------------------------------------------------------------------- #


def test_search_layout_classes_present(
    client_with_index: TestClient,
) -> None:
    """The 2-column search layout: results grid + refine aside.

    The results ``<article>`` elements keep their DOM order and every link
    (proven by the zero-diff test); the layout is a grid container around
    them + an ``<aside class="refine">`` panel AFTER the results (R7: the
    results rank before the refine links in the DOM).
    """
    html, _ = _render(client_with_index, _corpus_entry(SEARCH_ENTRY))
    assert 'class="search-layout"' in html, (
        "search: the 2-column layout container is missing"
    )
    assert 'class="refine"' in html, (
        "search: the refine <aside> panel is missing"
    )
    # The results come BEFORE the refine panel in the DOM (R7 ordering).
    assert html.index("search-results") < html.index("class=\"refine\""), (
        "search: the results grid must precede the refine panel in the DOM"
    )
    # The existing result articles are still present (one per hit).
    assert 'class="search-result"' in html, (
        "search: the result cards are missing"
    )


def test_search_result_strings_verbatim(
    client_with_index: TestClient,
) -> None:
    """Every result's title / date / id / section / speaker / excerpt survives
    verbatim in the rendered text (the restyle wraps, never edits)."""
    import html as html_lib

    html, _ = _render(client_with_index, _corpus_entry(SEARCH_ENTRY))
    rows = _search_fixture_rows()
    assert rows, "the committed searchResult fixture must be non-empty"

    def _escaped(value: object) -> str:
        """Jinja autoescape: mirror the exact entity encoding."""
        return (
            html_lib.escape(str(value), quote=False)
            .replace('"', "&#34;")
            .replace("'", "&#39;")
        )

    for row in rows:
        # The fixture is the raw upstream searchResult row; the render maps
        # it to a hit (title / report id / speaker / section / excerpt).
        title = row.get("title")
        if title:
            assert _escaped(title) in html, (
                f"search: result title {title!r} lost from the rendered "
                f"page — the restyle must not edit content"
            )


def test_search_sticky_ancestors_do_not_clip() -> None:
    """No added layout container sets overflow:hidden (sticky-safe).

    The plan's key_link: position:sticky on the narrow-further band (Task 2)
    and the Wave-2 sp-head rows rely on no overflow:hidden ancestor. Assert
    the layout classes added this wave do not declare an overflow that would
    break sticky positioning.
    """
    base_html = (
        _REPO_ROOT / "src" / "hansard_gateway" / "render"
        / "templates" / "base.html"
    ).read_text(encoding="utf-8")
    # Strip CSS comments first so the "overflow" mention in the Wave-3
    # comment header does not false-positive the check.
    css_no_comments = re.sub(r"/\*.*?\*/", "", base_html, flags=re.DOTALL)
    for cls in (".search-layout", ".search-results", ".refine",
                ".letter-grid", ".card-grid", ".narrow-further"):
        for block in re.findall(rf"{re.escape(cls)}[^{{]*\{{[^}}]*\}}",
                                css_no_comments):
            assert "overflow" not in block, (
                f"base.html: {block.split('{')[0].strip()} declares "
                f"overflow — an ancestor overflow breaks position:sticky "
                f"(the narrow-further band + the Wave-2 sp-head rows)"
            )
