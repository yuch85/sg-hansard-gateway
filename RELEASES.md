# Releases

Docker image tags for `ghcr.io/yuch85/sg-hansard-gateway`. `latest` always
points at the newest release. The gateway is a self-contained container —
upgrading is `docker pull` + recreate (your `/data` volume carries the index
and tokens; no migration is needed between these releases).

## 0.1.3 (2026-09-21)

**Human-facing restyle + speech citation** (mission 010, phase 27.3).

The gateway's pages were LLM-readable but not human-readable. 0.1.3
restyles every page type in a newspaper-warm system (cream paper, ink
serif, terracotta accents) and adds a citation highlight: every speech
carries a stable `#speech-N` Cite URL that an AI can discover in the
page and that, opened directly in a browser, scrolls to and
gold-highlights that exact speech.

- **Human-facing restyle, all page types** — masthead + provenance
  footnote on reports, sticky speaker index (turn number + speaker +
  first words), turn numbers, two-level speaker hierarchy, a
  persistent-speaker sticky header (the current speaker's name stays
  pinned while reading their speech), a print stylesheet, a quiet
  one-line nav bar kept at top AND bottom, and card-grid layouts for
  the search, launcher, nav, date, and facet pages. Zero JavaScript,
  zero forms, zero iframes, no external resources — every page is
  still one HTML document with inline CSS.
- **Citation highlight** — every speech on a report page gains
  `id="speech-N"` and one `Cite:` line carrying the absolute URL
  `…/report/{id}#speech-N` (plus its visible `.u` twin). Opening that
  URL in a browser scrolls to the speech and shows the gold
  "Cited passage" highlight — **per-navigation**: the highlight shows
  on the navigation that lands on the URL (there is no persistence,
  and there is no JavaScript to add one). Cite lines are **capped per
  page** so long sittings stay within the gateway's link/size budgets
  (the first N speeches in sequence order are indexed; when a cap
  drops a speech, a note line on the page says so).
- **Machine contract unchanged** — `?format=json` and `?format=text`
  are byte-identical to 0.1.2 for the corpus pages; the
  invalid-token 404 body remains byte-pinned (it moved to the
  token-free, nav-free 0.1.3 page — the new pin is
  `db5bd37212a04ab36d3eb130cdc7abfd`, the old pin
  `1a29cc1330d50031993c3cbcde2318d7` is the 0.1.2 value); transcript
  verbatim + SHA-256 footer untouched. The R1–R9 conformance suite
  was extended with the new assertions: the Cite cap arithmetic,
  `.u` visibility in screen AND print, and the rendered extraction
  corpus against a frozen machine-surface baseline.
- **Upgrade note** — `docker pull` + container recreate, exactly as
  prior releases; the `/data` volume carries the index and tokens
  over (no migration step). See the "v0.1.3 upgrade (image
  rollover)" section of `deploy/RUNBOOK.md` (steps 0–6, including the
  pre-upgrade image-identity capture and the image-rollover rollback).
- **Ordinal note** — Cite URLs reference the report's current speech
  numbering. Report content is immutable once crawled, so the
  `#speech-N` ordinals are stable for the life of a published report;
  this is the accepted v1 semantic (durable per-speech identifiers
  are a future consideration).
- **Known pre-existing exception** — the live worst-case search page
  renders 105.9 KB against the 100 KB page budget. This condition
  predates 0.1.3; 0.1.3 does not increase the page beyond the
  existing condition except for its approved shared-CSS change. It is
  not treated as a 0.1.3 regression. Future work will reduce the
  worst-case search page below 100 KB. The offline conformance suite
  does not exercise the live worst-case page, so this release does
  not claim universal budget enforcement across every live page.

Digest: `<sha256 of the 0.1.3 manifest — recorded in the mission log at push time>`
(= `latest`). Source: commit `845cbd4`
(github.com/yuch85/sg-hansard-gateway).

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
