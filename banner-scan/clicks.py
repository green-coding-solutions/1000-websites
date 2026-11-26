#!/usr/bin/env python3
"""Find and prove the screen coordinates of a site's consent accept and reject buttons.

    python3 /tmp/repo/banner-scan/clicks.py --url https://www.ikea.com/de/de/
    python3 /tmp/repo/banner-scan/clicks.py --geometry-check

Runs inside ribalba/parrot-browsers with Python's standard library only.
find-clicks.py starts it with docker exec, once per site, and reads one JSON
line from its stdout. The screenshots travel inside that line as base64.

A site gets up to two visits, each in a fresh plain profile (setup-profile.sh
with PARROT_CONSENT unset, plus the Marionette prefs of worker.py):

    A  wait for the banner, screenshot it, find the buttons with buttons.js,
       click "accept" with a real X click (xdotool) and check that it worked
    B  the same page again, find a way to reject in at most 3 clicks (reject,
       or settings and then reject or save), click it and check it

The window is never moved or resized through Marionette. Firefox gets the
geometry of the measured run: the container ran pin-windows.sh before the
window manager started, and position-window.sh runs after the window maps, as
replay.py does. Screen coordinates are window.mozInnerScreenX/Y plus the
client coordinates of the button, plus the content-box offsets of the iframes
around it.

--geometry-check compares where the page content starts on the X screen in a
Firefox started without Marionette (as the benchmark starts it) with the
Marionette-driven one, by painting a page magenta and finding it in a
screenshot of the root window.
"""
import argparse
import base64
import json
import os
import select
import shutil
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from worker import Marionette, MarionetteError, kill_firefox, make_profile, PORT  # noqa: E402

REPO = os.environ.get('BANNER_SCAN_REPO', '/tmp/repo')
COMMON = os.path.join(REPO, 'parrot', 'common')
WORK = '/tmp/click-finder'
DISPLAY = os.environ.get('DISPLAY', ':99')
SCREEN = (1440, 900)
POSITION_WINDOW = '/usr/local/bin/position-window.sh'


def read(name):
    with open(os.path.join(HERE, name), encoding='utf-8') as f:
        return f.read()


DETECT_SRC = read('detect.js') + '\nreturn detectBanner();'
BUTTONS_SRC = read('buttons.js') + '\nreturn findButtons(arguments[0]);'
FRAMEHITS_SRC = read('buttons.js') + '\nreturn frameHits(arguments[0], arguments[1]);'

BANNER_WAIT = 15.0      # seconds from the start of the navigation
LOAD_TIMEOUT = 30.0     # the longest wait for a page that is still loading and blank
SETTLE = 1.5            # after the banner is first seen
AFTER_FINAL = 3.0       # after an accept, reject or save click
# A verified path is looked at once more this much later. corriere.it answered
# a refusal through Consent-O-Matic with a subscription wall that came up
# several seconds after the click, and the measured visit idles about 20 s
# after its clicks.
HOLD_CHECK = 8.0
AFTER_STEP = 2.0        # after a settings click
MAX_REJECT_CLICKS = 3
MAX_ACCEPT_CLICKS = 2
FRAME_MIN_PCT = 3.0     # frames that are not marked as consent frames must be this big


class Conn(Marionette):
    """worker.py's client, with more than one command in flight.

    WebDriver:Navigate with the normal load strategy only answers after the
    load event, and the banner is watched from the start of the navigation.
    Marionette answers other commands while a navigation is pending, so the
    navigation is sent without waiting and its answer is filed when it comes.
    """

    def __init__(self, port, deadline):
        super().__init__(port, deadline)
        self.replies = {}

    def send(self, name, params=None):
        self.msg_id += 1
        data = json.dumps([0, self.msg_id, name, params or {}]).encode()
        self.sock.sendall(str(len(data)).encode() + b':' + data)
        return self.msg_id

    def _parse(self):
        head, sep, rest = self.buf.partition(b':')
        if not sep:
            return None
        n = int(head)
        if len(rest) < n:
            return None
        self.buf = rest[n:]
        return json.loads(rest[:n])

    def _file(self, block_until):
        msg = self._parse()
        if msg is not None:
            if msg[0] == 1:
                self.replies[msg[1]] = msg
            return True
        left = block_until - time.monotonic()
        r, _, _ = select.select([self.sock], [], [], max(0.0, left))
        if not r:
            return False
        chunk = self.sock.recv(1 << 20)
        if not chunk:
            raise MarionetteError('Marionette closed the connection')
        self.buf += chunk
        return True

    def ready(self, mid):
        while self._file(0):
            pass
        return mid in self.replies

    def wait(self, mid, timeout):
        deadline = time.monotonic() + timeout
        while mid not in self.replies:
            if not self._file(deadline) and time.monotonic() >= deadline:
                raise socket.timeout(f'no answer within {timeout:.0f}s')
        msg = self.replies.pop(mid)
        if msg[2]:
            err = msg[2]
            raise MarionetteError(f"{err.get('error')}: {str(err.get('message', ''))[:200]}")
        return msg[3]

    def call(self, name, params=None, timeout=30):
        return self.wait(self.send(name, params), timeout)


