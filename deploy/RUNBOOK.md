# Hansard Gateway Deploy Runbook (Phase 27)

TEMPLATE — substitute the <...> placeholders for your host before running
(canonical set: <REPO_DIR>, <UV_BIN>, <ENV_FILE>, <PROXY_HOST>, <LAN_IP>,
<APP_PORT>, <LIVE_HOST>; full list in tests/test_deploy_artifacts.py
CANONICAL_PLACEHOLDERS). Read top-to-bottom; every command states the host it
runs on. The owner executes; the executor only writes these files.

## Topology

```text
Internet -> Cloudflare (wildcard cert, CNAME <LIVE_HOST> -> apex domain)
         -> proxy Caddy  (<PROXY_HOST>, ~/docker/Caddyfile)
         -> this host  <LAN_IP>:<APP_PORT>  (uvicorn, user service, 0.0.0.0 bind)
         -> SPRS / Pair upstream (live, per request)
```

D-08: the app is NOT on the proxy — the proxy Caddy reverse-proxies across the
LAN to this host's IP. D-09: the hansard vhost has NO access logging (the token
is in the URL path). D-10: uvicorn runs with `--no-access-log`.

## Step 1 — One-time deploy: this host

```bash
cd <REPO_DIR>
uv sync --extra dev          # uv sync installs the dev group (pytest/respx)
mkdir -p logs

# Confirm the LAN IP the Caddy block must target (<LAN_IP>)
hostname -I

# Install + start the user service
mkdir -p ~/.config/systemd/user
cp deploy/systemd/alice-hansard-gateway.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now alice-hansard-gateway
systemctl --user status alice-hansard-gateway
# Expected: Active: active (running)

# Local smoke (no token needed)
curl -s http://localhost:8765/health
# Expected: {"status":"ok"}
```

## Step 2 — One-time deploy: proxy Caddy

```bash
# On this host: print the block (LAN IP already substituted — verify it
# matches the current `hostname -I` output first)
cat <REPO_DIR>/deploy/caddy/hansard.098020.xyz.caddyfile

# On the proxy:
ssh <PROXY_HOST>
# Append the block to ~/docker/Caddyfile (DO NOT replace existing content)
nano ~/docker/Caddyfile
# Validate + reload
docker exec caddy caddy validate --config /etc/caddy/Caddyfile
docker exec caddy caddy reload --config /etc/caddy/Caddyfile
exit
```

Then from this host:

```bash
curl -sfI https://<LIVE_HOST>/health
# Expected: HTTP/2 200 (Cloudflare -> Caddy -> this host)
curl -s https://<LIVE_HOST>/ | head -20
# Expected: the public documentation home
```

## Term index crawl (Phase 27.1)

The link-navigable ingress (spec §4) reads the local term index
(`~/.hansard/index.db`). The index is built by an offline crawl
(`python -m crawl.crawl_main`). The COLD crawl is a ONE-OFF manual step
BEFORE live acceptance (it takes ~1 day at ~1 req/s); the weekly timer then
handles INCREMENTS only (never re-writes from scratch).

The deploy is a human checkpoint (sudo/ssh — the owner has passwordless ssh
to the proxy and authorised the executor to deploy, CONTEXT 7). The executor
only writes the unit files + this RUNBOOK.

### 1. Install the weekly timer

```bash
mkdir -p ~/.config/systemd/user
cp <REPO_DIR>/deploy/systemd/alice-hansard-crawl.service \
   <REPO_DIR>/deploy/systemd/alice-hansard-crawl.timer \
   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now alice-hansard-crawl.timer
systemctl --user status alice-hansard-crawl.timer
# Expected: Active: active (waiting) — the timer is armed, not running
```

### 2. Run the COLD crawl (one-off, BEFORE live acceptance)

The cold crawl populates `~/.hansard/index.db` from scratch. It takes ~1 day
at ~1 req/s (rate-limited with jitter, wave 1); monitor via the journal.

```bash
cd <REPO_DIR>
uv run python -m crawl.crawl_main --cold
# Monitor (separate terminal):
journalctl --user -u alice-hansard-crawl -f
```

The crawl logs table counts + the `build_id` on completion (RESEARCH OQ4).
A failed crawl deletes the staging file and leaves the live index untouched
(atomic swap, wave 1) — re-run `--cold` to retry.

### 3. Verify the index file

```bash
ls -ld ~/.hansard
# Expected: drwx------ (0700) — the same dir that holds the consumer tokens
ls -l ~/.hansard/index.db
# Expected: -rw------- (0600)
# Confirm the crawl logged table counts + build_id:
journalctl --user -u alice-hansard-crawl | grep -E "build_id|rows"
```

### 4. Restart the gateway (pick up the index + the Phase 27.1 code)

```bash
systemctl --user restart alice-hansard-gateway
systemctl --user status alice-hansard-gateway
# Expected: Active: active (running)
```

### 5. Live byte-identity (release-blocking, RESEARCH manual-only table)

The deploy must NOT change the invalid-token 404 body (anti-enumeration):

