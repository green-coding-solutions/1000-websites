# Parrot recording v2
# Website benchmark, hidden step 1 of 3: bring Firefox up on a blank page.
#
# WRITTEN BY HAND, NOT RECORDED. There is no interaction here to record. What
# this file does is use replay.py's own "launch the app, wait for its window,
# pin its geometry, focus it" path, so that starting the browser gets the same
# 90 s window wait and the same failure reporting every other Parrot scenario
# gets - and so that it happens in a HIDDEN flow step, outside the measurement.
#
# The page under test is deliberately NOT on the command line. This instance
# has to come up cold, so that the measured phase is the first time it fetches
# the page. Navigation happens in visit.parrot with Alt+Home, against the home
# page that common/setup-profile.sh wrote into the profile from __GMT_VAR_PAGE__.
#
# windowclass is the res_CLASS, which is what xdotool --class matches. Read off
# a running Firefox 155.0.1 here:
#   WM_CLASS = ("Navigator", "firefox")
# so the class is `firefox` and the res_NAME is `Navigator` - which is the name
# common/pin-windows.sh has to be given, and is not the obvious guess.

startcommand = bash /tmp/repo/parrot/common/launch-browser.sh firefox /tmp/parrot-profile-measure about:blank
windowtitle  = Mozilla Firefox
windowclass  = firefox

# Let the browser finish starting. This is generous on purpose: it is hidden and
# unmeasured, and every second here is a second the measured phase does not
# spend on start-up work that leaked past the window mapping.
wait 8
# Click into the content area. Firefox comes up with the address bar focused, and
# this puts the focus on the document instead, so the measured Alt+Home is a
# navigation rather than an address-bar keystroke. It also leaves the pointer
# where scroll.parrot needs it, in the middle of the viewport.
mousemove 720 500
wait 0.3
mousedown 1
wait 0.1
mouseup 1
# The trailing wait only happens because there is an event after it - replay
# applies a `wait` to the NEXT event and drops a trailing one. `log` is that
# event, and it doubles as the line that says the step got this far.
wait 2
log Browser ready: Firefox is up on a blank page with a cold cache
