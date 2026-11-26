#!/usr/bin/env python3
"""Find the screen coordinates of every site's consent "accept" and "reject" buttons.

    banner-scan/find-clicks.py sites.txt
    banner-scan/find-clicks.py sites.txt --limit 50 --jobs 6
    banner-scan/find-clicks.py --sites https://www.spiegel.de/,www.ikea.com/de/de/

Every site is opened in the benchmark's Firefox, in the benchmark's window
geometry, behind the benchmark's squid, in a fresh plain profile. When a consent
banner shows, banner-scan/buttons.js finds its buttons, and clicks.py clicks
"accept" with a real X click and checks that the banner went away. A second
visit in another fresh profile does the same for "reject", in at most three
clicks. Sites without a way to reject are sorted into pay-or-consent,
consent-wall, ignorable and not-found.

The coordinates in clicks.csv are screen coordinates of the 1440x900 Xvfb, for
Parrot to replay with xdotool in the measured runs. They only hold for that
geometry: Firefox pinned to 0,0 at 1440x900 without decorations, by
parrot/common/pin-windows.sh before the window manager starts.

HOW IT RUNS

One squid and --jobs worker containers on a private docker network, booted
like the measured container: firefox-policies.sh, pin-windows.sh Navigator
1440 900, then the image's entrypoint. First the geometry check runs once in
the first worker: it compares where the page starts on the screen in a Firefox
without Marionette with the Marionette-driven one, and applies the difference
to every recorded click if there is one. Then each worker gets one site at a
time through docker exec of banner-scan/clicks.py. Containers and the network
are removed at the end, also after Ctrl-C.

Results go to banner-scan/results/clicks-<list>-<UTC>/: clicks.csv,
clicks.jsonl (everything clicks.py saw), shots/ and index.html.
"""
import argparse
import base64
import csv
import datetime
import html
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from scan import (CA_FILE, PROXY, docker, duration, host_of, paint, read_entries,  # noqa: E402
                  scenario_images, to_url)

REPO = HERE.parent
CLICKS = '/tmp/repo/banner-scan/clicks.py'
SCREEN = (1440, 900)

FIELDS = ['position', 'url', 'final_url', 'title', 'banner', 'banner_appears_s', 'blocking', 'cmps', 'accept',
          'accept_clicks', 'accept_labels', 'reject', 'reject_clicks', 'reject_labels', 'reject_steps', 'no_reject',
          'subscribe_offer', 'inner_screen', 'viewport', 'evidence', 'shot_banner', 'shot_accept', 'shot_reject']


