#!/usr/bin/env bash
# Give one site a Firefox profile of its own, copied from the batch template.
#
#   PARROT_URL=https://www.example.org/ fresh-profile.sh <profile-dir>
#   PARROT_URL=... PARROT_COM_CONSENT=accept fresh-profile.sh <profile-dir>
#
# The copy is made from common/make-template-profile.sh's result, so it has
# Consent-O-Matic with its rules, and no cookies, site storage or HTTP cache.
# Only the home page differs between sites: visit.parrot navigates to it with
# Alt+Home, so it is rewritten here in the profile's user.js, which Firefox
# applies on every start.
#
# PARROT_COM_CONSENT is the answer Consent-O-Matic gives:
#   reject (default)  every purpose denied, the add-on's own default
#   accept            every purpose allowed
# The add-on keeps this as consentValues in its storage.sync, which Firefox
# stores as plain JSON in storage-sync-v2.sqlite. Six categories: A
# preferences, B performance, D information storage, E content, F ads, X other.
# Both modes use the same rules, so the same banners are answered either way.
set -euo pipefail

PROFILE="${1:-}"
[[ -n "$PROFILE" ]] || { echo "usage: PARROT_URL=<url> fresh-profile.sh <profile-dir>" >&2; exit 2; }
: "${PARROT_URL:?PARROT_URL is not set}"
TEMPLATE="${PARROT_TEMPLATE_PROFILE:-/tmp/parrot-profile-template}"
CONSENT="${PARROT_COM_CONSENT:-reject}"

[[ -f "$TEMPLATE/user.js" ]] || { echo "[fresh-profile] FATAL: no template profile in $TEMPLATE, run make-template-profile.sh first" >&2; exit 1; }
case "$CONSENT" in
    reject|accept) ;;
    *) echo "[fresh-profile] FATAL: PARROT_COM_CONSENT=$CONSENT, expected reject or accept" >&2; exit 2 ;;
esac

rm -rf "$PROFILE"
cp -a "$TEMPLATE" "$PROFILE"
python3 - "$PROFILE" "$PARROT_URL" "$CONSENT" <<'PY'
import json, os, sqlite3, sys
profile, url, consent = sys.argv[1:4]

path = os.path.join(profile, "user.js")
lines = open(path, encoding="utf-8").read().splitlines()
key = 'user_pref("browser.startup.homepage",'
hits = [i for i, l in enumerate(lines) if l.startswith(key)]
if len(hits) != 1:
    sys.exit(f"[fresh-profile] FATAL: expected one homepage pref in {path}, found {len(hits)}")
lines[hits[0]] = f'{key} {json.dumps(url)});'
open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")

# Written in both modes, so a profile always states its answer explicitly
# rather than relying on the add-on's default.
db = os.path.join(profile, "storage-sync-v2.sqlite")
con = sqlite3.connect(db)
row = con.execute("SELECT data FROM storage_sync_data WHERE ext_id = 'gdpr@cavi.au.dk'").fetchone()
if row is None or row[0] is None:
    sys.exit(f"[fresh-profile] FATAL: no Consent-O-Matic settings in {db}")
data = json.loads(row[0])
data["consentValues"] = {k: consent == "accept" for k in "ABDEFX"}
con.execute("UPDATE storage_sync_data SET data = ? WHERE ext_id = 'gdpr@cavi.au.dk'", (json.dumps(data),))
con.commit()
con.close()
PY
printf '[fresh-profile] %s from %s, home page %s, Consent-O-Matic answers %s\n' \
    "$PROFILE" "$TEMPLATE" "$PARROT_URL" "$CONSENT"
