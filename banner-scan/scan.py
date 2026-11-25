#!/usr/bin/env python3
"""Find the sites on a list that show a cookie banner, with and without Consent-O-Matic.

    banner-scan/scan.py sites.txt
    banner-scan/scan.py sites.txt --limit 50 --jobs 6
    banner-scan/scan.py --sites https://www.spiegel.de/,www.ikea.com/de/de/

Every site is opened twice in the benchmark's Firefox, behind the benchmark's
squid: once in a plain profile and once in a profile with Consent-O-Matic, each
fresh. After the load event and a settle time the page is screenshotted and
banner-scan/detect.js decides whether a consent banner or another overlay covers
it. One directory per list goes to banner-scan/results/, with scan.csv, the
screenshots and index.html, a contact sheet that starts with the sites whose
banner the add-on does not handle.

HOW IT RUNS

One squid (greencoding/squid_reverse_proxy, as in the scenarios) and --jobs
worker containers (ribalba/parrot-browsers) on a private docker network. Each
worker container starts its Xvfb, writes Firefox's policies, and builds the
Consent-O-Matic template profile once with parrot/common/make-template-profile.sh.
Then scan.py hands it one site and variant at a time with docker exec of
banner-scan/worker.py, which drives Firefox through Marionette and prints one
JSON line. A visit that hangs is killed after a hard timeout and recorded as an
error, and the scan goes on. Containers and the network are removed at the end,
also after Ctrl-C.

WHAT IT CANNOT SEE is listed in banner-scan/README.md.
"""
import argparse
import base64
import csv
import datetime
import html
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SCENARIO = REPO / 'parrot' / 'usage_scenario.yml'
CA_FILE = '/tmp/repo/parrot/squid-ca.crt'
PROXY = 'http://squid:3128'
WORKER = '/tmp/repo/banner-scan/worker.py'

VARIANTS = ('plain', 'com')
VARIANT_NAMES = {'plain': 'plain Firefox', 'com': 'with Consent-O-Matic'}

# Contact sheet sections, in the order they appear.
GROUPS_BOTH = [
    ('remains', 'Banner remains with the add-on', 'needs fixing'),
    ('unsure-com', 'Unsure with the add-on', 'look at the screenshot'),
    ('handled', 'Banner handled by the add-on', 'plain shows a banner or overlay, the add-on run is clean'),
    ('none', 'No banner', 'clean in both'),
    ('error', 'Errors', 'a visit failed, see the error column'),
]
GROUPS_ONE = [
    ('banner', 'Banner', ''),
    ('unsure', 'Unsure', 'look at the screenshot'),
    ('none', 'No banner', ''),
    ('error', 'Errors', 'the visit failed'),
]
COLOURS = {'banner': '31', 'unsure': '33', 'none': '32', 'error': '2',
           'remains': '31', 'unsure-com': '33', 'handled': '36'}
COLOUR = sys.stdout.isatty() and not os.environ.get('NO_COLOR')


def paint(text, code):
    return f'\x1b[{code}m{text}\x1b[0m' if COLOUR and code else text


# --- input ------------------------------------------------------------------

def read_entries(path):
    """Same format as sites.txt: blank lines and # comments ignored."""
    entries = []
    for raw in Path(path).read_text(encoding='utf-8').splitlines():
        line = raw.split('#', 1)[0].strip()
        if line:
            entries.append(line)
    return entries


def to_url(entry):
    return entry if re.match(r'^[a-z][a-z0-9+.-]*://', entry, re.I) else f'https://{entry}/'


def host_of(url):
    return (urlsplit(url).hostname or 'site').lower()


def scenario_images():
    """The squid and browser images, read from parrot/usage_scenario.yml."""
    text = SCENARIO.read_text(encoding='utf-8')
    found = {}
    for service in ('squid', 'window-container'):
        m = re.search(rf'^  {re.escape(service)}:\s*\n\s+image:\s*(\S+)', text, re.M)
        if not m:
            sys.exit(f'could not read the {service} image from {SCENARIO}')
        found[service] = m.group(1)
    return found['squid'], found['window-container']


# --- containers ---------------------------------------------------------------

def docker(*args, check=True, timeout=120):
    p = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=timeout)
    if check and p.returncode:
        raise RuntimeError(f'docker {args[0]} failed: {p.stderr.strip()[-400:]}')
    return p


