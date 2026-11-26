#!/usr/bin/env python3
"""Measure many sites per GMT run: generate the batch scenarios, then submit them.

A single-site run spends about 4 minutes on GMT's own work (baseline, install,
boot, idle) and on the cluster's cooldown for 40 s of measured phases. Measured
on machine 15 in October 2026: 60 s baseline, 44 s install, 18 s boot, 60 s
idle and 80 s of flow per site, about 5.5 minutes per site and scenario. A batch
pays the fixed part once and then runs each visit in about 80 s (measured on
machine 15 on 2026-11-25): about 21 s for a fresh profile and the cache warmup,
12 s to start Firefox, the measured 33 s visit and 7 s scroll, a screenshot,
quitting Firefox, and GMT's 1 s between phases.

    # every site of the list, with the consent clicks banner-scan found
    tools/batch.py generate sites_tranco_top1000.txt --name tranco1000 \\
        --clicks sites_tranco_clicks.csv

    # submit them all to machine 15, review screenshots to the server
    tools/batch.py submit parrot/batches/tranco1000-*.yml \\
        --screenshot-url https://shots.example.org/upload

    # a quick local check with three sites, no clicks
    tools/batch.py generate sites.txt --name smoke --limit 3

    # after the runs: one row per visit with its RESULT, failed ones as a list
    tools/batch.py results parrot/batches/tranco1000-*.yml \\
        --csv results.csv --failed-list remeasure.txt

The generated files are committed like any scenario, because the cluster
clones the repository. Do not edit them by hand, regenerate them.

VISITS. Firefox runs without any consent add-on. Every site gets a visit
"none", which leaves a cookie banner alone. A site whose banner
banner-scan/find-clicks.py could answer also gets a visit "accept", and a visit
"reject" if it found a verified reject path. Those visits click the buttons
with Parrot, at the same time after the navigation for both, so the two can be
compared. The visits of one site run back to back, in an order that rotates
with the site's position, so that no variant is always first.

Each visit gets six flow steps, all through parrot/common/batch-site.sh:

    NNNN <host> <variant> prepare   hidden    plain profile, warmup with the clicks
    NNNN <host> <variant> start     hidden    start Firefox on about:blank
    NNNN <host> <variant> visit     MEASURED  Alt+Home, the clicks, idle to 33 s
    NNNN <host> <variant> shot      hidden    review screenshot and upload
    NNNN <host> <variant> scroll    MEASURED  25 wheel ticks, idle to 7 s
    NNNN <host> <variant> close     hidden    quit Firefox, report the RESULT note

A visit that fails never fails the run. Its RESULT note says so, see
batch-site.sh, and its measured phases are then an idle machine.

GMT's limit is the user's total_duration, 7600 s for user 8 on 2026-11-25. A
batch holds whole sites and at most 75 visits, about 100 minutes of flow.
"""
import argparse
import csv
import json
import math
import os
import re
import shlex
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

args_api = 'https://api.green-coding.io'

sys.path.insert(0, str(Path(__file__).resolve().parent))
from submit import GMT_PYTHON, REPO_URL, SUBMIT_SCRIPT, read_urls  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
BATCH_DIR = REPO_ROOT / 'parrot' / 'batches'
DEFAULT_MACHINE_ID = 15
DEFAULT_VISITS = 75
SECONDS_PER_VISIT = 80

# When the clicks happen, in seconds after the navigation. The same for the
# accept and the reject visit of a site, so the two are comparable. At least
# CLICK_MIN, and two seconds after the banner appeared in the scan, rounded up.
CLICK_MIN = 6
CLICK_MARGIN = 2
CLICK_MAX = 20

# GMT's schema checker accepts only these characters in a flow step's name.
FLOW_NAME_OK = re.compile(r'^[\.\s0-9a-zA-Z_\(\)-]+$')
CLICKS_OK = re.compile(r'^\d+,\d+(;\d+,\d+){0,2}$')

STAGES = [
    # (stage, hidden)
    ('prepare', True),
    ('start', True),
    ('visit', False),
    ('shot', True),
    ('scroll', False),
    ('close', True),
]


