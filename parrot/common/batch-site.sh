#!/usr/bin/env bash
# One visit of one site inside a batch run: many sites measured one after the
# other in a single GMT run, so the run's setup, baseline and idle are paid once
# per batch instead of once per site.
#
#   PARROT_URL=<url> [PARROT_CLICKS=x,y;x,y PARROT_CLICK_AT=<s>] \
#       batch-site.sh <stage> <n> <variant>
#
#   prepare   hidden    fresh plain profile, warmup with the same clicks
#   start     hidden    start Firefox on about:blank
#   visit     MEASURED  Alt+Home, the consent clicks, idle to 33 s
#   shot      hidden    review screenshot
#   scroll    MEASURED  25 wheel ticks, idle to 7 s
#   close     hidden    quit Firefox, report the visit's RESULT note
#
# <n> is the site's position in the list, <variant> what the visit does with a
# cookie banner:
#
#   none     nothing. The banner stays, the page is measured as it is before
#            anyone answers it. Every site gets this visit.
#   accept   the "accept" button is clicked
#   reject   the "reject" path is clicked, one to three clicks
#
# The clicks are real X input, Parrot's own mousemove and mousedown in a macro
# written for this visit, at PARROT_CLICK_AT seconds after the navigation. The
# coordinates and the time come from banner-scan/find-clicks.py, which found
# and verified them in the same 1440x900 window. tools/batch.py writes one flow
# step per stage, visit and site. Firefox has no consent add-on.
#
# A VISIT NEVER FAILS THE RUN
#
# In a single-site run a failure ends the run, and that is right. In a batch it
# would throw away every visit after it, so every stage here exits 0 and reports
# instead. The report is a GMT note, a line "<16-digit µs timestamp> <text>" on
# stdout that GMT stores because the flow command sets read-notes-stdout. Every
# visit ends with exactly one note
#
#   batch-site <n>-<variant> RESULT ok <url>
#   batch-site <n>-<variant> RESULT failed:<stage> <url> <reason>
#
# and only visits with "ok" have measured phases worth reading. A visit whose
# browser never came up still gets its two measured phases, idling to the same
# deadlines, so that the timing of every later visit stays the same. Their
# energy is that of an idle machine and must be thrown away by the RESULT note.
set -uo pipefail

STAGE="${1:-}"
N="${2:-}"
VARIANT="${3:-none}"
[[ -n "$STAGE" && "$N" =~ ^[0-9]+$ && "$VARIANT" =~ ^(none|accept|reject)$ ]] \
    || { echo "usage: PARROT_URL=<url> batch-site.sh <stage> <n> <none|accept|reject>" >&2; exit 2; }
: "${PARROT_URL:?PARROT_URL is not set}"
CLICKS="${PARROT_CLICKS:-}"
CLICK_AT="${PARROT_CLICK_AT:-8}"
if [[ "$VARIANT" != none && -z "$CLICKS" ]]; then
    echo "[batch-site] the $VARIANT visit needs PARROT_CLICKS" >&2
    exit 2
fi
if [[ -n "$CLICKS" && ! "$CLICKS" =~ ^[0-9]+,[0-9]+(\;[0-9]+,[0-9]+){0,2}$ ]]; then
    echo "[batch-site] PARROT_CLICKS must be x,y with up to three clicks joined by ;, not '$CLICKS'" >&2
    exit 2
fi

export DISPLAY="${DISPLAY:-:99}"
REPO=/tmp/repo
HERE="${REPO}/parrot/common"
PROFILE=/tmp/parrot-profile-measure
ID="${N}-${VARIANT}"
STATE_DIR=/tmp/batch-site
STATUS="${STATE_DIR}/${ID}.status"
MACRO="${STATE_DIR}/visit-${ID}.parrot"
mkdir -p "$STATE_DIR"

note() { printf '%s batch-site %s %s\n' "$(date +%s%6N)" "$ID" "$*"; }
log()  { printf '[batch-site %s] %s\n' "$ID" "$*"; }

# The first stage that fails is the one reported. Later stages see it and skip
# their work.
fail() {
    local stage="$1"; shift
    if [[ ! -s "$STATUS" ]]; then
        printf 'failed:%s %s\n' "$stage" "$*" > "$STATUS"
    fi
    note "FAILED ${stage}: $*"
}
failed() { [[ -s "$STATUS" ]]; }

# Quit every Firefox in the container: the measured one, a stray warmup, a
# pop-up. SIGTERM first so Firefox exits the normal way, then SIGKILL.
stop_firefox() {
    pkill -TERM -f '/opt/firefox/' 2>/dev/null || true
    for _ in $(seq 1 20); do
        pgrep -f '/opt/firefox/' >/dev/null 2>&1 || return 0
        sleep 0.5
    done
    pkill -KILL -f '/opt/firefox/' 2>/dev/null || true
    sleep 1
    ! pgrep -f '/opt/firefox/' >/dev/null 2>&1
}