class Fleet:
    """The network, squid and the worker containers, booted like the measured run."""

    def __init__(self, tag, squid_image, browser_image, jobs):
        self.tag, self.jobs = tag, jobs
        self.squid_image, self.browser_image = squid_image, browser_image
        self.workers = [f'{tag}-w{i}' for i in range(1, jobs + 1)]

    def start(self):
        print(f'starting {self.squid_image} and {self.jobs} x {self.browser_image}', file=sys.stderr)
        docker('network', 'create', self.tag)
        docker('run', '-d', '--rm', '--name', f'{self.tag}-squid', '--network', self.tag,
               '--network-alias', 'squid', self.squid_image)
        # The order of the measured container's setup-commands. pin-windows.sh
        # has to run before the entrypoint, because fluxbox reads its apps file
        # once when it starts.
        boot = ('bash /tmp/repo/parrot/common/firefox-policies.sh >/tmp/policies.log 2>&1 '
                '&& bash /tmp/repo/parrot/common/pin-windows.sh Navigator 1440 900 >/tmp/pin.log 2>&1 '
                '&& bash /usr/local/bin/entrypoint.sh >/tmp/entrypoint.log 2>&1 '
                '&& touch /tmp/click-finder-ready && exec sleep infinity')
        env = {'PARROT_URL': 'about:blank', 'SCREEN_SIZE': '1440x900x24', 'RESOLUTION': '1440x900',
               'WINDOW_X': '0', 'WINDOW_Y': '0', 'WINDOW_WIDTH': '1440', 'WINDOW_HEIGHT': '900',
               'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8'}
        env_args = [a for k, v in env.items() for a in ('-e', f'{k}={v}')]
        for name in self.workers:
            docker('run', '-d', '--rm', '--name', name, '--network', self.tag, '--shm-size=2g', *env_args,
                   '--mount', f'type=bind,source={REPO},target=/tmp/repo,readonly',
                   '--entrypoint', 'bash', self.browser_image, '-c', boot)

    def stop(self):
        names = [f'{self.tag}-squid', *self.workers]
        subprocess.run(['docker', 'rm', '-f', *names], capture_output=True)
        subprocess.run(['docker', 'network', 'rm', self.tag], capture_output=True)

    def wait_ready(self, seconds=60):
        for name in self.workers:
            deadline = time.monotonic() + seconds
            while docker('exec', name, 'test', '-f', '/tmp/click-finder-ready', check=False).returncode:
                if time.monotonic() > deadline:
                    log = docker('exec', name, 'cat', '/tmp/pin.log', '/tmp/entrypoint.log', check=False).stdout
                    raise RuntimeError(f'{name} did not boot: {log.strip()[-400:]}')
                time.sleep(0.5)

    def wait_for_squid(self, seconds=90):
        deadline, last = time.monotonic() + seconds, ''
        while time.monotonic() < deadline:
            p = docker('exec', self.workers[0], 'curl', '-sS', '-o', '/dev/null', '-w', '%{http_code}',
                       '--max-time', '5', '--proxy', PROXY, '--cacert', CA_FILE,
                       'https://www.google.com', check=False)
            if p.returncode == 0 and p.stdout.strip() not in ('', '000'):
                return
            last = p.stderr.strip()
            time.sleep(1)
        raise RuntimeError(f'squid was not usable within {seconds}s: {last}')

    def alive(self, name):
        p = docker('inspect', '-f', '{{.State.Running}}', name, check=False)
        return p.stdout.strip() == 'true'


def exec_json(container, args, timeout):
    """docker exec clicks.py and return its JSON line."""
    cmd = ['docker', 'exec', container, 'python3', CLICKS, *args]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        # docker exec does not take its process down with it.
        docker('exec', container, 'pkill', '-KILL', '-f', 'banner-scan/clicks.py', check=False)
        docker('exec', container, 'pkill', '-KILL', '-f', '/tmp/click-finder/profile-', check=False)
        return {'error': f'hard timeout after {int(timeout)}s'}
    lines = [line for line in p.stdout.decode('utf-8', 'replace').splitlines() if line.startswith('{')]
    if not lines:
        return {'error': f'clicks.py exit {p.returncode}: {p.stderr.decode("utf-8", "replace").strip()[-300:]}'}
    return json.loads(lines[-1])


# --- results -----------------------------------------------------------------

def save_shot(out, rel, data):
    if not data:
        return ''
    (out / rel).write_bytes(base64.b64decode(data))
    return rel


def finish_site(site, res, out, expected):
    """Turn clicks.py's answer into a CSV row and write the screenshots."""
    pos, url = site['position'], site['url']
    if 'error' in res and 'banner' not in res:
        res = {'banner': 'unsure', 'accept': 'failed', 'reject': 'failed', 'evidence': [f"error: {res['error']}"],
               'shots': {}, 'geometry': []}
    stem = f'shots/{pos:04d}-{host_of(url)}'
    shots = res.pop('shots', {}) or {}
    files = {k: save_shot(out, f'{stem}-{k.replace("_", "-")}.png', v) for k, v in shots.items()}
    ev = list(res.get('evidence') or [])
    # The geometry has to be the one the measured run has, on every look.
    bad = [g for g in res.get('geometry', []) if expected and (tuple(g[1]) != expected[0] or tuple(g[2]) != expected[1])]
    if bad:
        ev.insert(0, 'GEOMETRY MISMATCH: ' + '; '.join(f'{g[0]} inner_screen {g[1][0]},{g[1][1]} viewport '
                                                       f'{g[2][0]}x{g[2][1]}' for g in bad[:3]))
    row = {k: res.get(k, '') for k in FIELDS}
    row.update(position=pos, url=url, evidence=' | '.join(ev), shot_banner=files.get('banner', ''),
               shot_accept=files.get('accept', ''), shot_reject=files.get('reject', ''))
    site.update(row=row, detail=dict(res, position=pos, shots=files, geometry_bad=bool(bad)))


