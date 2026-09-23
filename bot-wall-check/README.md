# bot-wall-check

Which sites on a list can a benchmark run actually measure? `check.py` fetches
every entry once, through the same squid the scenarios measure through, and
sorts the answers into sites that served their page, bot walls, and everything
else. Run it on a list before submitting that list.

```bash
# the CrUX global top 1000
bot-wall-check/check.py bot-wall-check/crux-global-top1000-202608.txt

# any list in the sites.txt format, several at once, or just the first 50
bot-wall-check/check.py sites.txt sites_different --limit 50
```

It needs Docker and Python 3.9 or newer. curl runs inside the parrot image, and
both images are pulled on first use. The CrUX list takes about 80 s at the
default 16 sites in parallel (`--jobs`), with 30 s allowed per site
(`--timeout`).

Each list gets a CSV in `bot-wall-check/results/`, named after the list and
the start time, and a report on the terminal. From a run on 2026-09-16:

```
bot-wall check: crux-global-top1000-202608.txt
  1000 entries, 16 in parallel, 1m 17s
  through greencoding/squid_reverse_proxy:v5, curl from ribalba/parrot-browsers:v1
  leaving from 84.129.109.119 (AS3320 Deutsche Telekom AG, DE)

Summary
  group          sites  share
  ─────────────  ─────  ─────  ─────────────────────────
  benchmarkable    812  81.2%  ████████████████████████▍
  walled           116  11.6%  ███▌
  undecided         58   5.8%  █▊
  not a website     14   1.4%  ▍

What walls them
  verdict    detail                       sites  examples
  ─────────  ───────────────────────────  ─────  ─────────────────────────────────────────────────────────────
  blocked    http-403                        39  arionplay.com, auctions.yahoo.co.jp, baseball.yahoo.co.jp +36
  challenge  cloudflare                      19  18comic.vip, danbooru.donmai.us, itch.io +16
  challenge  aws-waf                         14  www.amazon.ca, www.amazon.co.jp, www.amazon.co.uk +11
  challenge  datadome                         9  allegro.pl, www.etsy.com, www.idealista.com +6
```

The full report also has a table per verdict and the bot-management cookies
seen.

## Verdicts

| Verdict | Group | Meaning |
| --- | --- | --- |
| `ok` | benchmarkable | The site served its page |
| `challenge` | walled | A bot manager's challenge page instead of the site |
| `blocked` | walled | 401, 403, 429 or another 4xx, or a page saying access is denied |
| `suspect` | undecided | A 2xx under 1 kB, or under 20 kB without a title. Look at it |
| `server-error` | undecided | The origin answered 5xx |
| `proxy-error` | undecided | squid could not fetch the page: its TLS handshake, connect or read failed. The site may be fine, but the scenarios reach it only through squid |
| `error` | undecided | No usable answer: a timeout, a redirect loop, a broken transfer |
| `no-homepage` | not a website | 404 or 410, or a 2xx titled "404 Not Found" |
| `no-website` | not a website | The host name does not resolve |

## CSV columns

| Column | |
| --- | --- |
| `position` | 1-based position in the list |
| `input` | The entry as written in the list |
| `url` | What was fetched. A bare domain becomes `https://<domain>/` |
| `verdict`, `detail` | See above. `detail` names the vendor, the status, the phrase (`title: ...` or `text: ...`), the squid error or the curl error |
| `http_status` | Of the final response. 0 when there was none |
| `final_url` | After redirects |
| `title` | The page's `<title>`, in the page's charset. The quickest way to spot a wall this script missed |
| `body_bytes` | Decoded size of the final response body |
| `redirects`, `seconds` | As curl counted them |
| `antibot_cookies` | Vendors whose bot-management cookies were set, whatever the verdict: `akamai`, `aws-waf`, `cloudflare`, `datadome`, `imperva`, `perimeterx` |

## How it fetches

Exactly as `parrot/common/check-url.sh` does, because that is the step that
lets a Parrot run go ahead: curl in `ribalba/parrot-browsers`, `-L`, through a
fresh `greencoding/squid_reverse_proxy`, verifying squid's certificates
against `parrot/squid-ca.crt`, with the request headers Chrome sends on a
navigation. The image names are read from `parrot/usage_scenario.yml` and the
headers from `check-url.sh`, so they cannot drift apart.

