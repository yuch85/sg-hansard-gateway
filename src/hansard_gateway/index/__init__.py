"""Offline term index for the prefix-ladder ingress (Phase 27.1).

The index is a metadata-only SQLite DB (report / term / term_prefix /
prefix_children / crawl_state / meta). It is built by the standalone crawl
job and read by the app through :class:`~hansard_gateway.index.loader.IndexService`.
Transcript bodies are NEVER stored (no-summarisation invariant, spec §3).
"""

from __future__ import annotations

from hansard_gateway.index.build import build_index
from hansard_gateway.index.extract import extract_terms, norm
from hansard_gateway.index.loader import IndexService
from hansard_gateway.index.schema import SCHEMA_STATEMENTS

__all__ = [
    "SCHEMA_STATEMENTS",
    "IndexService",
    "build_index",
    "extract_terms",
    "norm",
]