```bash
curl -s https://<LIVE_HOST>/a/bogus/ | md5sum
# MUST equal 1a29cc1330d50031993c3cbcde2318d7
```

Then run the live acceptance tests:

```bash
cd <REPO_DIR>
uv run python -m pytest -m live
```

## Step 3 — Live acceptance (release-relevant, spec §30 / addendum §29)

Use one of the three enabled team tokens (already provisioned in
`tokens.yaml`; `uv run python manage_tokens.py list` shows the labels — the
plaintexts were printed once at generation time and must have been saved
out-of-band; the store holds sha256 hashes only).

```bash
T="<one valid plaintext token>"

# §30-A: transcript in the raw bytes, no JS
curl -s https://<LIVE_HOST>/a/$T/report/037_20041019_S0004_T0023 \
  | grep "Singapore Armed Forces (Amendment No. 2) Bill"

# §30-B: date TOC links the report
curl -s https://<LIVE_HOST>/a/$T/date/2004-10-19 \
  | grep "037_20041019_S0004_T0023"

# §30-C: keyword search returns hits with gateway links
curl -s "https://<LIVE_HOST>/a/$T/search?q=Pension%20Fund&from=2004-01-01&to=2004-12-31" \
  | grep "a/$T/report"
```

## Step 4 — RELEASE-BLOCKING: no token in logs

```bash
# This host — after the live requests above:
grep -R "hg_" <REPO_DIR>/logs/
# MUST return nothing.

# Proxy — confirm the hansard vhost emitted NO access log containing the token
# (D-09 disabled access logging for this vhost; no other log path should exist).
```

## Step 5 — Token management + revocation (independent, no Caddy change)

```bash
cd <REPO_DIR>
uv run python manage_tokens.py list
uv run python manage_tokens.py generate hansard-team-4     # prints plaintext ONCE
uv run python manage_tokens.py disable hansard-team-2      # revoke one token
uv run python manage_tokens.py rotate hansard-team-3       # replace one token
```

The running service holds the token store in memory, so apply the change with
a quick in-place restart (no Caddy change, no rebuild, no URL change —
addendum §8):

```bash
systemctl --user restart alice-hansard-gateway
```

Verify independent revocation (addendum §29): with token 2 disabled,
`/a/<token-2>/report/037_20041019_S0004_T0023` returns 404 while tokens 1
and 3 still return 200.

## Step 6 — Upstream leakage check (release-blocking, addendum §19/§29)

Confirm NO outgoing SPRS/Pair request header or body contains a `hg_` token.
Offline proof: `tests/test_acceptance.py::test_upstream_requests_carry_no_token`
(respx-captured, green in the suite). Live spot-check: briefly enable the
debug logging line in `src/hansard_gateway/sprs/client.py` on this host
(substitute <REPO_DIR> for the path), make
one authenticated request, confirm the logged upstream request has no `hg_`
string, then revert the line.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Cloudflare **521** | proxy Caddy block not appended yet, or Caddy not reloaded | Step 2; `docker exec caddy caddy validate` then reload |
| **502** from Caddy | Stale/wrong LAN IP in the vhost, or the app is down | `hostname -I` on this host; update the `reverse_proxy` target; `systemctl --user status alice-hansard-gateway` |
| **503** from the app | Upstream concurrency saturated (global cap 15, addendum §17) or per-token cap hit | back off; check `journalctl --user -u alice-hansard-gateway` |
| Upstream **500 "No Results Found"** | NOT a failure — SPRS's empty-result marker (D-07); the gateway returns an empty results page | expected; nothing to fix |
| 404 on an authenticated URL | Token revoked/invalid — by design 404, not 401 (addendum §7) | `manage_tokens.py list`; do not treat as an outage |

## Docker deployment

The container image is documented in the README (build, run, env vars);
this section is the RUNBOOK voice for the same operations, with the
live bind-mount contract.

### 1. Build

```bash
cd <REPO_DIR>
docker build -t hansard-gateway:<TAG> .
# Expected: image built; no COPY of tokens.yaml / index.db in the log
```

### 2. Run with the /data volume

```bash
docker run -d --name hansard-gateway --restart unless-stopped \
  --mount type=bind,source=<HOME_HANSARD_DIR>,target=/data \
  -e HANSARD_PUBLIC_BASE_URL=https://<LIVE_HOST> \
  -e HANSARD_LOG_STREAM=1 \
  -p <APP_PORT>:8000 \
  hansard-gateway:<TAG>
# Expected: container up; curl -s localhost:<APP_PORT>/health -> {"status":"ok"}
```

### 3. Token bootstrap

See README "Token management" — generate on the host
(`uv run hg-tokens generate <label>`), the store lands in `<HOME_HANSARD_DIR>`,
restart the container (no hot-reload).

### 4. Crawl

See README "Crawl" — ad-hoc: `docker run --rm -v hansard-index:/data
hansard-gateway:<TAG> uv run python -m hansard_gateway crawl once [--cold]`
(full module form — the image has no ENTRYPOINT). Scheduled: the same line
from a host timer, or the systemd timer in `deploy/systemd/` for a host-side
crawl instead.

