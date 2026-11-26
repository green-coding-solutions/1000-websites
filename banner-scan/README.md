# banner-scan

Which sites on a list show a cookie banner in the benchmark's browser, and which
of those does Consent-O-Matic handle? `scan.py` opens every entry twice in the
benchmark's Firefox, behind the benchmark's squid: once in a plain profile and
once with the Consent-O-Matic add-on. It screenshots both and decides whether a
consent banner or another overlay covers the page. Run it on a list before
spending cluster time on it, and fix the sites the add-on leaves alone.

```bash
# a whole list
banner-scan/scan.py sites.txt

# the first 50, with 6 browsers in parallel
banner-scan/scan.py bot-wall-check/crux-global-top1000-202608.txt --limit 50 --jobs 6

# a few sites, or a few sites out of a list
banner-scan/scan.py --sites https://www.spiegel.de/,www.ikea.com/de/de/
banner-scan/scan.py sites.txt --sites www.spiegel.de,www.bild.de

# only one of the two browsers
banner-scan/scan.py sites.txt --variants com
```

It needs Docker and Python 3.9 or newer, nothing else on the host. Firefox runs
in `ribalba/parrot-browsers` and squid in `greencoding/squid_reverse_proxy`,
both read from `parrot/usage_scenario.yml`. The repository is mounted read-only.

Each list gets a directory in `banner-scan/results/`, named after the list and
the UTC start time:

| File | |
| --- | --- |
| `index.html` | The contact sheet. Open it in a browser. Sections in this order: banner remains with the add-on, unsure with the add-on, banner handled by the add-on, no banner, errors. Every site shows both screenshots side by side, the title, the final URL and what the detector saw |
| `scan.csv` | One row per site, see below |
| `detect.jsonl` | Everything the detector returned, per site and variant. For debugging a verdict |
| `shots/` | `<position>-<host>-plain.png` and `-com.png`, the viewport after the settle time |

The terminal gets a summary with the hosts of each group. From the validation
run on 2026-11-25 (`banner-scan/test-sites.txt`, 22 sites):

```
banner scan: test-sites
  22 sites, 4 in parallel, 3m 00s
  15.0 s per visit on average, 2 visits per site
  results: banner-scan/results/test-sites-20261125-141534/index.html

  Banner remains with the add-on     10  www.spiegel.de, www.reddit.com, www.bbc.co.uk, www.lemonde.fr +6
  Unsure with the add-on              4  www.zeit.de, www.booking.com, www.nytimes.com, www.ebay.de
  Banner handled by the add-on        5  www.ikea.com, www.google.com, www.youtube.com, www.chefkoch.de +1
  No banner                           3  www.wikipedia.org, www.tagesschau.de, github.com
  Errors                              0
```

## Validation

Every screenshot of the run above was checked by eye against its verdict.

| Site | plain | add-on | On screen |
| --- | --- | --- | --- |
| ikea.com/de/de | banner | none | OneTrust box bottom left, gone with the add-on |
| spiegel.de | banner | banner | Sourcepoint pay-or-consent wall over the whole page in both |
| reddit.com | banner | banner | Own cookie dialog bottom right in both |
| wikipedia.org | none | none | Clean |
| tagesschau.de | none | none | Clean, no banner at all |
| bbc.co.uk | banner | banner | Sourcepoint dialog plain. With the add-on BBC's own "Let us know you agree to cookies" bar at the bottom |
| zeit.de | unsure | unsure | "Ihre Anfrage wurde blockiert", a bot wall. Also without Marionette |
| lemonde.fr | banner | banner | Pay-or-consent wall in both |
| corriere.it | banner | banner | Consent wall plain. With the add-on the "Accesso Consentless" subscription wall that follows a refusal |
| amazon.de | banner | banner | Amazon's cookie box in both in this run. In the two runs before, the add-on visit was clean, and once the plain one too |
| google.com | banner | none | "Bevor Sie zu Google weitergehen" dialog, gone with the add-on |
| youtube.com | banner | none | Consent dialog, gone with the add-on. What remains is the "Your YouTube history is off" box, which is page content |
| github.com | none | none | Clean |
| booking.com | banner | unsure | OneTrust bar plain. With the add-on a Genius sign-in modal and Google's One Tap prompt |
| nytimes.com | unsure | unsure | DataDome captcha. Also without Marionette |
| bild.de, welt.de, t-online.de | banner | banner | Sourcepoint pay-or-consent walls |
| ebay.de | unsure | unsure | "Something went wrong" error page. Also without Marionette |
| chefkoch.de | banner | none | consentmanager wall, gone with the add-on |
| stackoverflow.com | banner | none | OneTrust box plain. With the add-on only Google's One Tap prompt, top right |
| gmx.net | banner | banner | Redirect to its own consent page |