def write_results(out, sites, meta):
    done = sorted((s for s in sites if s.get('row')), key=lambda s: s['position'])
    # errors='replace': page titles can hold lone surrogates, see scan.py.
    with (out / 'clicks.csv').open('w', newline='', encoding='utf-8', errors='replace') as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for s in done:
            w.writerow(s['row'])
    with (out / 'clicks.jsonl').open('w', encoding='utf-8', errors='replace') as f:
        for s in done:
            f.write(json.dumps(s['detail'], ensure_ascii=False) + '\n')
    (out / 'index.html').write_text(render_html(done, meta), encoding='utf-8', errors='replace')


CSS = '''
:root { color-scheme: light dark; --bg: #fafafa; --fg: #1b1b1b; --muted: #666; --card: #fff; --line: #ddd;
  --ok: #2e7d32; --bad: #c62828; --warn: #b26a00; --na: #757575; }
@media (prefers-color-scheme: dark) { :root { --bg: #161616; --fg: #e8e8e8; --muted: #9a9a9a; --card: #202020;
  --line: #333; --ok: #66bb6a; --bad: #ef5350; --warn: #ffb74d; --na: #9e9e9e; } }
* { box-sizing: border-box; }
body { margin: 0; padding: 16px; background: var(--bg); color: var(--fg);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
h1 { font-size: 20px; margin: 0 0 4px; }
h2 { font-size: 17px; margin: 28px 0 8px; padding-bottom: 4px; border-bottom: 2px solid var(--line); }
.meta { color: var(--muted); margin: 0 0 8px; }
.site { background: var(--card); border: 1px solid var(--line); border-radius: 6px; padding: 10px; margin: 10px 0; }
.head { display: flex; flex-wrap: wrap; gap: 6px 12px; align-items: baseline; }
.pos { color: var(--muted); font-variant-numeric: tabular-nums; }
.host { font-weight: 600; font-size: 15px; }
.badge { display: inline-block; padding: 0 6px; border-radius: 3px; font-size: 12px; font-weight: 600; color: #fff; }
.b-verified, .b-yes { background: var(--ok); } .b-failed { background: var(--bad); }
.b-not-found, .b-unsure, .b-pay-or-consent, .b-consent-wall { background: var(--warn); }
.b-n\\/a, .b-no, .b-ignorable { background: var(--na); }
.info { color: var(--muted); font-size: 12.5px; word-break: break-word; }
.shots { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 10px; margin-top: 8px; }
.shots figure { margin: 0; }
.shots img { width: 100%; height: auto; border: 1px solid var(--line); display: block; background: #888; }
.shots figcaption { font-size: 12.5px; margin-bottom: 4px; }
.noshot { padding: 40px 8px; text-align: center; color: var(--muted); border: 1px dashed var(--line); }
code { font-size: 12.5px; }
ul.ev { font-size: 12px; color: var(--muted); margin: 4px 0 0; padding-left: 16px; }
table { border-collapse: collapse; font-size: 13px; } td, th { padding: 2px 10px 2px 0; text-align: left; }
'''


def badge(value):
    v = str(value or 'n/a')
    return f'<span class="badge b-{html.escape(v)}">{html.escape(v)}</span>'


