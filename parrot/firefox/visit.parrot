# Parrot recording v2
# Website benchmark, MEASURED phase 1: load the page and idle for 30 s.
#
# Ported from the parrot repository's websites/firefox/visit.parrot, with the
# idle raised from 5 s to 30 s as in parrot/chrome/visit.parrot.
#
# Alt+Home is Firefox's "go to your home page", and the home page is the page
# under test: common/setup-profile.sh, or for a batch common/fresh-profile.sh,
# wrote it into the profile's user.js before the browser started. That is how a
# static macro navigates to a URL it cannot know. The URL is in the profile,
# not in the keystrokes.
#
# It is a plain navigation, NOT a reload. A reload (Ctrl+R, Ctrl+Shift+R) sends
# Cache-Control upstream and the squid config in greencoding/squid_reverse_proxy
# carries no `ignore-reload`, so a reload would go to the origin and bypass the
# cache the warmup filled.
#
# The browser is already running when this starts, because start.parrot ran in
# a hidden step, so no browser start-up is inside this phase. The phase itself
# ends on common/phase-clock.sh's 33 s deadline, which absorbs the second or so
# replay.py needs before its first key press.

startcommand = bash /tmp/repo/parrot/common/launch-browser.sh firefox /tmp/parrot-profile-measure about:blank
windowtitle  = Mozilla Firefox
windowclass  = firefox

log Visit page: navigate to the page under test and idle for 30 s
wait 0.1
keydown Alt_L
wait 0.05
keydown Home
wait 0.05
keyup Home
wait 0.05
keyup Alt_L
wait 30
log Page loaded and idled: 30 s after the navigation was started