class Fleet:
    """The network, squid and the worker containers of one scan."""

    def __init__(self, tag, squid_image, browser_image, jobs):
        self.tag, self.jobs = tag, jobs
        self.squid_image, self.browser_image = squid_image, browser_image
        self.workers = [f'{tag}-w{i}' for i in range(1, jobs + 1)]

    def start(self):
        print(f'starting {self.squid_image} and {self.jobs} x {self.browser_image}', file=sys.stderr)
        docker('network', 'create', self.tag)
        docker('run', '-d', '--rm', '--name', f'{self.tag}-squid', '--network', self.tag,
               '--network-alias', 'squid', self.squid_image)
        # Firefox's content processes map shared memory, and Docker's default
        # /dev/shm of 64 MB is too small for a heavy page.
        boot = ('bash /usr/local/bin/entrypoint.sh >/tmp/entrypoint.log 2>&1 '
                '&& bash /tmp/repo/parrot/common/firefox-policies.sh >/tmp/policies.log 2>&1 '
                '&& exec sleep infinity')
        for name in self.workers:
            docker('run', '-d', '--rm', '--name', name, '--network', self.tag, '--shm-size=2g',
                   '-e', 'SCREEN_SIZE=1440x900x24', '-e', 'LANG=C.UTF-8', '-e', 'LC_ALL=C.UTF-8',
                   '--mount', f'type=bind,source={REPO},target=/tmp/repo,readonly',
                   '--entrypoint', 'bash', self.browser_image, '-c', boot)

    def stop(self):
        names = [f'{self.tag}-squid', *self.workers]
        subprocess.run(['docker', 'rm', '-f', *names], capture_output=True)
        subprocess.run(['docker', 'network', 'rm', self.tag], capture_output=True)

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

    def prepare(self, name):
        """Wait for the container's Xvfb and policies, then build the template."""
        for _ in range(60):
            p = docker('exec', name, 'test', '-f', '/opt/firefox/distribution/policies.json', check=False)
            if p.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError(f'{name}: Firefox policies were not written')
        p = docker('exec', name, 'bash', '/tmp/repo/parrot/common/make-template-profile.sh',
                   check=False, timeout=180)
        if p.returncode:
            raise RuntimeError(f'{name}: make-template-profile.sh failed: {(p.stderr or p.stdout).strip()[-400:]}')
        return p.stdout.strip().splitlines()[-1] if p.stdout.strip() else ''

    def alive(self, name):
        p = docker('inspect', '-f', '{{.State.Running}}', name, check=False)
        return p.stdout.strip() == 'true'


# --- one visit -------------------------------------------------------------------

def visit(container, url, variant, args):
    budget = args.load_timeout + args.settle + 60
    cmd = ['docker', 'exec', container, 'python3', WORKER, '--url', url, '--variant', variant,
           '--settle', str(args.settle), '--load-timeout', str(args.load_timeout),
           '--budget', str(budget)]
    started = time.monotonic()
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=budget + 30)
    except subprocess.TimeoutExpired:
        # docker exec does not take its process down with it. Kill both the
        # worker and the Firefox it started, so the next visit starts clean.
        docker('exec', container, 'pkill', '-KILL', '-f', 'banner-scan/worker.py', check=False)
        docker('exec', container, 'pkill', '-KILL', '-f', '/tmp/banner-scan/profile-', check=False)
        return {'variant': variant, 'url': url, 'verdict': 'error',
                'error': f'hard timeout after {int(budget + 30)}s', 'evidence': [],
                'seconds': round(time.monotonic() - started, 1)}
    lines = [line for line in p.stdout.decode('utf-8', 'replace').splitlines() if line.startswith('{')]
    if not lines:
        err = p.stderr.decode('utf-8', 'replace').strip()[-300:]
        return {'variant': variant, 'url': url, 'verdict': 'error', 'evidence': [],
                'error': f'worker exit {p.returncode}: {err}', 'seconds': round(time.monotonic() - started, 1)}
    return json.loads(lines[-1])


# --- results ------------------------------------------------------------------

def group_of(site, variants):
    res = site['results']
    if any(res[v]['verdict'] == 'error' for v in variants):
        return 'error'
    if len(variants) == 1:
        return res[variants[0]]['verdict']
    if res['com']['verdict'] == 'banner':
        return 'remains'
    if res['com']['verdict'] == 'unsure':
        return 'unsure-com'
    if res['plain']['verdict'] in ('banner', 'unsure'):
        return 'handled'
    return 'none'


def max_coverage(det):
    if not det:
        return ''
    covers = [o.get('coverage', 0) for o in det.get('overlays', []) if not o.get('shell')]
    return max(covers) if covers else 0


def tcf_text(det):
    tcf = (det or {}).get('tcf')
    if not tcf:
        return ''
    return ' '.join(f'{k}={v}' for k, v in tcf.items() if v is not None)


