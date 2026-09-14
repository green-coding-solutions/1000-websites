#!/usr/bin/env bash
# Fail the run here, in a hidden step, if the page under test is not reachable
# through the proxy, or if the proxy is not the one websites/squid-ca.crt
# belongs to.
#
# This is the equivalent of the template's "Check HTTP Status Code" step, moved
# to the FRONT of the flow. Playwright can report a status code from the same
# navigation it measured; a real browser cannot, so the status has to be taken
# separately - and once it is separate, taking it first is strictly better. A
# typo in __GMT_VAR_PAGE__ then costs a couple of seconds instead of a warmup, a
# browser start and two measured phases of a browser rendering an error page.
#
# The certificate check is the other half, and it is the more valuable one.
# --cacert without --insecure means curl builds a chain from squid's freshly
# minted leaf to the CA committed in this repository. If squid is rebuilt with a
# new signing certificate, this line fails with a certificate error naming the
# problem, instead of two browsers silently showing an interstitial in a phase
# that still produces a plausible-looking energy figure.
set -euo pipefail

: "${PARROT_URL:?PARROT_URL is not set - the usage_scenario must pass __GMT_VAR_PAGE__ into the container environment}"

PROXY_HOST="${PARROT_PROXY_HOST:-squid}"
PROXY_PORT="${PARROT_PROXY_PORT:-3128}"
CA_FILE="${PARROT_CA:-/tmp/repo/parrot/squid-ca.crt}"

log() { printf '[check-url] %s\n' "$*"; }

[[ -f "$CA_FILE" ]] || { echo "[check-url] CA file not found: $CA_FILE" >&2; exit 1; }

log "GET ${PARROT_URL} via http://${PROXY_HOST}:${PROXY_PORT}, verifying against ${CA_FILE}"

status="$(curl -sS -L --max-time 60 \
    --proxy "http://${PROXY_HOST}:${PROXY_PORT}" \
    --cacert "$CA_FILE" \
    -o /dev/null -w '%{http_code}' \
    "$PARROT_URL")"

if [[ ! "$status" =~ ^[0-9]+$ ]] || (( status >= 400 )) || (( status == 0 )); then
    echo "[check-url] FATAL: ${PARROT_URL} returned HTTP ${status} through the proxy" >&2
    exit 1
fi

log "HTTP ${status} - page is reachable and the proxy's certificate chains to the committed CA"
