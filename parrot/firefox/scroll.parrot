# Parrot recording v2
# Website benchmark, MEASURED phase 2: scroll down for 5 s.
#
# The equivalent of the template's mouse-wheel loop, with one deliberate
# difference. The template scrolls until the page reports it is at the bottom
# and then sleeps; this scrolls for a fixed five seconds. A real wheel tick is
# not a number of CSS pixels - the browser decides how far it goes - so "scroll
# to the bottom" is not a fixed amount of work here and could not be compared
# between two runs of different pages. A fixed five seconds of scrolling can.
#
# The consequence, which belongs in any write-up of these numbers: this phase is
# five seconds of scrolling, not a fixed distance, and a wheel tick is not the
# same distance in Firefox as it is in Chrome. Compare pages within one browser.
#
# 25 ticks, 200 ms apart. The pointer is already at 720,500 - start.parrot put
# it there and left it there - but it is set again here so this file replays
# correctly on its own.

startcommand = bash /tmp/repo/parrot/common/launch-browser.sh firefox /tmp/parrot-profile-measure about:blank
windowtitle  = Mozilla Firefox
windowclass  = firefox

log Scroll down: 25 wheel-down ticks over 5 s
mousemove 720 500
label scroll_down
# The wait has to come FIRST inside the loop body. `loop` clears the pending
# wait when it jumps, so a wait written just before the `loop` line would be
# dropped on every iteration but the last.
wait 0.19
mousedown 5
wait 0.01
mouseup 5
loop scroll_down 25
log Scroll complete: 5 s of scrolling