def csv_fields(variants):
    fields = ['position', 'input', 'url', 'group', 'final_url', 'title']
    for v in variants:
        fields += [f'{v}_verdict', f'{v}_evidence', f'{v}_cmps', f'{v}_max_coverage', f'{v}_tcf',
                   f'{v}_scroll_locked', f'{v}_viewport', f'{v}_load_seconds', f'{v}_load_timeout', f'{v}_seconds',
                   f'{v}_error', f'{v}_shot']
    return fields


def csv_row(site, variants):
    first = site['results'][variants[-1]]
    row = {'position': site['position'], 'input': site['input'], 'url': site['url'],
           'group': site['group'], 'final_url': first.get('final_url', ''), 'title': first.get('title', '')}
    for v in variants:
        r = site['results'][v]
        det = r.get('detect') or {}
        row.update({
            f'{v}_verdict': r['verdict'],
            f'{v}_evidence': ' | '.join(r.get('evidence') or []),
            f'{v}_cmps': ';'.join(det.get('cmps', [])),
            f'{v}_max_coverage': max_coverage(det),
            f'{v}_tcf': tcf_text(det),
            f'{v}_scroll_locked': det.get('scroll_locked', ''),
            f'{v}_viewport': 'x'.join(str(n) for n in det.get('viewport', [])),
            f'{v}_load_seconds': r.get('load_seconds', ''),
            f'{v}_load_timeout': r.get('load_timeout', ''),
            f'{v}_seconds': r.get('seconds', ''),
            f'{v}_error': r.get('error', ''),
            f'{v}_shot': r.get('shot', ''),
        })
    return row


def write_results(out, sites, variants, meta):
    done = sorted((s for s in sites if s.get('group')), key=lambda s: s['position'])
    with (out / 'scan.csv').open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=csv_fields(variants))
        writer.writeheader()
        for site in done:
            writer.writerow(csv_row(site, variants))
    with (out / 'detect.jsonl').open('w', encoding='utf-8') as f:
        for site in done:
            f.write(json.dumps({'position': site['position'], 'url': site['url'], 'group': site['group'],
                                'results': site['results']}, ensure_ascii=False) + '\n')
    (out / 'index.html').write_text(render_html(done, variants, meta), encoding='utf-8')


CSS = '''
:root { color-scheme: light dark; --bg: #fafafa; --fg: #1b1b1b; --muted: #666; --card: #fff;
  --line: #ddd; --banner: #c62828; --unsure: #b26a00; --none: #2e7d32; --error: #757575; }
@media (prefers-color-scheme: dark) { :root { --bg: #161616; --fg: #e8e8e8; --muted: #9a9a9a;
  --card: #202020; --line: #333; --banner: #ef5350; --unsure: #ffb74d; --none: #66bb6a; --error: #9e9e9e; } }
* { box-sizing: border-box; }
body { margin: 0; padding: 16px; background: var(--bg); color: var(--fg);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
h1 { font-size: 20px; margin: 0 0 4px; }
h2 { font-size: 17px; margin: 32px 0 8px; padding-bottom: 4px; border-bottom: 2px solid var(--line); }
h2 small { font-weight: normal; color: var(--muted); }
.meta { color: var(--muted); margin: 0 0 12px; }
nav a { margin-right: 14px; }
.site { background: var(--card); border: 1px solid var(--line); border-radius: 6px; padding: 10px; margin: 10px 0; }
.head { display: flex; flex-wrap: wrap; gap: 6px 12px; align-items: baseline; }
.head .pos { color: var(--muted); font-variant-numeric: tabular-nums; }
.head .host { font-weight: 600; font-size: 15px; }
.badge { display: inline-block; padding: 0 6px; border-radius: 3px; font-size: 12px; font-weight: 600; color: #fff; }
.b-banner { background: var(--banner); } .b-unsure { background: var(--unsure); }
.b-none { background: var(--none); } .b-error { background: var(--error); }
.info { color: var(--muted); font-size: 12.5px; word-break: break-all; }
.shots { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 10px; margin-top: 8px; }
.shots figure { margin: 0; }
.shots img { width: 100%; height: auto; border: 1px solid var(--line); display: block; background: #888; }
.shots figcaption { font-size: 12.5px; margin-bottom: 4px; }
.ev { font-size: 12px; color: var(--muted); margin: 4px 0 0; padding-left: 16px; word-break: break-word; }
.noshot { padding: 40px 8px; text-align: center; color: var(--muted); border: 1px dashed var(--line); }
'''