### The /data volume contract (live bind-mount)

- The host dir contains ONLY what the app needs — `index.db` + `tokens.yaml`
  + `logs/`. The log dir is created BY THE APP at boot (logging_setup.py
  mkdir parents=True — proven by the Plan-3 log gate), so no pre-creation
  step is written here.
- Any host credential the app does NOT consume (e.g. an upstream-site
  consumer token kept for the launcher curl) MUST live OUTSIDE
  `<HOME_HANSARD_DIR>` so the container never gets read/write on it.
- Index persistence (INDEX-01): the index lives in the volume, so container
  replacement is safe; for a live bind-mount deployment the host path and
  the volume are the same directory. The weekly crawl timer's atomic
  `os.replace` swap is followed by the running container WITHOUT a restart
  (build_id reopen — proven in-container by the Plan-3 swap gate).

## Live Docker cutover (option b)

SCOPING NOTE: this section carries THIS deployment's live values (it is the
ops record for the live cutover). The publish-surface hygiene gate
(tests/test_publish_hygiene.py) scans deploy/ (Plan 2 widened it): the
forbidden tokens are the SECRET-bearing machine strings (home-path prefixes,
LAN IP prefixes, the RPi ssh target, the services env-file name). THEREFORE —
write the runbook with Plan 2's canonical placeholders in the tracked file: <LAN_IP> for the host LAN
address, <PROXY_HOST> for the RPi ssh target, <CONTAINER_HOST_PORT> for the
container published port (8766; pinned in step 3), the <REPO_DIR>/
<HOME_HANSARD_DIR>/<HOME_HANSARD_UPSTREAM_DIR> family for host paths
(PATTERN EXTENSION: <HOME_HANSARD_DIR> = ~/.hansard and
<HOME_HANSARD_UPSTREAM_DIR> = ~/.hansard-upstream — the B2 move adds the
second placeholder; the hygiene gate only forbids the SECRET-bearing strings,
and a ~-form path is not among them — the placeholders are used for
gate-clean consistency, not because ~ is forbidden), and the public URL as
the LITERAL https://hansard.098020.xyz (public DNS, load-bearing for the
runbook — it is not a secret and is not in the forbidden-token list). The
executor's ACTUAL commands use the real values from the plan's context block;
the committed runbook stays gate-clean.

End-to-end option (b): the container is pre-verified on
<CONTAINER_HOST_PORT> BEFORE any Caddy change; caddy validate runs before
caddy reload (an invalid reload keeps the old config live); the systemd app
keeps <APP_PORT> until retirement, so the two coexist (no port collision —
the container host port MUST be 8766, NOT 8765). The index + tokens are
MIGRATED via the bind mount — no re-crawl; the weekly crawl timer is
UNTOUCHED. The systemd unit is stopped (not disabled, not removed) AFTER the
live URL is verified, and stays installed + enabled as the rollback.

### Step 0 — Pre-flight (live system untouched)

```bash
systemctl --user status alice-hansard-gateway
# Expected: active (running)

# Byte-identity baseline BEFORE the flip — every later md5 comparison is
# measured against this value:
curl -s https://hansard.098020.xyz/a/bogus/ | md5sum
# MUST equal 1a29cc1330d50031993c3cbcde2318d7

docker image inspect hansard-gateway:27.2 >/dev/null
# Expected: exit 0 — image exists locally (built by the Docker plan)

# RECORD THE IMAGE ID (G4 — the immutable LOCAL image ID, NOT a
# registry/content digest; `--digests` does not turn {{.ID}} into a digest,
# so it is not used here. Record the value in the execution log. The
# registry digest + git-commit→image mapping are deferred to publication):
docker images --format '{{.Repository}}:{{.Tag}} {{.ID}}' hansard-gateway:27.2

ls -la <HOME_HANSARD_DIR>/
# Expected: index.db 0600 (~42MB) + consumer_token_team1 0600 (the
# credential step 1 moves)

ls -l <REPO_DIR>/tokens.yaml
# Expected: 0600 — the repo-root token store

# Pre-check the E2E report serves on the CURRENT (systemd) deployment:
T=$(cat <HOME_HANSARD_DIR>/consumer_token_team1)
curl -s -o /tmp/hg-cutover-precheck.html -w '%{http_code}\n' \
  "http://localhost:<APP_PORT>/a/$T/report/037_20041019_S0004_T0023"
# Expected: 200
grep -qE 'Transcript SHA-256: [0-9a-f]{64}' /tmp/hg-cutover-precheck.html
# Expected: exit 0 — the report (T-27-49: a missing report + unreachable SPRS
# returns 502, so this must pass BEFORE the flip)
rm -f /tmp/hg-cutover-precheck.html
# Expected overall: live system green on systemd, baseline md5 exact, image
# present + image ID recorded. If the baseline md5 is NOT exact: STOP — the
# baseline invariant is broken (mission-level alert, not a plan defect).
```

### Step 1 — Move the upstream consumer credential OUT of the mounted dir (B2)