The first run of `detect.js` got four of these wrong, and the detector was
changed for each:

* zeit.de, nytimes.com and ebay.de came out `none`. They showed a bot wall, a
  captcha and an error page. Bot wall and error page wording, and a captcha
  iframe or a page that is one big iframe, now make a visit `unsure`.
* reddit.com came out `unsure` and not `banner`. Its dialog sits in a shadow
  root and gets its content through slots, which a walk up `parentElement`
  and a text walk down `childNodes` both miss. Both walks now follow the
  composed tree.
* youtube.com with the add-on came out `banner` because a paragraph about watch
  history has an `aria-label` that mentions cookies. aria-labels no longer
  select inline notices, and inline notices only make a visit `unsure`.
* stackoverflow.com with the add-on came out `unsure` because of a sticky ad
  column. Sticky elements without consent words no longer count, and a large
  iframe counts by its own size, not by the size of its fixed container.

## Verdicts

Each visit gets one verdict.

| Verdict | Meaning |
| --- | --- |
| `banner` | Consent evidence is on screen. A known CMP's banner element is visible and on top, or a fixed overlay on top has consent wording, or the TCF API says its UI is visible, or the page redirected to a consent page |
| `unsure` | Nothing says consent, but something is off. A fixed overlay without consent words covers 25 % or more, a large cross-origin iframe sits on top, the scroll is locked while a CMP is loaded, an in-flow element with cookie or consent in its id or class has consent wording, the page is a bot wall or an error page, or the page is blank. Look at the screenshot |
| `none` | None of the above. Meant to be trustworthy: anything doubtful goes to `unsure` |
| `error` | The visit failed: no page load, a proxy or certificate error page, Firefox did not start or the hard timeout hit |

A site's group in the contact sheet comes from both visits. Any error makes it
`error`. Otherwise the add-on visit decides between "banner remains" (`banner`)
and "unsure" (`unsure`). An add-on visit with `none` is "handled" if the plain
visit was `banner` or `unsure`, and "no banner" if it was `none` too. With one
variant only, the groups are that variant's verdicts.

## CSV columns

| Column | |
| --- | --- |
| `position`, `input`, `url` | As in the list. A bare domain becomes `https://<domain>/` |
| `group` | `remains`, `unsure-com`, `handled`, `none` or `error`, see above |
| `final_url`, `title` | Of the add-on visit (of the only visit with one variant) |
| `<v>_verdict` | `plain_` and `com_` columns for the two variants |
| `<v>_evidence` | What decided the verdict, joined with ` \| `. Element, coverage, the words matched and the start of its text |
| `<v>_cmps` | CMPs whose globals or markup are on the page, whether they show anything or not |
| `<v>_max_coverage` | Share of the viewport won by the largest fixed or sticky overlay, in percent |
| `<v>_tcf` | The answer to the IAB TCF `ping` command, if the page has `__tcfapi` |
| `<v>_scroll_locked` | `overflow: hidden` on html or body, or a fixed body |
| `<v>_viewport` | The page's `innerWidth` x `innerHeight`. Smaller than 1438x788 means a Firefox notification bar took space, see below |
| `<v>_load_seconds`, `<v>_load_timeout` | Time to the load event, and whether the 30 s page load timeout hit (the page is still looked at) |
| `<v>_seconds` | The whole visit, from profile to quit |
| `<v>_error`, `<v>_shot` | Error text, and the screenshot path relative to the result directory |

