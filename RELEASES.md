# Releases

Docker image tags for `ghcr.io/yuch85/sg-hansard-gateway`. `latest` always
points at the newest release. The gateway is a self-contained container —
upgrading is `docker pull` + recreate (your `/data` volume carries the index
and tokens; no migration is needed between these releases).

## 0.1.7 — 2026-09-21 (footer nav + slim byline, universal JSON citation, safe cite cap — mission 010 v0.1.7)

Image: `ghcr.io/yuch85/sg-hansard-gateway:0.1.7` (= `:latest`)
digest `sha256:5a21cb8b48c689e595615c89f0397eb556cfc7b41815805f79a3ee9d477c9214`
(= local image ID built from commit `d196072`; the exact image that passed
the offline suite (443), the 390 px named checks, and the hard
machine-contract gate (7/7) is the image running in production).
Rollback: recreate the container from
`ghcr.io/yuch85/sg-hansard-gateway:0.1.6`
(digest `sha256:59337d154aca5bb872ec1a959eaec5d2e4653e107d1253f0e1a4c0bbfa15341a`)
— copy the FULL env list from a `docker inspect` config capture (T-27-77).

**What changed (three work items, copilot-reviewed plan — 3-pass
SOUNDS-GOOD gate):**

- **Report page-nav moved to the footer + byline slimmed (HTML-only).**
  The report page's page navigation (This sitting / Home / Previous /
  Next / Search title terms) now renders in the footer, after the
  provenance section; the report's info `dl` (Date/Section/Source/Official
  SPRS record) stays at the top — the human's first "what is this" answer.
  The byline no longer duplicates any `dl`-owned metadata (date,
  parliament, session, sitting, volume, section): it is now a compact
  descriptor, e.g. "Hansard report — Second Reading Bills". The
  global nav (every page) is untouched.
- **Per-speech citation URLs in `?format=json` (the one approved additive
  machine-contract amendment).** Every speech object now carries
  `"speech_id": "speech-N"` and `"cite_url"` — the absolute report URL +
  `#speech-N`, built by the SAME `abs_report_url` helper the HTML Cite
  lines use (no separate URL logic). This is UNIVERSAL: it does not depend
  on the HTML Cite cap, so even reports whose HTML shows zero Cite lines
  (e.g. bill-773) expose a valid cite URL for every speech. An AI reading
  the JSON can now cite the exact speech that supports a point; opening
  the `cite_url` in a browser scrolls to and gold-highlights that speech
  (the existing CSS `:target`). All pre-existing JSON fields are
  value-identical; the two new keys are appended per speech.
  `?format=text` is byte-identical.
- **Cite-cap constant replaced by a provable worst-case bound.**
  `CITE_EST_BYTES_PER_LINE = 220` → `CITE_WORST_BYTES_PER_LINE = 700`,
  derived from the maximum allowed dimensions of every variable component
  (token ≤ `settings.token_max_len`, report id ≤ 64, fragment ≤ 10 chars,
  post-escape speaker label ≤ 80, fixed public base URL) — O(1), enforced
  by tests, not measured from one fixture. Consequence: on reports where
  the old estimate over-admitted Cite lines, the HTML Cite count is now
  more conservative (bill-774: 14 → 11 live). Addressability is not
  affected — the JSON `cite_url` (above) is universal, and every speech
  keeps its `id="speech-N"` target regardless of the cap.
- **Sticky speaker name: diagnosed, no CSS change.** At 390 px under CDP
  the per-speech sticky header behaves exactly as designed: it persists
  while its own speech is scrolling, and the next speaker's header takes
  over at the article boundary. No overflow-creating ancestor exists in
  the computed style chain. The acceptance behavior is per-article sticky +
  next-speaker takeover (not a global cross-article speaker bar — that
  would duplicate speaker text, which the machine-readability invariant
  forbids). If a specific phone still shows the header disappearing
  mid-speech, that is a device-specific observation to report (it did not
  reproduce under CDP at 390 px).

**Known pre-existing issue (NOT introduced by 0.1.7, unchanged since
0.1.6/0.1.5):** the speaker-index TOC (`nav.toc li.toc-mp`) causes
horizontal overflow at phone width (document scrollWidth ≈ 705 px at a
390 px viewport). Measured identical on the pre-0.1.7 tree. Out of scope
for 0.1.7; will be addressed in a follow-up release.

- Machine contract: `?format=text` byte-identical (asserted live + offline);
  `?format=json` — exactly two additive per-speech fields
  (`speech_id`, `cite_url`), all pre-existing fields value-identical
  (hard build gate, 7/7 PASS before the image was built).
