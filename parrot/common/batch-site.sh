#!/usr/bin/env bash
# One site's steps inside a batch run: many sites measured one after the other
# in a single GMT run, so the run's setup, baseline and idle are paid once per
# batch instead of once per site.
#
#   PARROT_URL=<url> batch-site.sh prepare <n>   hidden:   profile + warmup
#   PARROT_URL=<url> batch-site.sh start   <n>   hidden:   start Firefox
#   PARROT_URL=<url> batch-site.sh visit   <n>   MEASURED: Alt+Home, idle to 33 s
#   PARROT_URL=<url> batch-site.sh shot    <n>   hidden:   review screenshot
#   PARROT_URL=<url> batch-site.sh scroll  <n>   MEASURED: 25 wheel ticks, to 7 s
#   PARROT_URL=<url> batch-site.sh close   <n>   hidden:   quit Firefox, clean up
#
# <n> is the site's position in the batch. tools/make-batch.py writes one flow
# step per stage and site.
#
# A SITE NEVER FAILS THE RUN
#
# In a single-site run a failure ends the run, and that is right. In a batch it
# would throw away every site after it, so every stage here exits 0 and reports
# instead. The report is a GMT note, a line "<16-digit µs timestamp> <text>" on
# stdout that GMT stores because the flow command sets read-notes-stdout. Every
# site ends with exactly one note
#
#   batch-site <n> RESULT ok <url>
#   batch-site <n> RESULT failed:<stage> <url> <reason>
#
# and only sites with "ok" have measured phases worth reading. A site whose
# browser never came up still gets its two measured phases, idling to the same
# deadlines, so that the timing of every later site stays the same. Their energy
# is that of an idle machine and must be thrown away by the RESULT note.
#
# The measured stages run the same commands as the single-site scenario,
# phase-clock.sh around a replay.py macro, plus this script's bash around them.
set -uo pipefail

STAGE="${1:-}"
N="${2:-}"
[[ -n "$STAGE" && "$N" =~ ^[0-9]+$ ]] || { echo "usage: PARROT_URL=<url> batch-site.sh <stage> <n>" >&2; exit 2; }
: "${PARROT_URL:?PARROT_URL is not set}"

export DISPLAY="${DISPLAY:-:99}"
REPO=/tmp/repo
HERE="${REPO}/parrot/common"
PROFILE=/tmp/parrot-profile-measure
STATE_DIR=/tmp/batch-site
STATUS="${STATE_DIR}/${N}.status"
mkdir -p "$STATE_DIR"

note() { printf '%s batch-site %s %s\n' "$(date +%s%6N)" "$N" "$*"; }
log()  { printf '[batch-site %s] %s\n' "$N" "$*"; }

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

# A measured stage: phase-clock start, the macro, phase-clock finish. If an
# earlier stage failed, the macro is skipped and the phase only idles to its
# deadline.
measured() {
    local phase="$1" macro="$2"
    bash "${REPO}/common/phase-clock.sh" start "$phase"
    if failed; then
        log "skipping the ${phase} macro, this site already $(cut -d' ' -f1 "$STATUS")"
    else
        python3 /usr/local/bin/replay.py "${REPO}/parrot/firefox/${macro}" \
            || fail "$phase" "replay.py ${macro} exited $?"
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
    note "START ${PARROT_URL}"
    stop_firefox || fail prepare "a Firefox from the previous site would not quit"
    if ! failed; then
        bash "${HERE}/fresh-profile.sh" "$PROFILE" || fail prepare "fresh-profile.sh failed"
    fi
    if ! failed; then
        bash "${HERE}/warmup.sh" firefox || fail prepare "warmup.sh failed"
    fi
    ;;

start)
    failed && exit 0
    python3 /usr/local/bin/replay.py "${REPO}/parrot/firefox/start.parrot" \
        || fail start "replay.py start.parrot exited $?"
    ;;

visit)
    measured visit visit.parrot
    ;;

shot)
    if failed; then
        log "screenshot anyway: it shows what the failed site left on the screen"
    fi
    PARROT_WINDOW_CLASS=firefox bash "${HERE}/screenshot.sh" /tmp/review-screenshot.png
    bash "${REPO}/common/upload-screenshot.sh" /tmp/review-screenshot.png parrot \
        "${PARROT_VARIANT:-firefox}" "$PARROT_URL" "${SCREENSHOT_URL:-off}"
    ;;

scroll)
    measured scroll scroll.parrot
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
