#!/usr/bin/env bash
# Capture what the measured Chrome is showing, for review.
#
#   screenshot.sh <out.png>
#
# Chrome's browser window rather than the page alone: Chrome's own address bar
# is in it, so a redirect, a bot wall or an error page shows where the browser
# really ended up, which a real browser has no other way of reporting.
# <out.png>.title gets the page title from the same window's title, for the
# gallery caption.
#
# Only the window, not the whole X screen, because a rootless Xwayland, the X
# server of a Wayland desktop, answers GetImage on the root window with
# BadMatch, which import reports as "unable to read X window image `root'".
# Machine 12's :0 refused it that way in the first headful run on 2026-09-23,
# and a local Wayland desktop reproduces it. In the Xvfb scenario Chrome fills
# the 1440x900 screen, so the picture is the same as the screen was.
#
# Never fails the run, for the same reason as common/upload-screenshot.sh.
set -uo pipefail

out="${1:?usage: screenshot.sh <out.png>}"
# :99 is the container's Xvfb, as in warmup.sh. The headful scenario sets :0.
export DISPLAY="${DISPLAY:-:99}"

log() { printf '[screenshot] %s\n' "$*"; }

rm -f "$out" "$out.title"

# The window position-window.sh picks, and so the one the macros drive: the
# largest visible window of Chrome's class. Chrome maps several windows with
# that class, and pop-ups a page opens are among them.
window="" area=0
for id in $(xdotool search --onlyvisible --class parrot-chrome 2>/dev/null); do
    geometry="$(xdotool getwindowgeometry --shell "$id" 2>/dev/null)" || continue
    width="$(awk -F= '/^WIDTH=/{print $2}' <<<"$geometry")"
    height="$(awk -F= '/^HEIGHT=/{print $2}' <<<"$geometry")"
    [[ "$width" =~ ^[0-9]+$ && "$height" =~ ^[0-9]+$ ]] || continue
    if (( width * height > area )); then
        area=$(( width * height )) window="$id"
    fi
done
if [[ -z "$window" ]]; then
    log "WARNING: no visible Chrome window on $DISPLAY, nothing to capture"
    exit 0
fi

# The window's area as the screen shows it (-screen), so that a pop-up the page
# opened on top of it is in the picture, as it was on screen while the visit was
# measured. Reading the window alone would give black there: an X server without
# backing store has no contents for the covered part of a window, and the Xvfb
# hands back black. Where the screen cannot be read, as on a rootless Xwayland,
# it falls back to the window's own contents, which Xwayland keeps per window.
#
# -silent: otherwise import rings the X bell first, a real beep on a host
# display. No -frame: a window manager's decoration is not part of Chrome.
if import -silent -screen -window "$window" "$out" 2>/tmp/screenshot-import.err; then
    source=screen
elif import -silent -window "$window" "$out" 2>>/tmp/screenshot-import.err; then
    source=window
else
    log "WARNING: import of window $window failed on $DISPLAY: $(head -c 300 /tmp/screenshot-import.err)"
    exit 0
fi

# The browser window is titled "<page title> - Google Chrome for Testing".
title="$(xdotool getwindowname "$window" 2>/dev/null || true)"
title="${title% - Google Chrome for Testing}"
printf '%s' "$title" > "$out.title"
log "captured window $window on $DISPLAY from the $source, $(stat -c %s "$out") bytes, title: ${title:-none}"
