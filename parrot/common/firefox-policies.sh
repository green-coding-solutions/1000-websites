#!/usr/bin/env bash
# Write Firefox's enterprise policies into the image's Firefox install.
#
#   firefox-policies.sh            (as root, once, before any Firefox starts)
#
# Some background activity of Firefox 155 can no longer be switched off with a
# pref. The clearest case was seen on screen on 2026-11-25: with
# app.update.auto and app.update.enabled false, as common/setup-profile.sh sets
# them, Firefox 155.0.1 still checked for updates and put an "Update available"
# panel over the page. Only the DisableAppUpdate policy stops it. The other
# policies below cover what the prefs in setup-profile.sh also try to cover,
# because a policy is checked by Firefox itself and cannot be overridden by a
# profile.
#
# The image is ribalba/parrot-browsers, which this repository consumes and does
# not build, so the file is written at container start rather than baked in.
# Firefox reads it from <install dir>/distribution/policies.json at start-up.
set -euo pipefail

FIREFOX_DIR="${PARROT_FIREFOX_DIR:-/opt/firefox}"
target="${FIREFOX_DIR}/distribution/policies.json"

mkdir -p "$(dirname "$target")"
cat > "$target" <<'JSON'
{
  "policies": {
    "DisableAppUpdate": true,
    "DisableTelemetry": true,
    "DisableFirefoxStudies": true,
    "DisableSystemAddonUpdate": true,
    "ExtensionUpdate": false,
    "UserMessaging": {
      "ExtensionRecommendations": false,
      "FeatureRecommendations": false,
      "UrlbarInterventions": false,
      "SkipOnboarding": true,
      "MoreFromMozilla": false,
      "Locked": true
    },
    "FirefoxHome": {
      "Search": false,
      "TopSites": false,
      "SponsoredTopSites": false,
      "Highlights": false,
      "Stories": false,
      "SponsoredStories": false,
      "Locked": true
    }
  }
}
JSON
python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$target"
echo "[firefox-policies] wrote $target"