## How it runs

`scan.py` starts one squid and `--jobs` worker containers on a private docker
network. Each worker starts its Xvfb with the image's entrypoint, writes
Firefox's policies with `parrot/common/firefox-policies.sh` and builds the
Consent-O-Matic template profile with `parrot/common/make-template-profile.sh`,
once. Then `scan.py` hands each worker one site and variant at a time, through
`docker exec` of `worker.py`. The containers and the network are removed at the
end, after Ctrl-C and on SIGTERM too, and the results so far are written.

`worker.py` builds a fresh profile for every visit. The plain profile comes from
`parrot/common/setup-profile.sh firefox` with `PARROT_CONSENT` unset, the add-on
profile from `parrot/common/fresh-profile.sh`, as in the benchmark. Firefox starts
through `parrot/common/launch-browser.sh`, so its command line is the
benchmark's, with `MOZ_MARIONETTE=1` in the environment because the script takes
no extra flags. `worker.py` drives it with a small Marionette client of its own
(standard library only): new session, window 1440x900, navigate with a 30 s
page load timeout, wait `--settle` seconds (12 by default, the add-on needs a few
to act), run `detect.js`, take a screenshot of the viewport, quit. Two prefs
switch off Marionette's "recommended prefs" so the browser keeps the
benchmark's configuration. A visit that does not finish within load timeout +
settle + 90 s is killed and recorded as an error, and the scan goes on.

The plain visit runs first, so the add-on visit is usually served from squid's
cache. That is how the benchmark loads pages too.

## How it decides

`detect.js` runs in the page and combines four kinds of evidence:

1. **Known CMPs.** Globals and DOM markers of about 40 consent platforms:
   OneTrust, Cookiebot, Didomi, Usercentrics, Sourcepoint, Google Funding
   Choices, consentmanager, Quantcast, TrustArc, CookieYes, Borlabs, Complianz,
   iubenda, Osano, Axeptio, Klaro, CookieConsent (orestbida), Termly, Civic,
   Sirdata, Ketch, Evidon, TrustCommander, Cookie Information, CookieFirst,
   HubSpot, Shopify, Ezoic, Truendo, CCM19, Wix, Microsoft, and the own
   dialogs of Amazon, Google, YouTube, Meta, BBC and Reddit. A marker alone
   says nothing, because most CMPs keep their markup after a choice and only
   hide it. A CMP counts only when one of its banner elements is visible and on
   top, checked with `elementFromPoint`.
2. **What is on top.** A grid of 36 x 24 points over the viewport, plus four rows
   along the edges for thin bars. Each point is resolved with `elementFromPoint`
   down through open shadow roots, and walked up the composed tree (slots
   included) to its innermost fixed or sticky ancestor, or an absolute one with
   a z-index of 1000 and more. Those are the overlays, and the share of points
   each one wins is its coverage. An overlay is a banner when its text (open
   shadow roots and button labels included) has a consent word and a button
   word, or when it holds an iframe whose src, id or title points at a CMP.
   The vocabulary covers en, de, fr, es, it, pt, nl, pl, ru, ja, zh, ko, tr,
   cs, sk, hu, sv, no, da, fi and el.
3. **Inline notices.** Elements with cookie, consent, gdpr or cmp in their id or
   class that sit in the viewport in the normal page flow and have consent
   wording. They only make a visit `unsure`.
4. **Side signals.** The TCF `ping` (`displayStatus: visible`), a consent page in
   the final URL (`consent.`, `myprivacy.`, `guce.` hosts, `/zustimmung`,
   `/consent-management/` paths), the scroll lock, bot wall and error page
   wording, and a blank page.

## What it cannot tell you

* **Closed shadow roots.** Their text cannot be read and `elementFromPoint`
  stops at their host. A banner in one is found only if a known CMP selector,
  its iframe or the host's own position gives it away.