def xenv():
    return dict(os.environ, DISPLAY=DISPLAY)


def screenshot(path):
    """The whole X screen, as the benchmark's review screenshot sees it."""
    subprocess.run(['import', '-silent', '-window', 'root', path], env=xenv(), capture_output=True, timeout=30)
    if not os.path.exists(path):
        return ''
    with open(path, 'rb') as f:
        data = base64.b64encode(f.read()).decode()
    os.remove(path)
    return data


def position_window():
    """What replay.py does after the window maps, with the measured run's env."""
    env = dict(xenv(), APP_WINDOW_CLASS='firefox', APP_WINDOW_TITLE='Mozilla Firefox',
               WINDOW_X='0', WINDOW_Y='0', WINDOW_WIDTH=str(SCREEN[0]), WINDOW_HEIGHT=str(SCREEN[1]),
               RESOLUTION=f'{SCREEN[0]}x{SCREEN[1]}')
    p = subprocess.run([POSITION_WINDOW], env=env, capture_output=True, text=True, timeout=60)
    return p.stdout.strip().splitlines()[-1] if p.stdout.strip() else ''


def window_geometry():
    out = subprocess.run('for w in $(xdotool search --onlyvisible --class firefox); do '
                         'xdotool getwindowgeometry --shell $w; done', shell=True, env=xenv(),
                         capture_output=True, text=True).stdout
    best = None
    for block in out.split('WINDOW=')[1:]:
        kv = dict(line.split('=', 1) for line in ('WINDOW=' + block).split() if '=' in line)
        try:
            g = (int(kv['X']), int(kv['Y']), int(kv['WIDTH']), int(kv['HEIGHT']))
        except (KeyError, ValueError):
            continue
        if not best or g[2] * g[3] > best[2] * best[3]:
            best = g
    return best


def xclick(x, y):
    """A real click: move the pointer, a short pause as a hand would make, button 1."""
    subprocess.run(['xdotool', 'mousemove', str(x), str(y)], env=xenv(), check=True, timeout=10)
    time.sleep(0.15)
    loc = subprocess.run(['xdotool', 'getmouselocation', '--shell'], env=xenv(), capture_output=True,
                         text=True, timeout=10).stdout
    kv = dict(line.split('=', 1) for line in loc.split() if '=' in line)
    subprocess.run(['xdotool', 'click', '1'], env=xenv(), check=True, timeout=10)
    return (int(kv.get('X', -1)), int(kv.get('Y', -1)))


# Second-level domains under a country code, so bbc.co.uk is one site.
SLD = {'co', 'com', 'net', 'org', 'gov', 'edu', 'ac', 'or', 'ne', 'go', 'gob', 'mil', 'nic', 'ltd', 'plc'}


def site_of(url):
    host = (urlsplit(url).hostname or '').lower().rstrip('.')
    parts = host.split('.')
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in SLD:
        return '.'.join(parts[-3:])
    return '.'.join(parts[-2:])


