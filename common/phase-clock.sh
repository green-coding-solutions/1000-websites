#!/usr/bin/env bash
# Give each measured phase the same wall-clock length in both scenarios.
#
#   bash /tmp/repo/common/phase-clock.sh start  <phase>
#   bash /tmp/repo/common/phase-clock.sh finish <phase>
#
# `start` is the first command of a measured phase and `finish` the last.
# `finish` sleeps until the phase's budget has passed since `start`, so the phase
# ends on the same deadline whichever driver ran it.
#
# WHY. The machine draws its baseline power for as long as a phase is open, so
# two phases of different length do not hold comparable energy, whatever ran in
# them. The two drivers do not take the same time for the same work, and not by a
# constant either. Parrot spends about a second starting replay.py and placing
# the window before its first key press. Playwright's page.goto waits for the
# load event and each mouse.wheel waits for the renderer to take the event, so a
# heavy page stretches both Playwright phases. Measured on machine 6 before this
# script existed, for the same pages:
#
#   Visit page and idle for 30 s   Parrot 31.39 to 31.42 s   Playwright 30.54 to 32.30 s
#   Scroll down and wait 5 s       Parrot  6.19 to  6.30 s   Playwright  5.33 to  7.21 s
#
# A phase that is already past its budget cannot be shortened, so `finish` fails
# the run instead of letting a number through that is not comparable.
#
# The stamp lives in the container's own /tmp: both commands of a phase run in
# the same container, and /tmp/repo is mounted read-only.
set -euo pipefail

# The budgets live here and nowhere else, so the two scenarios cannot drift
# apart. Each one is the slowest driver's work above plus headroom, and both
# scenarios pay the same idle for it.
budget_s() {
  case $1 in
    visit)  echo 33 ;;
    scroll) echo 7 ;;
    *) echo "[phase-clock] unknown phase '$1' (known: visit, scroll)" >&2; return 2 ;;
  esac
}

usage() {
  echo "usage: phase-clock.sh start|finish <phase>" >&2
  exit 2
}

# Nanoseconds as seconds with millisecond precision, for messages.
fmt() {
  printf '%d.%03d' $(( $1 / 1000000000 )) $(( $1 % 1000000000 / 1000000 ))
}

[[ $# -eq 2 ]] || usage
action=$1
phase=$2
budget=$(budget_s "$phase")
stamp=/tmp/phase-clock-$phase

case $action in
  start)
    date +%s%N > "$stamp"
    ;;
  finish)
    if [[ ! -s $stamp ]]; then
      echo "[phase-clock] $phase: finish without a start in this container" >&2
      exit 1
    fi
    start_ns=$(<"$stamp")
    rm -f "$stamp"
    elapsed=$(( $(date +%s%N) - start_ns ))
    remaining=$(( budget * 1000000000 - elapsed ))
    if (( remaining < 0 )); then
      echo "[phase-clock] $phase: work took $(fmt "$elapsed") s, over its ${budget} s budget. Failing the run, because a longer phase is not comparable." >&2
      exit 1
    fi
    echo "[phase-clock] $phase: work took $(fmt "$elapsed") s, idling $(fmt "$remaining") s to the ${budget} s budget"
    sleep "$(printf '%d.%09d' $(( remaining / 1000000000 )) $(( remaining % 1000000000 )))"
    ;;
  *)
    usage
    ;;
esac