* **Banners in a language or wording the vocabulary lacks**, or with no words at
  all (an image of text). A large one still lands in `unsure` through its
  coverage. A small bar at the edge comes out as `none`.
* **Small overlays that are not about consent.** Chat widgets, Google's One Tap
  sign-in prompt (stackoverflow.com), newsletter toasts and sticky ad bars under
  25 % of the viewport do not change the verdict. They are on the screenshot.
* **Banners that come late or only on interaction.** Anything that appears after
  the settle time, after scrolling or on mouse movement is not seen.
* **Whether a run on the cluster sees the same.** CMPs decide by country and
  address, and bot walls even more. Run the scan where the benchmark runs, as
  with `bot-wall-check`.
* **A settled answer.** One visit per variant is one sample. Sourcepoint and
  Funding Choices sometimes show a different message on the next visit.
* **Marionette's traces.** With Marionette on, `navigator.webdriver` is true,
  which the measured browser does not have. The walls seen in the validation
  run (zeit.de, nytimes.com, ebay.de) also appear in Firefox without
  Marionette, so they come from the proxy path. A site that reacts to the flag
  alone would still look different here.

## Firefox's notification bar

Firefox 155 puts a bar reading "You can't use speech synthesis because the
Speech Dispatcher library is missing" above the page when a page calls the
speech API, which some fingerprinting scripts do. It takes 57 px of the
viewport. In the validation run it appeared on nytimes.com, ebay.de and some
add-on visits (bbc.co.uk, chefkoch.de, stackoverflow.com). The `viewport` column
shows it as 1438x731 instead of 1438x788. The measured browser gets it too, so
`parrot/common/setup-profile.sh` switches it off by pref.

The full click finder run on the 1000 sites found a second one. Sites that ask
for DRM (Spotify, Prime Video, SoundCloud, CNN, Cisco and others) got a "You
must enable DRM" bar, because the image never downloads Widevine. find-clicks.py
reported these as GEOMETRY MISMATCH with the page at 0,142. `setup-profile.sh`
sets `browser.eme.ui.enabled` to false and the rerun of those sites had the page
at 0,85 again.

## Runtime

Measured on 2026-11-25 on a 20-core desktop with 4 workers. A visit takes 14
to 20 s, 15.0 s on average: about 2 s to build the profile and start Firefox,
the load (0.1 to 3 s here, as squid serves most of it), the 12 s settle, and
under a second for detection and screenshot. Building the template takes about
25 s per worker, once. With two visits per site and 4 workers, 22 sites took
3m 00s, so the CrUX top 1000 takes about 2.3 h. Every worker runs one
Firefox, so `--jobs` is bounded by memory more than by cores. The screenshots
take about 320 kB each, so about 650 MB for 1000 sites.

## Click finder

`find-clicks.py` finds, for every site with a consent banner, the screen
coordinates of the "accept" and the "reject" buttons, and proves them with real
X11 clicks. The coordinates are meant to be replayed by Parrot (xdotool, XTest)
inside the measured runs, so they are screen pixels of the benchmark's 1440x900
Xvfb with Firefox pinned to 0,0 at 1440x900 without decorations. They are only
valid for exactly that geometry.

```bash
# a whole list, same format as scan.py
banner-scan/find-clicks.py sites.txt

# the first 50, with 6 browsers in parallel
banner-scan/find-clicks.py sites_tranco_top1000.txt --limit 50 --jobs 6

# a few sites
banner-scan/find-clicks.py --sites https://www.spiegel.de/,www.ikea.com/de/de/
```

Results go to `banner-scan/results/clicks-<list>-<UTC>/`:

| File | |
| --- | --- |
| `clicks.csv` | One row per site, the columns below. The batch generator reads this file |
| `clicks.jsonl` | Everything `clicks.py` saw: all candidates with labels and coordinates, the detector's verdicts, the geometry of every look, timings |
| `index.html` | One card per site with a consent UI: before (visit A), after accept, before (visit B), after reject, the labels clicked and the classification. The sites without a consent UI are listed at the end |
| `shots/` | `<position>-<host>-banner.png`, `-accept.png`, `-banner-b.png` and `-reject.png`, each the whole X screen (`import -window root`), plus the two screenshots of the geometry check |
| `geometry.json` | The result of the geometry check |

