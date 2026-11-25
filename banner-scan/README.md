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
it is worth switching off in `parrot/common/setup-profile.sh`.

## Runtime

Measured on 2026-11-25 on a 20-core desktop with 4 workers. A visit takes 14
to 20 s, 15.0 s on average: about 2 s to build the profile and start Firefox,
the load (0.1 to 3 s here, as squid serves most of it), the 12 s settle, and
under a second for detection and screenshot. Building the template takes about
25 s per worker, once. With two visits per site and 4 workers, 22 sites took
3m 00s, so the CrUX top 1000 takes about 2.3 h. Every worker runs one
Firefox, so `--jobs` is bounded by memory more than by cores. The screenshots
take about 320 kB each, so about 650 MB for 1000 sites.
