# Wave-0 machine-surface baseline (IMMUTABLE)

This directory is the **IMMUTABLE Wave-0 machine-surface baseline** for the
Hansard-gateway UI restyle (phase 27.3). It is the **cumulative expected-delta
reference** that every later wave diffs against, and the directory the FINAL
`--verify` (Plan 05 Task 4) consumes:

```
HANSARD_LIVE_TOKEN="$TOK" uv run python scripts/machine_baseline.py \
    --verify --baseline-dir tests/fixtures/baselines/wave0
```

**Plans 03 and 04 MUST NOT modify any file under `baselines/wave0/`.** These
files must remain byte-identical from this capture onward. If a later wave
believes a Wave-0 file is wrong, it is a plan defect to raise — not a fixture to
edit.

## The two-baseline, two-directory scheme

There are TWO distinct baseline locations, both captured here in Wave 0 and
pinned by name so later waves can never conflate them:

1. **IMMUTABLE Wave-0** — `tests/fixtures/baselines/wave0/<entry>.json` (this
   directory). Captured NOW, **NEVER refreshed**. It is the *cumulative*
   expected-delta reference for the FINAL `--verify`.
2. **Per-wave regression fixtures** — `tests/fixtures/baselines/wave<N>/
   <entry>.json` (N = 1, 2, 3, …). Written at each wave boundary (wave1/ after
   Plan 02, wave2/ after Plan 03, wave3/ after Plan 04 if it ships
   `<details>`) and consumed by the **per-wave** `--verify`:

   ```
   HANSARD_LIVE_TOKEN="$TOK" uv run python scripts/machine_baseline.py \
       --verify --baseline-dir tests/fixtures/baselines/wave<N>
   ```

   The per-wave run proves that wave's delta ONLY (against the previous wave's
   fixture). The `--baseline-dir` argument selects which directory `--verify`
   and the capture write to.

The **offline regression test** (`tests/test_machine_baseline.py`) ALWAYS diffs
against `wave0/` (the immutable reference) — never against `baselines/wave<N>/`.

## The corpus (8 entries, the a1 corpus)

| Entry file            | Route (after `/a/{t}`)                  | Source   | Notes |
|-----------------------|-----------------------------------------|----------|-------|
| `launcher.json`       | `/`                                     | offline  | index-driven |
| `search_hib_p1.json`  | `/search?q=Health%20Information%20Bill` | live     | upstream-stubbed |
| `report_bill-774.json`| `/report/bill-774`                      | live     | upstream-stubbed |
| `nav_h.json`          | `/nav/h`                                | offline  | index-driven |
| `years.json`          | `/years`                                | offline  | index-driven |
| `members.json`        | `/members`                              | offline  | index-driven |
| `bills.json`          | `/bills`                                | offline  | index-driven |
| `date_2026-01-12.json`| `/date/2026-01-12`                      | offline  | index-adjacent; hits upstream (respx-stubbed offline) |

The **6 offline** entries are rendered in-process against the committed test
index (deterministic, reproducible — the real regression target the offline
suite diffs against). The **2 live** entries (report, search) are fetched from
the healthy production deployment; the offline index cannot reproduce their
content, so the offline test asserts only **structural invariants** for them
and full equality is asserted live by `--verify`.

The date TOC (`/date/…`) is the one "offline" entry whose route still hits the
SPRS upstream (a sitting sweep). It is respx-stubbed with the committed
`searchresult_20041019_p1.json` fixture in the offline capture (and in the
offline test) so it is deterministic — see `scripts/offline_render.py`.

## What each JSON holds (the machine-surface schema)

One extraction function (`scripts/machine_surface.py:extract_surface`) produces
every field — the SAME parser the capture tool and the regression test share
(one parser, not two). Fields, in document order:

- `hrefs` — every `href` attribute value in **document order** (including any
  in-page `#fragment` anchors once they exist).
