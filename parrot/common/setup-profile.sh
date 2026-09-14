#!/usr/bin/env bash
# Build a browser profile that talks to the squid caching proxy and trusts the
# certificates squid mints for the sites it bumps.
#
#   setup-profile.sh firefox /tmp/parrot-profile-measure
#   setup-profile.sh chrome  /tmp/parrot-profile-measure
#
# Run once per profile, before the browser is started. common/warmup.sh calls it
# a second time for its own throwaway profile.
#
# WHY THE CERTIFICATE WORK IS NEEDED
#
# greencoding/squid_reverse_proxy is a MITM cache: it terminates TLS, signs a
# certificate for the site on the fly, and serves the response from its own
# store. The GMT playwright templates get away with ignoring that, because
# Playwright is launched with ignoreHTTPSErrors. A real browser has no such
# switch that is safe to use - Firefox has no "ignore all certificate errors"
# pref at all - so the proxy's signing certificate has to be a trust anchor in
# the browser's own NSS database. That is what certutil does below.
#
# The certificate in websites/squid-ca.crt is an INTERMEDIATE ("Enterprise
# Subordinate CA"), not a self-signed root, and squid sends only the leaf plus
# this one. NSS is happy to anchor a chain at a non-self-signed certificate that
# is marked trusted, which is why the trust flags matter and why the check at
# the end of this script exists: a certificate that is present but not trusted
# fails exactly like one that was never imported, and it fails as a blank error
# page halfway through a measured phase rather than here.
set -euo pipefail

BROWSER="${1:-}"
PROFILE="${2:-}"

if [[ -z "$BROWSER" || -z "$PROFILE" ]]; then
    echo "usage: setup-profile.sh <firefox|chrome> <profile-dir>" >&2
    exit 2
fi

# Set from __GMT_VAR_PAGE__ in the usage_scenario. Firefox reaches it through
# the home page pref written below; Chrome through --homepage on its command
# line, so for Chrome this is only used to fail early if it is missing.
: "${PARROT_URL:?PARROT_URL is not set - the usage_scenario must pass __GMT_VAR_PAGE__ into the container environment}"

PROXY_HOST="${PARROT_PROXY_HOST:-squid}"
PROXY_PORT="${PARROT_PROXY_PORT:-3128}"
CA_FILE="${PARROT_CA:-/tmp/repo/parrot/squid-ca.crt}"
CA_NICK='GMT squid bump CA'

log() { printf '[setup-profile] %s\n' "$*"; }

[[ -f "$CA_FILE" ]] || { echo "[setup-profile] CA file not found: $CA_FILE" >&2; exit 1; }

# Create an NSS database in DIR if it has none, then (re)import the CA as a
# trust anchor. The delete is unconditional and its failure ignored, so that
# re-running this script cannot end up with two entries under one nickname.
import_ca() {
    local db="$1" trust="$2"
    mkdir -p "$db"
    if [[ ! -f "${db}/cert9.db" ]]; then
        certutil -N --empty-password -d "sql:${db}"
    fi
    certutil -D -n "$CA_NICK" -d "sql:${db}" >/dev/null 2>&1 || true
    certutil -A -n "$CA_NICK" -t "$trust" -d "sql:${db}" -i "$CA_FILE"

    if ! certutil -L -d "sql:${db}" | grep -qF "$CA_NICK"; then
        echo "[setup-profile] FATAL: ${CA_NICK} is not in ${db} after import" >&2
        exit 1
    fi
    log "trusted ${CA_NICK} in ${db} (${trust})"
}

case "$BROWSER" in
firefox)
    mkdir -p "$PROFILE"
    # "CT,c,c": trusted CA for TLS server and client auth. Anything less and
    # Firefox knows the certificate but will not build a chain to it.
    import_ca "$PROFILE" "CT,c,c"

    # user.js is re-read on every start and overrides prefs.js, so this file is
    # the whole configuration and a half-written prefs.js from a killed run
    # cannot survive into the next one.
    #
    # Two groups of prefs, and the distinction is worth keeping when editing:
    #
    #   1. Prefs that MAKE THE SCENARIO WORK - the proxy and the home page.
    #      Everything the measured phase does depends on these.
    #   2. Prefs that REMOVE BACKGROUND NETWORK TRAFFIC AND FIRST-RUN UI -
    #      updates, telemetry, safe-browsing list downloads, captive portal
    #      probes, the welcome tour. Each of these would otherwise fetch over
    #      the same proxy during the measured window, land in the energy figure,
    #      and do so on a schedule nobody controls. The welcome tour additionally
    #      steals the first tab, so the measured Alt+Home would navigate a tab
    #      that is not the one on screen.
    #
    # DNS-over-HTTPS is switched off (trr.mode 5) because a proxied browser does
    # not resolve names itself - squid does - and leaving DoH on adds a second,
    # unproxied network path that only sometimes runs.
    python3 - "$PROFILE/user.js" "$PARROT_URL" "$PROXY_HOST" "$PROXY_PORT" <<'PY'
