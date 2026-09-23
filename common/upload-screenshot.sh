#!/usr/bin/env bash
# Upload a review screenshot of the page under test to screenshot-server/.
#
#   upload-screenshot.sh <png> <driver> <variant> <page> <upload-url>
#
# <driver> is parrot or playwright, <variant> xvfb or headful. If <png>.title
# exists, its contents go along as the page title for the gallery caption.
#
# WHY UPLOAD. A run on the GMT cluster keeps no files, so what a run rendered can
# only be seen if it is sent somewhere while the run is still alive.
# screenshot-server/ receives it and shows every site, both drivers side by side,
# in one gallery.
#
# WHY IT NEVER FAILS THE RUN. It runs in a hidden phase, and no measurement
# depends on it. A review server that is down, slow or misconfigured must not
# cost a cluster run, so every problem is a WARNING line in the run's log and the
# script exits 0. An upload-url of "off" skips the upload, for runs that need no
# screenshots.
#
# No authentication: screenshot-server/ is meant to live for one batch of runs
# behind an HTTPS proxy and then be deleted.
set -uo pipefail

png="${1:-}" driver="${2:-}" variant="${3:-}" page="${4:-}" url="${5:-}"

log() { printf '[screenshot] %s\n' "$*"; }

if [[ "$url" == "off" ]]; then
    log "upload is off (__GMT_VAR_SCREENSHOT_URL__=off)"
    exit 0
fi
if [[ -z "$png" || -z "$driver" || -z "$variant" || -z "$page" || -z "$url" ]]; then
    log "WARNING: usage: upload-screenshot.sh <png> <driver> <variant> <page> <upload-url>; not uploading"
    exit 0
fi
if [[ ! -s "$png" ]]; then
    log "WARNING: no screenshot at $png, nothing to upload"
    exit 0
fi

title=""
[[ -f "$png.title" ]] && title="$(head -c 300 "$png.title")"

response="$(mktemp)"
errors="$(mktemp)"
# --url-query encodes each value, so a page URL with & or = in it arrives intact.
# 30 s bounds a hung server; the hidden phase has no deadline of its own.
status="$(curl -sS --max-time 30 -X PUT \
    -H 'Content-Type: image/png' --data-binary "@$png" \
    --url-query "page=$page" --url-query "driver=$driver" \
    --url-query "variant=$variant" --url-query "title=$title" \
    -o "$response" -w '%{http_code}' "$url" 2>"$errors")"

if [[ "$status" == 201 ]]; then
    log "uploaded $(stat -c %s "$png") bytes to $url: $(head -c 300 "$response")"
else
    log "WARNING: upload to $url failed with HTTP ${status:-none}: $(cat "$response" "$errors" | head -c 300 | tr '\n' ' ')"
fi
rm -f "$response" "$errors"
exit 0