- `anchor_texts` — the anchor text per href, parallel to `hrefs`.
- `u_texts` — every `<span class="u">` text in document order.
- `correspondence` — for each **absolute (https) token-bearing** href, whether
  a `.u` twin with the same URL exists and its index (the R2 href↔`.u`
  correspondence).
- `counts` — `total_anchors`, `absolute_anchors` (absolute + token-bearing),
  `u_spans`.
- `nav_strip` — count of the `Navigate:` marker (must be 2: top + bottom, R8).
- `format_links` — the `?format=json` / `?format=text` sibling URLs where the
  page class has them (R9).
- `byte_size` — `len(html.encode('utf-8'))` (for the §7.1 cap arithmetic in
  later waves).
- `meta` — entry name, route, source, token shape, `html_sha256`,
  `text_sha256`, `text_bytes`, base URL (provenance only).

## Capture procedure

```
TOK=$(cat ~/.hansard-upstream/consumer_token_team1)
HANSARD_LIVE_TOKEN="$TOK" uv run python scripts/machine_baseline.py \
    --baseline-dir tests/fixtures/baselines/wave0
```

Writes `tests/fixtures/baselines/wave0/<entry>.json` (8 files) and
`tests/fixtures/text_format_baseline/<entry>.txt` (8 files, one per entry —
the `?format=text` byte-compat fixtures). JSON is written with a stable
insertion order and `indent=2`; no wall-clock timestamp is embedded (the
mission log carries the date).

## Redaction rule (threat model T-27.3-01)

The live consumer token is a secret and must never be committed:

- **JSON fixtures**: every token-bearing URL is redacted to the `hg_…` shape
  (`scripts/machine_surface.py:redact_url`). Only a sha256 of the body is
  recorded.
- **Live `?format=text` fixtures** (report, search): the text view echoes full
  token-bearing URLs verbatim, so the live token is replaced with `hg_…` before
  the bytes are committed. The offline test asserts only structural invariants
  for these two entries (not text byte-equality), so redaction is safe.
- **Offline `?format=text` fixtures** (the 6 index entries): these carry the
  offline **test token** (`hg_testvalidtoken…`, the non-secret `TEST_TOKEN`
  already committed in `src/hansard_gateway/auth.py`). They are the
  byte-compat regression target and are NOT redacted (the test token is not a
  secret).

`grep -r <live-token> tests/fixtures/` must return nothing.

## Reconciliation against a1's measured counts

a1 measured the LIVE pages with the real 39K-report index. The offline fixtures
are captured against the small committed test index, so some counts differ by
design. The **live** entries (report, search) match a1's live measurement; the
**offline** entries reflect the fixture index:

| Entry   | a1 (live) | wave0 fixture | Reconciliation |
|---------|-----------|---------------|----------------|
| report  | "23 absolute token-bearing", 0 `#` | **23 total anchors = 21 token-bearing + 2 external SPRS provenance** links (which carry a `#/sprs3topic` fragment); 23 `.u` spans | a1's "23 absolute token-bearing" counted **23 total absolute anchors** (21 internal token-bearing + 2 external provenance, both absolute https). The 2 provenance links (`rel="noopener noreferrer"`) are pre-existing and are the ONLY `#`-fragment hrefs — they are NOT citation anchors. The pre-citation invariant that matters for Wave 1/2 is: **no `#speech-N` in-page anchors exist** (the `#` present is the SPRS section deep-link, unchanged by the restyle). |
| search  | 206 `.u` echoes | **206** `.u` spans / 206 absolute anchors | matches a1's live search echo wall. |
| launcher | ~230 `.u` echoes | **71** `.u` spans | a1's ~230 was the LIVE 39K-report index (more common topics). The offline launcher is driven by the 10-row fixture index → 71 anchors. This is the expected two-tier difference, not a defect. |

The `counts.absolute` for report is **21** (token-bearing), not 23 — see the
table. This was investigated, not silently accepted: the delta is the 2
pre-existing external SPRS provenance links.

