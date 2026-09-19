#!/usr/bin/env python3
"""manage_tokens.py — capability-token admin CLI for the Hansard Gateway.

Implements 27-SPEC-SECURITY-ADDENDUM §3/§4/§5/§23/§24 and 27-CONTEXT D-11:

- Tokens are ``"hg_" + secrets.token_urlsafe(32)`` (~256-bit entropy).
- Only the SHA-256 digest + label + enabled-flag + last4 + created_at are
  stored in ``tokens.yaml`` (mode 0600, gitignored). Plaintext is printed
  exactly once at generate/rotate time and never persisted.
- The store fails closed: malformed YAML means zero usable tokens.

Usage:
    python manage_tokens.py list
    python manage_tokens.py generate <label>
    python manage_tokens.py disable <label>
    python manage_tokens.py rotate <label>
"""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

#: Prefix for all Hansard Gateway capability tokens (addendum §3 shape).
TOKEN_PREFIX: str = "hg_"
#: token_urlsafe byte length -> ~256-bit entropy (addendum §3).
TOKEN_ENTROPY_BYTES: int = 32
#: Store file permission: owner read/write only (D-11).
STORE_FILE_MODE: int = 0o600
#: Relative location of the token store, next to this script.
STORE_RELATIVE_PATH: str = "tokens.yaml"
#: Banner printed once with every freshly generated plaintext token (§23).
SAVE_NOW_BANNER: str = (
    "SAVE THIS VALUE NOW.\n"
    "Only its hash will be stored."
)
#: Column width for the label field in `list` output.
LIST_LABEL_WIDTH: int = 24


class TokenStoreError(RuntimeError):
    """Raised when the token store is missing, unreadable, or malformed."""


class LabelNotFoundError(LookupError):
    """Raised when a generate/disable/rotate targets a missing label."""


#: Exit code when the token store is unreadable / malformed (fail closed).
EXIT_STORE_ERROR: int = 2
#: Exit code when a label is missing or already exists (user error).
EXIT_LABEL_ERROR: int = 3


def _store_path() -> Path:
    """Return the absolute path of the token store.

    Honors HANSARD_TOKENS_PATH (27.2-REQ-02: the container mounts the store at
    its own path). The default is the repo-root ``tokens.yaml`` — computed from
    this file's location (``src/hansard_gateway/`` → two parents up) so the CLI
    resolves the same store from a fresh clone, the root shim, or the image.
    """
    override = os.environ.get("HANSARD_TOKENS_PATH")
    if override:
        return Path(override).expanduser()
    return (Path(__file__).resolve().parent.parent.parent / STORE_RELATIVE_PATH).expanduser()


def _new_token() -> str:
    """Generate a fresh capability token: hg_ + 256 bits of URL-safe random."""
    return TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_ENTROPY_BYTES)


def _token_sha256(plaintext: str) -> str:
    """Return the SHA-256 hex digest of a plaintext token."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def load_store() -> list[dict[str, Any]]:
    """Load the token list from tokens.yaml, failing closed on any problem.

    Returns an empty list when the file is missing; raises
    :class:`TokenStoreError` (exit 2) on unreadable/malformed content so a
    broken store can never be silently treated as "all tokens revoked".
    """
    path = _store_path()
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TokenStoreError(f"token store unreadable: {exc}") from exc
    try:
        doc = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise TokenStoreError(f"token store malformed YAML: {exc}") from exc
    tokens = doc.get("tokens") if isinstance(doc, dict) else None
    if tokens is None:
        tokens = []
    if not isinstance(tokens, list):
        raise TokenStoreError("token store 'tokens' is not a list")
    for entry in tokens:
        if not isinstance(entry, dict):
            raise TokenStoreError(f"token entry is not a mapping: {entry!r}")
    return tokens


def save_store(tokens: list[dict[str, Any]]) -> None:
    """Atomically write tokens.yaml and chmod it to 0600 (D-11)."""
    path = _store_path()
    tmp_path = path.with_suffix(".yaml.tmp")
    tmp_path.write_text(
        yaml.safe_dump(
            {"tokens": tokens}, sort_keys=False, default_flow_style=False
        ),
        encoding="utf-8",
    )
    os.chmod(tmp_path, STORE_FILE_MODE)
    os.replace(tmp_path, path)
    os.chmod(path, STORE_FILE_MODE)


def _find_label(tokens: list[dict[str, Any]], label: str) -> dict[str, Any]:
    """Return the entry for *label*; raise LabelNotFoundError if missing."""
    for entry in tokens:
        if entry.get("label") == label:
            return entry
    raise LabelNotFoundError(label)


def cmd_list(_args: argparse.Namespace) -> None:
    """List all tokens: label, enabled flag, and last4 (never plaintext)."""
    tokens = load_store()
    if not tokens:
        print("(no tokens)")
        return
    for entry in tokens:
        enabled = "yes" if entry.get("enabled") else "no"
        last4 = str(entry.get("last4", "-"))
        print(
            f"{str(entry.get('label', '?')):<{LIST_LABEL_WIDTH}}"
            f"{enabled:<8}****{last4}"
        )


def cmd_generate(args: argparse.Namespace) -> None:
    """Generate a new token for *args.label*; print plaintext exactly once.

    Raises LabelNotFoundError when the label already exists (use rotate).
    """
    tokens = load_store()
    if any(e.get("label") == args.label for e in tokens):
        raise LabelNotFoundError(args.label)
    plaintext = _new_token()
    tokens.append(
        {
            "label": args.label,
            "sha256": _token_sha256(plaintext),
            "enabled": True,
            "last4": plaintext[-4:],
            "created_at": _now_iso(),
        }
    )
    save_store(tokens)
    print(f"New token for {args.label}:\n\n{plaintext}\n\n{SAVE_NOW_BANNER}")


def cmd_disable(args: argparse.Namespace) -> None:
    """Set enabled=false for *args.label* (independent revocation, §8)."""
    tokens = load_store()
    entry = _find_label(tokens, args.label)
    entry["enabled"] = False
    save_store(tokens)
    print(f"disabled: {args.label}")


def cmd_rotate(args: argparse.Namespace) -> None:
    """Rotate *args.label*: new token stored, old digest replaced (§9)."""
    tokens = load_store()
    entry = _find_label(tokens, args.label)
    plaintext = _new_token()
    entry["sha256"] = _token_sha256(plaintext)
    entry["last4"] = plaintext[-4:]
    entry["enabled"] = True
    save_store(tokens)
    print(f"New token for {args.label}:\n\n{plaintext}\n\n{SAVE_NOW_BANNER}")


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser with the four subcommands."""
    parser = argparse.ArgumentParser(
        prog="hg-tokens",
        description="Hansard Gateway capability-token admin CLI.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list tokens (label/enabled/last4 only)")
    gen = sub.add_parser("generate", help="generate a new token for a label")
    gen.add_argument("label")
    dis = sub.add_parser("disable", help="revoke a token by label")
    dis.add_argument("label")
    rot = sub.add_parser("rotate", help="replace a token by label")
    rot.add_argument("label")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code (2 store, 3 label)."""
    args = build_parser().parse_args(argv)
    try:
        {"list": cmd_list, "generate": cmd_generate, "disable": cmd_disable,
         "rotate": cmd_rotate}[args.command](args)
    except TokenStoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_STORE_ERROR
    except LabelNotFoundError as exc:
        label = exc.args[0] if exc.args else "?"
        print(f"error: no token with label {label!r}", file=sys.stderr)
        return EXIT_LABEL_ERROR
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