def render_html(sites, meta):
    esc = html.escape
    g = meta.get('geometry') or {}
    banner_sites = [s for s in sites if s['row']['accept'] != 'n/a' or s['row']['reject'] != 'n/a']
    other = [s for s in sites if s not in banner_sites]
    parts = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width, initial-scale=1">',
             f'<title>Click finder {esc(meta["name"])}</title><style>{CSS}</style></head><body>',
             f'<h1>Click finder: {esc(meta["name"])}</h1>',
             f'<p class="meta">{len(sites)} sites, {esc(meta["started"])} UTC, {meta["jobs"]} in parallel, '
             f'{esc(meta["elapsed"])}. Firefox in {esc(meta["browser_image"])} behind {esc(meta["squid_image"])}, '
             f'fresh plain profile per visit, window pinned to 0,0 1440x900.</p>']
    if g:
        parts.append('<p class="meta">Geometry check: page content starts at '
                     f'{esc(str(g.get("plain_box")))} (x, y, w, h) without Marionette and at '
                     f'{esc(str(g.get("marionette_box")))} with it. mozInnerScreen and inner size '
                     f'{esc(str(g.get("marionette_js")))}. Correction applied to recorded clicks: '
                     f'{esc(str(g.get("correction")))}.</p>')
    counts = Counter((s['row']['accept'], s['row']['reject']) for s in banner_sites)
    parts.append('<table><tr><th>accept</th><th>reject</th><th>sites</th></tr>' + ''.join(
        f'<tr><td>{badge(a)}</td><td>{badge(r)}</td><td>{n}</td></tr>' for (a, r), n in counts.most_common()) + '</table>')
    parts.append(f'<h2>Sites with a consent UI ({len(banner_sites)})</h2>')
    for s in banner_sites:
        r = s['row']
        d = s['detail']
        parts.append(f'<div class="site" id="s{r["position"]}"><div class="head">'
                     f'<span class="pos">#{r["position"]}</span>'
                     f'<a class="host" href="{esc(r["url"])}">{esc(host_of(r["url"]))}</a>'
                     f'banner {badge(r["banner"])} accept {badge(r["accept"])} reject {badge(r["reject"])}'
                     + (f' {badge(r["no_reject"])}' if r['no_reject'] else '')
                     + (f' <span class="info">subscribe offer</span>' if r['subscribe_offer'] == 'yes' else '')
                     + (' <span class="badge b-failed">geometry</span>' if d.get('geometry_bad') else '')
                     + '</div>')
        parts.append(f'<div class="info">{esc(r["title"] or "(no title)")} | {esc(r["final_url"] or "-")} | '
                     f'CMPs: {esc(r["cmps"] or "-")} | banner after {esc(str(r["banner_appears_s"] or "-"))} s | '
                     f'blocking {esc(r["blocking"] or "-")}</div>')
        shots = d.get('shots', {})
        figs = [('banner', 'Before (visit A)', ''),
                ('accept', 'After accept', f'{r["accept_labels"]} at {r["accept_clicks"]}'),
                ('banner-b', 'Before (visit B)', ''),
                ('reject', 'After reject', f'{r["reject_labels"]} at {r["reject_clicks"]}'
                 + (f', {r["reject_steps"]} step(s)' if r['reject_steps'] else ''))]
        parts.append('<div class="shots">')
        for key, cap, what in figs:
            rel = shots.get(key) or shots.get(key.replace('-', '_'))
            what = what if what.strip(' at,') else ''
            parts.append(f'<figure><figcaption>{esc(cap)}{": <code>" + esc(what) + "</code>" if what else ""}'
                         '</figcaption>')
            parts.append(f'<a href="{esc(rel)}"><img loading="lazy" src="{esc(rel)}" alt="{esc(cap)}"></a>'
                         if rel else '<div class="noshot">no screenshot</div>')
            parts.append('</figure>')
        parts.append('</div>')
        if r['evidence']:
            parts.append('<ul class="ev">' + ''.join(f'<li>{esc(e)}</li>' for e in r['evidence'].split(' | ')) + '</ul>')
        parts.append('</div>')
    parts.append(f'<h2>No consent UI ({len(other)})</h2><table>')
    for s in other:
        r = s['row']
        shot = s['detail'].get('shots', {}).get('banner', '')
        parts.append(f'<tr><td class="pos">#{r["position"]}</td><td><a href="{esc(r["url"])}">'
                     f'{esc(host_of(r["url"]))}</a></td><td>{badge(r["banner"])}</td>'
                     f'<td>{f"<a href={chr(34)}{esc(shot)}{chr(34)}>screenshot</a>" if shot else ""}</td>'
                     f'<td class="info">{esc(r["evidence"][:200])}</td></tr>')
    parts.append('</table></body></html>\n')
    return '\n'.join(parts)


# --- the run ------------------------------------------------------------------

