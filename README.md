# 1000-websites

Scripts to benchmark the energy consumption of the top 1000 websites.

The repository holds **two usage scenarios that measure the same thing in the
same shape**, so that the number they produce can be compared against each
other:

| | `playwright/usage_scenario.yml` | `parrot/usage_scenario.yml` |
| --- | --- | --- |
| Browser | Chromium (Playwright's bundled build) | Google Chrome for Testing 152.0.7977.82 |
| Driven by | Playwright, over the Chrome DevTools Protocol | [Parrot](https://github.com/green-coding-solutions/parrot), replaying real X11 input |
| Window | headful, in the container's own Xvfb | headful, in the container's own Xvfb + fluxbox |
| Based on | GMT's `templates/website/usage_scenario_cached.yml` (the webnrg benchmark) | Parrot's `websites/usage_scenario_chrome_cached.yml` |

The reason both exist is to find out how much of a website energy measurement is
the website and how much is the measuring apparatus. Playwright drives the
browser through a debugging protocol that no real user goes through; Parrot
drives the same page by moving a pointer and pressing keys on an X server. If
the two agree, the cheap method is good enough for 1000 sites. If they do not,
the gap is the thing worth reporting.

## The flow

Both scenarios do exactly this, and only this:

1. **Warm the cache.** Load the page once through a
   [squid](https://hub.docker.com/r/greencoding/squid_reverse_proxy) MITM
   caching proxy, in a throwaway browser with a throwaway profile, for 20 s.
   Hidden and unmeasured.
2. **Load the page.** Navigate to `__GMT_VAR_PAGE__`. Measured.
3. **Idle for 30 s.** Record what the page does when nobody touches it: timers,
   animations, polling, ads rotating. Same measured phase as step 2, because a
   real browser cannot signal "load finished" the way Playwright can.
4. **Scroll for 5 s.** 25 wheel-down ticks, 200 ms apart. Measured.

The two measured phases are `Visit page and idle for 30 s` and
`Scroll down and wait 5 s`, named identically in both scenarios, and both emit
the same `website_load` custom metric.

## What is genuinely identical

These were measured in both containers rather than assumed, because they are
what decides how much work the page actually does:

* **The layout viewport is `1432x809` in both.** Parrot's Chrome window is
  pinned by fluxbox to 1440x900 with no decorations; 91 px go to the tab strip
  and toolbar, and 8 px to the vertical scrollbar. Playwright is set to that
  measured number rather than to a round one. It is not 1440 because an
  emulated Playwright viewport gets overlay scrollbars that take no layout
  width, so asking for 1440 would hand the page 8 px more than Parrot does.
* **One scroll tick moves the document 120 px in both**, for 3000 px over the
  5 s phase. One X button-5 tick was measured to scroll Chrome exactly 120 px,
  so the Playwright side dispatches `wheel(0, 120)` rather than the GMT
  template's 200, which would have scrolled 5000 px against Parrot's 3000.
* **Both use the same squid proxy image and the same 20 s warmup**, so both
  measured loads start with a warm proxy cache and a cold browser cache.
* **Both are headful**, with the browser rendering into an Xvfb inside its own
  container. Neither depends on an X server on the host, so neither is tied to
  a particular measurement machine by its display setup.
* **Both launch Chrome with the same switches.** `parrot/common/launch-browser.sh`
  passes every switch Playwright 1.62.1 gives Chromium, as read from
  `/proc/<pid>/cmdline` of the browser Playwright actually launched rather than
  from documentation, and checked the same way on Parrot's Chrome. Parrot leaves
  out the two it cannot use, `--remote-debugging-pipe` (Playwright's control
  channel) and `--no-startup-window`, and adds `--class` and `--homepage`, which
  its macros need and the page never sees. The shared switches that plausibly
  change energy:

  | Switch | What it changes |
  | --- | --- |
  | `--disable-dev-shm-usage` | Chrome creates its shared memory files in `/tmp` instead of `/dev/shm`. Measured in Parrot's container: 62 open under `/dev/shm` without the switch, none with it. |
  | `--enable-unsafe-swiftshader` | Lets Chrome fall back to SwiftShader, a CPU implementation of a GPU, and neither container has a GPU. It is the only GPU switch in the list, and with it Parrot's Chrome changed rendering path: before, it composited pages in software and offered no WebGL; now, like Playwright's, it composites every page through a SwiftShader GPU process and hands pages a WebGL context. Both browsers log two failed GPU-process starts on the way. WebGL content is computed but not displayed correctly in either container: a probe canvas drew about 300 frames in 5 s in both, and was missing from Parrot's screen and a broken image in Playwright's screenshot. |
  | `--disable-features=PaintHolding` | Chrome normally holds the old page's pixels briefly during a navigation. Disabled, it paints the new page immediately, inside the measured load. |
  | `--disable-back-forward-cache` | Removes the bfcache. |
  | `--disable-background-timer-throttling`, `--disable-renderer-backgrounding`, `--disable-backgrounding-occluded-windows` | Turn off the throttling Chrome applies to work it considers unimportant, which is exactly the kind of work the 30 s idle phase exists to measure. |
  | `--disable-features=HttpsUpgrades,Translate,OptimizationHints,ThirdPartyStoragePartitioning` | Four behaviours a shipped Chrome has and neither of these does. |
  | `--disable-ipc-flooding-protection`, `--disable-hang-monitor` | Remove two limiters on runaway pages. |
  | `--disable-popup-blocking` | Pop-ups a script opens without a click are allowed, and load. |

  The switches are copied rather than left to differ so that a delta between the
  scenarios comes from what drives the page, not from how Chrome is configured.
  The price is that neither scenario measures Chrome the way a person runs it.
  The list is Playwright's: when GMT upgrades Playwright, re-read it from a
  running browser and update `launch-browser.sh` to match.

## What is not identical, and why

Worth knowing before reading any delta between the two as a property of a
website:

* **The Chrome versions differ by one.** Both are Chrome for Testing:
  Playwright 1.62.1 bundles 151.0.7922.34, Parrot's image pins 152.0.7977.82.
* **Playwright's Chrome tells pages it is automated, Parrot's does not.**
  Chromium sets `navigator.webdriver` to `true` whenever it runs with
  `--remote-debugging-pipe`, and Playwright cannot run without that switch.
  Read from a page in each container: `true` under Playwright, `false` under
  Parrot. A site with bot detection can serve the two scenarios different
  content; `bot-detection/` lists sites known to.
* **The reachability check sits in different places.** Playwright checks the
  HTTP status of the very navigation it measured, afterwards. Parrot cannot do
  that, so it fetches the page through the proxy with `curl` first, in a hidden
  step, which also verifies that squid's certificate still chains to
  `parrot/squid-ca.crt`. Both checks are unmeasured.
* **Parrot's measured phases carry about a second of `replay.py` overhead**,
  visible as 31 s and 6 s against Playwright's 30 s and 5 s.
* **Neither scenario stops scrolling at the bottom of the page.** GMT's template
  does, and it was removed here on purpose: ending early on a short page makes
  the phase a different length on every site, which would make the sites
  incomparable with each other as well as the scenarios with each other.

## Running one locally

Both take exactly one variable, the page. From a Green Metrics Tool checkout:

```bash
cd /home/didi/code/green-metrics-tool

# Playwright
venv/bin/python runner.py \
  --uri /home/didi/code/1000-websites \
  --filename playwright/usage_scenario.yml \
  --variable __GMT_VAR_PAGE__=https://www.green-coding.io \
  --dev-no-sleeps --dev-no-system-checks

# Parrot
venv/bin/python runner.py \
  --uri /home/didi/code/1000-websites \
  --filename parrot/usage_scenario.yml \
  --variable __GMT_VAR_PAGE__=https://www.green-coding.io \
  --allow-unsafe --dev-no-sleeps --dev-no-system-checks
```

`--allow-unsafe` is needed for Parrot in CLI mode only, and only because of its
`docker-run-args: --shm-size=1g`. On the cluster that string is allowlisted for
the submitting user instead, so no such flag is involved.

To watch a Parrot run happen, set `DEBUG: 1` in the `window-container`
environment and uncomment the noVNC port, then open `http://localhost:6080`.

## Submitting to the cluster

`tools/submit.py` wraps `gmt-helpers`' `submit_software.py` and fills in the
things that are easy to get wrong 2000 times in a row: the repository URL
spelled without `.git` (energy-ID tasks match `runs.uri` exactly, and the wrong
spelling silently groups with nothing), the `--branch` and `--filename` that the
API requires even though argparse calls them optional, and the machine.

```bash
# one site, both scenarios
tools/submit.py --url https://www.tagesschau.de

# see the commands without sending anything
tools/submit.py --url https://www.tagesschau.de --dry-run

# one scenario only
tools/submit.py --url https://www.tagesschau.de --scenario parrot

# the sweep: one URL per line, blank lines and # comments ignored
tools/submit.py --url-file sites.txt
```

Both scenarios default to **machine 15** (`GUI High Performance Benchmarking -
TX1330 M4`), which is where the existing Parrot website sweep runs. Runs on
different machines are not comparable, so a run submitted anywhere else compares
only against itself.

**The cluster clones from GitHub.** A scenario only reaches it once the branch
is committed and pushed; the working tree is not what runs.

Check results with:

```bash
curl 'https://api.green-coding.io/v2/runs?uri=https://github.com/green-coding-solutions/1000-websites'
```

## Layout

```
playwright/usage_scenario.yml   the webnrg benchmark, headful Chromium
parrot/usage_scenario.yml       the same flow in a real Chrome
parrot/chrome/*.parrot          the three input macros: start, visit, scroll
parrot/common/*.sh              profile setup, proxy CA import, window pinning,
                                warmup, reachability check
parrot/squid-ca.crt             the proxy's signing CA, imported into the
                                browser's NSS store so the MITM cache works
tools/submit.py                 cluster submission for one URL or a list
```

`playwright/usage_scenario.yml` pulls its containers from GMT itself via
`!include-gmt-helper gmt-playwright-with-cache.yml`, and overrides that
partial's `flow-prepend` to start headful Chromium instead of headless Firefox.

`parrot/usage_scenario.yml` uses the prebuilt image `ribalba/parrot-browsers:v1`.
This repository consumes that image, it does not build it: the Dockerfile, the
pinned Chrome version and `make browsers` live in the
[parrot](https://github.com/green-coding-solutions/parrot) repository under
`websites/`. The macros and shell scripts under `parrot/` are ported from that
same directory, with the repository paths rewritten and the idle raised from
5 s to 30 s.
