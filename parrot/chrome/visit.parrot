# Parrot recording v2
# Website benchmark, MEASURED phase 1: load the page and idle for 30 s.
#
# The page under test is NOT on a command line here, and cannot be: a .parrot
# file is a static recording that has to work for any __GMT_VAR_PAGE__. The URL
# reaches the browser as its HOME PAGE instead, and this macro presses Alt+Home.
#
# Chrome takes that home page from --homepage in common/launch-browser.sh rather
# than from the profile that common/setup-profile.sh writes, because Chrome
# protects the preference with a MAC and reverts values it did not write itself.
#
# Alt+Home is Chrome's "open your home page in the current tab", and passing
# --homepage also makes Chrome treat the home page as something other than the
# new tab page, so the key lands on the URL rather than on the NTP.

startcommand = bash /tmp/repo/parrot/common/launch-browser.sh chrome /tmp/parrot-profile-measure about:blank
windowtitle  =
windowclass  = parrot-chrome

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