def run_list(fleet, ready, name, entries, args, out, meta, correction, expected):
    (out / 'shots').mkdir(parents=True, exist_ok=True)
    sites = [{'position': i, 'input': e, 'url': to_url(e)} for i, e in entries]
    work = queue.Queue()
    for s in sites:
        work.put(s)
    lock = threading.Lock()
    counts = Counter()
    started = time.monotonic()
    stop = threading.Event()
    tty = sys.stderr.isatty()

    def progress():
        done = sum(counts.values())
        line = (f'{name}: {done}/{len(sites)}  ' + '  '.join(f'{k} {counts[k]}' for k in sorted(counts))
                + f'  {duration(time.monotonic() - started)}')
        sys.stderr.write(f'\r\x1b[K{line}' if tty else f'{line}\n')
        sys.stderr.flush()

    def run(container):
        while not stop.is_set():
            try:
                site = work.get_nowait()
            except queue.Empty:
                return
            t = time.monotonic()
            res = exec_json(container, ['--url', site['url'], '--correction', '%d,%d' % correction,
                                        '--budget', str(args.budget)], args.budget + 40)
            res['seconds'] = round(time.monotonic() - t, 1)
            finish_site(site, res, out, expected)
            if stop.is_set():
                return
            with lock:
                r = site['row']
                key = 'no banner' if r['accept'] == 'n/a' and r['reject'] == 'n/a' else f"reject {r['reject']}"
                counts[key] += 1
                progress()
            if not fleet.alive(container):
                print(f'\n{container} is gone, its remaining sites go to the others', file=sys.stderr)
                return

    threads = [threading.Thread(target=run, args=(c,), daemon=True) for c in ready]
    for t in threads:
        t.start()
    try:
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(0.5)
    except KeyboardInterrupt:
        stop.set()
        meta['elapsed'] = duration(time.monotonic() - started) + ' (interrupted)'
        write_results(out, sites, meta)
        raise
    if tty:
        sys.stderr.write('\n')
    meta['elapsed'] = duration(time.monotonic() - started)
    write_results(out, sites, meta)
    return [s for s in sites if s.get('row')], time.monotonic() - started


def report(name, sites, elapsed, jobs, out, meta):
    rows = [s['row'] for s in sites]
    print(f'\n{paint("click finder: " + name, "1")}')
    print(f'  {len(rows)} sites, {jobs} in parallel, {duration(elapsed)}')
    secs = [s['detail'].get('seconds') for s in sites if isinstance(s['detail'].get('seconds'), (int, float))]
    if secs:
        print(f'  {sum(secs) / len(secs):.1f} s per site on average, {len(secs) * 1.0 / max(elapsed, 1) * 60:.1f} sites per minute')
    g = meta.get('geometry') or {}
    print(f'  geometry: page at {g.get("marionette_js")} (mozInnerScreenX/Y, innerWidth/Height), '
          f'without Marionette at {g.get("plain_box")}, correction {g.get("correction")}')
    try:
        print(f'  results: {out.relative_to(Path.cwd())}/index.html')
    except ValueError:
        print(f'  results: {out}/index.html')
    print()
    bad = [r for r in rows if r['evidence'].startswith('GEOMETRY MISMATCH')]
    if bad:
        print(paint(f'  GEOMETRY MISMATCH on {len(bad)} sites, their clicks are not valid for the measured run:', '31'))
        for r in bad[:10]:
            print(f'    {host_of(r["url"])}: {r["evidence"].split(" | ")[0]}')
        print()

    def line(title, items, colour=None):
        hosts = [host_of(r['url']) for r in items]
        ex = ', '.join(hosts[:5]) + (f' +{len(hosts) - 5}' if len(hosts) > 5 else '')
        print(f'  {paint(title.ljust(26), colour)} {len(items):>5}  {ex}')

    banner = [r for r in rows if r['accept'] != 'n/a' or r['reject'] != 'n/a']
    line('No consent UI', [r for r in rows if r not in banner], '2')
    line('Consent UI', banner, '1')
    for k, c in (('verified', '32'), ('failed', '31'), ('not-found', '33')):
        line(f'  accept {k}', [r for r in banner if r['accept'] == k], c)
    for k, c in (('verified', '32'), ('failed', '31'), ('not-found', '33')):
        line(f'  reject {k}', [r for r in banner if r['reject'] == k], c)
    for k in ('pay-or-consent', 'consent-wall', 'ignorable', 'not-found'):
        line(f'  no reject: {k}', [r for r in banner if r['no_reject'] == k], '33')
    print()