### clicks.csv

| Column | |
| --- | --- |
| `position`, `url` | As in the list |
| `final_url`, `title` | Of visit A when the banner was seen |
| `banner` | `yes` (detect.js said `banner`), `unsure` or `no`. A visit that failed before the page was looked at is `unsure` with the error in `evidence` |
| `banner_appears_s` | Seconds from the start of the navigation to the first `banner` verdict, polled about once a second |
| `blocking` | `yes` when the scroll is locked or the consent UI (with its backdrop) covers 30 % or more of the viewport |
| `cmps` | CMPs whose globals or markup detect.js found |
| `accept`, `reject` | `verified`, `failed`, `not-found`, or `n/a` when there is no consent UI |
| `accept_clicks`, `reject_clicks` | Screen coordinates `x,y`, several clicks joined with `;` in order. Filled for failed paths too, so only replay them when the status is `verified` |
| `accept_labels`, `reject_labels` | The button texts, joined with ` \| ` |
| `reject_steps` | Number of clicks of the verified reject path |
| `no_reject` | Empty when the reject path is verified. Otherwise `pay-or-consent` (a subscription or ad-free offer sits in the consent UI next to accept), `consent-wall` (no way to reject, the banner blocks the page and cannot be closed), `ignorable` (no way to reject, but the banner does not block the page, or its close button removes it) or `not-found` (no consent buttons found) |
| `subscribe_offer` | `yes` when a subscribe button or a price per month sits in the consent UI next to an accept button |
| `inner_screen`, `viewport` | `window.mozInnerScreenX,Y` and `innerWidth,innerHeight` as measured. `0,85` and `1440,815` in Firefox 155 |
| `evidence` | Why, joined with ` \| `. A geometry that differs from the geometry check starts with `GEOMETRY MISMATCH` |
| `shot_banner`, `shot_accept`, `shot_reject` | Screenshot paths relative to the result directory |

### How it runs

The fleet is like `scan.py`'s, with its own container names
(`click-finder-<pid>-*`), but every worker boots like the measured container:
`firefox-policies.sh`, then `pin-windows.sh Navigator 1440 900`, then the
image's entrypoint, with `SCREEN_SIZE=1440x900x24`, `RESOLUTION=1440x900`,
`WINDOW_X/Y/WIDTH/HEIGHT`, `LANG=C.UTF-8` and `--shm-size=2g`. The order
matters, because fluxbox reads its apps file once when it starts. Firefox
starts through `launch-browser.sh` with a plain profile from
`setup-profile.sh` (PARROT_CONSENT unset) plus the Marionette prefs of
`worker.py`. After the window maps, `position-window.sh` runs with the measured
run's environment, as `replay.py` does, and a click at 720,500 puts the focus
in the content area, as `start.parrot` does. Nothing calls
`WebDriver:SetWindowRect`.

Each site gets up to two visits in fresh profiles, run by `clicks.py` through
`docker exec`:

1. **Visit A.** Navigate with the normal load strategy and a 30 s timeout. The
   navigation is sent without waiting, and `detect.js` is polled about once a
   second for 15 s from its start. A page that is still loading and blank at
   15 s gets until 30 s. When the verdict is `banner`, wait 1.5 s more and
   take the "before" screenshot. Then `buttons.js` finds the candidates and the
   best accept button is clicked with `xdotool mousemove`, a 0.15 s pause and
   `click 1`. Before each click the button is located again until it stops
   moving, because banners slide in.
2. **Visit B.** The same page in a new profile, and a reject path of at most
   three clicks: a first-layer reject, or settings and then reject or save
   (toggles left as they are, and the evidence says how many were on before
   save), or settings and one more step. When a reject leaves the UI open, a
   save comes next. When visit B finds no consent button at all, it is tried
   once more, because some sites show their banner only on some visits.
