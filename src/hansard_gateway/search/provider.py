"""SearchProvider protocol — the contract /search and /date call into.

SPRS is authoritative; Pair is discovery augmentation (spec §6). A provider
never raises for an upstream outage on the search path — it returns a
SearchPage (possibly with a note) so the route stays up.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

from hansard_gateway.models import SearchPage


class SearchProvider(Protocol):
    """A normalized search provider (keyword / date range / speaker filters)."""

    async def search(
        self,
        *,
        query: str,
        date_from: date | None,
        date_to: date | None,
        speaker: str | None,
        page: int,
        limit: int,
    ) -> SearchPage:
        """Return one normalized page of hits for the given filters."""
        ...
