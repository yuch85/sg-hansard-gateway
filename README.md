# Hansard Gateway

A live, token-gated, server-rendered HTTP gateway over Singapore Hansard
(sprs.parl.gov.sg) for link-only LLM clients: every page is plain HTML with
click navigation from one constant URL, each transcript carries a SHA-256
verbatim footer, and no headless browser is involved anywhere.

The included Caddy configuration is a reference reverse-proxy deployment; Caddy is not required by the application and may be replaced by any HTTPS reverse proxy.

## Quickstart

```bash
git clone <repo-url> && cd <repo-dir>
uv sync --extra dev
uv run python -m pytest -m 'not live'   # full offline suite, no secrets needed
docker build -t hansard-gateway:latest .
docker run --rm -p 8000:8000 hansard-gateway:latest   # boots in the empty state
```

The container boots with no index and no tokens and degrades to an empty
state (`/health` still returns `{"status":"ok"}`). Add a token and an index
below and the same container serves.

## Token management — the easy way

One CLI in every runtime. The flow is exactly three steps: generate on the
host, mount read-only, start the container.

1. **Generate** on the host: `uv run hg-tokens generate <label>` — prints the
   `hg_…` plaintext exactly once (store it now; only its SHA-256 is kept) and
   writes `tokens.yaml` at mode 0600 next to the invocation.
2. **Mount** the store read-only at the token path (container default
   `/data/tokens.yaml`), e.g. `--mount type=bind,source=./tokens.yaml,
   target=/data/tokens.yaml,readonly=true`.
3. **Start** the container.

Subcommands (the store holds sha256 digests only — the plaintext is never
stored, checks are constant-time, and a malformed store fails closed):

| Command | Effect |
|---|---|
| `hg-tokens list` | label / enabled / last4 — never the plaintext |
| `hg-tokens generate <label>` | new token; plaintext printed once |
| `hg-tokens rotate <label>` | new plaintext printed once; the old token stops working |
| `hg-tokens disable <label>` | revoke |

In a running container: `docker exec <container> hg-tokens <subcommand>`.
The store has no hot-reload — restart the container after any token change.

## Docker deployment

Environment variables are documented in [.env.example](.env.example) — every
`HANSARD_*` var with its code default and its container default. The `/data`
volume is the contract: the index (`/data/index.db`) MUST live on a
persistent volume (named volume or bind mount); without it the app simply
degrades to the empty state.

Two vars need operator attention:

- `HANSARD_PUBLIC_BASE_URL` **MUST be set to your domain.** The code default
  is the live origin and must never ship into a new deployment — absolute
  token-bearing URLs are built from it.
- `HANSARD_LOG_FILE` + `HANSARD_LOG_STREAM=1` for production logging (the app
  creates the log parent dir itself at boot — no pre-creation step anywhere).

Always run via the module entrypoint (`python -m hansard_gateway`), never
`uvicorn …:app` — the uvicorn CLI path skips the logging setup, which would
silently disable `HANSARD_LOG_FILE` / `HANSARD_LOG_STREAM`.

The image carries a HEALTHCHECK; `docker inspect` reports `healthy` shortly
after boot.

## Crawl — easy to run

The offline crawl builds the term index. Three equivalent ways:

- **On the host:** `uv run python -m hansard_gateway crawl once [--cold]`
- **Ad-hoc in a container** (the image has no ENTRYPOINT, so the container
  form is the full module invocation — a bare `crawl once` after the image
  would replace the CMD and fail):
  `docker run --rm -v hansard-index:/data <image> uv run python -m hansard_gateway crawl once [--cold]`
- **Scheduled:** the same `docker run` line from a host timer or cron for the
  weekly cadence (the systemd timer under `deploy/systemd/` is the model for
  a host-side timer instead). `--cold` is a full rebuild from the start date;
  the default is incremental/resumable.

