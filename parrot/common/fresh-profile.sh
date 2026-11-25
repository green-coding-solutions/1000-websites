#!/usr/bin/env bash
# Give one site a Firefox profile of its own, copied from the batch template.
#
#   PARROT_URL=https://www.example.org/ fresh-profile.sh <profile-dir>
#
# The copy is made from common/make-template-profile.sh's result, so it has
# Consent-O-Matic with its rules, and no cookies, site storage or HTTP cache.
# Only the home page differs between sites: visit.parrot navigates to it with
# Alt+Home, so it is rewritten here in the profile's user.js, which Firefox
# applies on every start.
set -euo pipefail

PROFILE="${1:-}"
[[ -n "$PROFILE" ]] || { echo "usage: PARROT_URL=<url> fresh-profile.sh <profile-dir>" >&2; exit 2; }
: "${PARROT_URL:?PARROT_URL is not set}"
TEMPLATE="${PARROT_TEMPLATE_PROFILE:-/tmp/parrot-profile-template}"

[[ -f "$TEMPLATE/user.js" ]] || { echo "[fresh-profile] FATAL: no template profile in $TEMPLATE, run make-template-profile.sh first" >&2; exit 1; }

rm -rf "$PROFILE"
cp -a "$TEMPLATE" "$PROFILE"
python3 - "$PROFILE/user.js" "$PARROT_URL" <<'PY'
import json, sys
path, url = sys.argv[1:3]
lines = open(path, encoding="utf-8").read().splitlines()
key = 'user_pref("browser.startup.homepage",'
hits = [i for i, l in enumerate(lines) if l.startswith(key)]
if len(hits) != 1:
    sys.exit(f"[fresh-profile] FATAL: expected one homepage pref in {path}, found {len(hits)}")
lines[hits[0]] = f'{key} {json.dumps(url)});'
open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
PY
printf '[fresh-profile] %s from %s, home page %s\n' "$PROFILE" "$TEMPLATE" "$PARROT_URL"