## Change-attribution table (which later waves may change which fields)

- **Wave 1 (citation highlight, Plan 02)** — CONSUMED 2026-09-20 (Plan 02
  Task 3): report pages gain N absolute token-bearing `Cite` hrefs + N `.u`
  spans (N = capped Cite lines, G-A5-3; for bill-774, N = 14 — all speeches,
  since 14 < (400 − 23) // 2). `counts.absolute_anchors` (21→35),
  `counts.u_spans` (23→37), `counts.total_anchors` (23→37), `hrefs`,
  `u_texts`, `correspondence` grow on **report** entries only, plus a fixed
  +604-byte inline-CSS delta on EVERY page (the `:target` + production `.u`
  + `.cite`/`.cite-note` rules in base.html, inherited by all 8 entries).
  Search/launcher/nav/date/facet href/`.u` sets must NOT change.
  **Attribution note (two-dir scheme):** the `baselines/wave1/` fixture
  directory holds the per-wave regression capture; its `report_bill-774.json`
  is the LIVE reference (captured from the 0.1.2 deployment, which does not
  yet carry the Cite feature — the v0.1.3 cutover is Plan 05), so the +14
  Cite delta is NOT yet visible in wave1/. The delta itself is proven offline
  by `tests/test_citation.py` (verbatim Cite href + `.u` twin per speech, cap
  = 14 for bill-774) and reconciled here against this table. The per-wave
  `--verify` against wave1/ passes on the 0.1.2 deployment (no drift); the
  FINAL `--verify` against this wave0/ directory will show the +14 report
  delta once 0.1.3 is live (Plan 05 Task 4).
- **Wave 2 (report restyle, Plan 03)** — CONSUMED 2026-09-20 (Plan 03
  Task 3): report pages gain N in-page `#speech-N` hrefs for the TOC
  (N = min(speech_count, TOC_max), capped per M3; for the offline E2E
  fixture, N = 5 — all speeches, since 5 < any cap). This adds hrefs
  containing `#` to **report** entries (the first TOC `#` fragments).
  `hrefs`, `counts.total_anchors`, `anchor_texts` change on report only;
  `counts.absolute_anchors` and `counts.u_spans` do NOT change (the TOC
  links are same-page fragment anchors — no token, no .u twin). Plus a
  fixed +3452-byte inline-CSS delta on EVERY page (the a8 wireframe
  system in base.html, inherited by all 8 entries) ON TOP of the Wave-1
  +604-byte delta. Search/launcher/nav/date/facet href/.u sets must NOT
  change. **Attribution note (two-dir scheme):** the `baselines/wave2/`
  fixture directory holds the per-wave regression capture; its
  `report_bill-774.json` is the LIVE reference (captured from the 0.1.2
  deployment, which does not yet carry the TOC feature — the v0.1.3
  cutover is Plan 05), so the +5 TOC delta is NOT yet visible in wave2/.
  The delta itself is proven offline by `tests/test_report_restyle.py`
  (TOC structure, capped per M3) and the offline regression gate
  (`test_machine_baseline.py`: the report entry's offline render gains
  exactly +N in-page `#speech-N` hrefs, absolute/.u unchanged). The
  per-wave `--verify` against wave2/ passes on the 0.1.2 deployment (no
  drift); the FINAL `--verify` against this wave0/ directory will show
  the +14 Cite + +N TOC report delta once 0.1.3 is live (Plan 05 Task 4).
- **Wave 3 / Wave 4 (search/launcher/nav/date restyle, Plan 03/04)**: layout-
  only. The href/`.u` sets must NOT change on search/launcher/nav/date/facet
  pages. If `<details>` echo grouping ships (EXPERIMENTAL), the `.u`/href sets
  must still pass the extraction corpus.

**Any change to a field not listed for that wave on that page class is a
regression** — the offline regression test + the per-wave / final `--verify`
catch it.