3. **No reject path.** The site is classified as described for `no_reject`.
   For a blocking banner without a reject path, its close button is tried in
   visit B, and only the evidence gets its coordinates.

A click counts as **verified** when, 3 s later (2 s after a settings click),
no accept, reject or save button is left on top anywhere and either the
detector no longer says `banner` or the container that held the button is
gone. A floating "privacy settings" button that many CMPs leave behind does not
count. When the UI that held the button is gone and another consent UI
appears, the path goes on if the new UI has a button of the same kind
(bbc.co.uk's own "Yes, I agree" bar after accepting in Sourcepoint), and is
otherwise done with a note. A click that opens a new window or leaves the site
(another registrable domain) fails the path.

Two more rules came from the first checks of the validation run:

* **The UI that stays must not block the page.** If the other consent UI locks
  the scroll or covers 30 % of the viewport or more, the path is not done.
  t-online.de's "individuell anpassen, Alle Zustimmungen widerrufen" left its
  pay-or-consent wall up, which earlier counted as a verified reject.
* **A verified path is looked at again 8 s later** (`HOLD_CHECK`). A new
  consent UI or wall that was not there right after the click fails the path.
  corriere.it took "preferenze, Deny All, Save" and put its "Accetta e
  continua" wall back a few seconds later. The measured visit idles about 20 s
  after its clicks, so a wall that comes back would be in the measurement.

### How buttons.js finds the buttons

`buttons.js` runs in the top document and, through `WebDriver:SwitchToFrame`,
in every visible iframe on top that is at least 0.5 % of the viewport and
either looks like a consent frame (src, id or title) or sits in a consent
container or is 3 % or larger. Frames inside frames are searched one more
level. Consent UIs of Sourcepoint, TrustArc and others live in such frames.

1. **Consent containers.** Fixed overlays and high z-index absolute ones found
   with a grid of `elementFromPoint` probes, the banner elements of about 45
   known CMPs, open dialogs, and elements with cookie, consent, gdpr, cmp or
   privacy in their id or class. Each needs consent wording. Sign-in prompts
   such as Google's One Tap are skipped. In a consent frame the whole body is
   the container.
2. **Candidates.** Clickable elements inside a container (button, a,
   `[role=button]`, input buttons, onclick, the outermost element with a pointer
   cursor), visible, with a click point inside the viewport where the element
   itself is on top, checked with `elementFromPoint` through open shadow roots.
   For a frame, the parent is asked whether the frame is on top at that point
   too. Wrappers that hold other candidates are dropped.
3. **Labels.** The visible text, aria-label, title or value, at most 8 words,
   are matched against vocabulary in en, de, fr, es, it, pt, nl, pl, ru, tr,
   cs, hu, sv, no, da, fi, el, ja, zh, ko, vi, id, th, ar and hi. The order is
   subscribe, reject (also negated accepts and "accept only necessary"), save
   ("save", "confirm", "accept selection"), settings, accept and close. "Accept
   all" ranks above a bare "OK". A label that repeats inside one UI acts on one
   purpose or vendor, like the Accept and Reject pairs in Sourcepoint's privacy
   manager, and is never clicked.

Screen coordinates are `mozInnerScreenX + clientX` and `mozInnerScreenY +
clientY` at a devicePixelRatio of 1, plus the content-box offsets of the
iframes around the button. Each frame's own `mozInnerScreenX/Y` cross-checks
the offsets. A mismatch is noted in the evidence.

### The geometry check

Marionette paints a striped URL bar and a robot icon, and a different toolbar
height would shift every click. So `find-clicks.py` starts with
`clicks.py --geometry-check` in the first worker. It paints a page magenta,
opens it once in a Firefox started like the benchmark starts it
(`launch-browser.sh firefox <plain profile> <url>`, no Marionette) and once in
the Marionette-driven one, and finds the magenta box in a screenshot of the
root window. If the two differ, the difference is added to every recorded
click (and printed in red). The clicks themselves always use the measured
geometry of the Marionette run.

On 2026-11-25 both put the page at x 0, y 85, 1440x815, the same as
`mozInnerScreenX/Y` and `innerWidth/Height`. The Firefox window was 1440x900
at 0,0 in both. So the correction is 0,0. Every look at every validation site
measured `0,85` and `1440,815` as well.

### Validation

`find-clicks.py` on 30 sites on 2026-11-25. Every before and after screenshot
was checked by eye.

| Site | banner | accept | reject | steps | no_reject | On screen |
| --- | --- | --- | --- | --- | --- | --- |
| ikea.com/de/de | yes | verified | verified | 1 | | OneTrust box bottom left, gone after "Alle Cookies akzeptieren" and after "Optionale Cookies ablehnen" |
| spiegel.de | yes | verified | failed | | pay-or-consent | Sourcepoint wall, gone after "Consent and continue". "Preferences" opens a privacy manager with per-purpose buttons and required purposes, no way out |
| bild.de | yes | verified | failed | | pay-or-consent | Wall with "Alle akzeptieren" and "BILD PUR abonnieren". "Einstellungen" shows required purposes |
| lemonde.fr | yes | verified | not-found | | pay-or-consent | "Accepter et continuer" or "S'abonner", nothing else |
| reddit.com | yes | verified | verified | 1 | | Own dialog bottom right (shadow DOM), gone after "Accept All" and after "Reject Optional Cookies" |
| bbc.co.uk | yes | verified | verified | 1 | | Sourcepoint dialog. Accept takes "I agree" and then BBC's own bar "Yes, I agree". After "I do not agree" BBC's bar stays at the bottom, noted in the evidence |
| google.com | yes | verified | verified | 1 | | "Bevor Sie zu Google weitergehen", gone after "Alle akzeptieren" and "Alle ablehnen" |
| youtube.com | yes | verified | verified | 1 | | Consent dialog, gone. The "Your YouTube history is off" box that stays is page content |
| zeit.de | unsure | n/a | n/a | | | "Ihre Anfrage wurde blockiert", a bot wall |
| t-online.de | yes | verified | verified | 2 | | Pay-or-consent wall. "individuell anpassen" and then "Alle Zustimmungen widerrufen" leave a clean page, 3 s later. subscribe_offer is yes |
| chefkoch.de | yes | verified | failed | | pay-or-consent | Wall with "Allen Zwecken zustimmen" or PUR. In "Einstellungen", "Zustimmung widerrufen" goes back to the wall |
| stackoverflow.com | yes | verified | verified | 1 | | OneTrust box bottom left, gone. Google's One Tap prompt stays and is ignored |
| booking.com | yes | verified | verified | 1 | | OneTrust bar at the bottom, gone. The Genius sign-in modal and One Tap stay |
| amazon.de | no | n/a | n/a | | | No cookie box in this run, the shop page loaded |
| corriere.it | yes | verified | verified | 3 | | Consent wall with "Rifiuta e abbonati". "preferenze", "Deny All", "Save" leave a clean page |
| aftonbladet.se | yes | verified | failed | | pay-or-consent | Schibsted dialog. "Avvisa personanpassad reklam" is a paid service (49 kr/månad) and leads to annonsval.schibsted.se, so it fails |
| accuweather.com | yes | verified | verified | 1 | | Ketch panel on the left, gone after "Consent" and "Do Not Consent" |
| ihg.com | yes | verified | verified | 1 | | Cookie bar at the bottom, gone. A "Cyber Sale" promo modal stays |
| mcafee.com/de-de | yes | verified | verified | 1 | | Centred dialog, gone |
| wisc.edu | yes | verified | not-found | | ignorable | Notice bar with only "Accept cookie notice" |
| ok.ru | yes | verified | verified | 2 | | Bar at the top. "Configure" and "Allow selected" with 0 of 4 toggles on |
| x.com | yes | verified | verified | 1 | | Bar at the bottom, gone |
| microsoft.com/de-de | yes | verified | verified | 1 | | Bar at the top, gone. The store chat prompt stays |
| tiktok.com | yes | verified | verified | 1 | | Blue bar at the bottom, gone. The log-in modal stays |
| wikipedia.org, github.com, tagesschau.de | no | n/a | n/a | | | Clean |
| bing.com | yes | verified | verified | 1 | | Bar at the bottom, gone |
| linkedin.com | yes | verified | verified | 1 | | Bar at the top, gone. Visit B showed no bar the first time and was retried |
| cloudflare.com | yes | verified | verified | 1 | | OneTrust bar at the bottom, gone |

The first runs got these wrong, and `buttons.js` or `clicks.py` was changed for
each:

* "User Agreement" counted as accept and Google's One Tap frame as a consent
  UI on linkedin.com. "agree" and "accept" no longer match "agreement" and
  "acceptable", and sign-in prompts are skipped.
* "Allen Zwecken zustimmen" (chefkoch.de) was a settings button because of
  "Zwecke". An accept with an "all" word now wins over settings.
* The TCF purpose "Speichern von oder Zugriff auf Informationen" counted as a
  save button. Labels of more than 8 words are not buttons, and save labels
  have at most 5.
* The per-purpose "Reject" buttons in Sourcepoint's privacy manager were
  clicked as if they rejected everything. Repeated labels are now skipped.
* corriere.it's settings layer was dropped as an app shell, because it is long
  and full of vendor links. A container that starts with consent wording is
  kept. A pointer-cursor wrapper around "Deny All" and "Accept All" became one
  candidate, and after "Deny All" the path did not save.
* bbc.co.uk's reject failed because BBC shows its own bar after Sourcepoint
  closes. The rule for a second consent UI is above.
* aftonbladet.se's paid reject was not seen as an offer. Prices without
  decimals (49 kr/månad) now count.
* amazon.de was still blank after 15 s. A page that is still loading gets
  until the 30 s load timeout.

### What it cannot tell you

* **Whether the cluster sees the same.** As with the scan, CMPs decide by
  country, address and A/B test. Spiegel showed its wall in English here,
  linkedin.com showed its bar in one of two visits, and t-online.de accepted a
  withdrawal that a later page view may not accept. Coordinates found here
  hold for the banner seen here.
* **Timing in the measured run.** The banner appeared 1 to 9 s after the
  navigation started, and up to 19 s on a busy machine. A replay that clicks
  too early hits the page behind the banner.
* **Closed shadow roots and canvas UIs.** Their buttons cannot be read, so the
  site comes out as `not-found`.
* **Buttons below the fold of a scrollable dialog.** Only points inside the
  viewport are clicked, and nothing is scrolled.
* **Wording outside the vocabulary.** A reject button with an unusual label is
  missed, and the site gets `not-found` or a settings path. The labels seen are
  in `clicks.jsonl`.
* **Whether a reject is complete.** "Verified" means the UI went away. Toggles
  that a settings layer has on by default (legitimate interest) are counted
  in the evidence but not switched off, and a CMP may still set cookies.
* **Blocking.** The backdrop of a promo modal (ihg.com) counts like a consent
  backdrop, and a large box that leaves the page usable (ikea.com, 30.5 %)
  counts as blocking by the 30 % rule.
* **Marionette's traces.** `navigator.webdriver` is true here and not in the
  measured run. The geometry is the same, see above.

### Runtime

Measured on 2026-11-25 on the 20-core desktop with 4 workers, while a
`scan.py` run used 4 more browsers. A site with a consent UI took 30 s on
average (19 to 57 s), up to 48 s on average when the machine was busier. A
site without one takes about 21 s, mostly the 15 s wait. 30 sites took 3m 50s
to 5m 35s, so 1000 sites take about 2 to 3 hours with 4 workers. The geometry
check adds about 30 s per run. The screenshots take about 1.5 MB per site, so
about 1.5 GB for 1000 sites.