The container must NEVER get read/write on YC's upstream-site credential;
the gateway app consumes only index.db + tokens.yaml.

```bash
mkdir -p <HOME_HANSARD_UPSTREAM_DIR>
mv <HOME_HANSARD_DIR>/consumer_token_team1 \
   <HOME_HANSARD_UPSTREAM_DIR>/consumer_token_team1
# Expected: ls <HOME_HANSARD_DIR>/ shows NO consumer_token_team1;
# ls -la <HOME_HANSARD_UPSTREAM_DIR>/ shows consumer_token_team1 0600
# (mv preserves the mode).
```

NO unit change is required: the live alice-hansard-crawl.service unit and the
services env file contain NO reference to the credential file (verified at
planning: grep across deploy/, src/, crawl/, manage_tokens.py, and
~/.config/systemd/user/ returns zero hits — the crawl authenticates to the
upstream via its own fixed header set, not a token file; the file is read
only by HUMANS, e.g. curl via $(cat …)). JUDGMENT CALL: the copilot issue
suggested updating a unit token-path reference — that reference does not
exist, so the move is file-only; if a grep at execution time finds a
reference after all, update it to the new location and re-verify the crawl
unit before continuing.

```bash
systemctl --user status alice-hansard-crawl
systemctl --user list-timers | grep alice-hansard-crawl
# Expected: the crawl unit/timer still healthy — the live crawl is
# unaffected by the move. (Rollback of this step = mv the file back.)
```

NOTE: every later token curl in this section uses the POST-MOVE path
<HOME_HANSARD_UPSTREAM_DIR>/consumer_token_team1.

### Step 2 — Migrate + synchronize the token store (no re-crawl)

The container /data is BIND-MOUNTED at the existing <HOME_HANSARD_DIR>
(RESEARCH recommendation (i): the weekly timer's atomic os.replace swap
stays visible to the container — zero crawl-scheduling change; the crawl
timer is NOT touched).

```bash
# Pre-copy baseline (B3 — machine-assert, don't just inspect): if
# <HOME_HANSARD_DIR>/tokens.yaml exists, sha256sum it into the execution log
# and diff it against <REPO_DIR>/tokens.yaml. If they DIFFER: STOP and report
# — the live store has diverged from the repo source and the cutover must
# not silently overwrite it.
diff <REPO_DIR>/tokens.yaml <HOME_HANSARD_DIR>/tokens.yaml
# (no output if identical; stop on divergence)

# This cp -p IS the synchronization — it makes BOTH stores (repo-root legacy
# + the container dir) byte-identical at cutover time; preserves 0600:
cp -p <REPO_DIR>/tokens.yaml <HOME_HANSARD_DIR>/tokens.yaml

# Post-copy verify:
diff <REPO_DIR>/tokens.yaml <HOME_HANSARD_DIR>/tokens.yaml
# Expected: no output (both stores identical)
sha256sum <HOME_HANSARD_DIR>/tokens.yaml
# Expected: recorded in the execution log
ls -la <HOME_HANSARD_DIR>/
# Expected: index.db + tokens.yaml, both 0600; the dir contains ONLY those
# two (+ logs/ once the app boots — the app creates it, step 2b). Do NOT
# re-generate tokens; do NOT run a crawl.
```

### Step 2b — Log-dir fact (B5)