class Browser:
    """One Firefox with a fresh plain profile, driven through Marionette."""

    def __init__(self, url, name, deadline, marionette=True):
        os.makedirs(WORK, exist_ok=True)
        self.profile = os.path.join(WORK, f'profile-{name}')
        self.log_path = os.path.join(WORK, f'firefox-{name}.log')
        subprocess.run(['pkill', '-KILL', '-f', '--', self.profile], capture_output=True)
        self.proc = self.mn = None
        make_profile('plain', self.profile, url)
        env = dict(xenv(), PARROT_URL=url)
        if marionette:
            env['MOZ_MARIONETTE'] = '1'
        with open(self.log_path, 'w') as log:
            self.proc = subprocess.Popen(['bash', os.path.join(COMMON, 'launch-browser.sh'), 'firefox',
                                          self.profile, 'about:blank'],
                                         env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        self.mn = Conn(PORT, min(deadline, time.monotonic() + 40))
        self.mn.call('WebDriver:NewSession', {'capabilities': {'alwaysMatch': {
            'pageLoadStrategy': 'normal', 'acceptInsecureCerts': False}}})
        self.mn.call('WebDriver:SetTimeouts', {'pageLoad': 30000, 'script': 20000})
        handle = self.mn.call('WebDriver:GetWindowHandle')
        self.handle = handle['value'] if isinstance(handle, dict) else handle
        self.position = position_window()
        # start.parrot clicks into the content area before the measured
        # Alt+Home, which also leaves the pointer at 720,500.
        xclick(720, 500)
        time.sleep(0.3)

    def top(self):
        self.mn.call('WebDriver:SwitchToFrame', {'id': None}, timeout=10)

    def js(self, script, args=None, timeout=15):
        return self.mn.call('WebDriver:ExecuteScript', {'script': script, 'args': args or []}, timeout=timeout)['value']

    def quit(self):
        if self.mn:
            try:
                self.mn.call('Marionette:Quit', {'flags': ['eForceQuit']}, timeout=10)
            except (MarionetteError, OSError):
                pass
            self.mn.close()
        kill_firefox(self.proc, self.profile)
        shutil.rmtree(self.profile, ignore_errors=True)


# --- looking at the page ------------------------------------------------------------

def detect(b):
    try:
        b.top()
        return b.js(DETECT_SRC, timeout=20) or {}
    except (MarionetteError, OSError) as e:
        return {'verdict': 'error', 'evidence': [f'detect: {e}']}


def locate(b, correction):
    """All candidates of the page and its frames, with screen coordinates."""
    b.top()
    top = b.js(BUTTONS_SRC, [{}], timeout=20)
    base = top['inner_screen']
    res = {'url': top['url'], 'inner_screen': base, 'viewport': top['viewport'], 'dpr': top['dpr'],
           'scroll_locked': top['scroll_locked'], 'cover_pct': top['cover_pct'], 'cands': [],
           'containers': [], 'holders': set(), 'toggles': {'total': 0, 'on': 0, 'on_labels': []},
           'price_offer': {}, 'offscreen': 0, 'notes': []}

    def add(info, path, off, frame_holder):
        for c in info['containers']:
            key = frame_holder or f"top|{c['element']}"
            res['containers'].append(dict(c, path=path, key=key))
            res['holders'].add(key)
        for cand in info['candidates']:
            c = info['containers'][cand['container']]
            cand = dict(cand, path=path, holder=frame_holder or f"top|{c['element']}")
            cand['sx'] = int(round(base[0] + off[0] + cand['x']))
            cand['sy'] = int(round(base[1] + off[1] + cand['y']))
            cand['rx'] = cand['sx'] + correction[0]
            cand['ry'] = cand['sy'] + correction[1]
            res['cands'].append(cand)
        t = info['toggles']
        res['toggles']['total'] += t['total']
        res['toggles']['on'] += t['on']
        res['toggles']['on_labels'] += t['on_labels']
        if info.get('price_offer'):
            res['price_offer'][frame_holder or 'top'] = info['price_offer']
        res['offscreen'] += info.get('offscreen', 0)

    add(top, 'top', (0, 0), None)

    def walk(info, path, off, holder, depth):
        for i, f in enumerate(info['frames']):
            consent = f['cmp'] or f['in_container']
            if not consent and f['area_pct'] < FRAME_MIN_PCT:
                continue
            label = f['label'] or f'frame {i}'
            fpath = f'{path} > {label[:60]}'
            fholder = holder or f'frame|{label[:100]}'
            try:
                b.mn.call('WebDriver:SwitchToFrame', {'id': f['el']}, timeout=10)
            except (MarionetteError, OSError) as e:
                res['notes'].append(f'switch to {label[:40]}: {e}')
                continue
            sub = None
            try:
                sub = b.js(BUTTONS_SRC, [{'consentFrame': consent, 'inFrame': True}], timeout=15)
            except (MarionetteError, OSError) as e:
                res['notes'].append(f'buttons.js in {label[:40]}: {e}')
            foff = (off[0] + f['offset'][0], off[1] + f['offset'][1])
            if sub and depth < 2 and sub.get('frames'):
                walk(sub, fpath, foff, fholder, depth + 1)
            b.mn.call('WebDriver:SwitchToParentFrame', {}, timeout=10)
            if not sub:
                continue
            if sub['containers']:
                res['holders'].add(fholder)
            # The candidate is on top inside the frame. Check that the frame
            # is on top at that point in its parent too.
            pts = [[f['offset'][0] + c['x'], f['offset'][1] + c['y']] for c in sub['candidates']]
            if pts:
                hits = b.js(FRAMEHITS_SRC, [f['el'], pts], timeout=10)
                covered = [c['label'] for c, h in zip(sub['candidates'], hits) if not h]
                if covered:
                    res['notes'].append(f'covered in parent: {", ".join(covered[:3])}')
                sub['candidates'] = [c for c, h in zip(sub['candidates'], hits) if h]
            # Cross-check: the frame's own mozInnerScreen against the offsets.
            want = (base[0] + foff[0], base[1] + foff[1])
            have = sub['inner_screen']
            if abs(want[0] - have[0]) > 1 or abs(want[1] - have[1]) > 1:
                res['notes'].append(f'frame offset mismatch in {label[:40]}: computed {want[0]:.0f},{want[1]:.0f}, '
                                    f'frame says {have[0]:.0f},{have[1]:.0f}')
            add(sub, fpath, foff, fholder)

    walk(top, 'top', (0, 0), None, 1)
    b.top()
    return res


def stable_pick(b, pick, correction, tries=3):
    """Locate again until the chosen button stops moving (banners slide in)."""
    for _ in range(tries):
        time.sleep(0.4)
        try:
            again = locate(b, correction)
        except (MarionetteError, OSError, KeyError, TypeError):
            return pick, None
        same = [c for c in again['cands'] if c['kind'] == pick['kind'] and c['label'] == pick['label']
                and c['holder'] == pick['holder']]
        if not same:
            return pick, again
        best = min(same, key=lambda c: abs(c['sx'] - pick['sx']) + abs(c['sy'] - pick['sy']))
        if abs(best['sx'] - pick['sx']) <= 1 and abs(best['sy'] - pick['sy']) <= 1:
            return best, again
        pick = best
    return pick, None


# --- choosing and clicking --------------------------------------------------------------

WHY_RANK = {'cmp': 3, 'dialog': 2, 'overlay': 2, 'consent frame': 3, 'frame text': 2, 'element': 1}


def rank(c):
    return (c['strength'], not c['href_away'], c['tag'] in ('button', 'input'), c['rect'][2] * c['rect'][3])


def per_item(loc):
    """Buttons whose label repeats inside one consent UI act on one purpose or
    vendor each, like the Accept and Reject pairs in Sourcepoint's privacy
    manager on spiegel.de. Clicking one of them answers nothing as a whole."""
    seen = {}
    for c in loc['cands']:
        key = (c['holder'], c['kind'], c['label'].lower())
        seen[key] = seen.get(key, 0) + 1
    return {k for k, n in seen.items() if n > 1}


def choose(loc, mode, kinds, clicked):
    repeated = per_item(loc)

    def fresh(kind):
        return sorted((c for c in loc['cands'] if c['kind'] == kind and not c['href_away']
                       and (c['label'], c['sx'], c['sy']) not in clicked
                       and (c['holder'], c['kind'], c['label'].lower()) not in repeated), key=rank, reverse=True)
    if mode == 'accept':
        acc = fresh('accept')
        return acc[0] if acc else None
    rej = fresh('reject')
    t = loc['toggles']
    sav = fresh('save') if ('settings' in kinds or 'reject' in kinds or (t['total'] and not t['on'])) else []
    # A "Deny all" in a settings layer often only sets the toggles, and the
    # choice still has to be saved (corriere.it). After a reject that left the
    # UI open, save comes first.
    if sav and 'reject' in kinds:
        return sav[0]
    if rej:
        return rej[0]
    if sav:
        return sav[0]
    if len(kinds) < MAX_REJECT_CLICKS - 1:
        done = {lbl for lbl, _, _ in clicked}
        sett = [c for c in fresh('settings') if c['label'] not in done]
        if sett:
            return sett[0]
    return None


def look(b, correction, sites):
    """The state after a click: windows, URL, detector, candidates."""
    st = {'popup': False, 'away': False, 'url': '', 'det': {}, 'loc': None, 'error': ''}
    try:
        handles = b.mn.call('WebDriver:GetWindowHandles', timeout=10)
        handles = handles['value'] if isinstance(handles, dict) else handles
        if len(handles) > 1:
            st['popup'] = True
            for h in handles:
                if h != b.handle:
                    try:
                        b.mn.call('WebDriver:SwitchToWindow', {'handle': h}, timeout=10)
                        b.mn.call('WebDriver:CloseWindow', {}, timeout=10)
                    except (MarionetteError, OSError):
                        pass
            b.mn.call('WebDriver:SwitchToWindow', {'handle': b.handle}, timeout=10)
        st['url'] = b.mn.call('WebDriver:GetCurrentURL', timeout=10)['value']
        st['away'] = site_of(st['url']) not in sites
        st['det'] = detect(b)
        st['loc'] = locate(b, correction)
    except (MarionetteError, OSError, KeyError, TypeError) as e:
        st['error'] = f'{type(e).__name__}: {e}'[:200]
    return st


def settled(st, holder):
    """True when the consent UI the click was for is done.

    No accept, reject or save button is left on top anywhere, and either the
    detector no longer says banner or the container that held the button is
    gone. A floating "privacy settings" button that many CMPs leave behind
    does not count.
    """
    loc = st['loc']
    if not loc:
        return False
    if any(c['kind'] in ('accept', 'reject', 'save') for c in loc['cands']):
        return False
    return st['det'].get('verdict') != 'banner' or holder not in loc['holders']


def run_path(b, loc, mode, correction, sites, shot_dir, deadline, ev):
    """Click through one path. Returns status, clicks, labels, kinds, the last state."""
    clicks, labels, kinds, clicked = [], [], [], set()
    status, st = 'not-found', None
    max_clicks = MAX_ACCEPT_CLICKS if mode == 'accept' else MAX_REJECT_CLICKS
    for _ in range(max_clicks):
        if time.monotonic() > deadline - 15:
            ev.append(f'{mode}: out of time')
            break
        pick = choose(loc, mode, kinds, clicked)
        if not pick:
            break
        pick, _ = stable_pick(b, pick, correction)
        if mode == 'reject' and pick['kind'] == 'save':
            t = loc['toggles']
            ev.append(f"reject: {t['on']} of {t['total']} toggles on before save"
                      + (f" ({', '.join(t['on_labels'][:3])})" if t['on'] else ''))
        at = xclick(pick['sx'], pick['sy'])
        if at != (pick['sx'], pick['sy']):
            ev.append(f'{mode}: pointer at {at}, wanted {pick["sx"]},{pick["sy"]}')
        clicks.append((pick['rx'], pick['ry']))
        labels.append(pick['label'])
        kinds.append(pick['kind'])
        clicked.add((pick['label'], pick['sx'], pick['sy']))
        final = pick['kind'] in ('accept', 'reject', 'save')
        time.sleep(AFTER_FINAL if final else AFTER_STEP)
        st = look(b, correction, sites)
        if st['error']:
            ev.append(f'{mode}: after click: {st["error"]}')
            status = 'failed'
            break
        if st['popup'] or st['away']:
            ev.append(f'{mode}: click on "{pick["label"]}" ' + ('opened a new window' if st['popup']
                                                               else f'left the site for {st["url"][:80]}'))
            status = 'failed'
            break
        if settled(st, pick['holder']):
            status = 'verified' if final else 'failed'
            if not final:
                ev.append(f'{mode}: the consent UI went away after "{pick["label"]}", a {pick["kind"]} button')
            break
        status = 'failed'
        loc = st['loc']
        if final and pick['holder'] not in loc['holders']:
            # The UI that held the button is gone, but another one is up.
            # bbc.co.uk answers a refusal in Sourcepoint with its own bar that
            # only offers "Yes, I agree". Go on if the new UI has a button of
            # this path's kind, otherwise the path is done and the rest is noted.
            nxt = choose(loc, mode, kinds, clicked)
            # Only a UI that leaves the page usable may stay. t-online.de's
            # pay-or-consent wall stayed up after its "withdraw all consents"
            # path, scroll locked and covering the page, which is not a refusal
            # that lets the user in.
            if loc['scroll_locked'] or loc['cover_pct'] >= 30:
                ev.append(f'{mode}: a blocking consent UI stays after the path '
                          f"(scroll {'locked' if loc['scroll_locked'] else 'free'}, covers {loc['cover_pct']}%)")
            elif not (nxt and nxt['kind'] == pick['kind'] and len(clicks) < max_clicks):
                status = 'verified'
                left = [c['label'] for c in loc['cands'] if c['kind'] in ('accept', 'reject', 'save')]
                ev.append(f'{mode}: done, the consent UI went away, another one stays: {", ".join(left[:3])}')
                break
    if status == 'verified' and st and st.get('loc') and time.monotonic() < deadline - HOLD_CHECK - 10:
        status, st = hold_check(b, st, mode, correction, sites, ev)
    return status, clicks, labels, kinds, st


def hold_check(b, st, mode, correction, sites, ev):
    """Look again HOLD_CHECK s later. A consent UI or wall that was not there
    right after the click fails the path. One that was there (bbc.co.uk's own
    bar after a refusal in Sourcepoint) does not."""
    before = set(st['loc']['holders'])
    time.sleep(HOLD_CHECK)
    st2 = look(b, correction, sites)
    if st2['error'] or not st2.get('loc'):
        return 'verified', st
    if st2['popup'] or st2['away']:
        ev.append(f'{mode}: {HOLD_CHECK:.0f} s later the browser ' +
                  ('had a new window' if st2['popup'] else f'was on {st2["url"][:80]}'))
        return 'failed', st2
    new = [c for c in st2['loc']['cands']
           if c['holder'] not in before and c['kind'] in ('accept', 'reject', 'save', 'subscribe')]
    if new:
        ev.append(f'{mode}: a consent UI came back {HOLD_CHECK:.0f} s later: '
                  + ', '.join(c['label'] for c in new[:3]))
        return 'failed', st2
    return 'verified', st2


# --- one visit -----------------------------------------------------------------------------

def wait_for_banner(b, url, deadline):
    """Navigate and watch for the banner, about once a second for BANNER_WAIT s."""
    t0 = time.monotonic()
    nav = b.mn.send('WebDriver:Navigate', {'url': url})
    seen, last, polls, load_s = None, {}, 0, None
    while True:
        tick = time.monotonic()
        if load_s is None and b.mn.ready(nav):
            load_s = round(tick - t0, 1)
        det = detect(b)
        polls += 1
        if det.get('verdict') not in (None, 'error'):
            last = det
        if det.get('verdict') == 'banner':
            seen = round(tick - t0, 1)
            break
        # A page that has not finished loading and shows nothing yet gets
        # until the page load timeout. amazon.de was still blank at 15 s.
        waiting = BANNER_WAIT
        if load_s is None and (not det.get('verdict') or det.get('verdict') == 'unsure'):
            waiting = LOAD_TIMEOUT
        if tick - t0 >= waiting or time.monotonic() > deadline - 30:
            break
        time.sleep(max(0.0, 1.0 - (time.monotonic() - tick)))
    if load_s is None and b.mn.ready(nav):
        load_s = round(time.monotonic() - t0, 1)
    try:
        b.mn.replies.pop(nav, None)
    except AttributeError:
        pass
    return seen, last, polls, load_s


def geometry_of(loc):
    return (tuple(int(round(v)) for v in loc['inner_screen']), tuple(loc['viewport']))


def visit_site(url, correction, deadline):
    out = {'url': url, 'final_url': '', 'title': '', 'banner': 'no', 'banner_appears_s': '', 'blocking': '',
           'cmps': '', 'accept': 'n/a', 'accept_clicks': '', 'accept_labels': '', 'reject': 'n/a',
           'reject_clicks': '', 'reject_labels': '', 'reject_steps': '', 'no_reject': '', 'subscribe_offer': '',
           'inner_screen': '', 'viewport': '', 'evidence': [], 'shots': {}, 'geometry': [], 'timing': {},
           'candidates': {}, 'detect': {}}
    ev = out['evidence']
    sites = {site_of(url)}

    def note_geometry(loc, when):
        g = geometry_of(loc)
        out['geometry'].append([when, list(g[0]), list(g[1])])
        if loc.get('dpr') != 1:
            ev.append(f'devicePixelRatio is {loc.get("dpr")} at {when}')

    def cand_summary(loc):
        return [{k: c[k] for k in ('kind', 'label', 'sx', 'sy', 'path', 'holder', 'strength')} for c in loc['cands']][:40]

    # ---- visit A: banner and accept
    t = time.monotonic()
    b = None
    try:
        b = Browser(url, 'a', deadline)
        seen, det, polls, load_s = wait_for_banner(b, url, deadline)
        out['timing']['a_load_s'] = load_s
        out['detect']['a'] = {k: det.get(k) for k in ('verdict', 'evidence', 'cmps', 'scroll_locked', 'viewport')}
        out['final_url'], out['title'] = det.get('url', ''), det.get('title', '')
        out['cmps'] = ';'.join(det.get('cmps') or [])
        if seen is not None:
            out['banner'], out['banner_appears_s'] = 'yes', seen
            time.sleep(SETTLE)
        else:
            out['banner'] = 'unsure' if det.get('verdict') == 'unsure' else 'no'
        sites.add(site_of(det.get('url') or url))
        out['shots']['banner'] = screenshot(os.path.join(WORK, 'shot.png'))
        loc = locate(b, correction)
        note_geometry(loc, 'A banner')
        out['inner_screen'] = '%d,%d' % geometry_of(loc)[0]
        out['viewport'] = '%d,%d' % geometry_of(loc)[1]
        out['candidates']['a'] = cand_summary(loc)
        actionable = [c for c in loc['cands'] if c['kind'] in ('accept', 'reject', 'save', 'settings', 'subscribe')]
        if out['banner'] == 'no' or (out['banner'] == 'unsure' and not actionable):
            out['accept'] = out['reject'] = 'n/a'
            out['blocking'] = ''
            out['timing']['a_s'] = round(time.monotonic() - t, 1)
            return out
        if out['banner'] == 'unsure':
            ev.append('detector unsure, but a consent UI with buttons is on top')
        out['blocking'] = 'yes' if (loc['scroll_locked'] or loc['cover_pct'] >= 30) else 'no'
        ev.append(f"blocking: scroll {'locked' if loc['scroll_locked'] else 'free'}, consent UI covers {loc['cover_pct']}%")
        holders_accept = {c['holder'] for c in loc['cands'] if c['kind'] == 'accept'}
        offer = [c for c in loc['cands'] if c['kind'] == 'subscribe' and c['holder'] in holders_accept]
        price = [p for h, p in loc['price_offer'].items() if h in holders_accept or (h == 'top' and holders_accept)]
        out['subscribe_offer'] = 'yes' if (offer or price) else 'no'
        if offer:
            ev.append(f'offer: "{offer[0]["label"]}"')
        elif price:
            ev.append(f'price in the consent UI: "{price[0]}"')
        for n in loc['notes'][:3]:
            ev.append('A: ' + n)
        if not [c for c in loc['cands'] if c['kind'] == 'accept']:
            out['accept'] = 'not-found'
            ev.append('accept: no accept button found' + (f" (labels seen: {', '.join(c['label'] for c in loc['cands'][:5])})"
                                                           if loc['cands'] else ''))
        else:
            status, clicks, labels, kinds, st = run_path(b, loc, 'accept', correction, sites, WORK, deadline, ev)
            out['accept'] = status
            out['accept_clicks'] = ';'.join(f'{x},{y}' for x, y in clicks)
            out['accept_labels'] = ' | '.join(labels)
            if st and st['loc']:
                note_geometry(st['loc'], 'A after accept')
            if st and status != 'verified' and st['loc']:
                left = [c['label'] for c in st['loc']['cands'] if c['kind'] in ('accept', 'reject', 'save')]
                ev.append(f"accept: after the click the detector says {st['det'].get('verdict')}"
                          + (f", buttons still on top: {', '.join(left[:3])}" if left else ''))
            out['shots']['accept'] = screenshot(os.path.join(WORK, 'shot.png'))
    except Exception as e:  # noqa: BLE001 - one site's error, not the run's
        ev.append(f'error in visit A: {type(e).__name__}: {e}'[:300])
        if not out['inner_screen']:
            # The page was never looked at, so there is nothing to go on.
            out['banner'] = 'unsure'
            out['accept'] = out['reject'] = 'failed'
            return out
        if out['accept'] == 'n/a':
            out['accept'] = 'failed'
    finally:
        if b:
            b.quit()
        out['timing']['a_s'] = round(time.monotonic() - t, 1)

    # ---- visit B: reject
    t = time.monotonic()
    b = None
    reject_loc = None
    try:
        # Some sites show the banner only on some visits (linkedin.com showed
        # its cookie bar in one of two). A visit B without any consent button
        # is tried once more in a new profile.
        for attempt in (1, 2):
            b = Browser(url, 'b', deadline)
            seen, det, polls, load_s = wait_for_banner(b, url, deadline)
            out['timing']['b_load_s'] = load_s
            out['detect']['b'] = {k: det.get(k) for k in ('verdict', 'evidence')}
            if seen is None:
                ev.append(f"B: no banner within {BANNER_WAIT:.0f} s, detector says {det.get('verdict')}")
            else:
                time.sleep(SETTLE)
            sites.add(site_of(det.get('url') or url))
            out['shots']['banner_b'] = screenshot(os.path.join(WORK, 'shot.png'))
            loc = reject_loc = locate(b, correction)
            useful = [c for c in loc['cands'] if c['kind'] in ('accept', 'reject', 'save', 'settings')]
            if useful or attempt == 2 or time.monotonic() > deadline - 60:
                break
            ev.append('B: no consent buttons, trying visit B again')
            b.quit()
            b = None
        note_geometry(loc, 'B banner')
        out['candidates']['b'] = cand_summary(loc)
        for n in loc['notes'][:3]:
            ev.append('B: ' + n)
        status, clicks, labels, kinds, st = run_path(b, loc, 'reject', correction, sites, WORK, deadline, ev)
        out['reject'] = status
        out['reject_clicks'] = ';'.join(f'{x},{y}' for x, y in clicks)
        out['reject_labels'] = ' | '.join(labels)
        if status == 'verified':
            out['reject_steps'] = len(clicks)
        if st and st['loc']:
            note_geometry(st['loc'], 'B after reject')
        if status == 'failed' and st and st['loc']:
            left = [c['label'] for c in st['loc']['cands'] if c['kind'] in ('accept', 'reject', 'save', 'settings')]
            ev.append(f"reject: after {len(clicks)} click(s) the detector says {st['det'].get('verdict')}"
                      + (f", on top: {', '.join(left[:4])}" if left else ''))
        if status != 'verified':
            out['no_reject'] = classify_no_reject(b, out, reject_loc, correction, sites, deadline, ev)
        if clicks or out['no_reject']:
            out['shots']['reject'] = screenshot(os.path.join(WORK, 'shot.png'))
    except Exception as e:  # noqa: BLE001
        ev.append(f'error in visit B: {type(e).__name__}: {e}'[:300])
        if out['reject'] == 'n/a':
            out['reject'] = 'failed'
    finally:
        if b:
            b.quit()
        out['timing']['b_s'] = round(time.monotonic() - t, 1)
    return out


def classify_no_reject(b, out, loc, correction, sites, deadline, ev):
    """Why there is no way to reject: pay-or-consent, consent-wall, ignorable, not-found."""
    if not loc or not loc['cands']:
        ev.append('no_reject: no consent buttons found in visit B')
        return 'not-found'
    if out['subscribe_offer'] == 'yes' or any(c['kind'] == 'subscribe' for c in loc['cands']):
        out['subscribe_offer'] = 'yes'
        return 'pay-or-consent'
    if out['blocking'] != 'yes':
        return 'ignorable'
    # Blocking, but maybe it can be closed. Try the close button, if there is
    # one, and see whether the UI goes away. The click is not a reject path
    # and its coordinates go to the evidence only.
    if out['reject'] == 'not-found':
        closes = sorted((c for c in loc['cands'] if c['kind'] == 'close' and not c['href_away']), key=rank, reverse=True)
        if closes and time.monotonic() < deadline - 15:
            c = closes[0]
            xclick(c['sx'], c['sy'])
            time.sleep(AFTER_FINAL)
            st = look(b, correction, sites)
            if not st['error'] and not st['popup'] and not st['away'] and settled(st, c['holder']):
                ev.append(f"no_reject: blocking, but \"{c['label']}\" at {c['rx']},{c['ry']} closes it")
                return 'ignorable'
            ev.append(f"no_reject: \"{c['label']}\" at {c['rx']},{c['ry']} did not close it")
    return 'consent-wall'


# --- geometry check -------------------------------------------------------------------

GEOM_HTML = ('<!doctype html><html style="margin:0;background:#ff00ff">'
             '<body style="margin:0;background:#ff00ff"></body></html>')


def magenta_box():
    """The bounding box of pure magenta on the X screen."""
    path = os.path.join(WORK, 'geom.rgb')
    subprocess.run(['import', '-silent', '-window', 'root', '-depth', '8', 'rgb:' + path], env=xenv(),
                   check=True, timeout=30)
    with open(path, 'rb') as f:
        data = f.read()
    os.remove(path)
    w, h = SCREEN
    xs, ys = [], []
    px = b'\xff\x00\xff'
    for y in range(h):
        row = data[y * w * 3:(y + 1) * w * 3]
        first = next((i // 3 for i in range(0, len(row), 3) if row[i:i + 3] == px), None) if px in row else None
        if first is None:
            continue
        last = next(i // 3 for i in range(len(row) - 3, -1, -3) if row[i:i + 3] == px)
        ys.append(y)
        xs += [first, last]
    if not ys:
        return None
    return [min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1]


def geometry_check():
    os.makedirs(WORK, exist_ok=True)
    page = os.path.join(WORK, 'geom.html')
    with open(page, 'w') as f:
        f.write(GEOM_HTML)
    url = 'file://' + page
    res = {'window_plain': None, 'window_marionette': None}

    # 1. As the benchmark starts it: no Marionette, the URL on the command line.
    profile = os.path.join(WORK, 'profile-geom-plain')
    shutil.rmtree(profile, ignore_errors=True)
    env = dict(xenv(), PARROT_URL=url)
    env.pop('PARROT_CONSENT', None)
    env.pop('MOZ_MARIONETTE', None)
    subprocess.run(['bash', os.path.join(COMMON, 'setup-profile.sh'), 'firefox', profile], env=env,
                   capture_output=True, check=True, timeout=60)
    proc = subprocess.Popen(['bash', os.path.join(COMMON, 'launch-browser.sh'), 'firefox', profile, url],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        time.sleep(10)
        position_window()
        time.sleep(1)
        res['window_plain'] = window_geometry()
        res['plain_box'] = magenta_box()
        res['plain_shot'] = screenshot(os.path.join(WORK, 'shot.png'))
    finally:
        kill_firefox(proc, profile)
        shutil.rmtree(profile, ignore_errors=True)

    # 2. As this tool drives it.
    b = Browser(url, 'geom', time.monotonic() + 60)
    try:
        b.mn.call('WebDriver:Navigate', {'url': url})
        time.sleep(2)
        js = b.js('return [mozInnerScreenX, mozInnerScreenY, innerWidth, innerHeight, devicePixelRatio]')
        res['marionette_js'] = js
        res['window_marionette'] = window_geometry()
        res['marionette_box'] = magenta_box()
        res['marionette_shot'] = screenshot(os.path.join(WORK, 'shot.png'))
    finally:
        b.quit()
    pb, mb = res['plain_box'], res['marionette_box']
    if pb and mb:
        res['correction'] = [pb[0] - int(js[0]), pb[1] - int(js[1])]
        res['js_matches_screen'] = mb[:2] == [int(js[0]), int(js[1])] and mb[2:] == [int(js[2]), int(js[3])]
        res['same'] = pb == mb
    return res


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--url')
    p.add_argument('--geometry-check', action='store_true')
    p.add_argument('--correction', default='0,0',
                   help='x,y added to every recorded click, from the geometry check (default 0,0)')
    p.add_argument('--budget', type=float, default=170, help='hard limit for the site in seconds')
    args = p.parse_args()
    started = time.monotonic()
    if args.geometry_check:
        result = geometry_check()
    elif args.url:
        correction = tuple(int(v) for v in args.correction.split(','))
        result = visit_site(args.url, correction, started + args.budget)
    else:
        p.error('give --url or --geometry-check')
    result['seconds'] = round(time.monotonic() - started, 1)
    sys.stdout.write(json.dumps(result) + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
