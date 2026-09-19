"""Central configuration for the Hansard Gateway.

Single source of truth for every limit, timeout, TTL, hostname, and port in the
service (STYLE.md: no magic numbers outside config; D-12). All other modules
import the module-level `settings` singleton and never hardcode their own
constants.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

# --- Deployment env var names (27.2-REQ-02) ------------------------------------
#: Bind port override (systemd unit / Docker image).
ENV_APP_PORT = "HANSARD_APP_PORT"
#: Term index DB location override (container mount point).
ENV_INDEX_DB_PATH = "HANSARD_INDEX_DB_PATH"
#: Token store location override (container mount point).
ENV_TOKENS_PATH = "HANSARD_TOKENS_PATH"
#: Public origin override (different domain per deployment).
ENV_PUBLIC_BASE_URL = "HANSARD_PUBLIC_BASE_URL"
#: Structured request-log file override (D-09).
ENV_LOG_FILE = "HANSARD_LOG_FILE"
#: robots.txt override file (27.2-REQ-05); unset = the built-in default body.
ENV_ROBOTS_PATH = "HANSARD_ROBOTS_PATH"


@dataclass(frozen=True)
class Settings:
    """Immutable runtime configuration. Every field has a locked default."""

    # --- Per-token rate limits (addendum §16) ---
    #: Bumped for the prefix ladder (spec 7.4): a research session now costs
    #: 4-8 extra index-only hops before the first search.
    per_token_rate_per_min: int = 300
    per_token_rate_per_day: int = 5000
    per_token_concurrent_upstream: int = 5

    # --- Global upstream concurrency (addendum §17) ---
    global_upstream_concurrency: int = 15

    # --- Search limits (addendum §18) ---
    search_max_query_len: int = 300
    search_max_limit: int = 50
    search_min_page: int = 1

    # --- In-process upstream-content cache (D-13, spec §15) ---
    cache_ttl_s: int = 600
    #: Upper bound on cached entries; the TTL is the primary eviction policy.
    cache_maxsize: int = 1000

    # --- Upstream timeouts (spec §16; F-5 wave 6 per-attempt cap) ---
    connect_timeout_s: float = 5.0
    #: Per-ATTEMPT read/write cap on every upstream call (F-5). SPRS
    #: occasionally stalls ~35s host-side; with this cap a stall degrades to
    #: a fast 5xx-with-nav (3 attempts x 15s + backoff ≈ 50s worst case)
    #: instead of hanging a client that has a shorter fetch timeout.
    upstream_attempt_timeout_s: float = 15.0

    # --- Retry policy (D-07) ---
    retry_attempts: int = 3
    retry_backoff_s: float = 0.5

    # --- Upstream endpoints / SSRF allowlist (addendum §20) ---
    upstream_base: str = "https://sprs.parl.gov.sg/search"
    upstream_host_allowlist: tuple[str, ...] = (
        "sprs.parl.gov.sg",
        "search.pair.gov.sg",
    )
    pair_host: str = "search.pair.gov.sg"

    # --- Browser-like fixed header set (RESEARCH Finding 2) ---
    upstream_user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
    upstream_content_type: str = "application/json"
    upstream_referer: str = "https://sprs.parl.gov.sg/search/"
    #: Base of the official public record URL (provenance, spec §13).
    upstream_public_base: str = "https://sprs.parl.gov.sg"

    # --- Self URL (absolute, token-bearing links in responses) ---
    #: Canonical public origin of THIS gateway. Used to build absolute
    #: token-preserving URLs in JSON/HTML so a client (e.g. an LLM web tool)
    #: can follow a server-issued link without reconstructing it.
    public_base_url: str = "https://hansard.098020.xyz"

    # --- Capability-token shape guard (min/max total length of a valid token) ---
    token_min_len: int = 23
    token_max_len: int = 123

    # --- Search sweep parameters (D-06, sgparl algorithm) ---
    #: Rows per upstream searchResult page (the upstream's own page size).
    search_page_size: int = 20
    #: Up to this many probe fetches for maxResult (LB nodes disagree on totals).
    search_max_probes: int = 3
    #: Stop after this many consecutive sweeps gain no new unique rows.
    search_max_no_gain: int = 4
    #: COLD-sweep page budget (27.1-search-hop-rectify). A first-page sweep
    #: whose probed maxResult exceeds budget*page_size stops after `budget`
    #: sweep pages and returns the rows collected so far (a normal 200 page
    #: with the F-4 honest header + a rendered continuation link). 5 pages =
    #: 100 rows ~= 8 upstream POSTs ~= 1-2s of SPRS latency — inside the
    #: ~5s fetch budget of the free-tier LLM web tools that can only click
    #: rendered links. Queries whose probed total fits in the budget sweep to
    #: completion and cache exactly as before.
    search_cold_page_budget: int = 5

    # --- Pair provider (27-PAIR-SPIKE: X-Browser-ID header, fixed per process) ---
    pair_browser_id: str = "00000000-0000-4000-8000-000000000000"
    #: Pair rows are discovery-only (spec §6); capped below the search limit.
    pair_max_hits: int = 20
    pair_source: str = "hansard"
    #: Pair success status is 201 (verified in the spike), not 200.
    pair_success_status: int = 201
    pair_api_path: str = "/api/v1/search"

    # --- Term index / prefix ladder (Phase 27.1, spec §3 + §4) ---
    #: Local metadata-only index DB (0600, gitignored; built by the offline crawl).
    index_db_path: Path = field(default_factory=lambda: Path("~/.hansard/index.db").expanduser())
    #: Max prefix depth a term is reachable at (spec §4.2; depth 5 confirmed by YC).
    ladder_depth: int = 5
    #: Block A renders at most this many terms; above it, truncate (spec §4.2).
    ladder_term_cap: int = 300
    #: Block A renders this many terms when the cap is exceeded (spec §4.2).
    ladder_truncate_at: int = 200
    #: A child holding more than this many terms is "fat": Block B skip-level
    #: renders its grandchildren alongside it (YC addition 2026-09-18).
    fat_branch_threshold: int = 2000
    #: Launcher "Common topics" count (spec §4.1).
    common_topics_n: int = 150
    #: Launcher "Recent sittings" count (spec §4.1).
    recent_sittings_n: int = 30
    #: Zero-result "Did you mean" count (spec §6.1).
    did_you_mean_n: int = 20
    #: Search "Related topics" count (spec §4.4).
    related_terms_n: int = 20
    #: Report page "top-N title terms" links (spec §4.4).
    report_title_terms_n: int = 5
    #: Search "Refine by speaker" count (spec §4.4).
    refine_speakers_n: int = 20
    #: Search "Refine by date" recent-years count (spec §4.4).
    recent_years_n: int = 10

    # --- Crawl job (Phase 27.1, spec §3.1) ---
    #: Floor for sitting-date enumeration (Parliament's first sitting era).
    crawl_start_date: str = "1955-01-01"
    #: Re-fetch any sitting crawled more than this many days ago.
    crawl_recrawl_age_days: int = 90

    # --- Page budgets (spec §7.1) ---
    page_budget_html_bytes: int = 102400
    page_budget_links: int = 400
    page_budget_css_bytes: int = 2048

    # --- Service ---
    app_port: int = 8765
    tokens_path: Path = field(default_factory=lambda: Path("tokens.yaml"))
    parser_version: str = "sprs-1.0"
    #: Structured request-log file (D-09). Empty = stream only (test default);
    #: the systemd unit sets HANSARD_LOG_FILE.
    log_file: str = field(
        default_factory=lambda: os.environ.get("HANSARD_LOG_FILE", "")
    )
    #: Operator-supplied robots.txt override file (27.2-REQ-05). None = serve
    #: the built-in default body; a path to an existing file is served verbatim
    #: (retrieval policy, NOT access control — RFC 9309).
    robots_path: Optional[Path] = field(default=None)

    # --- Sliding-window lengths (derived from the per-minute / per-day limits) ---
    minute_window_s: int = 60
    day_window_s: int = 86400

    # --- HTTP status codes (named, not magic) ---
    http_bad_request: int = 400
    http_not_found: int = 404
    http_unprocessable: int = 422
    http_too_many_requests: int = 429
    http_internal_error: int = 500
    http_bad_gateway: int = 502
    http_unavailable: int = 503
    http_gateway_timeout: int = 504


def _env_settings() -> Settings:
    """Build the Settings singleton from the locked defaults + HANSARD_* env.

    Every deployment-relevant field is overridable (27.2-REQ-02); the
    capability-token shape guard (token_min_len/token_max_len) is deliberately
    NOT overridable — auth.py builds its token regex from those at import.
    Hydrating here (before any consumer import) is what makes a fresh
    container honor its env.
    """
    overrides: dict[str, object] = {}
    if os.environ.get(ENV_APP_PORT):
        overrides["app_port"] = int(os.environ[ENV_APP_PORT])
    if os.environ.get(ENV_INDEX_DB_PATH):
        overrides["index_db_path"] = Path(os.environ[ENV_INDEX_DB_PATH]).expanduser()
    if os.environ.get(ENV_TOKENS_PATH):
        overrides["tokens_path"] = Path(os.environ[ENV_TOKENS_PATH]).expanduser()
    if os.environ.get(ENV_PUBLIC_BASE_URL):
        overrides["public_base_url"] = str(os.environ[ENV_PUBLIC_BASE_URL])
    if os.environ.get(ENV_LOG_FILE):
        overrides["log_file"] = str(os.environ[ENV_LOG_FILE])
    if os.environ.get(ENV_ROBOTS_PATH):
        overrides["robots_path"] = Path(os.environ[ENV_ROBOTS_PATH]).expanduser()
    return replace(Settings(), **overrides)


#: Module-level singleton consumed across the package.
settings = _env_settings()
