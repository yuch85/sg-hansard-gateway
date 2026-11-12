#!/usr/bin/env bash
# docker-entrypoint.sh — two-process container model (Phase 27.5, S1/S4/S6).
#
# tini is PID 1 (CMD ["tini", "--", "/app/docker-entrypoint.sh"]) and this
# script supervises up to two children:
#   1. the gateway server — ALWAYS started;
#   2. the crawl loop — only when HANSARD_CRAWL_ENABLED parses true
#      (1/true/yes, any case — equivalent outcome to config.py).
#
# S1: the image still has NO ENTRYPOINT — `docker run <image> uv run python
# -m hansard_gateway crawl once` replaces the whole CMD (tini included), so
# one-shot crawls run exactly as on 0.1.x.
# S4: a loop crash never takes the server down — the loop runs under a
# supervisor subshell that restarts it after HANSARD_CRAWL_RESTART_SECONDS.
# S6: `docker stop` (SIGTERM to tini -> this script) is forwarded to BOTH
# children; the container exits 0 on a clean signal shutdown.
#
# The server port is HARDCODED 8000 inside the container: the HEALTHCHECK
# probes hardcoded 127.0.0.1:8000, so honoring HANSARD_APP_PORT here would
# make the container permanently unhealthy. Operators map ports with
# `docker run -p 9000:8000`; HANSARD_APP_PORT stays a host-run knob.

set -u

# Restart delay for a crashed crawl loop (named, env-overridable so tests and
# ops can shrink it). Validated numerically: a non-numeric or non-positive
# value would kill the supervisor's sleep, so warn and fall back to 30.
RESTART_SECONDS=30
_raw_restart="${HANSARD_CRAWL_RESTART_SECONDS:-30}"
if [[ "$_raw_restart" =~ ^[0-9]+$ ]] && [ "$_raw_restart" -gt 0 ]; then
  RESTART_SECONDS="$_raw_restart"
else
  echo "hansard-entrypoint: WARNING invalid HANSARD_CRAWL_RESTART_SECONDS='${_raw_restart}' (want a positive integer) — using 30" >&2
fi

# Boolean parsing with an equivalent outcome to config.py _env_bool:
# leading/trailing whitespace is stripped, then 1/true/yes (any case) enable.
_trim() {
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"   # leading whitespace
  s="${s%"${s##*[![:space:]]}"}"   # trailing whitespace
  printf '%s' "$s"
}
_is_true_word() {
  case "$(_trim "$1")" in
    1 | [Tt][Rr][Uu][Ee] | [Yy][Ee][Ss]) return 0 ;;
  esac
  return 1
}

CRAWL_ENABLED=0
_is_true_word "${HANSARD_CRAWL_ENABLED:-0}" && CRAWL_ENABLED=1

INTERVAL_HOURS="${HANSARD_CRAWL_INTERVAL_HOURS:-24}"
FIRST_RUN_DAYS="${HANSARD_CRAWL_FIRST_RUN_DAYS:-31}"
FULL_HISTORY=0
_is_true_word "${HANSARD_CRAWL_FULL_HISTORY:-0}" && FULL_HISTORY=1
INDEX_DB="${HANSARD_INDEX_DB_PATH:-/data/index.db}"

echo "hansard-entrypoint: starting server on 0.0.0.0:8000 (container always binds 8000; map with docker run -p)"
if [ "$CRAWL_ENABLED" = 1 ]; then
  echo "hansard-entrypoint: crawler ON (HANSARD_CRAWL_ENABLED=1) — interval ${INTERVAL_HOURS}h, full_history=${FULL_HISTORY}, first-run guard window ${FIRST_RUN_DAYS} days"
  if [ ! -s "$INDEX_DB" ] && [ "$FULL_HISTORY" != 1 ]; then
    echo "hansard-entrypoint: fresh index — the first crawl covers only the last ${FIRST_RUN_DAYS} days; set HANSARD_CRAWL_FULL_HISTORY=1 for full history (~26k requests, many hours at ~1 req/s)"
  fi
else
  echo "hansard-entrypoint: crawler OFF (set HANSARD_CRAWL_ENABLED=1 to run the in-image crawl loop)"
fi

uv run python -m hansard_gateway serve --host 0.0.0.0 --port 8000 &
SERVER_PID=$!
LOOP_SUP_PID=""

if [ "$CRAWL_ENABLED" = 1 ]; then
  (
    LOOP_CHILD=""
    on_term() {
      [ -n "$LOOP_CHILD" ] && kill -TERM "$LOOP_CHILD" 2>/dev/null
      [ -n "$LOOP_CHILD" ] && wait "$LOOP_CHILD" 2>/dev/null
      exit 0
    }
    trap on_term TERM INT
    while true; do
      uv run python -m hansard_gateway crawl loop &
      LOOP_CHILD=$!
      wait "$LOOP_CHILD"
      rc=$?
      LOOP_CHILD=""
      echo "hansard-entrypoint: crawl loop exited rc=${rc} — restarting in ${RESTART_SECONDS}s" >&2
      sleep "$RESTART_SECONDS" &
      LOOP_CHILD=$!
      wait "$LOOP_CHILD"
      LOOP_CHILD=""
    done
  ) &
  LOOP_SUP_PID=$!
fi

shutdown() {
  trap - TERM INT
  [ -n "$LOOP_SUP_PID" ] && kill -TERM "$LOOP_SUP_PID" 2>/dev/null
  kill -TERM "$SERVER_PID" 2>/dev/null
  wait "$SERVER_PID" 2>/dev/null
  [ -n "$LOOP_SUP_PID" ] && wait "$LOOP_SUP_PID" 2>/dev/null
  echo "hansard-entrypoint: shutdown complete — exit 0"
  exit 0
}
trap shutdown TERM INT

# Wait on the server: when it exits, stop the loop side and exit with the
# server's code (the container's job is the server; the loop is a companion).
wait "$SERVER_PID"
SERVER_RC=$?
echo "hansard-entrypoint: server exited rc=${SERVER_RC} — stopping the crawl loop" >&2
if [ -n "$LOOP_SUP_PID" ]; then
  kill -TERM "$LOOP_SUP_PID" 2>/dev/null
  wait "$LOOP_SUP_PID" 2>/dev/null
fi
exit "$SERVER_RC"
