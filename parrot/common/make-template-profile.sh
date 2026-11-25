#!/usr/bin/env bash
# Build the Firefox profile that every site of a batch starts from, with
# Consent-O-Matic installed, its rules downloaded and its first-run tab done.
#
#   make-template-profile.sh            (once per run, in a hidden step)
#
# WHY A TEMPLATE
#
# A profile fresh from common/setup-profile.sh is not fit to measure with when
# it has Consent-O-Matic in it. On its first start the add-on opens its options
# page in a second tab, and on the first page it sees it downloads its rules
# from raw.githubusercontent.com, about 200 JSON files. Both were seen on
# 2026-11-25. In a measured phase the tab would cover the page, and the
# download would be energy that belongs to the add-on, not to the site.
#
# So this script starts Firefox once on a neutral page, lets the add-on do both,
# quits Firefox, and removes everything the neutral page left behind: cookies,
# site storage, history, the HTTP cache. What remains is the add-on, its cached
# rules (kept for 22 to 48 h by the add-on itself, far longer than a run) and
# the flag that its options page was shown. common/fresh-profile.sh copies this
# directory for each site, so every measured visit still starts with an empty
# browser cache and no cookies.
#
# The rules come from the master branch of github.com/cavi-au/Consent-O-Matic,
# whatever it holds at the time of the run. The script logs the newest rule
# file's commit if the GitHub API answers, so that a run records which rules it
# used. On 2026-11-25 master was at 8ca8500d26 from 2025-11-07.
set -euo pipefail

export DISPLAY="${DISPLAY:-:99}"
HERE="$(dirname "$(readlink -f "$0")")"
TEMPLATE="${PARROT_TEMPLATE_PROFILE:-/tmp/parrot-profile-template}"
NEUTRAL_URL="${PARROT_TEMPLATE_URL:-https://example.com/}"
SECONDS_UP="${PARROT_TEMPLATE_SECONDS:-20}"

log() { printf '[template] %s\n' "$*"; }
die() { printf '[template] FATAL: %s\n' "$*" >&2; exit 1; }

pkill -f -- "$TEMPLATE" 2>/dev/null || true
rm -rf "$TEMPLATE"
PARROT_CONSENT=consent-o-matic PARROT_URL=about:blank \
    bash "${HERE}/setup-profile.sh" firefox "$TEMPLATE"

log "starting Firefox on ${NEUTRAL_URL} for ${SECONDS_UP}s so Consent-O-Matic can fetch its rules"
bash "${HERE}/launch-browser.sh" firefox "$TEMPLATE" "$NEUTRAL_URL" \
    >/tmp/parrot-template.log 2>&1 &
sleep "$SECONDS_UP"

# SIGTERM makes Firefox quit the normal way, which flushes the add-on's storage
# to disk. A SIGKILL could leave the rules only in memory.
log "quitting Firefox"
pkill -TERM -f -- "$TEMPLATE" 2>/dev/null || true
for _ in $(seq 1 60); do
    pgrep -f -- "$TEMPLATE" >/dev/null 2>&1 || break
    sleep 0.5
done
pgrep -f -- "$TEMPLATE" >/dev/null 2>&1 && die "Firefox did not quit within 30 s"

# The add-on has to be installed and active, and its storage has to hold the
# rules. Its storage.local is an IndexedDB under storage/default/moz-extension+++*.
# The rules are one value, under the key cachedEntries, which Firefox stores
# compressed as a file of its own next to the database because it is large:
# idb/<name>.files/1, 154 kB on 2026-11-25. Without the rules there is no such
# file, or only a small one.
grep -q '"id":"gdpr@cavi.au.dk"' "$TEMPLATE/extensions.json" \
    || die "Consent-O-Matic is not in extensions.json"
python3 - "$TEMPLATE/extensions.json" <<'PY' || die "Consent-O-Matic is installed but not active"
import json, sys
addons = json.load(open(sys.argv[1]))["addons"]
com = [a for a in addons if a["id"] == "gdpr@cavi.au.dk"]
sys.exit(0 if com and com[0]["active"] else 1)
PY
rules_bytes=$(find "$TEMPLATE"/storage/default/moz-extension+++*/idb/ -path '*.files/*' -type f -printf '%s\n' 2>/dev/null | sort -n | tail -1)
rules_bytes="${rules_bytes:-0}"
if (( rules_bytes < 50000 )); then
    die "Consent-O-Matic's storage has no rules blob (largest ${rules_bytes} bytes), so its rules were not downloaded. Log: $(tail -c 500 /tmp/parrot-template.log)"
fi
log "Consent-O-Matic active, its cached rules take ${rules_bytes} bytes"

# Everything the neutral page and the first start left that a site could see or
# that would warm the next start.
(
    cd "$TEMPLATE"
    rm -rf cache2 startupCache thumbnails safebrowsing crashes minidumps \
        storage/default/http* \
        cookies.sqlite* webappsstore.sqlite* places.sqlite* favicons.sqlite* \
        sessionstore* sessionCheckpoints.json SiteSecurityServiceState.bin \
        bounce-tracking-protection.sqlite* \
        content-prefs.sqlite* formhistory.sqlite* \
        lock .parentlock
)
log "template ready in ${TEMPLATE} ($(du -sh "$TEMPLATE" | cut -f1))"

commit=$(curl -s --max-time 10 --proxy "http://${PARROT_PROXY_HOST:-squid}:${PARROT_PROXY_PORT:-3128}" \
    --cacert "${PARROT_CA:-/tmp/repo/parrot/squid-ca.crt}" \
    -H 'Cache-Control: no-store' \
    'https://api.github.com/repos/cavi-au/Consent-O-Matic/commits?path=rules&per_page=1' \
    | python3 -c 'import json,sys; c=json.load(sys.stdin)[0]; print(c["sha"][:10], c["commit"]["committer"]["date"])' 2>/dev/null) || true
log "Consent-O-Matic rules: master at ${commit:-unknown (GitHub API did not answer)}"
