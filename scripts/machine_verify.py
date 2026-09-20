"""Live drift check for the machine-surface baselines (Phase 27.3).

:func:`run_verify` re-runs extraction against the LIVE deployment and diffs the
result against a committed baseline directory. It is the drift check an
executor runs at wave boundaries:

* **Per-wave** ``--verify --baseline-dir tests/fixtures/baselines/wave<N>``
  proves that wave's delta ONLY (against the previous wave's fixture).
* **Final** ``--verify --baseline-dir tests/fixtures/baselines/wave0`` proves
  the CUMULATIVE delta against the IMMUTABLE Wave-0 reference.

Only the LIVE corpus entries (report, search) are compared here; the 6
index-driven entries are offline captures, so live drift of them is out of
scope (different index content). Full equality of the machine surface is
asserted for each live entry. Kept separate from ``machine_baseline.py`` (the
capture driver) so each file stays under the 300-LOC STYLE.md cap.
"""

from __future__ import annotations

import json
from pathlib import Path

from baseline_corpus import CORPUS, entry_path
from machine_surface import extract_surface, redact_surface, surface_to_dict
from offline_render import live_get

#: The surface fields the drift check compares. ``byte_size`` is EXCLUDED: the
#: live report page embeds a "Retrieved <timestamp>" that changes per request,
#: so the byte count wobbles even when the machine surface is unchanged. The
#: structural machine surface (hrefs/.u/correspondence/counts/nav/format) is
#: what the restyle must not silently change.
_COMPARED_KEYS: tuple[str, ...] = (
    "counts", "nav_strip", "hrefs", "u_texts",
    "anchor_texts", "correspondence", "format_links",
)


def run_verify(*, baseline_dir: Path, live_token: str,
               repo_root: Path) -> int:
    """Re-run extraction LIVE and diff against the committed baseline dir.

    For each LIVE corpus entry, re-fetch the page, re-extract, redact the token
    to the ``hg_…`` shape (the committed baselines store redacted URLs), and
    compare the machine surface against ``<baseline_dir>/<entry>.json``.
    Returns 0 if all live entries match, 1 otherwise.
    """
    baseline_dir = (
        baseline_dir if baseline_dir.is_absolute() else repo_root / baseline_dir
    )
    failures: list[str] = []
    for entry in CORPUS:
        if entry.source != "live":
            print(f"  verify   {entry.name:18s} [offline] (skipped live drift)")
            continue
        committed = json.loads(
            (baseline_dir / f"{entry.name}.json").read_text(encoding="utf-8")
        )
        html = live_get(entry_path(entry), token=live_token)
        surface = redact_surface(surface_to_dict(extract_surface(html)))
        for key in _COMPARED_KEYS:
            if surface.get(key) != committed.get(key):
                failures.append(f"{entry.name}: surface[{key}] drifted")
    if failures:
        print("VERIFY FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("VERIFY PASS: live surface == committed baseline (live entries)")
    return 0
