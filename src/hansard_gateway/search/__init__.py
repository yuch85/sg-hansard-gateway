"""Search provider package: SearchProvider protocol + SPRS + Pair adapters."""

from __future__ import annotations

from hansard_gateway.search.pair import PairSearchProvider
from hansard_gateway.search.provider import SearchProvider
from hansard_gateway.search.sprs import SprsSearchProvider

__all__ = ["PairSearchProvider", "SearchProvider", "SprsSearchProvider"]
