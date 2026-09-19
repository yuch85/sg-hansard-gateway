"""Token-independent in-process TTLCache for upstream content (D-13).

Keys carry NO token/label — all three capability tokens share the same upstream
content, so cache poisoning via token-dependent keys is impossible (T-27-07).
The HTTP *response* is `private, no-store` (Plan 04); that no-store applies to
the wire, NOT to this in-process store of fetched upstream bytes.
"""

from __future__ import annotations

import hashlib
from typing import Any

from cachetools import TTLCache

from hansard_gateway.config import Settings

#: Canonical query-param names, in the order the search key hashes them.
#: `page` is deliberately NOT a field (27.1-search-hop-rectify): the key
#: addresses a SWEEP (all collected rows), not one rendered page. Page
#: selection is a render-time slice over the cached sweep, and a per-page
#: memo (see `search_page_key`) makes any clicked ?page=N a cache hit
#: WITHOUT re-sweeping — the click-only pagination invariant.
_SEARCH_KEY_FIELDS = (
    "keyword",
    "date_from",
    "date_to",
    "limit",
)


class ReportCache:
    """TTLCache wrapper with stable, token-independent key builders."""

    def __init__(self, *, settings: Settings) -> None:
        self._cache: TTLCache[str, Any] = TTLCache(
            maxsize=settings.cache_maxsize, ttl=settings.cache_ttl_s
        )

    def report_key(self, *, report_id: str) -> str:
        """Key for a fetched topic payload."""
        return f"report:{report_id}"

    def search_key(
        self,
        *,
        keyword: str,
        date_from: str,
        date_to: str,
        limit: int,
    ) -> str:
        """Key for a search SWEEP (page-agnostic, 27.1-search-hop-rectify),
        hashed from the canonical query params. The page is NOT part of the
        key — page N is a render-time slice of the same sweep, so a truncated
        cold sweep cached once answers every page of the collected rows."""
        params = {
            "keyword": keyword,
            "date_from": date_from,
            "date_to": date_to,
            "limit": limit,
        }
        canonical = "&".join(
            f"{name}={params[name]}" for name in _SEARCH_KEY_FIELDS
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return f"search:{digest}"

    def search_page_key(self, *, sweep_key: str, page: int) -> str:
        """Key for the per-page memo of a sweep (the rendered SearchPage for
        one page number). Lets ?page=N answer in milliseconds from the cache
        without re-slicing + re-rendering (27.1-search-hop-rectify)."""
        return f"{sweep_key}:page:{page}"

    def get(self, *, key: str) -> Any | None:
        """Return the cached value for `key`, or None on miss."""
        return self._cache.get(key)

    def set(self, *, key: str, value: Any) -> None:
        """Store `value` under `key` (subject to the TTL)."""
        self._cache[key] = value

    def size(self) -> int:
        """Current number of cached entries."""
        return len(self._cache)