def q(value):
    """A YAML scalar. JSON strings are valid YAML double-quoted scalars."""
    return json.dumps(value, ensure_ascii=False)


def host_of(url):
    host = urlsplit(url).hostname
    if not host:
        raise ValueError(f'no host in {url!r}')
    return host.encode('idna').decode('ascii')


def check_url(url):
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        raise ValueError(f'not an http(s) URL: {url!r}')
    # The URL goes into a bash command line inside single quotes.
    if "'" in url or any(c.isspace() for c in url):
        raise ValueError(f'URL contains a quote or whitespace: {url!r}')


def read_clicks(path):
    """url -> row of banner-scan/find-clicks.py's clicks.csv."""
    rows = {}
    with open(path, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            rows[row['url']] = row
    return rows


def plan_site(position, url, clicks):
    """The visits of one site: a list of (variant, clicks, click_at)."""
    row = clicks.get(url) if clicks else None
    visits = [('none', '', None)]
    if row and row.get('banner') == 'yes':
        appears = float(row['banner_appears_s'] or 0)
        click_at = min(CLICK_MAX, max(CLICK_MIN, math.ceil(appears + CLICK_MARGIN)))
        # find-clicks.py notes "GEOMETRY MISMATCH: A banner ...; B banner ..."
        # when the page did not start at the measured run's 0,85 1440x815, for
        # example under a browser notification bar. Visit A found the accept
        # clicks, visit B the reject clicks. A mismatch only after the click
        # (a page that opens a side panel) leaves the coordinates valid. A
        # reject in several clicks is not trusted then, the bar may have come
        # between two of them.
        evidence = row.get('evidence', '')
        mismatch = evidence.split(' | ')[0] if evidence.startswith('GEOMETRY MISMATCH') else ''
        multi = row.get('reject_steps', '') not in ('', '1')
        for variant, looks in (('accept', ['A banner']),
                               ('reject', ['B banner'] + (['B after reject'] if multi else []))):
            coords = row.get(f'{variant}_clicks', '')
            if any(look in mismatch for look in looks):
                continue
            if row.get(variant) == 'verified' and coords:
                if not CLICKS_OK.match(coords):
                    raise ValueError(f'{url}: {variant}_clicks {coords!r} is not x,y;x,y')
                visits.append((variant, coords, click_at))
    # Rotate, so that each variant comes first for a third of the sites.
    k = position % len(visits)
    return visits[k:] + visits[:k]


def render(batch_name, index, count, sites, source):
    """sites: list of (position, url, visits)."""
    n_visits = sum(len(v) for _, _, v in sites)
    first, last = sites[0][0], sites[-1][0]
    out = []
    w = out.append
    w('---')
    w(f'# GENERATED by tools/batch.py from {source}, batch {index} of {count}:')
    w(f'# list positions {first} to {last}, {len(sites)} sites, {n_visits} visits. Do not edit, regenerate.')
    w('#')
    w('# Measures each site in a real Firefox 155 without consent add-on, driven by')
    w('# Parrot, from a warm squid cache. Sites with a cookie banner are visited up')
    w('# to three times: banner left alone, accepted and rejected, with real clicks.')
    w('# The README section "Batch runs" says how.')
    w(f'name: {q(f"1000 Websites Parrot Firefox - {batch_name} {index:02d}/{count:02d}")}')
    w('author: Didi Hoffmann <didi@green-coding.io>')
    w(f'description: {q(f"{len(sites)} sites in {n_visits} visits, each: warmup, load and idle 30 s, scroll 5 s, in Firefox, cookie banners left, accepted or rejected")}')
    w('')
    w('custom_metrics:')
    w('  website_load:')
    w('    unit: Website Load and Idle Cached')
    w('    sci: True')
    w('')
    w('services:')
    w('  squid:')
    w('    image: greencoding/squid_reverse_proxy:v5')
    w('    healthcheck:')
    w('      test: ["CMD", "curl", "-fs", "--proxy", "http://squid:3128", "https://www.google.com", "--insecure"]')
    w('      interval: "1h"')
    w('      start_period: "60s"')
    w('      start_interval: "1s"')
    w('')
    w('  window-container:')
    w('    image: ribalba/parrot-browsers:v1')
    w('    depends_on:')
    w('      - squid')
    w('    environment:')
    w('      # Each visit passes its own PARROT_URL on the command line. This one')
    w('      # only satisfies the scripts that insist on the variable being set.')
    w('      PARROT_URL: about:blank')
    w('      SCREENSHOT_URL: __GMT_VAR_SCREENSHOT_URL__')
    w('      SCREEN_SIZE: 1440x900x24')
    w('      RESOLUTION: 1440x900')
    w('      WINDOW_X: 0')
    w('      WINDOW_Y: 0')
    w('      WINDOW_WIDTH: 1440')
    w('      WINDOW_HEIGHT: 900')
    w('      LANG: C.UTF-8')
    w('      LC_ALL: C.UTF-8')
    w('    docker-run-args:')
    w('      - --shm-size=1g')
    w('    setup-commands:')
    w('      - command: bash /tmp/repo/parrot/common/firefox-policies.sh')
    w('      # Firefox 155 maps its window as WM_CLASS ("Navigator", "firefox").')
    w('      # The consent clicks rely on this pin: banner-scan/find-clicks.py')
    w('      # found the coordinates in the same 1440x900 window at 0,0.')
    w('      - command: bash /tmp/repo/parrot/common/pin-windows.sh Navigator 1440 900')
    w('      - command: bash /usr/local/bin/entrypoint.sh')
    w('')
    w('flow:')
    for n, url, visits in sites:
        host = host_of(url)
        w('')
        w(f'  # {n}: {url}, visits: {", ".join(v for v, _, _ in visits)}')
        for variant, coords, click_at in visits:
            env = f'PARROT_URL={shlex.quote(url)}'
            if coords:
                env += f' PARROT_CLICKS={shlex.quote(coords)} PARROT_CLICK_AT={click_at}'
            for stage, hidden in STAGES:
                name = f'{n:04d} {host} {variant} {stage}'
                if not FLOW_NAME_OK.match(name):
                    raise ValueError(f'flow name GMT would reject: {name!r}')
                w(f'  - name: {q(name)}')
                w('    container: window-container')
                if hidden:
                    w('    hidden: true')
                w('    commands:')
                w('      - type: console')
                w(f"        command: {q(f'{env} bash /tmp/repo/parrot/common/batch-site.sh {stage} {n} {variant}')}")
                w('        shell: bash')
                w('        read-notes-stdout: true')
                w('        ignore-errors: true')
    return '\n'.join(out) + '\n'


def cmd_generate(args):
    urls = read_urls(args.list)
    if args.limit:
        urls = urls[:args.limit]
    if not urls:
        sys.exit('no URLs in the list')
    seen = {}
    for position, url in enumerate(urls, 1):
        check_url(url)
        host = host_of(url)
        if host in seen:
            sys.exit(f'{host} is in the list twice, at {seen[host]} and {position}')
        seen[host] = position
    if not re.match(r'^[a-z0-9][a-z0-9-]*$', args.name):
        sys.exit('--name must be lower case letters, digits and dashes')
    clicks = read_clicks(args.clicks) if args.clicks else {}
    if args.clicks:
        missing = [u for u in urls if u not in clicks]
        if missing:
            print(f'{len(missing)} sites are not in {args.clicks} and get only the visit "none", '
                  f'e.g. {missing[0]}', file=sys.stderr)

    sites = [(position, url, plan_site(position, url, clicks)) for position, url in enumerate(urls, 1)]
    batches, current = [], []
    for site in sites:
        if current and sum(len(v) for _, _, v in current) + len(site[2]) > args.visits:
            batches.append(current)
            current = []
        current.append(site)
    if current:
        batches.append(current)

    BATCH_DIR.mkdir(parents=True, exist_ok=True)
    source = Path(args.list).name + (f' and {Path(args.clicks).name}' if args.clicks else '')
    written = []
    for index, batch in enumerate(batches, 1):
        path = BATCH_DIR / f'{args.name}-{index:02d}.yml'
        path.write_text(render(args.name, index, len(batches), batch, source), encoding='utf-8')
        written.append((path, batch))

    counts = {}
    for _, _, visits in sites:
        for variant, _, _ in visits:
            counts[variant] = counts.get(variant, 0) + 1
    total = sum(counts.values())
    print(f'{len(sites)} sites, {total} visits ({", ".join(f"{k} {v}" for k, v in counts.items())}) '
          f'in {len(batches)} batches of up to {args.visits} visits:')
    for path, batch in written:
        print(f'  {path.relative_to(REPO_ROOT)}  {len(batch)} sites, {sum(len(v) for _, _, v in batch)} visits')
    print(f'about {args.visits * SECONDS_PER_VISIT / 60:.0f} min of flow per full batch, '
          f'{total * SECONDS_PER_VISIT / 3600:.1f} h for all, plus about 6 min of GMT setup per batch')
    return 0




def cmd_submit(args):
    if args.screenshot_url == 'off':
        print('review screenshots are off; pass --screenshot-url', file=sys.stderr)
    elif not args.screenshot_url.startswith(('https://', 'http://')):
        sys.exit(f'--screenshot-url is not an http(s) URL: {args.screenshot_url}')
    if not args.dry_run:
        for path in (SUBMIT_SCRIPT, GMT_PYTHON):
            if not path.exists():
                sys.exit(f'not found: {path}')

    failures = []
    for file in args.files:
        path = Path(file).resolve()
        rel = path.relative_to(REPO_ROOT).as_posix()
        name = f'1000 Websites Parrot Firefox - {path.stem}'
        if args.name_suffix:
            name += f' - {args.name_suffix}'
        cmd = [
            str(GMT_PYTHON), str(SUBMIT_SCRIPT), 'submit',
            '--name', name,
            '--repo-url', REPO_URL,
            '--branch', args.branch,
            '--filename', rel,
            '--machine-id', str(args.machine_id),
            '--schedule-mode', 'one-off',
            '--variables', f'__GMT_VAR_SCREENSHOT_URL__={args.screenshot_url}',
        ]
        if args.email:
            cmd += ['--email', args.email]
        if args.dry_run:
            print(shlex.join(cmd))
            continue
        print(f'--> {rel}', flush=True)
        if subprocess.run(cmd, check=False).returncode != 0:
            failures.append(rel)
    if failures:
        print(f'\n{len(failures)} submission(s) FAILED:', *failures, sep='\n  ', file=sys.stderr)
        return 1
    return 0


# "batch-site 12-accept RESULT ok <url>". The trial batch of 2026-11-25 wrote
# "batch-site 12 RESULT ...", without a variant, which reads as variant "com".
RESULT_NOTE = re.compile(r'^batch-site (\d+)(?:-(none|accept|reject))? RESULT (\S+) (\S+) ?(.*)$')


def gmt_token():
    """The cluster token, as submit_software.py finds it. Runs of user 8 are
    not public: without the token /v2/runs leaves them out and /v1/notes
    answers 403."""
    token = os.environ.get('GMT_AUTH_TOKEN', '').strip()
    path = Path.home() / '.gmt' / 'token'
    if not token and path.exists():
        token = path.read_text(encoding='utf-8').strip()
    return token


def api_get(path):
    url = f'{args_api}{path}'
    request = urllib.request.Request(url)
    if token := gmt_token():
        request.add_header('X-Authentication', token)
    with urllib.request.urlopen(request, timeout=60) as response:
        body = response.read()
    # GMT answers 204 with an empty body when nothing matches.
    if not body.strip():
        return []
    payload = json.loads(body)
    if not payload.get('success'):
        raise RuntimeError(f'{url}: {payload}')
    return payload['data']


def cmd_results(args):
    """Every site's RESULT note from the newest run of each batch file."""
    global args_api
    args_api = args.api.rstrip('/')
    wanted = {Path(f).resolve().relative_to(REPO_ROOT).as_posix() for f in args.files}
    # Columns of /v2/runs: id, name, uri, branch, created_at, invalid, filename,
    # variables, machine, commit, end_measurement, failed, machine_id, ...
    # Newest first by default. The filename filter may match more than the
    # exact name, so the first exact match is taken.
    newest = {}
    for filename in wanted:
        query = urllib.parse.urlencode({'uri': REPO_URL, 'filename': filename, 'limit': 20})
        for run in api_get(f'/v2/runs?{query}'):
            if run[6] == filename and run[2] == REPO_URL:
                newest[filename] = {'id': run[0], 'created': run[4], 'failed': run[11]}
                break

    rows = []
    for filename in sorted(wanted):
        run = newest.get(filename)
        if run is None:
            print(f'{filename}: no run yet', file=sys.stderr)
            continue
        found = 0
        for note in api_get(f'/v1/notes/{run["id"]}'):
            match = RESULT_NOTE.match(note[2])
            if match:
                found += 1
                n, variant, status, url, reason = match.groups()
                rows.append({'position': int(n), 'variant': variant or 'com', 'url': url,
                             'result': status, 'reason': reason,
                             'batch': filename, 'run_id': run['id'], 'run_failed': run['failed']})
        if run['failed'] or not found:
            print(f'{filename}: run {run["id"]} failed={run["failed"]}, {found} RESULT notes', file=sys.stderr)
    rows.sort(key=lambda r: (r['position'], r['variant']))

    out = sys.stdout if args.csv == '-' else open(args.csv, 'w', newline='', encoding='utf-8')
    writer = csv.DictWriter(out, fieldnames=['position', 'variant', 'url', 'result', 'reason', 'batch', 'run_id', 'run_failed'])
    writer.writeheader()
    writer.writerows(rows)
    if out is not sys.stdout:
        out.close()

    counts = {}
    for row in rows:
        counts[row['result']] = counts.get(row['result'], 0) + 1
    print('results: ' + ', '.join(f'{k} {v}' for k, v in sorted(counts.items())), file=sys.stderr)
    if args.failed_list:
        # A site is remeasured as a whole when any of its visits failed.
        failed = list(dict.fromkeys(r['url'] for r in rows if r['result'] != 'ok'))
        Path(args.failed_list).write_text(
            '# Sites whose batch run did not end with RESULT ok, from tools/batch.py results.\n'
            + ''.join(f'{u}\n' for u in failed), encoding='utf-8')
        print(f'{len(failed)} failed sites written to {args.failed_list}', file=sys.stderr)
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n', 1)[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = p.add_subparsers(dest='command', required=True)

    g = sub.add_parser('generate', help='write parrot/batches/<name>-NN.yml from a URL list')
    g.add_argument('list', help='one URL per line, # comments and blank lines ignored')
    g.add_argument('--name', required=True, help='batch name, used in file and run names')
    g.add_argument('--clicks', help="banner-scan/find-clicks.py's clicks.csv: sites with verified consent "
                                    'clicks also get the visits accept and reject')
    g.add_argument('--visits', type=int, default=DEFAULT_VISITS,
                   help=f'most visits per run, whole sites only (default {DEFAULT_VISITS})')
    g.add_argument('--limit', type=int, help='only the first N sites of the list')
    g.set_defaults(func=cmd_generate)

    s = sub.add_parser('submit', help='submit generated batch files to the cluster')
    s.add_argument('files', nargs='+', help='parrot/batches/*.yml files')
    s.add_argument('--machine-id', type=int, default=DEFAULT_MACHINE_ID,
                   help=f'default {DEFAULT_MACHINE_ID}, the GUI machine the website sweep runs on')
    s.add_argument('--branch', default='main')
    s.add_argument('--screenshot-url', default='off')
    s.add_argument('--name-suffix')
    s.add_argument('--email')
    s.add_argument('--dry-run', action='store_true')
    s.set_defaults(func=cmd_submit)

    r = sub.add_parser('results', help="collect every site's RESULT note from the newest run of each batch")
    r.add_argument('files', nargs='+', help='parrot/batches/*.yml files')
    r.add_argument('--api', default='https://api.green-coding.io')
    r.add_argument('--csv', default='-', help='where to write the per-site CSV (default stdout)')
    r.add_argument('--failed-list', help='also write the URLs of failed sites here, ready for generate')
    r.set_defaults(func=cmd_results)

    args = p.parse_args()
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