def render_html(sites, variants, meta):
    groups = GROUPS_BOTH if len(variants) == 2 else GROUPS_ONE
    by_group = {key: [s for s in sites if s['group'] == key] for key, _, _ in groups}
    esc = html.escape
    parts = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width, initial-scale=1">',
             f'<title>Banner scan {esc(meta["name"])}</title><style>{CSS}</style></head><body>',
             f'<h1>Banner scan: {esc(meta["name"])}</h1>',
             f'<p class="meta">{len(sites)} sites, {esc(meta["started"])} UTC, '
             f'{esc(", ".join(VARIANT_NAMES[v] for v in variants))}, settle {meta["settle"]} s, '
             f'{meta["jobs"]} in parallel, {esc(meta["elapsed"])}. Firefox in {esc(meta["browser_image"])} '
             f'behind {esc(meta["squid_image"])}.</p><nav>']
    for key, title, _ in groups:
        parts.append(f'<a href="#{key}">{esc(title)} ({len(by_group[key])})</a>')
    parts.append('</nav>')
    for key, title, hint in groups:
        parts.append(f'<h2 id="{key}">{esc(title)} <small>{len(by_group[key])} sites'
                     f'{". " + esc(hint) if hint else ""}</small></h2>')
        for s in by_group[key]:
            parts.append(render_site(s, variants))
    parts.append('</body></html>\n')
    return '\n'.join(parts)


def render_site(site, variants):
    esc = html.escape
    res = site['results']
    first = res[variants[-1]]
    badges = ' '.join(f'<span class="badge b-{esc(res[v]["verdict"])}">{v}: {esc(res[v]["verdict"])}</span>'
                      for v in variants)
    out = [f'<div class="site" id="s{site["position"]}"><div class="head">'
           f'<span class="pos">#{site["position"]}</span>'
           f'<a class="host" href="{esc(site["url"])}">{esc(host_of(site["url"]))}</a> {badges}</div>',
           f'<div class="info">{esc(first.get("title") or "(no title)")}</div>',
           f'<div class="info">final URL: {esc(first.get("final_url") or "-")}</div>',
           '<div class="shots">']
    for v in variants:
        r = res[v]
        cap = f'{esc(VARIANT_NAMES[v])}: <span class="badge b-{esc(r["verdict"])}">{esc(r["verdict"])}</span>'
        det = r.get('detect') or {}
        if det.get('cmps'):
            cap += f' <span class="info">CMPs: {esc(", ".join(det["cmps"]))}</span>'
        out.append(f'<figure><figcaption>{cap}</figcaption>')
        if r.get('shot'):
            out.append(f'<a href="{esc(r["shot"])}"><img loading="lazy" src="{esc(r["shot"])}" '
                       f'alt="{esc(v)} screenshot"></a>')
        else:
            out.append('<div class="noshot">no screenshot</div>')
        lines = list(r.get('evidence') or [])
        if r.get('error'):
            lines.insert(0, 'error: ' + r['error'])
        if lines:
            out.append('<ul class="ev">' + ''.join(f'<li>{esc(line)}</li>' for line in lines) + '</ul>')
        out.append('</figure>')
    out.append('</div></div>')
    return '\n'.join(out)


# --- the scan -------------------------------------------------------------------

def duration(seconds):
    seconds = int(round(seconds))
    return f'{seconds // 60}m {seconds % 60:02d}s' if seconds >= 60 else f'{seconds}s'


def scan_list(fleet, ready, name, entries, args, variants, out, meta):
    shots = out / 'shots'
    shots.mkdir(parents=True, exist_ok=True)
    sites = [{'position': i, 'input': e, 'url': to_url(e), 'results': {}, 'group': ''}
             for i, e in entries]
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
        line = (f'{name}: {done}/{len(sites)}  ' +
                '  '.join(f'{k} {counts[k]}' for k in sorted(counts)) +
                f'  {duration(time.monotonic() - started)}')
        sys.stderr.write(f'\r\x1b[K{line}' if tty else f'{line}\n')
        sys.stderr.flush()

    def run(container):
        while not stop.is_set():
            try:
                site = work.get_nowait()
            except queue.Empty:
                return
            for v in variants:
                r = visit(container, site['url'], v, args)
                shot = r.pop('screenshot', '')
                if shot:
                    rel = f'shots/{site["position"]:04d}-{host_of(site["url"])}-{v}.png'
                    (out / rel).write_bytes(base64.b64decode(shot))
                    r['shot'] = rel
                site['results'][v] = r
                if stop.is_set():
                    return
            with lock:
                site['group'] = group_of(site, variants)
                counts[site['group']] += 1
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
        write_results(out, sites, variants, meta)
        raise
    if tty:
        sys.stderr.write('\n')
    meta['elapsed'] = duration(time.monotonic() - started)
    write_results(out, sites, variants, meta)
    return sites, time.monotonic() - started