def main():
    p = argparse.ArgumentParser(description='Find and prove the click coordinates of consent buttons.',
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument('lists', nargs='*', type=Path,
                   help='one site per line, a domain or a URL. Blank lines and # comments are ignored')
    p.add_argument('--sites', help='comma separated sites. Alone, they are the list. With lists, '
                                   'only these entries of the lists are run')
    p.add_argument('--jobs', type=int, default=4, help='worker containers, one Firefox each (default 4)')
    p.add_argument('--limit', type=int, help='only the first N entries of each list')
    p.add_argument('--budget', type=float, default=170, help='hard limit per site in seconds (default 170)')
    p.add_argument('--out-dir', type=Path, default=HERE / 'results',
                   help='where the result directories go (default banner-scan/results)')
    args = p.parse_args()
    if not shutil.which('docker'):
        sys.exit('docker is not on PATH')

    wanted = [s.strip() for s in (args.sites or '').split(',') if s.strip()]
    lists = []
    if not args.lists:
        if not wanted:
            sys.exit('give a list file or --sites')
        lists.append(('sites', list(enumerate(wanted, 1))))
    for path in args.lists:
        if not path.is_file():
            sys.exit(f'no such list: {path}')
        entries = list(enumerate(read_entries(path), 1))
        if wanted:
            keys = {host_of(to_url(w)) for w in wanted} | set(wanted)
            entries = [(i, e) for i, e in entries if e in keys or host_of(to_url(e)) in keys]
        if args.limit:
            entries = entries[:args.limit]
        if not entries:
            sys.exit(f'no entries in {path}')
        lists.append((path.stem, entries))

    squid_image, browser_image = scenario_images()
    fleet = Fleet(f'click-finder-{os.getpid()}', squid_image, browser_image, args.jobs)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    signal.signal(signal.SIGHUP, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        fleet.start()
        fleet.wait_ready()
        fleet.wait_for_squid()
        print('geometry check: Firefox with and without Marionette (about 25 s)', file=sys.stderr)
        geo = exec_json(fleet.workers[0], ['--geometry-check'], 150)
        if 'error' in geo or not geo.get('marionette_js') or not geo.get('plain_box'):
            raise RuntimeError(f'geometry check failed: {str(geo)[:300]}')
        js = geo['marionette_js']
        correction = tuple(geo.get('correction') or (0, 0))
        expected = ((int(js[0]), int(js[1])), (int(js[2]), int(js[3])))
        if js[4] != 1:
            raise RuntimeError(f'devicePixelRatio is {js[4]}, the coordinates would be wrong')
        msg = (f'  without Marionette the page starts at {geo["plain_box"][:2]}, with it at '
               f'{geo["marionette_box"][:2]} (mozInnerScreen {js[0]},{js[1]}, inner {js[2]}x{js[3]})')
        print(msg, file=sys.stderr)
        if correction != (0, 0):
            print(paint(f'  the two differ, every recorded click is shifted by {correction}', '31'), file=sys.stderr)
        for name, entries in lists:
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S')
            out = (args.out_dir / f'clicks-{name}-{stamp}').resolve()
            (out / 'shots').mkdir(parents=True, exist_ok=True)
            geo_meta = {k: v for k, v in geo.items() if not k.endswith('_shot')}
            for k in ('plain_shot', 'marionette_shot'):
                if geo.get(k):
                    save_shot(out, f'shots/geometry-{k.split("_")[0]}.png', geo[k])
            meta = {'name': name, 'started': stamp, 'jobs': len(fleet.workers), 'squid_image': squid_image,
                    'browser_image': browser_image, 'elapsed': '', 'geometry': geo_meta}
            (out / 'geometry.json').write_text(json.dumps(geo_meta, indent=1) + '\n')
            sites, elapsed = run_list(fleet, fleet.workers, name, entries, args, out, meta, correction, expected)
            report(name, sites, elapsed, len(fleet.workers), out, meta)
    except KeyboardInterrupt:
        print('\ninterrupted, results so far are written, removing the containers', file=sys.stderr)
        return 130
    except RuntimeError as e:
        print(f'\n{e}', file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGHUP, signal.SIG_DFL)
        fleet.stop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
