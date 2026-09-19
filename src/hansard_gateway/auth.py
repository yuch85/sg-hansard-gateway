"""Capability-token auth: TokenStore, AuthContext, require_capability_token.

Mirrors the caselaw-mcp TokenRegistry prior art but keyed on the hansard
tokens.yaml schema (label / sha256 / enabled / last4). Fail-closed: malformed
or unreadable store yields zero entries. Invalid, revoked, and malformed tokens
all raise `TokenRejected` which the app renders as an identical 404 page — no
enumeration (addendum §7). Only the token *label* is ever logged.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml
from fastapi import Depends

from hansard_gateway.config import settings

logger = logging.getLogger(__name__)

#: Shape guard for capability tokens. Short-circuits malformed tokens BEFORE
#: hashing so garbage never reaches the constant-time compare (RESEARCH A2).
#: Bounds come from Settings (token_min_len / token_max_len, total length).
_TOKEN_SHAPE_RE = re.compile(
    r"hg_[A-Za-z0-9_-]{"
    f"{settings.token_min_len - 3},{settings.token_max_len - 3}"
    "}"
)

#: Known plaintext test token (valid shape: hg_ + 26 chars). Test scaffolding
#: only — referenced by tests/conftest.py; never a real credential.
TEST_TOKEN: str = "hg_testvalidtoken0123456789abcdef"
TEST_TOKEN_LABEL: str = "hansard-team-1"


@dataclass(frozen=True)
class TokenEntry:
    """One tokens.yaml entry. The plaintext token is NEVER stored here."""

    label: str
    sha256: str
    enabled: bool
    last4: str


@dataclass(frozen=True)
class AuthContext:
    """Authenticated request context passed to downstream routes.

    `__repr__`/`__str__` mask the token so it cannot leak into tracebacks or
    log frames (defense in depth, RESEARCH A2).
    """

    token_label: str
    token: str

    def __repr__(self) -> str:  # noqa: D105 — masking is the whole point
        return f"AuthContext(token_label={self.token_label!r}, token='****')"

    def __str__(self) -> str:
        return self.__repr__()


class TokenRejected(Exception):
    """Raised on any token failure. The app maps this to an identical 404 page."""


def _hash_token(plaintext: str) -> str:
    """SHA-256 hex digest of the plaintext token."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


class TokenStore:
    """YAML-backed capability-token store. SIGHUP-reloadable, fail-closed."""

    def __init__(self, *, path: Path) -> None:
        self._path = Path(path)
        self._entries: dict[str, TokenEntry] = {}
        self.reload()

    def reload(self) -> None:
        """Re-read tokens.yaml from disk. Fail-closed on malformed input."""
        try:
            raw = self._path.read_text(encoding="utf-8") if self._path.exists() else ""
        except OSError as exc:
            logger.error(
                "token store unreadable — fail-closed",
                extra={"path": str(self._path), "err": str(exc)},
            )
            self._entries = {}
            return
        try:
            doc = yaml.safe_load(raw) or {}
        except yaml.YAMLError as exc:
            logger.error(
                "token store malformed YAML — fail-closed",
                extra={"path": str(self._path), "err": str(exc)},
            )
            self._entries = {}
            return
        if not isinstance(doc, dict) or not isinstance(doc.get("tokens"), list):
            logger.error(
                "token store root not a mapping with 'tokens' list — fail-closed",
                extra={"path": str(self._path)},
            )
            self._entries = {}
            return
        new_entries: dict[str, TokenEntry] = {}
        for t in doc["tokens"]:
            if not isinstance(t, dict):
                logger.warning("token entry skipped — not a mapping")
                continue
            try:
                entry = TokenEntry(
                    label=str(t["label"]),
                    sha256=str(t["sha256"]).lower(),
                    enabled=bool(t.get("enabled", False)),
                    last4=str(t.get("last4", "")),
                )
            except (KeyError, ValueError, TypeError) as exc:
                logger.warning(
                    "token entry skipped",
                    extra={"err": str(exc), "label": str(t.get("label", "?"))},
                )
                continue
            new_entries[entry.sha256] = entry
        self._entries = new_entries
        logger.info(
            "token store loaded",
            extra={"path": str(self._path), "count": len(new_entries)},
        )

    def enabled_entries(self) -> list[TokenEntry]:
        """Return only entries currently enabled (revocation check)."""
        return [e for e in self._entries.values() if e.enabled]

    def count(self) -> int:
        """Total number of stored entries (enabled + disabled)."""
        return len(self._entries)


# Module-level store, loaded once at startup; reload() re-reads tokens.yaml.
_store: Optional[TokenStore] = None


def get_token_store() -> TokenStore:
    """FastAPI dependency returning the process-wide TokenStore (lazy init)."""
    global _store
    if _store is None:
        _store = TokenStore(path=settings.tokens_path)
    return _store


async def require_capability_token(
    *, token: str, store: TokenStore = Depends(get_token_store)
) -> AuthContext:
    """Validate a capability token; raise TokenRejected on any failure.

    Malformed tokens are rejected before hashing; valid-shape tokens are
    SHA-256'd and compared in constant time against every enabled entry.
    """
    if _TOKEN_SHAPE_RE.fullmatch(token) is None:
        raise TokenRejected()
    digest = _hash_token(token)
    for entry in store.enabled_entries():
        if hmac.compare_digest(digest, entry.sha256):
            return AuthContext(token_label=entry.label, token=token)
    raise TokenRejected()