def report(name, sites, variants, elapsed, jobs, out):
    groups = GROUPS_BOTH if len(variants) == 2 else GROUPS_ONE
    counts = Counter(s['group'] for s in sites)
    print(f'\n{paint("banner scan: " + name, "1")}')
    print(f'  {len(sites)} sites, {jobs} in parallel, {duration(elapsed)}')
    per = [s['results'][v].get('seconds') for s in sites for v in variants
           if isinstance(s['results'].get(v, {}).get('seconds'), (int, float))]
    if per:
        print(f'  {sum(per) / len(per):.1f} s per visit on average, {len(variants)} visits per site')
    try:
        print(f'  results: {out.relative_to(Path.cwd())}/index.html')
    except ValueError:
        print(f'  results: {out}/index.html')
    print()
    width = max(len(t) for _, t, _ in groups)
    for key, title, _ in groups:
        hosts = [host_of(s['url']) for s in sites if s['group'] == key]
        examples = ', '.join(hosts[:4]) + (f' +{len(hosts) - 4}' if len(hosts) > 4 else '')
        print(f'  {paint(title.ljust(width), COLOURS.get(key))}'
              f'  {counts[key]:>5}  {examples}')
    print()


def main():
    p = argparse.ArgumentParser(
        description='Find consent banners on a list of sites, in plain Firefox and with Consent-O-Matic.',
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument('lists', nargs='*', type=Path,
                   help='one site per line, a domain or a URL. Blank lines and # comments are ignored')
    p.add_argument('--sites', help='comma separated sites. Alone, they are the list. With lists, '
                                   'only these entries of the lists are scanned')
    p.add_argument('--jobs', type=int, default=4, help='worker containers, one Firefox each (default 4)')
    p.add_argument('--settle', type=float, default=12,
                   help='seconds to wait after the load event before looking (default 12)')
    p.add_argument('--load-timeout', type=float, default=30, help='page load timeout in seconds (default 30)')
    p.add_argument('--limit', type=int, help='scan only the first N entries of each list')
    p.add_argument('--variants', default='plain,com', help='plain, com or plain,com (default)')
    p.add_argument('--out-dir', type=Path, default=HERE / 'results',
                   help='where the result directories go (default banner-scan/results)')
    args = p.parse_args()

    variants = [v.strip() for v in args.variants.split(',') if v.strip()]
    if not variants or any(v not in VARIANTS for v in variants) or len(set(variants)) != len(variants):
        sys.exit(f'--variants takes plain, com or both, not {args.variants!r}')
    variants = [v for v in VARIANTS if v in variants]
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
            sys.exit(f'no entries to scan in {path}')
        lists.append((path.stem, entries))

    squid_image, browser_image = scenario_images()
    tag = f'banner-scan-{os.getpid()}'
    fleet = Fleet(tag, squid_image, browser_image, args.jobs)
    # docker stop and a closed terminal end the scan like Ctrl-C does, so the
    # containers are still removed.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    signal.signal(signal.SIGHUP, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        fleet.start()
        fleet.wait_for_squid()
        ready = list(fleet.workers)
        if 'com' in variants:
            print('building the Consent-O-Matic template in every worker (about 25 s)', file=sys.stderr)
            results = {}

            def prep(name):
                try:
                    results[name] = fleet.prepare(name)
                except Exception as e:  # noqa: BLE001
                    results[name] = e
            threads = [threading.Thread(target=prep, args=(n,), daemon=True) for n in fleet.workers]
            for t in threads:
                t.start()
            for t in threads:
                while t.is_alive():
                    t.join(0.5)
            ready = [n for n in fleet.workers if not isinstance(results.get(n), Exception)]
            for n in fleet.workers:
                if isinstance(results.get(n), Exception):
                    print(f'  {results[n]}', file=sys.stderr)
            if not ready:
                raise RuntimeError('no worker could build the Consent-O-Matic template')
            rules = next((results[n] for n in ready if results[n]), '')
            if rules:
                print(f'  {rules}', file=sys.stderr)
        for name, entries in lists:
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S')
            out = (args.out_dir / f'{name}-{stamp}').resolve()
            meta = {'name': name, 'started': stamp, 'settle': args.settle, 'jobs': len(ready),
                    'squid_image': squid_image, 'browser_image': browser_image, 'elapsed': ''}
            sites, elapsed = scan_list(fleet, ready, name, entries, args, variants, out, meta)
            report(name, sites, variants, elapsed, len(ready), out)
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
