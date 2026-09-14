# Parrot recording v2
# Website benchmark, MEASURED phase 2: scroll down for 5 s.
#
# 25 wheel-down ticks 200 ms apart is 5 s. The sibling scenario in this
# repository, playwright/usage_scenario.yml, performs the same 25 ticks at the
# same 200 ms spacing through Playwright, and dispatches 120 px per tick,
# because one X button-5 tick was measured to move this Chrome exactly 120 px.
#
# There is deliberately NO "stop when the bottom is reached" check, which is
# where this differs from GMT's template. Ending early on a short page would
# make the phase a different length on every site, and the two scenarios in this
# repository are only comparable if the phase is always the same 5 s.

startcommand = bash /tmp/repo/parrot/common/launch-browser.sh chrome /tmp/parrot-profile-measure about:blank
windowtitle  =
windowclass  = parrot-chrome

log Scroll down: 25 wheel-down ticks over 5 s
mousemove 720 500
label scroll_down
wait 0.19
mousedown 5
wait 0.01
mouseup 5
loop scroll_down 25
log Scroll complete: 5 s of scrolling