import json, sys

path, url, proxy_host, proxy_port = sys.argv[1:5]

prefs = [
    # 1. the scenario itself
    ("browser.startup.homepage",                        url),
    ("browser.startup.page",                            1),
    ("network.proxy.type",                              1),
    ("network.proxy.http",                              proxy_host),
    ("network.proxy.http_port",                         int(proxy_port)),
    ("network.proxy.ssl",                               proxy_host),
    ("network.proxy.ssl_port",                          int(proxy_port)),
    ("network.proxy.share_proxy_settings",              True),
    ("network.proxy.no_proxies_on",                     ""),
    ("network.trr.mode",                                5),

    # 2. no background network, no first-run UI
    #
    # termsofuse.*: Firefox 155 opens a MODAL "Welcome to Firefox / Terms of
    # Use" panel over the content area on first run and will not let anything
    # through until it is dismissed. It was measured here: without these three
    # prefs the browser comes up on the new tab page with the panel on top, and
    # Alt+Home in the measured phase does nothing at all. The failure is silent -
    # the phase still runs, still takes 5 s and still produces a number.
    # bypassNotification alone is the documented switch; acceptedVersion is set
    # to the shipped termsofuse.currentVersion as well, so that a Firefox which
    # ignores the bypass still considers the terms accepted.
    ("termsofuse.bypassNotification",                   True),
    ("termsofuse.acceptedVersion",                      4),
    ("datareporting.policy.dataSubmissionPolicyBypassNotification", True),
    ("app.update.auto",                                 False),
    ("app.update.enabled",                              False),
    ("browser.shell.checkDefaultBrowser",               False),
    ("browser.startup.homepage_override.mstone",        "ignore"),
    ("browser.aboutwelcome.enabled",                    False),
    ("startup.homepage_welcome_url",                    ""),
    ("startup.homepage_welcome_url.additional",         ""),
    ("browser.uitour.enabled",                          False),
    ("browser.newtabpage.activity-stream.feeds.telemetry", False),
    ("browser.newtabpage.activity-stream.telemetry",    False),
    ("browser.ping-centre.telemetry",                   False),
    ("datareporting.policy.dataSubmissionEnabled",      False),
    ("datareporting.healthreport.uploadEnabled",        False),
    ("toolkit.telemetry.enabled",                       False),
    ("toolkit.telemetry.unified",                       False),
    ("toolkit.telemetry.archive.enabled",               False),
    ("browser.safebrowsing.malware.enabled",            False),
    ("browser.safebrowsing.phishing.enabled",           False),
    ("browser.safebrowsing.downloads.enabled",          False),
    ("browser.safebrowsing.update.enabled",             False),
    ("network.captive-portal-service.enabled",          False),
    ("network.connectivity-service.enabled",            False),
    ("browser.region.update.enabled",                   False),
    ("browser.region.network.url",                      ""),
    ("browser.search.suggest.enabled",                  False),
    ("browser.urlbar.suggest.searches",                 False),
    ("browser.urlbar.suggest.quicksuggest.sponsored",   False),
    ("extensions.getAddons.cache.enabled",              False),
    ("extensions.update.enabled",                       False),
    ("extensions.pocket.enabled",                       False),
    ("dom.push.enabled",                                False),
    ("dom.webnotifications.enabled",                    False),
    ("signon.rememberSignons",                          False),
    ("browser.sessionstore.resume_from_crash",          False),
    ("browser.tabs.warnOnClose",                        False),
    ("browser.warnOnQuit",                              False),
]

with open(path, "w", encoding="utf-8") as fh:
    fh.write("// Written by websites/common/setup-profile.sh. Do not edit by hand.\n")
    for name, value in prefs:
        fh.write(f"user_pref({json.dumps(name)}, {json.dumps(value)});\n")
PY
    log "wrote $PROFILE/user.js (homepage ${PARROT_URL}, proxy ${PROXY_HOST}:${PROXY_PORT})"
    ;;

chrome)
    # Chrome does not read a per-profile certificate store. On Linux it reads
    # the user's shared NSS database at ~/.pki/nssdb, so the import is global
    # and both the warmup profile and the measured profile get it from one call.
    # "C,,": trusted CA for TLS server auth, which is all Chrome consults here.
    import_ca "${HOME}/.pki/nssdb" "C,,"

    # Everything else Chrome needs - proxy, home page, no first run - is on its
    # command line, in the macros' startcommand. Nothing is written into the
    # profile, deliberately: Chrome protects `homepage` with a MAC in Secure
    # Preferences and silently reverts values it did not write itself, so a
    # seeded Preferences file is not a reliable way to set it.
    mkdir -p "$PROFILE"
    log "prepared $PROFILE (Chrome takes proxy and home page from its command line)"
    ;;

*)
    echo "[setup-profile] unknown browser: $BROWSER (expected firefox or chrome)" >&2
    exit 2
    ;;
esac
