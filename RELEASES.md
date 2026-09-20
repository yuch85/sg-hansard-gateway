# Releases

Docker image tags for `ghcr.io/yuch85/sg-hansard-gateway`. `latest` always
points at the newest release. The gateway is a self-contained container —
upgrading is `docker pull` + recreate (your `/data` volume carries the index
and tokens; no migration is needed between these releases).

## 0.1.2 (2026-09-20)

**Provenance links point at the exact Hansard section** (mission 008).

The "Official SPRS record" link on every report page (HTML, text, and JSON
`source_url`) previously pointed at the **full sitting** report on the
official Singapore Parliamentary Reporting System (SPRS). It now points at
the **same section** the gateway page shows:

- Legacy report ids (`026_19950301_S0002_T0009` — the spec-style
  `htmlFileName` with the `_S{N}_T{N}` suffix) →
  `https://sprs.parl.gov.sg/search/#/topic?reportid=<id>`
- Modern report ids (`bill-773` and similar live `reportId`s) →
  `https://sprs.parl.gov.sg/search/#/sprs3topic?reportid=<id>`

Both routes render the single section in a normal browser (verified in
headed Chrome; they are the URLs SPRS's own search results link to). The
full-sitting routes (`#/report`, `#/fullreport`) are kept only as a fallback
for id shapes the gateway does not recognise. The provenance label was
renamed from "Official SPRS sitting record" to "Official SPRS record", with
the note that it opens the section on the official site in a browser.

Notes:

- No index, token, endpoint, or configuration changes — a pure link-target
  fix. Existing deployments upgrade with a normal image swap.
- The SPRS section pages are JavaScript-rendered: a human in a browser sees
  the section; a plain-text fetcher gets the application shell. That is the
  intended division of labour — the gateway page remains the reliable view
  for AI clients; the SPRS link is the human verification path.

Digest: `sha256:343d7be1e18f92b5c3e2270d60b495a72fed37ddf9fa1e0eb02cbb53d170fb68`
(= `latest`). Source: commit `376ac59`
(github.com/yuch85/sg-hansard-gateway).

## 0.1.0 (2026-09-19)

First public release (mission 006 / Phase 27.2).

- Self-hosted single-container gateway: server-rendered HTML (plus
  `?format=json` and `?format=text` on every content route) over the
  Singapore Parliamentary Hansard corpus, retrieved live from SPRS/PAIR.
- Two access modes over the same content: **Mode 1 drop-in (click-only)**
  — a constant start URL + instruction sheet; the client moves only by
  following rendered links (verified end-to-end with ChatGPT, see the
  README demo). **Mode 2 direct GET** — plain `GET`s on
  `/a/{token}/search?q=…`, `/date/YYYY-MM-DD`, `/report/{id}`; a strict
  superset of Mode 1.
- Pre-indexed navigation tree (optional crawl): A–Z topic ladder, year /
  member / bill facets, exact-term search boosting — index-only pages work
  even when the upstream is degraded.
- Security: capability tokens in the URL path (SHA-256 digests at rest,
  constant-time checks, byte-identical 404 for invalid tokens), per-token
  rate limits (300/min, 5000/day), host-locked upstream allowlist,
  `no-store` + `noindex` on protected content, no access logs of
  token-bearing URIs.
- Public machine contract at `/openapi.json` (no token) + `/docs`;
  `Caddy` reference deployment in `examples/caddy/`.