The live command sets HANSARD_LOG_FILE=/data/logs/hansard-gateway.log and
NOTHING pre-creates <HOME_HANSARD_DIR>/logs/ — that is correct: the app
creates the log parent dir at boot (logging_setup.py:
log_path.parent.mkdir(parents=True, exist_ok=True) — verified in code and
proven in-container by the Docker plan's log gate). Do NOT pre-create the dir
manually. Expected: no action; the log file appears after the container's
first boot (step 2c proves it).

### Step 2c — Start + pre-verify the container (BEFORE any Caddy change)

```bash
docker run -d --name hansard-gateway --restart unless-stopped \
  -p 8766:8000 \
  --mount type=bind,source=<HOME_HANSARD_DIR>,target=/data \
  -e HANSARD_INDEX_DB_PATH=/data/index.db \
  -e HANSARD_TOKENS_PATH=/data/tokens.yaml \
  -e HANSARD_PUBLIC_BASE_URL=https://hansard.098020.xyz \
  -e HANSARD_LOG_FILE=/data/logs/hansard-gateway.log \
  -e HANSARD_LOG_STREAM=1 \
  hansard-gateway:27.2
# Expected: container up. Port 8766 — NOT 8765: the systemd app keeps 8765
# until retirement, so the two coexist (the rollback safety net).
```

Then verify on the LAN port — ALL SIX must pass before step 3:

```bash
T=$(cat <HOME_HANSARD_UPSTREAM_DIR>/consumer_token_team1)
curl -sf http://<LAN_IP>:8766/health
# Expected: {"status":"ok"}

curl -sf -o /tmp/hg-launcher.html -w '%{http_code}\n' http://<LAN_IP>:8766/a/$T/
# Expected: 200; grep the body for absolute https://hansard.098020.xyz URLs

curl -sf -o /dev/null -w '%{http_code}\n' "http://<LAN_IP>:8766/a/$T/search?q=Pension%20Fund"
# Expected: 200

curl -sf -o /tmp/hg-report.html -w '%{http_code}\n' \
  http://<LAN_IP>:8766/a/$T/report/037_20041019_S0004_T0023
# Expected: 200
grep -qE 'Transcript SHA-256: [0-9a-f]{64}' /tmp/hg-report.html
# Expected: exit 0 — the SHA-256 footer is present

curl -s http://<LAN_IP>:8766/a/bogus/ | md5sum
# MUST equal 1a29cc1330d50031993c3cbcde2318d7 (byte-identity in the
# container on the migrated index — compare against the step-0 baseline)

test -s <HOME_HANSARD_DIR>/logs/hansard-gateway.log
# Expected: exit 0 — the log file EXISTS and is non-empty (B5 proven live;
# if it does NOT appear, the app's mkdir did not run — stop and investigate,
# do not pre-create the dir manually)

rm -f /tmp/hg-launcher.html /tmp/hg-report.html
```

If ANY check fails: docker stop hansard-gateway and investigate — the
systemd app is still serving on 8765, nothing is down.

### Step 3 — Flip (near-atomic)

```bash
ssh <PROXY_HOST>
cp ~/docker/Caddyfile ~/docker/Caddyfile.bak-<date>
# Expected: backup exists BEFORE the edit.

# Edit the hansard.098020.xyz block: reverse_proxy <LAN_IP>:8765 ->
# <LAN_IP>:<CONTAINER_HOST_PORT>. ONE line. Keep the header_up
# Host/X-Real-IP/X-Forwarded-Proto options, the request_body max_size, and
# the no-access-log property.
# PINNED VALUES: <CONTAINER_HOST_PORT> = 8766 — the container's published
# host port (step 2c's -p 8766:8000); 8765 = the legacy systemd port — the
# rollback-only target (step 7). The reverse_proxy line therefore takes
# 8766, never 8765, until the rollback reverts it.

# GOTCHA (hit live 2026-09-19): the caddy container's /etc/caddy/Caddyfile
# is a BIND of the host file, but the bind does NOT track the host inode —
# after an in-place sed/edit, `docker exec caddy cat /etc/caddy/Caddyfile`
# STILL SHOWS THE OLD CONTENT even though the host file is correct (the
# container keeps the pre-edit inode). `docker exec caddy caddy
# reload --config /etc/caddy/Caddyfile` therefore re-loads the STALE
# config and the flip silently does not take (the live URL keeps dialing
# the old port). Load the config via STDIN instead — the host file content
# is what you intend:
cat ~/docker/Caddyfile | docker exec -i caddy caddy validate --config - --adapter caddyfile
# Expected: "Valid configuration" — an invalid load keeps the old config
# live. If validate FAILS: do NOT reload — fix or roll back.
cat ~/docker/Caddyfile | docker exec -i caddy caddy reload --config - --adapter caddyfile
# Then VERIFY THE FLIP ACTUALLY LANDED before moving on:
curl -s https://hansard.098020.xyz/health   # from <LAN_IP>
# Expected: 200 within one reload cycle — if it still 502s with
# "connection refused" on the OLD port in the caddy error log, the config
# did not land (stale-bind symptom above); re-run the stdin load.
exit
```

### Step 4 — End-to-end on the LIVE public URL (the real gate)

```bash
T=$(cat <HOME_HANSARD_UPSTREAM_DIR>/consumer_token_team1)
curl -sf https://hansard.098020.xyz/health
# Expected: 200 {"status":"ok"}

curl -sf -o /dev/null -w '%{http_code}\n' https://hansard.098020.xyz/a/$T/
# Expected: 200

curl -sf -o /dev/null -w '%{http_code}\n' "https://hansard.098020.xyz/a/$T/search?q=Pension%20Fund"
# Expected: 200

curl -sf -o /tmp/hg-live-report.html -w '%{http_code}\n' \
  https://hansard.098020.xyz/a/$T/report/037_20041019_S0004_T0023
# Expected: 200
grep -qE 'Transcript SHA-256: [0-9a-f]{64}' /tmp/hg-live-report.html
# Expected: exit 0 (same check as step 2c, on the live URL)
rm -f /tmp/hg-live-report.html

curl -s https://hansard.098020.xyz/a/bogus/ | md5sum
# MUST equal 1a29cc1330d50031993c3cbcde2318d7 (compare against the step-0
# pre-flip baseline md5)

curl -s https://hansard.098020.xyz/robots.txt | wc -c
# Expected: 148
```

(Optional, only if it does not burn ChatGPT budget: one MODE-2 GET probe per
the mission-005 protocol — otherwise record MODE-1 as the CO follow-up.)
Expected: all six live checks green. Record the exact outputs in the
execution log / SUMMARY (token values REDACTED — shape only; include the
step-0 image ID — G4).

### Step 5 — Retire the systemd gateway (AFTER step 4 is green)

```bash
systemctl --user stop alice-hansard-gateway
# Do NOT disable — the unit stays installed + enabled as the documented
# rollback; :8765 is now free.
systemctl --user is-active alice-hansard-gateway
# Expected: inactive
systemctl --user is-enabled alice-hansard-gateway
# Expected: enabled (unit retained)
curl -sf -o /dev/null -w '%{http_code}\n' https://hansard.098020.xyz/health
# Expected: 200 — the container owns the live URL
systemctl --user list-timers | grep alice-hansard-crawl
# Expected: the weekly timer UNTOUCHED and still armed, writing
# <HOME_HANSARD_DIR>/index.db (the bind mount)
```

### Step 6 — Persistence check (INDEX-01, after the flip)

```bash
T=$(cat <HOME_HANSARD_UPSTREAM_DIR>/consumer_token_team1)
curl -s -o /tmp/hg-persist.html https://hansard.098020.xyz/a/$T/report/037_20041019_S0004_T0023
# Record the body's SHA footer + the index build_id (read via a read-only
# sqlite query in the container) BEFORE the restart.
docker restart hansard-gateway
# Wait for /health (up to ~60s).
curl -sf -o /tmp/hg-persist2.html https://hansard.098020.xyz/a/$T/report/037_20041019_S0004_T0023
# Expected: the same report returns 200 with the IDENTICAL SHA footer (the
# index survived container restart from the bind mount); the build_id is
# unchanged (no re-crawl). The post-restart container keeps the SAME name
# AND the SAME 64-hex .Id (a replaced container keeps the name — the .Id
# equality is the identity proof).
rm -f /tmp/hg-persist.html /tmp/hg-persist2.html
```

The weekly timer's next os.replace swap will be visible to the container
WITHOUT a restart (loader reopens on build_id change — proven in-container
at fixture scale by the Docker plan's swap gate) — noted, not force-run (no
re-crawl).

### Step 7 — Rollback (documented — executable at ANY point after step 3)

```bash
# B3 — SYNCHRONIZE BEFORE starting systemd (container store → legacy store;
# the container store is the post-cutover authority, so any token mutation
# made after the flip is carried back to the legacy store by this sync):
cp -p <HOME_HANSARD_DIR>/tokens.yaml <REPO_DIR>/tokens.yaml
diff <REPO_DIR>/tokens.yaml <HOME_HANSARD_DIR>/tokens.yaml
# Expected: no output — byte-identical BEFORE systemd start.

# Revert the proxy:
ssh <PROXY_HOST>
# Restore the reverse_proxy target to <LAN_IP>:8765 (the legacy systemd
# port) — or restore Caddyfile.bak-<date>
docker exec caddy caddy validate --config /etc/caddy/Caddyfile
docker exec caddy caddy reload --config /etc/caddy/Caddyfile
exit

# This host:
systemctl --user start alice-hansard-gateway
# Expected: rebinds :8765; the live URL serves systemd again within one
# reload; the index is unchanged.
curl -s https://hansard.098020.xyz/a/bogus/ | md5sum
# Expected: back to 1a29cc1330d50031993c3cbcde2318d7.
docker stop hansard-gateway   # optional
```

B2 ROLLBACK (only if the credential move itself must be undone):

```bash
mv <HOME_HANSARD_UPSTREAM_DIR>/consumer_token_team1 \
   <HOME_HANSARD_DIR>/consumer_token_team1
# Expected: 0600 preserved by mv.
```

### Step 8 — Post-cutover token administration (B3 invariant + REQ-07)

The live container reads /data/tokens.yaml (the <HOME_HANSARD_DIR> copy);
the host CLI defaults to the repo-root store. INVARIANT: "Rollback is only
valid when the legacy store has been re-synced from the container store
immediately before systemd start (step 7's cp -p) — the container store is
the authority post-cutover; any token mutation after the flip is covered by
that sync."

Admin procedure — use the token CLI against the container's store:

```bash
# In-container (the canonical post-cutover path; the in-container default
# is /data/tokens.yaml):
docker exec hansard-gateway hg-tokens list

# Host-side equivalent (MUST target the container store — it is canonical):
HANSARD_TOKENS_PATH=<HOME_HANSARD_DIR>/tokens.yaml uv run hg-tokens <subcommand>
```

The sha256 store has no hot-reload guarantee — restart the container after
token changes:

```bash
docker restart hansard-gateway
```

Expected: `hg-tokens list` shows the three migrated team tokens (labels
only — no live token values appear anywhere in this runbook; paths only).

### Follow-ups (deferred per gate — NOT executed in the cutover)

- G5 — read-only rootfs + tmpfs hardening of the live container is deferred:
  the app writes only under /data (logs) and needs no other writable path,
  but proving --read-only requires the full write-surface audit this phase
  does not schedule — schedule it post-cutover once the live deployment is
  stable.
- G6 — a staged rollback DRILL is deferred: the rollback is
  exercised-for-real only if needed; a drill would require a second Caddy +
  an index copy, and the pre-verify-on-8766 + validate-before-reload already
  make the flip itself low-risk — schedule the drill post-cutover.

## Rollback

```bash
# This host:
systemctl --user disable --now alice-hansard-gateway

# Proxy: remove the hansard block from ~/docker/Caddyfile, then:
docker exec caddy caddy validate --config /etc/caddy/Caddyfile
docker exec caddy caddy reload --config /etc/caddy/Caddyfile
```

## v0.1.3 upgrade (image rollover)

SCOPING NOTE: this is the RELEASE UPGRADE procedure (0.1.2 -> 0.1.3) for the
live Docker deployment. It supersedes the legacy-systemd rollback of the
"Live Docker cutover (option b)" step 7 for release upgrades: that unit was
retired at cutover and is documented history. The rollback for THIS upgrade is
an IMAGE rollover — recreate the container from the step-0 recorded pre-upgrade
image identity. Placeholder policy is the same as the rest of this runbook
(no home-path prefixes, no LAN IP literals, no proxy ssh target, no
services env-file name; the public URL literal is allowed).

The `/data` bind mount is untouched by the container recreate — index +
tokens carry over. No data migration step exists or should be invented.

### Step 0 — Pre-flight: record the pre-upgrade identity (BEFORE any pull)

```bash
# Current image identity — this is the ROLLBACK TARGET (G-A5-2):
docker inspect hansard-gateway --format '{{.Image}}'
# Expected: the 0.1.2/27.3-era build (local tag + 64-hex .Id)

# Full recreate configuration — the old container is DESTROYED during the
# upgrade, so docker inspect on it is unavailable afterwards. Capture the
# complete env/volume/port/restart set NOW:
docker inspect hansard-gateway > /tmp/hg-preup-container-config.json
# Expected: file written; contains Env, Mounts, HostConfig.PortBindings,
# HostConfig.RestartPolicy, Command — the full recreate recipe.

# Bogus-token md5 baseline on the live URL (the 0.1.2 anti-enumeration body):
curl -s https://hansard.098020.xyz/a/bogustoken000000000000000zz/ \
  | md5sum
# Expected: 1a29cc1330d50031993c3cbcde2318d7 (0.1.2 value — kept as the
# rollback reference. The 0.1.3 body is a different, also byte-pinned,
# invalid-token page: db5bd37212a04ab36d3eb130cdc7abfd).

# Pre-upgrade ?format=text bodies for the FULL 8-entry corpus (same 8
# entries as the Phase 27.3 Wave-0 baseline corpus; the acceptance-4
# byte-identity claim is 'for the corpus pages', not just report + search).
# Run BEFORE the pull — these are the only reference:
TOK=$(cat <HOME_HANSARD_UPSTREAM_DIR>/consumer_token_team1)
mkdir -p /tmp/hg-preup-text
curl -s "https://hansard.098020.xyz/a/$TOK/report/bill-774?format=text" \
  > /tmp/hg-preup-text/report_bill-774.txt
curl -s "https://hansard.098020.xyz/a/$TOK/search?q=Health%20Information%20Bill&format=text" \
  > /tmp/hg-preup-text/search_hib_p1.txt
curl -s "https://hansard.098020.xyz/a/$TOK/?format=text" > /tmp/hg-preup-text/launcher.txt
curl -s "https://hansard.098020.xyz/a/$TOK/nav/h?format=text" > /tmp/hg-preup-text/nav_h.txt
curl -s "https://hansard.098020.xyz/a/$TOK/years?format=text" > /tmp/hg-preup-text/years.txt
curl -s "https://hansard.098020.xyz/a/$TOK/members?format=text" > /tmp/hg-preup-text/members.txt
curl -s "https://hansard.098020.xyz/a/$TOK/bills?format=text" > /tmp/hg-preup-text/bills.txt
curl -s "https://hansard.098020.xyz/a/$TOK/date/2026-01-12?format=text" > /tmp/hg-preup-text/date_2026-01-12.txt
# Expected: 8 non-empty files (note the search URL uses &format=text —
# one query string, not a second ?).
```

### Step 1 — Pull the new image

```bash
docker pull ghcr.io/yuch85/sg-hansard-gateway:0.1.3
# Expected: pull succeeds (PAT-authenticated on this host); the local
# 64-hex .Id is recorded in the execution log.
```

### Step 2 — Recreate the container from the STEP-0 saved config

```bash
# Reconstruct the EXACT current env/volume/port/restart set from
# /tmp/hg-preup-container-config.json (Env, Mounts, HostConfig.PortBindings,
# HostConfig.RestartPolicy), then:
docker rm hansard-gateway
docker run -d --name hansard-gateway <restart-policy-from-step-0> \
  <mounts-from-step-0> <env-from-step-0> <port-bindings-from-step-0> \
  ghcr.io/yuch85/sg-hansard-gateway:0.1.3
# Expected: container up. The /data bind mount is UNTOUCHED — the index +
# tokens carry over; this IS the whole migration. Do not re-generate
# tokens; do not re-crawl.
```

### Step 3 — Verify on the LAN port (<APP_PORT>)

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:<APP_PORT>/health
# Expected: 200
curl -s -o /dev/null -w '%{http_code}\n' "http://localhost:<APP_PORT>/a/$TOK/"
# Expected: 200
R=$(curl -s "http://localhost:<APP_PORT>/a/$TOK/report/bill-774")
echo "$R" | grep -q 'Cite:' && echo "$R" | grep -q '#speech-1'
# Expected: exit 0 — the v0.1.3 Cite line + speech id are present
echo "$R" | grep -qE 'Transcript SHA-256: [0-9a-f]{64}'
# Expected: exit 0 — the verbatim transcript SHA footer is present
curl -s "http://localhost:<APP_PORT>/a/bogustoken000000000000000zz/" \
  | md5sum
# Expected: db5bd37212a04ab36d3eb130cdc7abfd (the 0.1.3 invalid-token
# body — byte-pinned; it is NOT the step-0 baseline value, and that is
# intended: the 404 page was restyled in v0.1.3)
```

### Step 4 — Verify on the LIVE public URL (the acceptance-4 field proof)

```bash
test "$(curl -s -o /dev/null -w '%{http_code}' https://hansard.098020.xyz/health)" = "200"
test "$(curl -s -o /dev/null -w '%{http_code}' "https://hansard.098020.xyz/a/$TOK/")" = "200"
R=$(curl -s "https://hansard.098020.xyz/a/$TOK/report/bill-774")
echo "$R" | grep -q 'id="speech-1"'
echo "$R" | grep -q "report/bill-774#speech-1"
echo "$R" | grep -qE 'Transcript SHA-256: [0-9a-f]{64}'
curl -s "https://hansard.098020.xyz/a/bogustoken000000000000000zz/" | md5sum
# Expected: db5bd37212a04ab36d3eb130cdc7abfd (post-upgrade pin)

# FULL 8-entry corpus: each ?format=text body byte-identical to its
# step-0 pre-upgrade capture (machine contract unchanged by the restyle):
curl -s "https://hansard.098020.xyz/a/$TOK/report/bill-774?format=text" \
  | cmp -s - /tmp/hg-preup-text/report_bill-774.txt && echo report OK
curl -s "https://hansard.098020.xyz/a/$TOK/search?q=Health%20Information%20Bill&format=text" \
  | cmp -s - /tmp/hg-preup-text/search_hib_p1.txt && echo search OK
curl -s "https://hansard.098020.xyz/a/$TOK/?format=text" \
  | cmp -s - /tmp/hg-preup-text/launcher.txt && echo launcher OK
curl -s "https://hansard.098020.xyz/a/$TOK/nav/h?format=text" \
  | cmp -s - /tmp/hg-preup-text/nav_h.txt && echo nav_h OK
curl -s "https://hansard.098020.xyz/a/$TOK/years?format=text" \
  | cmp -s - /tmp/hg-preup-text/years.txt && echo years OK
curl -s "https://hansard.098020.xyz/a/$TOK/members?format=text" \
  | cmp -s - /tmp/hg-preup-text/members.txt && echo members OK
curl -s "https://hansard.098020.xyz/a/$TOK/bills?format=text" \
  | cmp -s - /tmp/hg-preup-text/bills.txt && echo bills OK
curl -s "https://hansard.098020.xyz/a/$TOK/date/2026-01-12?format=text" \
  | cmp -s - /tmp/hg-preup-text/date_2026-01-12.txt && echo date OK
# Expected: all eight "OK" lines — any mismatch is a hard-stop (step 5).
```

### Step 5 — ROLLBACK (executable at ANY point; no pre-cutover rehearsal required)

```bash
# Recreate from the STEP-0 recorded image identity (the 0.1.2/27.3 build),
# with the env/volumes/ports/restart-policy from
# /tmp/hg-preup-container-config.json:
docker rm hansard-gateway
docker run -d --name hansard-gateway <restart-policy-from-step-0> \
  <mounts-from-step-0> <env-from-step-0> <port-bindings-from-step-0> \
  <STEP-0-RECORDED-IMAGE-IDENTITY>
# Re-verify after rollback (independently re-verifiable):
curl -s "https://hansard.098020.xyz/a/bogustoken000000000000000zz/" | md5sum
# Expected: BACK to 1a29cc1330d50031993c3cbcde2318d7 (the step-0 value)
curl -s -o /dev/null -w '%{http_code}\n' \
  "https://hansard.098020.xyz/a/$TOK/report/bill-774"
# Expected: 200

# Any hard-acceptance failure (screenshots, Cite guarantee, verbatim Cite
# URL, click-through, corpus byte-identity, or the cap field check)
# triggers THIS step immediately, before any live patching. The step-7
# legacy-systemd rollback in the cutover section is documented history —
# the systemd unit is retired; this image-rollover rollback supersedes it
# for release upgrades.
```

### Step 6 — Post-upgrade record

```bash
docker inspect hansard-gateway --format '{{.Image}}'
# Expected: the 0.1.3 image identity — record it (tag + .Id) in the
# execution log alongside the step-0 recorded identity (G-A5-2: both
# image identities on the record) and a pointer to the acceptance
# evidence (screenshots, corpus diffs, digests).
```
