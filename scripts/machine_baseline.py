"""Freeze and verify the gateway's machine-readable surface (Phase 27.3).

Dual-baseline capture for the Hansard-gateway UI restyle. Before a single
template changes, the machine surface of a fixed 8-entry page corpus is frozen
so every later wave (Cite links, TOC links, restyle) diffs against a clean
baseline and is attributed. Two baselines per entry:

1. **Machine-surface JSON** (``tests/fixtures/baselines/<dir>/<entry>.json``)
   — the rendered-HTML extraction (every ``href`` in document order, every
   ``span.u`` text, the href-to-``.u`` correspondence, anchor texts, counts,
   the nav-strip marker count, the ``?format=json``/``?format=text`` sibling
   links, page byte size). The DOM-contract freeze.
2. **?format=text bytes** (``tests/fixtures/text_format_baseline/<entry>.txt``)
   — the raw extraction-grade text view. The extraction-contract freeze (must
   stay byte-identical through the restyle).

Capture sources (two-tier decision, see the wave0/README + the regression test
docstring): the **6 index-driven entries** (launcher, nav/h, years, members,
bills, date) are captured **OFFLINE** via the in-process TestClient against the
committed test index (deterministic, the real regression target); the **2
upstream-stubbed entries** (report, search) are captured **LIVE** from the
healthy deployment (the offline index cannot reproduce them; the offline test
asserts structural invariants, full equality is asserted live by
:func:`run_verify`).

Deterministic: same source + same token + same corpus => byte-identical JSON
(stable key order, no wall-clock timestamps — the mission log carries the
date). The shared extraction lives in :mod:`scripts.machine_surface` (ONE
parser for capture AND the regression test). The token is read from
:data:`TOKEN_ENV` (never from disk, never printed); URL fields are redacted to
the ``hg_…`` shape and only a sha256 of the body is recorded.

Usage::

    # Freeze the IMMUTABLE Wave-0 baseline (default --baseline-dir):
    TOK=$(cat ~/.hansard-upstream/consumer_token_team1) \
        && HANSARD_LIVE_TOKEN="$TOK" uv run python scripts/machine_baseline.py \
            --baseline-dir tests/fixtures/baselines/wave0

    # Live drift check against a baseline dir (per-wave / final --verify):
    TOK=$(cat ~/.hansard-upstream/consumer_token_team1) \
        && HANSARD_LIVE_TOKEN="$TOK" uv run python scripts/machine_baseline.py \
            --verify --baseline-dir tests/fixtures/baselines/wave0

``--baseline-dir`` selects which directory capture writes to and ``--verify``
diffs against (default ``tests/fixtures/baselines/wave0``, the IMMUTABLE
Wave-0 reference). Per-wave refreshes pass ``--baseline-dir
tests/fixtures/baselines/wave<N>`` so the wave0/ files stay byte-identical
across the whole phase.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from baseline_corpus import (
    CORPUS,
    CorpusEntry,
    entry_path,
    text_path,
)
from machine_surface import (
    extract_surface,
    redact_url,
    surface_to_dict,
)
from offline_render import (
    LIVE_BASE_URL,
    live_get,
    offline_client,
    render_offline,
)

# ---- Constants (named — no magic strings, per STYLE.md) ----

#: The environment variable holding the live consumer token (read by the
#: shell, passed through — this script never prints it).
TOKEN_ENV: str = "HANSARD_LIVE_TOKEN"

#: The repo root (this file lives at <root>/scripts/machine_baseline.py).
REPO_ROOT: Path = Path(__file__).resolve().parent.parent

#: The fixed text-fixture directory (one .txt per corpus entry).
TEXT_FIXTURE_DIR: Path = REPO_ROOT / "tests" / "fixtures" / "text_format_baseline"

#: Default baseline JSON directory (the IMMUTABLE Wave-0 reference).
DEFAULT_BASELINE_DIR: str = "tests/fixtures/baselines/wave0"

# ---- Capture ----


def _write_json(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(obj, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _baseline_record(entry: CorpusEntry, *, html: str, text_bytes: bytes,
                     source: str, token_shape: str,
                     base_url: str) -> dict[str, Any]:
    """Per-entry baseline JSON (surface + capture metadata). Token-bearing URLs
    are redacted to the ``hg_…`` shape; only a sha256 of the body is recorded."""
    record = surface_to_dict(extract_surface(html))
    record["meta"] = {
        "entry": entry.name,
        "route": entry_path(entry),
        "source": source,
        "token_shape": token_shape,
        "html_sha256": hashlib.sha256(html.encode("utf-8")).hexdigest(),
        "text_sha256": hashlib.sha256(text_bytes).hexdigest(),
        "text_bytes": len(text_bytes),
        "base_url": base_url,
    }
    for key in ("hrefs", "anchor_texts", "u_texts"):
        record[key] = [redact_url(x) for x in record[key]]
    record["correspondence"] = [
        {**c, "href": redact_url(c["href"])} for c in record["correspondence"]
    ]
    for fmt in list(record["format_links"].keys()):
        record["format_links"][fmt] = [
            redact_url(x) for x in record["format_links"][fmt]
        ]
    return record


def capture_all(*, baseline_dir: Path) -> list[Path]:
    """Capture both baselines for every corpus entry; return written paths.

    Index-driven entries are rendered offline; report + search are fetched
    live (token from :data:`TOKEN_ENV`). Writes ``<baseline_dir>/<entry>.json``
    and ``tests/fixtures/text_format_baseline/<entry>.txt``.
    """
    baseline_dir = (
        baseline_dir if baseline_dir.is_absolute() else REPO_ROOT / baseline_dir
    )
    baseline_dir.mkdir(parents=True, exist_ok=True)
    TEXT_FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

    live_token = os.environ.get(TOKEN_ENV, "")
    if any(e.source == "live" for e in CORPUS) and not live_token:
        raise SystemExit(
            f"capture: {TOKEN_ENV} env var is required for the live entries "
            "(report, search). Read it with: TOK=$(cat "
            "~/.hansard-upstream/consumer_token_team1) && "
            f"HANSARD_LIVE_TOKEN=\"$TOK\" uv run python scripts/machine_baseline.py"
        )

    # Offline hrefs use the default settings base (the render layer reads the
    # module-level singleton); live hrefs use the real public host.
    from hansard_gateway.config import settings as _settings

    offline_base = _settings.public_base_url.rstrip("/")
    client: Any = None
    written: list[Path] = []
    for entry in CORPUS:
        if entry.source == "offline":
            if client is None:
                client = offline_client()
            html, text_bytes = render_offline(client, entry)
            source, shape = "offline", "hg_…(offline test token)"
            base = offline_base
        else:
            html = live_get(entry_path(entry), token=live_token)
            raw_text = live_get(text_path(entry), token=live_token)
            # The live ?format=text echoes the full token-bearing URLs verbatim.
            # The committed text fixture must NOT carry the secret live token
            # (threat model T-27.3-01) — redact it to the hg_… shape. The
            # offline test asserts only structural invariants for the live
            # entries (not text byte-equality), so redaction is safe here.
            text_bytes = raw_text.replace(live_token, "hg_…").encode("utf-8")
            source, shape = "live", "hg_…(live, redacted)"
            base = LIVE_BASE_URL

        record = _baseline_record(
            entry, html=html, text_bytes=text_bytes, source=source,
            token_shape=shape, base_url=base,
        )
        json_path = baseline_dir / f"{entry.name}.json"
        _write_json(json_path, record)
        txt_path = TEXT_FIXTURE_DIR / f"{entry.name}.txt"
        txt_path.write_bytes(text_bytes)
        written.extend([json_path, txt_path])
        print(f"  captured {entry.name:18s} [{source:7s}] "
              f"abs={record['counts']['absolute_anchors']:3d} "
              f"u={record['counts']['u_spans']:3d} nav={record['nav_strip']}")
    return written


# ---- CLI ----


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--baseline-dir", default=DEFAULT_BASELINE_DIR,
        help="baseline JSON dir to write/verify (default: "
             f"{DEFAULT_BASELINE_DIR}, the IMMUTABLE Wave-0 reference)",
    )
    p.add_argument(
        "--verify", action="store_true",
        help="re-run extraction live and diff against --baseline-dir "
             "instead of capturing",
    )
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    baseline_dir = Path(args.baseline_dir)
    live_token = os.environ.get(TOKEN_ENV, "")

    if args.verify:
        if not live_token:
            raise SystemExit(
                f"verify: {TOKEN_ENV} env var is required "
                "(TOK=$(cat ~/.hansard-upstream/consumer_token_team1))"
            )
        from machine_verify import run_verify

        return run_verify(
            baseline_dir=baseline_dir, live_token=live_token,
            repo_root=REPO_ROOT,
        )

    written = capture_all(baseline_dir=baseline_dir)
    print(f"captured {len(written)} files -> {args.baseline_dir} + "
          f"{TEXT_FIXTURE_DIR.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