# The visit macro of this visit: parrot/firefox/visit.parrot with the consent
# clicks in the middle. The waits add up to 30 s after the navigation, like the
# fixed macro, whatever the clicks take. replay.py applies a wait to the event
# after it, so the last wait sits before the closing log line.
write_macro() {
    local spent rest
    {
        printf '# Parrot recording v2\n'
        printf '# Written by parrot/common/batch-site.sh for visit %s of %s.\n' "$ID" "$PARROT_URL"
        printf 'startcommand = bash /tmp/repo/parrot/common/launch-browser.sh firefox %s about:blank\n' "$PROFILE"
        printf 'windowtitle  = Mozilla Firefox\nwindowclass  = firefox\n\n'
        printf 'log Visit page (%s): navigate and idle for 30 s\n' "$VARIANT"
        printf 'wait 0.1\nkeydown Alt_L\nwait 0.05\nkeydown Home\nwait 0.05\nkeyup Home\nwait 0.05\nkeyup Alt_L\n'
    } > "$MACRO"
    spent=0.25
    if [[ -n "$CLICKS" ]]; then
        local first=1 c i=0
        IFS=';' read -ra list <<<"$CLICKS"
        for c in "${list[@]}"; do
            i=$((i + 1))
            if (( first )); then
                printf 'wait %s\n' "$CLICK_AT" >> "$MACRO"
                spent=$(python3 -c "print($spent + $CLICK_AT)")
                first=0
            else
                printf 'wait 1.5\n' >> "$MACRO"
                spent=$(python3 -c "print($spent + 1.5)")
            fi
            printf 'log Consent click %d of %d (%s) at %s\n' "$i" "${#list[@]}" "$VARIANT" "$c" >> "$MACRO"
            printf 'mousemove %s %s\nwait 0.3\nmousedown 1\nwait 0.1\nmouseup 1\n' "${c%,*}" "${c#*,}" >> "$MACRO"
            spent=$(python3 -c "print($spent + 0.4)")
        done
    fi
    rest=$(python3 -c "print(round(30.25 - $spent, 3))")
    if python3 -c "import sys; sys.exit(0 if $rest >= 0.5 else 1)"; then
        printf 'wait %s\n' "$rest" >> "$MACRO"
    else
        fail visit "the clicks end ${spent} s after the navigation, past the 30 s idle"
        return 1
    fi
    printf 'log Page loaded, %s, idled: 30 s after the navigation was started\n' "$VARIANT" >> "$MACRO"
}

# A measured stage: phase-clock start, the macro, phase-clock finish. If an
# earlier stage failed, the macro is skipped and the phase only idles to its
# deadline.
measured() {
    local phase="$1" macro="$2"
    bash "${REPO}/common/phase-clock.sh" start "$phase"
    if failed; then
        log "skipping the ${phase} macro, this visit already $(cut -d' ' -f1 "$STATUS")"
    else
        python3 /usr/local/bin/replay.py "$macro" \
            || fail "$phase" "replay.py $(basename "$macro") exited $?"
        if [[ "$phase" == visit ]]; then
            echo "$(date +%s%N) website_load=1"
        fi
    fi
    if ! bash "${REPO}/common/phase-clock.sh" finish "$phase"; then
        fail "$phase" "the ${phase} phase ran over its deadline"
    fi
}

case "$STAGE" in
prepare)
    rm -f "$STATUS"
    note "START ${VARIANT} ${PARROT_URL}${CLICKS:+ clicks ${CLICKS} at ${CLICK_AT}s}"
    stop_firefox || fail prepare "a Firefox from the previous visit would not quit"
    if ! failed; then
        PARROT_CONSENT=none bash "${HERE}/setup-profile.sh" firefox "$PROFILE" \
            || fail prepare "setup-profile.sh failed"
    fi
    if ! failed; then
        write_macro || true
    fi
    if ! failed; then
        PARROT_CLICKS="$CLICKS" PARROT_CLICK_AT="$CLICK_AT" bash "${HERE}/warmup.sh" firefox \
            || fail prepare "warmup.sh failed"
    fi
    ;;

start)
    failed && exit 0
    python3 /usr/local/bin/replay.py "${REPO}/parrot/firefox/start.parrot" \
        || fail start "replay.py start.parrot exited $?"
    ;;

visit)
    measured visit "$MACRO"
    ;;

shot)
    if failed; then
        log "screenshot anyway: it shows what the failed visit left on the screen"
    fi
    PARROT_WINDOW_CLASS=firefox bash "${HERE}/screenshot.sh" /tmp/review-screenshot.png
    bash "${REPO}/common/upload-screenshot.sh" /tmp/review-screenshot.png parrot \
        "firefox-${VARIANT}" "$PARROT_URL" "${SCREENSHOT_URL:-off}"
    ;;

scroll)
    measured scroll "${REPO}/parrot/firefox/scroll.parrot"
    ;;

close)
    stop_firefox || fail close "Firefox would not quit, even with SIGKILL"
    rm -rf "$PROFILE"
    if failed; then
        note "RESULT $(cut -d' ' -f1 "$STATUS") ${PARROT_URL} $(cut -d' ' -f2- "$STATUS")"
    else
        note "RESULT ok ${PARROT_URL}"
    fi
    ;;

*)
    echo "[batch-site] unknown stage '$STAGE'" >&2
    exit 2
    ;;
esac
exit 0
