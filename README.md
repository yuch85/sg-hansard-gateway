# sg-hansard-gateway

**Give any chat app access to Singapore Parliamentary Hansard.**

Bring up the gateway on a VPS or self-hosted box, generate a token, and hand
your chat window your server URL + a short instruction sheet. From then on
the chat app researches Hansard for you — search, sit by date, full verbatim
transcripts with a SHA-256 provenance footer — and every answer is
re-verifiable. No MCP server, no connector, no SDK, no headless browser:
the gateway is plain server-rendered HTTP, and the token is the entire
security model (rotatable, revocable, digests-only at rest).

## The two modes

| | **Mode 1 — drop-in (click-only)** | **Mode 2 — direct GET** |
|---|---|---|
| For | Chat apps whose web tool can open a URL and follow links (ChatGPT is the reference client — see the [demo](#demo)) | More capable agents that can issue plain HTTP GETs (code tools, API tools) |
| How | Paste one instruction sheet into the chat window with your URL + token. The client opens the start page and moves **only by following links rendered on the page** — it never constructs a URL | The same content as plain `GET`s: `/a/{token}/search?q=…`, `/a/{token}/date/YYYY-MM-DD`, `/a/{token}/report/REPORT_ID` |
| Setup | The sample sheet is [`docs/chat-instructions.md`](docs/chat-instructions.md) — paste it as-is (it's written to be dropped in as the first message or a system instruction) | Nothing to install; endpoints are documented inline in the sheet and in the public machine contract `{your-url}/openapi.json` |
| Trust | The client is fenced by the instructions ("never invent a URL") | No fence needed — the client is already programmatic |

Mode 2 is a strict superset of Mode 1: every Mode 2 URL is exactly the link
a Mode 1 page renders. If your client can do both, Mode 1 is the proven
path; Mode 2 is the escape hatch for clients whose browsing tool refuses the
start page.

## Demo

A walkthrough of the gateway as driven by **ChatGPT** (Mode 1 — click
navigation from one constant URL, no headless browser). The video below is a
screen recording of that session — ChatGPT answers from the gateway and
cites it:

[![ChatGPT demo of the Hansard Gateway](docs/demo-poster.png)](https://yuch85.github.io/sg-hansard-gateway/media/demo-chatgpt.mp4)
<sub><b>▶️ Watch the demo</b> (plays in a new tab · ~55 s · [mp4](media/demo-chatgpt.mp4), 7.1 MB)</sub>

## Quickstart

**Run the published image (fastest):**

```bash
# one-time: authenticate to GHCR (a token with read:packages scope, or `gh auth login`)
docker login ghcr.io
docker pull ghcr.io/yuch85/sg-hansard-gateway:latest
docker run --rm -p 8000:8000 ghcr.io/yuch85/sg-hansard-gateway:latest   # boots in the empty state
```

**Build from source:**

```bash
git clone https://github.com/yuch85/sg-hansard-gateway.git && cd sg-hansard-gateway
uv sync --extra dev
uv run python -m pytest -m 'not live'   # full offline suite, no secrets needed
docker build -t sg-hansard-gateway:latest .
docker run --rm -p 8000:8000 sg-hansard-gateway:latest   # boots in the empty state
```

The container boots with no index and no tokens and degrades to an empty
state (`/health` still returns `{"status":"ok"}`). Add a token and an index
below and the same container serves.

### Quick Docker Compose

A minimal single-service compose (no Caddy — put your own HTTPS reverse
proxy in front, or use the [examples/caddy/](examples/caddy/) reference for
a two-container Caddy setup):

```yaml
services:
  gateway:
    image: ghcr.io/yuch85/sg-hansard-gateway:latest
    ports:
      - "8000:8000"
    environment:
      HANSARD_PUBLIC_BASE_URL: https://your-domain.example   # REQUIRED — your domain
      HANSARD_LOG_STREAM: "1"
    volumes:
      - hansard-data:/data          # index.db + tokens.yaml live here (persistent)
    restart: unless-stopped

volumes:
  hansard-data:
```

Generate a token and place it + an index on the volume before first start
(see [Token management](#token-management--the-easy-way) and [Crawl](#crawl--easy-to-run)).

## Token management — the easy way

Tokens are how your URLs are secured: every content URL embeds one, you can
rotate or revoke any of them, and a client holding a dead token gets a
byte-identical 404 (no enumeration signal).

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

The four groups and why the wildcard carries the Allow: user-directed
retrieval agents get explicit `Allow: /a/` groups, but some fetchers do not
expose a distinct product user agent — under RFC 9309 an unknown product
token falls through to the wildcard, so only the wildcard's `Allow: /a/`
makes the policy robust for them. The wildcard then `Disallow: /` for
everything else. Note also that `noindex` (via `X-Robots-Tag` on every
protected page) controls *indexing*, not *fetch permission* — they are
different policies and both are in place.

**Compatibility.** The gateway has been **tested with ChatGPT** (the
link-only client flow in the demo above). It is expected to work with other
chat apps that drive a similar link-only / fetch-then-navigate environment,
though that is not exhaustively verified. Some clients (notably Claude) have
shown inconsistent behavior around robots.txt interpretation; if a client
refuses the site despite a permissive robots file, treat it as a
client-side policy decision, not a gateway defect — the token remains the
only real gate, and the robots file is advisory retrieval policy.

## Security notes

- The token rides in the URL path, so no access logs exist anywhere in the
  chain — the reference vhost carries no access logging and the app logs
  only the token label.
- Invalid tokens get a byte-identical 404, not a 401 — there is no
  enumeration signal.
- Upstream requests go only to the host allowlist, never to an operator-
  supplied URL.
- Protected content responses carry `Cache-Control: private, no-store`.

## Independent Project Notice

This is an independent, open-source software project and is not
affiliated with, operated by, sponsored by, or endorsed by the
Parliament of Singapore or any Singapore Government agency.

The project provides an interoperability layer for AI-assisted
research against publicly available Singapore Parliamentary Hansard
resources. The official Parliamentary record remains the authoritative
source. Users should verify research results against the official
source before relying on them.

This project does not bypass authentication, access controls,
paywalls, or other technical restrictions.

## Acknowledgements

This project builds on prior work in the Singapore parliamentary data
ecosystem:

- [sgparl](https://github.com/wongpeiting/sgparl) (wongpeiting) — the
  maintained reference implementation for the SPRS API, and the primary
  source for this project's SPRS API model. Ported or derived from it:
  the required request headers (bare requests are rejected upstream);
  the search sweep/dedupe algorithm in
  `hansard_gateway/search/sprs.py` (probe the load-balanced `maxResult`
  totals, sweep 20-row pages, dedupe by `reportId` until a no-gain budget
  is reached — the two backend nodes disagree on totals and orderings);
  the finding that the legacy `getHansardReport` endpoint is retired
  (both eras are served by `searchResult` + `getHansardTopic`, tagged
  `reportVersion` sprs2/sprs3); the upstream retry policy (500s are
  intermittent noise; "No Results Found" 500 is a terminal empty
  result); trailing-`#` stripping of `reportId` before topic fetches;
  and both speaker-extraction parsers (pre-2012 `<!--MP_NAME:...-->`
  HTML comments with `<b>` fallback; post-2012 `<p><strong>` walks with
  carry-forward). Licensed CC0 1.0 (public domain); credited here as good
  practice.
- [singapore-parliament-speeches](https://github.com/parleh-mate/singapore-parliament-speeches)
  and its [dbt model](https://github.com/parleh-mate/singapore-parliament-speeches-dbt)
  (parleh-mate) — raw Hansard extraction; prior art consulted during
  research. Licensed CC0 1.0.
- [parliament-summary](https://github.com/limdingwen/parliament-summary)
  (limdingwen) — oversight/summary site; prior art consulted during
  research. Licensed CC0 1.0.
- [second-reading](https://github.com/isaacyclai/second-reading)
  (isaacyclai) — modern Hansard UI; prior art consulted during
  research. Licensed MIT.

The gateway itself is independent software — no code is vendored from the
above projects.
