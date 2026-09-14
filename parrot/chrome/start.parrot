# Parrot recording v2
# Website benchmark, hidden step 1 of 3: bring Chrome up on a blank page.
#
# WRITTEN BY HAND, NOT RECORDED. It exists so that browser start-up, which is
# seconds of process launch and profile work that has nothing to do with the
# page, happens in a hidden step and lands in neither measured phase. It brings
# the browser up on about:blank; visit.parrot then navigates via Alt+Home.
#
# windowclass is `parrot-chrome` because common/launch-browser.sh passes
# --class=parrot-chrome. Left to itself Chrome maps under
#   WM_CLASS = ("chromium-browser (/tmp/parrot-profile-measure)", "Chromium-browser")
# - the res_name carries the profile PATH - which was read off a running window
# here. A class that contains a filesystem path is not something a macro or a
# window-manager rule should have to name.
#
# windowtitle is deliberately EMPTY. Chrome's window title is the page title,
# and on about:blank there is none: the window really does map with WM_NAME "".
# helpers.py keeps an explicitly empty value rather than substituting a default,
# so this means "match on class alone", which is what is wanted.

startcommand = bash /tmp/repo/parrot/common/launch-browser.sh chrome /tmp/parrot-profile-measure about:blank
windowtitle  =
windowclass  = parrot-chrome

wait 8
mousemove 720 500
wait 0.3
mousedown 1
wait 0.1
mouseup 1
wait 2
log Browser ready: Chrome is up on a blank page with a cold cache
