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
4. **Take a review screenshot.** Capture what the page shows and upload it, so
   that a run on the cluster can be checked by eye. Hidden and unmeasured; see
   [Review screenshots](#review-screenshots).
5. **Scroll for 5 s.** 25 wheel-down ticks, 200 ms apart. Measured.

The two measured phases are `Visit page and idle for 30 s` and
`Scroll down and wait 5 s`, named identically in both scenarios, and both emit
the same `website_load` custom metric.

**Both phases last exactly as long in both scenarios: 33 s and 7 s.** The
machine draws its baseline power for as long as a phase is open, so phases of
different length hold energy that cannot be compared, whatever ran inside them.
The drivers do not take equally long for the same work, and not by a constant:
Parrot spends about a second starting `replay.py` before its first key press,
Playwright's `page.goto` waits for the load event, and its wheel events wait for
the renderer. On machine 6, before the deadlines, the visit took 31.39 s in
Parrot and 30.54 to 32.30 s in Playwright, the scroll 6.19 s against 5.33 to
7.21 s. Each measured phase therefore opens with `common/phase-clock.sh start`
and closes with `common/phase-clock.sh finish`, which idles to the deadline. A
phase already past its deadline fails the run rather than produce a number that
is not comparable. The deadlines live only in that script.

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
  Parrot's `curl` sends the request headers its Chrome sends on a navigation,
  captured from the browser itself: with curl's own headers, bot managers
  answered 403 on ebay.de, idealo.de, mediamarkt.de and wetteronline.de, all of
  which the browsers load. It also sends `Cache-Control: no-store`, so that
  squid does not keep what curl was given and hand it to the browsers; a copy
  curl got from idealo.de left the warmup and the measured browser reloading a
  blank page.
* **Neither scenario can measure a site that refuses HTTP/1.1.** squid speaks
  only HTTP/1.1 to origin servers. mobile.de serves HTTP/2 and rejects
  HTTP/1.1, so through squid it answers curl and both browsers with 403, with
  or without squid's `Via` and `X-Forwarded-For` headers. Parrot's check stops
  such a run before it measures the block page; Playwright's check stops it
  after.
* **squid's cache can trap a page behind a bot manager in a reload loop.** Its
  configuration caches every document, including those marked `private` or
  `no-store`, so a page that reloads itself, as bot-manager challenges do, gets
  squid's stored copy back each time instead of a fresh one. amazon.de's warmup
  did this in two of four test runs, re-requesting the same document about 270
  times in 22 s with no reachability check involved. The measured load after it
  rendered normally each time, but nothing guarantees that, so check what runs
  on such sites actually rendered.
* **The phases last equally long, but the work inside is laid out
  differently.** Parrot spends about a second starting `replay.py` before its
  first key press and then idles to the deadline; Playwright starts navigating
  almost at once and idles longer. The machine's baseline is therefore the same
  in both, and what remains different is the driver's own CPU work, which is
  part of what this repository sets out to measure.
* **Neither scenario stops scrolling at the bottom of the page.** GMT's template
  does, and it was removed here on purpose: ending early on a short page makes
  the phase a different length on every site, which would make the sites
  incomparable with each other as well as the scenarios with each other.

## Headful on the host's X display

Each scenario has a headful twin that draws on the machine's own X server
instead of an Xvfb inside the container: `parrot/usage_scenario_headful.yml`
and `playwright/usage_scenario_headful.yml`. The flow, the phase names and
deadlines, the squid cache and the `website_load` metric are the ones above, so
the two twins compare against each other the way the Xvfb pair does. Against
the Xvfb pair, a delta also contains whatever the host's X server, compositor
and display do with the frames Chrome sends them.

Both mount `/tmp/.X11-unix`, set `DISPLAY=:0` and connect as root without a
cookie, so the host has to let them in:

```bash
xhost +si:localuser:root     # or parrot's broader: xhost +local:
```

Without an X server on `:0` that accepts them, the Parrot one fails in BOOT with
`[entrypoint] cannot reach host display :0`, and the Playwright one cannot start
its browser. Machine 15 ran Parrot's xpdf host-display scenario successfully on
2026-06-04 and failed that way on 2026-06-18 and 2026-08-19, so check its
display before submitting there.

What had to change beyond pointing at `:0`:

* **Parrot's Chrome is forced to a device scale factor of 1.** Chrome scales by
  the X server's `Xft.dpi`, which an Xvfb does not set. On a desktop reporting
  144 dpi, a 1440x900 window gave pages 928x471 CSS px at `devicePixelRatio`
  1.5 instead of 1432x809 at 1. The headful scenario sets
  `PARROT_DEVICE_SCALE_FACTOR=1`, and `launch-browser.sh` turns that into
  `--force-device-scale-factor=1` for the warmup and the measured browser.
  Unset, the command line is unchanged, so the Xvfb scenario runs the same
  Chrome as before. Playwright needs no switch: its emulated viewport sets the
  factor to 1 itself.
* **Parrot's window is placed by `position-window.sh` alone.** There is no
  fluxbox to pin it, and `position-window.sh` only warns when a window manager
  overrides it, so a hidden step fails the run unless the window is 1440x900.
* **Playwright checks its viewport.** A hidden step fails the run unless the
  page reports 1432x809 at a `devicePixelRatio` within 0.001 of 1. On the 144
  dpi desktop it reported 1.0000000298023224, the emulated 1 plus float noise,
  which is why it is not an equality test.
* **Parrot moves the host's real pointer and presses real keys.** Do not touch
  the host's mouse or keyboard while a run is in progress.

Locally, run them like the others but with `--allow-unsafe` for both: the X
socket is an absolute volume, which GMT's CLI only mounts in unsafe mode. On the
cluster, user 8 has `/tmp/.X11-unix` allowlisted.

A failing check in the Playwright one does not end a local run. A Playwright step
that throws never signals GMT that it returned, and GMT only gives up waiting
after the flow process timeout, which a cluster job takes from the user's
`flow_process_duration` and `runner.py` leaves unset. The first local run of
this scenario sat for 32 minutes on a viewport check that had failed at once.
The HTTP status check in the Xvfb scenario behaves the same way.

## Running one locally

Both take two variables: the page, and the upload URL for the review
screenshot, where `off` skips the upload. From a Green Metrics Tool checkout:

```bash
cd /home/didi/code/green-metrics-tool

# Playwright
venv/bin/python runner.py \
  --uri /home/didi/code/1000-websites \
  --filename playwright/usage_scenario.yml \
  --variable __GMT_VAR_PAGE__=https://www.green-coding.io \
  --variable __GMT_VAR_SCREENSHOT_URL__=off \
  --dev-no-sleeps --dev-no-system-checks

# Parrot
venv/bin/python runner.py \
  --uri /home/didi/code/1000-websites \
  --filename parrot/usage_scenario.yml \
  --variable __GMT_VAR_PAGE__=https://www.green-coding.io \
  --variable __GMT_VAR_SCREENSHOT_URL__=off \
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

# the headful variants, on the machine's own X display; combines with the above
tools/submit.py --url https://www.tagesschau.de --headful

# label a batch: " - batch-2026-09-15" is appended to every run name, so the
# dashboard search finds the whole batch
tools/submit.py --url-file sites.txt --name-suffix batch-2026-09-15
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

## Review screenshots

A run on the cluster keeps no files, and watching runs on a screen does not
scale: a Parrot run took about 68 s on a local machine with GMT's sleeps off, so
580 sites take 11 hours to watch. Every scenario therefore takes one screenshot
of the loaded page and uploads it to `screenshot-server/`, which shows all of
them in one gallery, one row per site, with both drivers side by side.

* **When:** in a hidden phase, `Screenshot for review`, right after
  `Visit page and idle for 30 s`. It shows the page as the visit phase
  measured it, and none of its work is in a measured phase. In Playwright it
  comes before the status check, so a page that fails that check still leaves
  a screenshot.
* **What:** Parrot grabs the whole X screen with `import`, so Chrome's address
  bar shows where the browser really ended up after redirects. Playwright uses
  `page.screenshot`, which is the 1432x809 viewport without browser UI. Both send
  the page title along.
* **Never fails the run.** A capture or upload problem is a `[screenshot]
  WARNING` line in the run's log, and the run goes on. A review server that is
  down must not cost a cluster run.
* **One side effect:** the scroll phase now starts a second or two later
  after the page loaded than before, on a page that had that much longer to
  settle. Its 7 s deadline is unchanged.

One variable configures it, and every scenario requires it:
`__GMT_VAR_SCREENSHOT_URL__`, the server's upload endpoint, e.g.
`https://shots.example.org/upload`, or `off` to skip the upload.
`tools/submit.py` fills it in from `--screenshot-url` or `$SCREENSHOT_URL`, and
without either it submits with screenshots off and says so.

The server has no authentication: anyone who knows the URL can upload to it and
see the gallery. That is fine for a server that lives for one batch of runs
behind an HTTPS proxy and is deleted afterwards, and not for anything longer.

```bash
tools/submit.py --url-file sites.txt --screenshot-url https://shots.example.org/upload
```

How to run the server, with docker compose behind an HTTPS proxy, is in
`screenshot-server/README.md`.

## Layout

```
playwright/usage_scenario.yml   the webnrg benchmark, headful Chromium
parrot/usage_scenario.yml       the same flow in a real Chrome
playwright/usage_scenario_headful.yml
parrot/usage_scenario_headful.yml
                                both of the above on the host's X display (:0)
                                instead of an Xvfb in the container
parrot/chrome/*.parrot          the three input macros: start, visit, scroll
parrot/common/*.sh              profile setup, proxy CA import, window pinning,
                                warmup, reachability check, review screenshot
parrot/squid-ca.crt             the proxy's signing CA, imported into the
                                browser's NSS store so the MITM cache works
common/phase-clock.sh           the fixed 33 s and 7 s deadlines both
                                scenarios' measured phases end on
common/upload-screenshot.sh     uploads the review screenshot, for both drivers
screenshot-server/              receives the review screenshots and shows them
                                in one gallery
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