- 404 body UNCHANGED (md5 `d9eb4742…`): base.html was not modified, so no
  T-27-62 re-pin was required (verified live post-cutover).
- CSS budget: unchanged (no base.html CSS delta this release).
- Suite: 443 passed / 2 deselected.

## 0.1.6 — 2026-09-21 (mobile nav word-wrap fix, mission 010 c7-F4b)

Image: `ghcr.io/yuch85/sg-hansard-gateway:0.1.6` (= `:latest`)
digest `sha256:59337d154aca5bb872ec1a959eaec5d2e4653e107d1253f0e1a4c0bbfa15341a`
(= local image ID built from commit `4ad229f`; the exact image that passed
local + 390 px QC is the image running in production).
Rollback: recreate the container from
`ghcr.io/yuch85/sg-hansard-gateway:0.1.5`
(digest `sha256:7d48200636a96287969b795dbc6d8b0b8e7ac8c3780f4df348016b17e9ac8df9`)
— copy the FULL env list from a `docker inspect` config capture (T-27-77).

**The fix:** on 0.1.5 the pre-transcript block (global nav + page nav +
search-term echoes) did not word-wrap to the window width on phones. Cause:
the 0.1.4 F4 single-line `.u` clip
(`white-space:nowrap; overflow-x:auto`) made the nav echo URLs — the longest
tokens on the page — overflow the viewport horizontally instead of wrapping.
The transcript itself has no `.u`, which is why it wrapped fine. 0.1.6 adds
a narrow-width rule so the nav `.u` URLs wrap:
`nav.global-nav span.u, nav.page-nav span.u { display:inline;
white-space:normal; overflow:visible; max-height:none; margin-top:0;
word-break:break-all }`. Speech `.u` keeps the single-line clip (it sits
inside a padded speech card, no page overflow). R2-safe: the URL text stays
in the DOM and in extraction; only the wrap behavior changes.

- Machine contract: `?format=text` / `?format=json` unchanged (CSS-only).
- 404 body re-pinned (T-27-62): md5 `e9c1042c…` → `9e8affea…`; no-enumeration
  invariant (token-free + nav-free) intact.
- Offline suite: 431 passed / 2 deselected. 390 px verification: document
  width == viewport (no horizontal overflow), nav `.u` wraps
  (`white-space:normal`, `word-break:break-all`), transcript unchanged.

## 0.1.5 — 2026-09-21 (mobile viewport fix, mission 010 c7)

Image: `ghcr.io/yuch85/sg-hansard-gateway:0.1.5` (= `:latest`)
digest `sha256:7d48200636a96287969b795dbc6d8b0b8e7ac8c3780f4df348016b17e9ac8df9`
(= local image ID `b95a5094`, built once from commit `ffca05b`; the exact
image that passed local + 390 px QC is the image running in production).
Rollback: recreate the container from
`ghcr.io/yuch85/sg-hansard-gateway:0.1.4`
(digest `sha256:3aeb1d2ebab3b5b9138dc330968c0fbbcfa0c3a8921c752fe3a16981105a2a1e`,
local ID `6afd9c3e332e`, retained) — copy the FULL env list from a
`docker inspect` config capture (T-27-77), not just volume/port/restart.

**The fix:** v0.1.4 shipped with **no `<meta name="viewport">` on any page**.
Phones (and any narrow client) then lay the page out at the 980 px desktop
viewport and zoom out ~2.5× — text rendered at ~6 px, and the content sat in
a third-width column with a wide empty right margin. The 62rem narrow media
queries (the 0.1.4 mobile type scale, the single-line `.u` clip, the
scroll-margin clearance) never fired on a real phone. One line added to
`base.html` head —
`<meta name="viewport" content="width=device-width, initial-scale=1">` —
makes every page lay out at the device width; the 0.1.4 narrow-scale CSS now
applies as intended (17 px root, 14 px floor, readable on phones).

- Machine contract: `?format=text` / `?format=json` unchanged (the meta is an
  HTML `<head>` line; the plain-text and JSON serializers never emit it).
- 404 body re-pinned (T-27-62): md5 `c8605b58…` → `e9c1042c…` (the invalid-
  token 404 inherits base.html); no-enumeration invariant (token-free +
  nav-free) intact.
- Offline suite: 431 passed / 2 deselected. 390 px verification (CDP
  390×844 @3x): device-width resolves (722 CSS px, not 980), narrow scale
  active (body 17 px / speech 16.15 px / TOC 14.45 px / `.u` 14.11 px —
  all above the 14 px floor), `:target` gold highlight visible.