Going through squid is the point. squid terminates TLS and speaks HTTP/1.1 to
the origin, so the origin sees squid's TLS and HTTP version whichever client
is behind it, and that alone gets some sites to refuse: mobile.de answers 403.

## How it decides

The first rule that matches wins:

1. No response at all: `error`, with curl's message.
2. squid's `X-Squid-Error` header: `no-website` for `ERR_DNS_FAIL`, otherwise
   `proxy-error`.
3. A vendor's challenge or block page, on pages under 128 kB: `challenge` or
   `blocked`, with the vendor.
4. A wall's wording (`access denied`, `verify you are human`,
   `prove your humanity`, ...) in the title, or in the text of a page under
   32 kB: `challenge` or `blocked`.
5. The status: 404 or 410 is `no-homepage`, 202 or 428 `challenge`, any other
   4xx `blocked`, 5xx `server-error`, and an unfinished redirect or transfer
   `error`.
6. A 2xx titled "404 Not Found" is `no-homepage`. A 2xx under 1 kB, or under
   20 kB without a title, is `suspect`.
7. Everything else is `ok`.

**The status code alone decides nothing**, because the walls that matter do not
answer with an error. Akamai's `bm-verify` interstitial is a 200, AWS WAF's
challenge is a 202, Cloudflare's "Just a moment..." page is a 403 and Reddit's
"Prove your humanity" page is a 200. `check-url.sh` fails a run only from 400
up, so of these four it stops only Cloudflare's.

**The vendor markers are narrow on purpose.** Cloudflare injects
`/cdn-cgi/challenge-platform/scripts/jsd/main.js` into ordinary pages, and
Imperva injects `/_Incapsula_Resource?SWJIYLWA=` into ordinary pages. A first
version of this check matched those and called medium.com, discord.com,
nist.gov and digicert.com walled; all four serve their pages. The markers used
now occur only on the interstitial or block page itself, such as Cloudflare's
`window._cf_chl_opt` or AWS WAF's `gokuProps`, and Cloudflare's marker also
catches its localised pages ("보안 검사중", "Xác minh bảo mật").

## What it cannot tell you

* **Walls that the browser triggers.** A site that serves its page and walls
  the browser only after its scripts have run, or after the browser scrolls or
  clicks, passes. So does anything decided by the browser's own fingerprint.
* **What another address would get.** Bot managers score the address a request
  comes from, and gambling and government sites restrict by country: 14 of the
  27 Indian government sites in the CrUX list time out from Germany. Run the
  check on the machine that will run the benchmark, and compare the "leaving
  from" line.
* **A settled answer for sites that alternate.** amazon.de has answered with
  Akamai's interstitial in some runs and AWS WAF's challenge in others, and a
  site that walls some requests can pass once. One pass is one sample.
* **Walls it has no marker for**, when they answer 2xx with a title and more
  than 20 kB. Read the `title` column of the `ok` rows.
* **Consent gates.** "DPG Media Privacy Gate" on nu.nl, hln.be and ad.nl is
  `ok`: everyone gets it, bot or not. A benchmark would still measure the gate.
* **Cookie redirects.** curl keeps no cookies, like `check-url.sh`, so a site
  that sets a cookie and redirects to itself until it gets it back comes out as
  `error` with `curl-47: maximum (50) redirects followed`. A browser follows
  that redirect. `check-url.sh` does not: on www.ozon.ru it exits 47 and the
  Parrot run stops there.

## The CrUX list

`crux-global-top1000-202608.txt` is the top-1000 bucket of the Chrome UX Report
for August 2026, one origin per line. CrUX ranks origins by page loads in
Chrome, so every entry is a page people open in a browser, which a top-1000
website benchmark needs. Tranco ranks domains by how often they are resolved,
and about a quarter of its top 1000 are infrastructure with no website behind
them: on 2026-09-16, 222 did not resolve and 22 answered 404, among them
akamai.net and gtld-servers.net. The same check found 14 such entries in the
CrUX list.

CrUX publishes buckets, not ranks, so the list is in no order and is sorted
bytewise. It is real traffic and includes adult sites. The file's header says
where it comes from and has the command that regenerates it byte for byte, or
for another month or a country (`data/country/de/...`).
