"""27.2-REQ-01: publish-hygiene grep gate over tracked files.

Two scans:

1. **Live-secret scan** — over ALL tracked files: the consumer-token plaintext
   (``~/.hansard/consumer_token_team1``) and every ``tokens.yaml`` sha256 digest
   must appear in no tracked file. This is release-blocking (T-27.2-01): a live
   secret in a tracked file ships to the public clone.

2. **Machine-string scan** — over ALL tracked files (Phase 27.2 Plan 2
   generalized deploy/ to placeholders, so the Plan 1 deploy/ carve-out is
   removed): no ``/home/tyc``, no ``192.168.8.`` literal, no
   ``.env.services`` ref, no ``pi@192.168.8.165``.

``tests/fixtures/`` is excluded entirely — the invalid-token 404 fingerprint
fixture (md5 d9eb47423d3230fb8bda25763636eb35) is committed ground truth and
must survive the gate. The gitignored ``live_acceptance/`` suite is untracked
by construction, so both scans cannot see it (T-27.47 guard test).
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import yaml

REPO_ROOT: Path = Path(__file__).resolve().parent.parent

#: Live-secret scan covers ALL tracked files (no carve-outs).
_LIVE_SECRET_SCAN_EXCLUDES: tuple[str, ...] = ("tests/fixtures",)

#: Machine-string scan excludes only the committed fingerprint fixture
#: (deploy/ is covered — Phase 27.2 Plan 2 generalized it to placeholders).
MACHINE_STRING_SCAN_EXCLUDES: tuple[str, ...] = ("tests/fixtures",)

#: Forbidden machine-specific strings (27.2-REQ-01 hygiene gate).
_FORBIDDEN_MACHINE_STRINGS: tuple[str, ...] = (
    "/home/tyc",
    "192.168.8.",
    ".env.services",
    "pi@192.168.8.165",
)


def _tracked_files() -> list[Path]:
    """All git-tracked files in the repo (relative to REPO_ROOT)."""
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [REPO_ROOT / line for line in out.stdout.splitlines() if line]


def _path_excluded(rel: str, excludes: tuple[str, ...]) -> bool:
    """True if *rel* (repo-relative) falls under any exclude prefix."""
    return any(rel == e or rel.startswith(e + "/") for e in excludes)


def _machine_string_scan_targets() -> list[Path]:
    """Tracked files subject to the machine-string scan.

    Excludes ``tests/fixtures/`` (the committed 404 fingerprint fixture) AND
    the hygiene-gate test file (this one), whose ``_FORBIDDEN_*`` constants
    must carry the forbidden tokens verbatim to define the gate (the literal
    list is checked by ``test_gate_literals_are_intact`` below).
    """
    gate_sources = {Path(__file__).resolve()}
    return [
        path
        for path in _tracked_files()
        if not _path_excluded(str(path.relative_to(REPO_ROOT)), MACHINE_STRING_SCAN_EXCLUDES)
        and path.resolve() not in gate_sources
    ]


def _live_secrets() -> tuple[list[str], list[str]]:
    """Return (sha256_digests, [consumer_token]) read from the live store.

    Both are skip-if-absent: the gate must run on a clean CI box that has no
    live tokens.yaml / consumer token.
    """
    digests: list[str] = []
    tokens_yaml = REPO_ROOT / "tokens.yaml"
    if tokens_yaml.exists():
        doc = yaml.safe_load(tokens_yaml.read_text(encoding="utf-8")) or {}
        for entry in doc.get("tokens", []):
            if isinstance(entry, dict) and entry.get("sha256"):
                digests.append(str(entry["sha256"]))
    consumer: list[str] = []
    consumer_path = Path.home() / ".hansard" / "consumer_token_team1"
    if consumer_path.exists():
        consumer.append(consumer_path.read_text(encoding="utf-8").strip())
    return digests, consumer


def test_live_secrets_appear_in_no_tracked_file() -> None:
    """The consumer token + tokens.yaml digests are in NO tracked file."""
    digests, consumers = _live_secrets()
    if not digests and not consumers:
        return  # clean CI box: nothing to leak; the scan is vacuously green

    secrets = [*digests, *consumers]
    violations: list[str] = []
    for path in _tracked_files():
        rel = str(path.relative_to(REPO_ROOT))
        if _path_excluded(rel, _LIVE_SECRET_SCAN_EXCLUDES):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for secret in secrets:
            if secret and secret in text:
                violations.append(f"{rel}: {secret[:8]}…({len(secret)} chars)")
    assert not violations, "live secret(s) leaked into tracked files:\n" + "\n".join(violations)


def test_no_forbidden_machine_strings_in_tracked_files() -> None:
    """No /home/tyc, 192.168.8., .env.services, or pi@192.168.8.165 in any
    tracked file (only tests/fixtures/ excluded — the 404 fingerprint)."""
    violations: list[str] = []
    for path in _machine_string_scan_targets():
        rel = str(path.relative_to(REPO_ROOT))
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for bad in _FORBIDDEN_MACHINE_STRINGS:
            if bad in text:
                violations.append(f"{rel}: {bad!r}")
    assert not violations, "forbidden machine string(s) in tracked files:\n" + "\n".join(violations)


def test_gate_literals_are_intact() -> None:
    """The forbidden machine-string list is exactly the four canonical entries.

    Guards the self-exclusion above: the gate can only skip its own source
    while the literal tokens still define it. A missing/altered entry would
    silently shrink the publish-hygiene surface (27.2-REQ-01 / T-27.2-05).
    """
    source = Path(__file__).read_text(encoding="utf-8")
    for literal in _FORBIDDEN_MACHINE_STRINGS:
        assert f'"{literal}"' in source, f"gate literal {literal!r} altered or removed"
    assert len(_FORBIDDEN_MACHINE_STRINGS) == 4, "forbidden list must stay exactly 4 entries"


def test_live_acceptance_is_never_tracked() -> None:
    """The gitignored live-acceptance suite is not in the tracked tree.

    The carve-out of the live production launcher token (27.2-REQ-01 /
    mission 006 OPEN-1) depends on this file never being committed — if a
    future change tracks it, the live-secret scan would catch it, but this
    guard fails FIRST with a precise message.
    """
    out = subprocess.run(
        ["git", "ls-files", "--", "live_acceptance/"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert not out.stdout.strip(), (
        "live_acceptance/ is tracked — it holds live production credentials "
        "(spec §9 launcher token) and must stay gitignored (27.2-REQ-01)"
    )


def test_404_fingerprint_fixture_survives() -> None:
    """The invalid-token 404 body fixture is present + byte-stable (md5).

    Guards the byte-identity invariant the hygiene gate's exclusion list
    preserves: the fixture must keep its md5 d9eb47423d3230fb8bda25763636eb35.
    """
    fixture = REPO_ROOT / "tests" / "fixtures" / "invalid_token_404_body.html"
    assert fixture.exists(), "404 fingerprint fixture is missing"
    digest = hashlib.md5(fixture.read_bytes()).hexdigest()
    assert digest == "d9eb47423d3230fb8bda25763636eb35"