Known boundary (unchanged, T-27-67/T-27-79): citation Cite lines +
`#speech-N` deep links exist in the HTML view only; a text-fetching AI's
extracted representation does not preserve them, so the highlight fires on a
real browser click, not from an AI's text fetch. Follow-ups queued (c8:
deterministic Cite coverage across modern reports; c9: per-speech
`speech_id` + `cite_url` in `?format=json` from the same URL source of truth
as the HTML).

## 0.1.4 — 2026-09-21 (mobile responsiveness, mission 010 CHARLIE)

Image: `ghcr.io/yuch85/sg-hansard-gateway:0.1.4` (= `:latest`)
digest `sha256:3aeb1d2ebab3b5b9138dc330968c0fbbcfa0c3a8921c752fe3a16981105a2a1e`
(= local image ID `6afd9c3e332e`, built once from commit `d94170d`; the exact
image that passed local Docker QC is the image running in production).
Rollback: recreate the container from `ghcr.io/yuch85/sg-hansard-gateway:0.1.3`
(image ID `323a5ec42059`, retained locally).

Mobile fixes (390 px, phone-class; desktop rendering unchanged — all changes
are under the `@media (max-width:62rem)` query or parser-side):

- **F4 — `.u` echo single-line at narrow width.** `span.u` was wrapping to
  4–6 lines per URL (230 spans ≈ 17 KB of vertical space on bill-774; the
  first speech sat 2,020 px down). Now `display:inline-block;
  white-space:nowrap; overflow-x:auto` — one line, full URL horizontally
  reachable, nothing hidden (R2: the text stays in the DOM and in extraction;
  `overflow-x:auto` is not a hiding declaration). Global nav collapses from
  222 px to ≈ one row; page nav from 501 px to ≈3–4 compact rows.
- **F2 — mobile type scale.** The 62rem query was layout-only (no font-size
  rules). Now `html{font-size:106.25%}` (17px root lifts the whole rem scale)
  + every sub-14px declaration overridden to a 0.83rem (14.11px) floor
  (`.u` included — quietness now comes from single-line + muted color, not
  size). Measured: no element below 14px at 390px.
- **F1 — `:target` highlight de-occlusion.** The gold highlight was applied
  but the sticky `.sp-head` painted over the "Cited passage" marker + the
  top of the gold. Now `scroll-margin-top:var(--sp-head-clear)` (11rem =
  187px, from the measured worst-case header height of 160px on the
  production container — the plan's 8rem/136px was a single-element
  measurement, corrected at QC) + the marker deterministically anchored
  below the header band. Verified: fragment navigation lands with the
  highlight top edge and marker clear of the sticky header.
- **F3 — legacy-sitting paragraph restore.** Pre-2003 payloads carry NO
  `<p>` markup (the 1993 Application of English Law Bill payload: 22 KB,
  0 `<p>`, 112 `<br>`, wrapped in full `<html>`) and 2004-era payloads use
  `<p>` blocks; the parser collapsed both into one string per speech. Now
  both eras are split on their real boundaries at parse time. **Intentional
  compatibility change, confined to legacy reports outside the frozen
  compatibility corpus:** legacy `?format=text` gains the restored paragraph
  newlines (character content excluding boundary whitespace is provably
  unchanged — machine-asserted), and legacy `transcript_sha256` values
  change correspondingly. Modern reports (the frozen corpus, e.g. bill-774)
  remain `?format=text` byte-identical.
- **404 re-pin.** The tokenless invalid-token 404 body inherits base.html's
  `<style>`; new md5 `c8605b585f493a6b5e6d936f111c9793` (token-free +
  nav-free no-enumeration invariant intact; capture on an EXISTING route).

Caps (measured on the production container, bill-774): inline CSS 8,141 B
≤ 8,192 B budget; anchors 51 ≤ 400; page 101,258 B — see the known
pre-existing exception below (unchanged by this release). Zero JS/forms/
iframes/external resources (R4 gate green). Suite: 431 passed / 2
deselected.

Known pre-existing exception (carried from 0.1.3, unchanged): the live
206-hit search rendering is 105.9 KB against the 100 KB page budget. This
condition predates 0.1.3 (and therefore 0.1.4); the offline conformance
suite does not lint that live worst-case page, so the 100 KB budget is not
claimed as universally enforced. Future work will reduce it.

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

Digest: `sha256:adc6b241784fb5d255815730666df876c59844066d174d2dd44b66d6db09274a`
(= `latest` — both tags' registry digests extracted via
`docker buildx imagetools inspect` and asserted equal at push time).
Source: commit `aee162d` (github.com/yuch85/sg-hansard-gateway; the
app code is the wave-4 close `845cbd4` plus the 0.1.3 release docs).

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
