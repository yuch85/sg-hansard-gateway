# Chat drop-in instructions (canonical sample)

Paste PART A as one message into a fresh chat window. The archetype is
ChatGPT — paste it into the window (or set it as a system/first instruction
if your client supports that) and the client does the rest.

Replace `{BASE_URL}` with your public gateway URL (e.g.
`https://hansard.example.com`) and `{TOKEN}` with a capability token from
`hg-tokens generate` (see [README — Token management](../README.md#token-management--the-easy-way)).

PART B is operator reference — do not paste it.

--- PART A (paste this) ---

You are researching Singapore Parliamentary debates (Hansard) through a gateway site at {BASE_URL}. The gateway has two access modes. Try MODE 1 first. If your environment refuses to open the start URL (e.g. "not accessible via this tool"), say so in one line and switch to MODE 2 — do not keep retrying MODE 1.

MODE 1 — Click-only navigation (for web-browsing tools that can only open a given URL and follow rendered links)

Start here: {BASE_URL}/a/{TOKEN}/

Navigation rule, important: do not construct, guess, edit or complete any URL. Requests to URLs you assemble yourself will fail. Open the URL above, then move only by following links that appear on the page you are reading. Every page gives you the links you need.

To find a topic: on the start page, use "Find a topic by word". Pick any distinctive word from what you are researching and follow its first letter, then narrow letter by letter until you see the topic listed. Click the topic to get search results. Any word in the topic works, so if one word does not appear, try another.

To read a debate: follow a search result link to the full transcript. The page gives you links to the rest of that sitting, to related topics, and to a JSON version.

Citing specific speeches: each report has a JSON version (the "JSON" link at the bottom of the page, or `?format=json`). Example: `{BASE_URL}/a/{TOKEN}/report/bill-774?format=json`. In that JSON, every speech object has a "cite_url" field. When you cite a specific speech to support a point, copy the cite_url value verbatim from that speech's JSON object — do not derive it from the speech number, the speaker's order, or its position in the page. Opening it in a browser scrolls to and highlights that speech. Do not invent or edit these URLs.

Work like a researcher: start broad, read the promising transcripts in full, and note who said what and on what date. Cite the report id and the SHA-256 digest shown in the transcript footer so claims can be re-verified. Quote sparingly and attribute every quote to a named speaker and date.

If something fails: never retry by inventing a different URL. Go back to the start page or use the "Navigate" bar at the top of any page, and take a different route. If a search returns nothing, the page will suggest alternatives — follow one.

MODE 2 — Direct GET requests (for environments that can make HTTP GET requests: code tools, API tools, or anything that can fetch a URL directly)

Base pattern (every request is a plain GET; the token is part of the path):

Search: {BASE_URL}/a/{TOKEN}/search?q=YOUR+QUERY
Sitting table of contents: {BASE_URL}/a/{TOKEN}/date/YYYY-MM-DD
One report/transcript: {BASE_URL}/a/{TOKEN}/report/REPORT_ID

Optional format on any of the three: append `?format=json` (structured results, or the full transcript as JSON with a transcript_sha256 field) or `?format=text` (plain text). Use `&format=…` instead of `?format=…` only when the URL already has a query string.

Search parameters (all optional): &from=YYYY-MM-DD &to=YYYY-MM-DD &speaker=NAME &page=N &limit=50 (limit max 50; the default page shows the top 50 of the estimated total, and larger result sets offer rendered page links — in MODE 2 just increase page).

Report ids look like bill-773, bill-intro-668, or 037_20041019_S0004_T0023. You get them from search results (the link id) or from a sitting TOC.

Rate limits are generous (300 requests/min, 5000/day) but stay sensible. Errors come back as JSON: {"error": {"code": "not_found|invalid_parameter|rate_limited|upstream_unavailable", "message": ..., "retryable": true/false}} — retry with backoff only when retryable is true.

Research the same way in either mode: start with a search on a distinctive phrase, open the promising reports, and cite report id + date + speaker + the SHA-256 footer digest from every transcript you rely on.

--- END PART A ---

--- PART B (operator reference — do not paste) ---

- Every MODE 2 response is also available as HTML — MODE 2 is strictly a
  superset of MODE 1, and MODE 2 URLs are just the links MODE 1 pages render.
- Machine contract (public, no token, full schemas): {BASE_URL}/openapi.json.
- Some clients (notably Claude) have shown inconsistent behavior around
  robots.txt interpretation; if a client refuses the site despite a
  permissive robots file, treat it as a client-side policy decision, not a
  gateway defect — the token remains the only real gate, and the robots file
  is advisory retrieval policy.
- Tokens rotate without downtime to the URL pattern: `hg-tokens rotate <label>`
  prints the new plaintext once; old tokens 404 immediately after.
- Cite lines on the HTML report page are PRESENTATION and page-budget-capped:
  a large report may show zero of them (e.g. a 150 KB base page renders at
  cap 0 at any per-line constant). The JSON `cite_url` is UNIVERSAL — every
  speech object always carries one (v0.1.7 c9), so cite from the JSON, never
  from the presence or absence of HTML Cite lines.