The crawl writes through a staging file and an atomic `os.replace` — a
running container follows the swap without a restart (the loader re-probes
`meta.build_id` on each query and reopens when it changes).

## Reference Docker Compose deployment (Gateway + Caddy)

[examples/caddy/](examples/caddy/) is a two-container reference: the gateway
plus a Caddy reverse proxy with auto-HTTPS. The included Caddy configuration
is a reference reverse-proxy deployment; Caddy is not required by the
application and may be replaced by any HTTPS reverse proxy.

The Caddyfile deliberately carries no access-logging directive: the
capability token rides in the URL path, and access logs record full URIs, so
the proxy must not log token-bearing URIs. Sanitized application-level
structured logging (token label only) is the audit trail.

## robots.txt operator options

The gateway serves a built-in default robots.txt (148 bytes):

```
User-agent: Claude-User
Allow: /a/

User-agent: Claude-SearchBot
Allow: /a/

User-agent: ClaudeBot
Allow: /a/

User-agent: *
Allow: /a/
Disallow: /
```

Set `HANSARD_ROBOTS_PATH` to a file to serve your own body instead (unset =
the default above; the file is served verbatim).

**robots.txt is retrieval policy, NOT access control** (RFC 9309): the token
remains the only gate, and a crawler without one gets the identical byte-404.

The four groups and why the wildcard carries the Allow: Claude's user-facing
agents (Claude-User, user-directed retrieval, and Claude-SearchBot) and
ClaudeBot get explicit `Allow: /a/` groups, but Claude's fetcher does not
expose a distinct product user agent — under RFC 9309 an unknown product
token falls through to the wildcard, so only the wildcard's `Allow: /a/`
makes the policy robust for it. The wildcard then `Disallow: /` for
everything else. Note also that `noindex` (via `X-Robots-Tag` on every
protected page) controls *indexing*, not *fetch permission* — they are
different policies and both are in place.

Worked example: on 2026-09-18 Claude's user-directed retrieval client began
refusing the site with a `ROBOTS_DISALLOWED` error even though the permissive
robots file had been fetched and honored — the site was refused on Claude's
side and the cause was never resolved upstream. The escalation packet
prepared at the time (server-side logs, exact robots bytes, an independent
client control fetch) is the diagnostic record of that incident.

## Security notes

- The token rides in the URL path, so no access logs exist anywhere in the
  chain — the reference vhost carries no access logging and the app logs
  only the token label.
- Invalid tokens get a byte-identical 404, not a 401 — there is no
  enumeration signal.
- Upstream requests go only to the host allowlist, never to an operator-
  supplied URL.
- Protected content responses carry `Cache-Control: private, no-store`.

## Acknowledgements

This project builds on prior work in the Singapore parliamentary data
ecosystem:

- [sgparl](https://github.com/wongpeiting/sgparl) (wongpeiting) — the
  maintained reference implementation for the SPRS API. The search
  sweep/dedupe algorithm in `hansard_gateway/search/sprs.py` (probe the
  load-balanced `maxResult` totals, sweep 20-row pages, dedupe by
  `reportId` until a no-gain budget is reached) is ported from it.
  Licensed CC0 1.0 (public domain); credited here as good practice.
- [singapore-parliament-speeches](https://github.com/parleh-mate/singapore-parliament-speeches)
  and its [dbt model](https://github.com/parleh-mate/singapore-parliament-speeches-dbt)
  (parleh-mate) — raw Hansard extraction; prior art consulted during
  research. Licensed CC0 1.0.
- [parliament-summary](https://github.com/limdingwen/parliament-summary)
  (limdingwen) — oversight/summary site; prior art consulted during
  research. Licensed CC0 1.0.
- [second-reading](https://github.com/isaacyclai/second-reading)
  (isaacyclai) — modern Hansard UI; prior art consulted during research.
  Licensed MIT.

The gateway itself is independent software — no code is vendored from the
above projects.
