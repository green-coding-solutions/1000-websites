#!/usr/bin/env bash
# Fill the squid cache with everything the page under test needs, then get out
# of the way.
#
#   warmup.sh <firefox|chrome>
#
# This is the equivalent of gmtPlaywrightCache() in GMT's
# templates/website/usage_scenario_cached.yml: load the page once so that the
# proxy has it, then throw the browser state away so the measured load starts
# with a cold browser cache and a warm proxy cache.
#
# WHY A SEPARATE BROWSER INSTANCE AND A SEPARATE PROFILE
#
# Playwright throws the browser cache away by closing the context and opening a
# new one. A real browser has no such operation, and the two obvious substitutes
# are both wrong:
#
#   * A hard reload (Ctrl+Shift+R) sends Cache-Control: no-cache upstream, and
#     the squid config in greencoding/squid_reverse_proxy does NOT carry
#     `ignore-reload`. So a hard reload defeats the very cache this step exists
#     to fill, and the measured phase would go to the origin.
#   * Reusing the profile leaves the measured load reading its own disk cache,
#     which is a different measurement from the one the template defines.
#
# So the warmup gets its own profile and its own process, and both are gone
# before the measured instance starts.
#
# WHY IT IS NOT A .parrot MACRO
#
# Nothing here is an interaction. It is a process that has to be started, waited
# for and killed, and killing a browser from a macro means sending Ctrl+Q and
# hoping. This step is hidden and unmeasured, so it costs nothing to write it as
# what it is.
set -euo pipefail

BROWSER="${1:-}"
if [[ -z "$BROWSER" ]]; then
    echo "usage: warmup.sh <firefox|chrome>" >&2
    exit 2
fi

: "${PARROT_URL:?PARROT_URL is not set - the usage_scenario must pass __GMT_VAR_PAGE__ into the container environment}"

export DISPLAY="${DISPLAY:-:99}"
HERE="$(dirname "$(readlink -f "$0")")"
PROFILE="${PARROT_WARMUP_PROFILE:-/tmp/parrot-profile-warmup}"

# 20 s, not the 5 s the measured phase idles for. The measured phase only has to
# render a page the proxy already holds; this one has to discover every
# subresource on a cold cache, over the real network, including whatever the
# page's own scripts fetch after load. Too short here does not fail loudly - it
# leaves part of the page uncached, and the measured load quietly becomes part
# network fetch. Raise it for a heavy page rather than lowering it.
WARMUP_SECONDS="${PARROT_WARMUP_SECONDS:-20}"

log() { printf '[warmup] %s\n' "$*"; }

rm -rf "$PROFILE"
bash "${HERE}/setup-profile.sh" "$BROWSER" "$PROFILE"

log "loading ${PARROT_URL} in ${BROWSER} for ${WARMUP_SECONDS}s to fill the proxy cache"
bash "${HERE}/launch-browser.sh" "$BROWSER" "$PROFILE" "$PARROT_URL" \
    >/tmp/parrot-warmup.log 2>&1 &
warmup_pid=$!

sleep "$WARMUP_SECONDS"

log "stopping the warmup browser"
kill "$warmup_pid" 2>/dev/null || true
# The launcher execs the browser, so $warmup_pid is the browser itself. Its
# children (content processes, the GPU process) exit with it, but not
# instantly, and the profile path is unique enough to sweep by.
pkill -f "$PROFILE" 2>/dev/null || true

# THE CHECK THAT MATTERS. replay.py finds the app by window class, so a warmup
# window that is still mapped when the next flow step runs is the window the
# measured phases would drive: right browser, wrong profile, page already
# loaded, and the measurement is of a reload that never happened. It would not
# look like an error anywhere.
for _ in $(seq 1 40); do
    if ! pgrep -f "$PROFILE" >/dev/null 2>&1; then
        break
    fi
    sleep 0.5
done

if pgrep -f "$PROFILE" >/dev/null 2>&1; then
    echo "[warmup] FATAL: the warmup browser is still running after 20s" >&2
    pgrep -af "$PROFILE" >&2 || true
    exit 1
fi

# `|| true` is load-bearing under `set -o pipefail`: with the warmup browser
# gone there may be no windows at all, xdotool exits 1 on an empty search, and
# the assignment would take the whole script down at its last line - after the
# work succeeded.
remaining="$( (xdotool search --onlyvisible --name . 2>/dev/null || true) | wc -l)"
log "warmup browser gone; ${remaining} visible window(s) left on ${DISPLAY}"
rm -rf "$PROFILE"
