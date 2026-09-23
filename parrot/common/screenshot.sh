#!/usr/bin/env bash
# Capture what the measured Chrome is showing, for review.
#
#   screenshot.sh <out.png>
#
# The whole X screen rather than the page alone: Chrome's own address bar is in
# it, so a redirect, a bot wall or an error page shows where the browser really
# ended up, which a real browser has no other way of reporting. <out.png>.title
# gets the page title from Chrome's window title, for the gallery caption.
#
# Never fails the run, for the same reason as common/upload-screenshot.sh.
set -uo pipefail

out="${1:?usage: screenshot.sh <out.png>}"
# :99 is the container's Xvfb, as in warmup.sh. The headful scenario sets :0.
export DISPLAY="${DISPLAY:-:99}"

log() { printf '[screenshot] %s\n' "$*"; }

rm -f "$out" "$out.title"
if ! import -window root "$out" 2>/tmp/screenshot-import.err; then
    log "WARNING: import failed on $DISPLAY: $(head -c 300 /tmp/screenshot-import.err)"
    exit 0
fi

# Chrome maps several windows with this class; only the browser window's title
# ends in the product name, as "<page title> - Google Chrome for Testing".
title="$( (xdotool search --onlyvisible --class parrot-chrome getwindowname %@ 2>/dev/null || true) \
    | grep -m1 -- '- Google Chrome for Testing$' || true)"
title="${title% - Google Chrome for Testing}"
printf '%s' "$title" > "$out.title"
log "captured $DISPLAY, $(stat -c %s "$out") bytes, title: ${title:-none}"
