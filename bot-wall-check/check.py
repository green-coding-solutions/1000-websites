#!/usr/bin/env python3
"""Check lists of sites for bot walls, through the squid the scenarios measure through.

    bot-wall-check/check.py bot-wall-check/crux-global-top1000-202608.txt

Every entry is fetched once, the way parrot/common/check-url.sh fetches the page
under test, and the response decides whether a benchmark run would measure the
site or something standing in front of it. One CSV per list goes to
bot-wall-check/results/, and a summary table is printed.

WHY IT READS THE PAGE AND NOT THE STATUS CODE

The walls that matter do not answer with an error. Akamai's bm-verify
interstitial is a 200, AWS WAF's challenge is a 202, Reddit's "Prove your
humanity" page is a 200. check-url.sh fails a run only on 400 and above, so it
lets all three through, and so would any check that stops at the status line.

WHY THROUGH SQUID, AND WHY CURL RUNS IN THE PARROT IMAGE

The scenarios reach every site through greencoding/squid_reverse_proxy, which
terminates TLS and speaks HTTP/1.1 to the origin. The origin sees squid's TLS
and squid's HTTP version whichever client is behind it, and some sites refuse
exactly that: mobile.de answers 403. So the check goes through a fresh squid
from the same image.

curl runs inside the parrot image rather than on the host because it is then
the curl check-url.sh runs, and because it can decode zstd. Chrome asks for
zstd, check-url.sh sends Chrome's headers, and a curl built without zstd fails
the transfer when a server takes it up on that.

Both image names are read from parrot/usage_scenario.yml and the headers from
check-url.sh, so this check cannot drift away from the scenario it predicts.

WHAT IT CANNOT SEE

A site that serves its page and walls the browser only after its scripts have
run. Whatever depends on the address the requests leave from: run it on the
machine that runs the benchmark. And a wall this script has no marker for, if
that wall answers 200 with a title and more than 20 kB; the title column is
there to catch those by eye. bot-wall-check/README.md has the details.
"""
import argparse
import codecs
import concurrent.futures
import csv
import datetime
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SCENARIO = REPO / 'parrot' / 'usage_scenario.yml'
CHECK_URL = REPO / 'parrot' / 'common' / 'check-url.sh'

# As seen from inside the client container, which mounts the repository where
# GMT mounts it and reaches squid under the scenario's service name.
CA_FILE = '/tmp/repo/parrot/squid-ca.crt'
PROXY = 'http://squid:3128'

# Separates curl's error message from its --write-out line on stderr.
MARK = '@@bot-wall-check@@'

VERDICTS = {
    'ok': 'the site served its page',
    'challenge': 'a bot-manager challenge page instead of the site',
    'blocked': 'refused: 401, 403, 429, another 4xx, or a block page',
    'suspect': 'a 2xx too small or untitled to be the site',
    'server-error': 'the origin answered 5xx',
    'proxy-error': 'squid could not fetch it: TLS, connect or read failure',
    'error': 'no usable answer: timeout, redirect loop, broken transfer',
    'no-homepage': '404 or 410 for the page itself',
    'no-website': 'the host name does not resolve',
}
GROUPS = {
    'benchmarkable': ('ok',),
    'walled': ('challenge', 'blocked'),
    'undecided': ('suspect', 'server-error', 'proxy-error', 'error'),
    'not a website': ('no-homepage', 'no-website'),
}
COLOURS = {
    'ok': '32', 'benchmarkable': '32',
    'challenge': '31', 'blocked': '31', 'walled': '31',
    'suspect': '33', 'server-error': '33', 'proxy-error': '33', 'error': '33', 'undecided': '33',
    'no-homepage': '2', 'no-website': '2', 'not a website': '2',
}

# Markers that occur on a vendor's challenge or block page and not on a page the
# vendor merely protects: (vendor, substrings of the page, exact page titles).
# The obvious loose markers are left out on purpose. Cloudflare injects
# /cdn-cgi/challenge-platform/scripts/jsd/main.js into ordinary pages, and
# Imperva injects /_Incapsula_Resource?SWJIYLWA= into ordinary pages, so either
# would call a working site walled.
CHALLENGE_MARKERS = [
    ('akamai', ('bm-verify', '/_sec/cp_challenge/'), ()),
    ('aws-waf', ('gokuprops', 'awswafcaptcha'), ()),
    ('cloudflare', ('window._cf_chl_opt',), ('just a moment...',)),
    ('datadome', ('captcha-delivery.com',), ()),
    ('perimeterx', ('px-captcha',), ()),
    ('imperva', ('_incapsula_resource?cwudnsai',), ()),
]
BLOCK_MARKERS = [
    ('cloudflare', ('used cloudflare to restrict access',), ('attention required! | cloudflare',)),
    ('imperva', ('incapsula incident id',), ()),
]
# Every challenge and block page seen so far is a few kB. A real homepage that
# happens to quote a marker is not, so larger pages are not searched.
MARKER_PAGE_LIMIT = 128_000

# What walls say about themselves. In the title they count on a page of any size:
# reddit.com serves 166 kB titled "Reddit - Prove your humanity". In the body
# they count only on pages small enough that the phrase is the page, because a
# news site can run a headline saying "access denied" and still be the site.
CHALLENGE_TEXT = ('verify you are human', 'are you a robot', 'are you a human',
                  'prove your humanity', 'unusual traffic', 'pardon our interruption',
                  'checking your browser', 'security check', 'verification required',
                  'one more step')
BLOCK_TEXT = ('access denied', 'zugriff verweigert', 'request unsuccessful',
              'you have been blocked', 'request blocked')
TEXT_PAGE_LIMIT = 32_000

# Bot-management cookies. Recorded whatever the verdict: a cookie says a vendor
# is watching, not that it blocked anything. A trailing _ matches a prefix.
ANTIBOT_COOKIES = [
    ('akamai', ('_abck', 'bm_sz', 'ak_bmsc', 'bm_sv', 'bm_mi', 'sec_cpt')),
    ('aws-waf', ('aws-waf-token',)),
    ('cloudflare', ('__cf_bm', 'cf_clearance')),
    ('datadome', ('datadome',)),
    ('imperva', ('visid_incap_', 'incap_ses_', 'reese84')),
    ('perimeterx', ('_px2', '_px3', '_pxhd', '_pxvid', 'pxcts')),
]

FIELDS = ['position', 'input', 'url', 'verdict', 'detail', 'http_status', 'final_url',
          'title', 'body_bytes', 'redirects', 'seconds', 'antibot_cookies']

COLOUR = sys.stdout.isatty() and not os.environ.get('NO_COLOR')


# --- what the check is built from ----------------------------------------

def scenario_images():
    """The squid and client images, read from parrot/usage_scenario.yml."""
    text = SCENARIO.read_text(encoding='utf-8')
    found = {}
    for service in ('squid', 'window-container'):
        m = re.search(rf'^  {re.escape(service)}:\s*\n\s+image:\s*(\S+)', text, re.M)
        if not m:
            sys.exit(f'could not read the {service} image from {SCENARIO}')
        found[service] = m.group(1)
    return found['squid'], found['window-container']


def chrome_headers():
    """check-url.sh's CHROME_HEADERS, plus the no-store it also sends."""
    text = CHECK_URL.read_text(encoding='utf-8')
    block = re.search(r'^CHROME_HEADERS=\((.*?)^\)', text, re.M | re.S)
    headers = re.findall(r"-H '([^']*)'", block.group(1)) if block else []
    if not headers:
        sys.exit(f'could not read CHROME_HEADERS from {CHECK_URL}')
    # As in check-url.sh: squid's configuration stores even no-store responses,
    # and a copy this request left behind would be served to the next one.
    return headers + ['Cache-Control: no-store']


def read_entries(path):
    """Same format as tools/submit.py --url-file: blank lines and # comments ignored."""
    entries = []
    for raw in Path(path).read_text(encoding='utf-8').splitlines():
        line = raw.split('#', 1)[0].strip()
        if line:
            entries.append(line)
    return entries


def to_url(entry):
    return entry if re.match(r'^[a-z][a-z0-9+.-]*://', entry, re.I) else f'https://{entry}/'


# --- containers ------------------------------------------------------------

def docker(*args):
    p = subprocess.run(['docker', *args], capture_output=True, text=True)
    if p.returncode:
        raise SystemExit(f'docker {args[0]} failed: {p.stderr.strip()}')
    return p.stdout.strip()


def start_containers(tag, squid_image, client_image):
    print(f'starting {squid_image} and {client_image} (a first run pulls them)', file=sys.stderr)
    docker('network', 'create', tag)
    docker('run', '-d', '--rm', '--name', f'{tag}-squid', '--network', tag,
           '--network-alias', 'squid', squid_image)
    docker('run', '-d', '--rm', '--name', f'{tag}-client', '--network', tag,
           '--mount', f'type=bind,source={REPO},target=/tmp/repo,readonly',
           '--entrypoint', 'sleep', client_image, 'infinity')
    return f'{tag}-client'


def stop_containers(tag):
    subprocess.run(['docker', 'rm', '-f', f'{tag}-squid', f'{tag}-client'], capture_output=True)
    subprocess.run(['docker', 'network', 'rm', tag], capture_output=True)


def wait_for_squid(client, seconds=60):
    """The scenario's squid healthcheck, but verifying against the committed CA."""
    deadline, last = time.monotonic() + seconds, ''
    while time.monotonic() < deadline:
        p = subprocess.run(['docker', 'exec', client, 'curl', '-sS', '-o', '/dev/null',
                            '-w', '%{http_code}', '--max-time', '5', '--proxy', PROXY,
                            '--cacert', CA_FILE, 'https://www.google.com'],
                           capture_output=True, text=True)
        if p.returncode == 0 and p.stdout.strip() not in ('', '000'):
            return
        last = p.stderr.strip()
        time.sleep(1)
    hint = ''
    if 'certificate' in last.lower():
        hint = ' (squid signs with a CA that is not parrot/squid-ca.crt)'
    raise SystemExit(f'squid was not usable within {seconds}s: {last}{hint}')


def egress_of(client):
    """Where the requests leave from. It changes what bot managers do, so it is reported."""
    p = subprocess.run(['docker', 'exec', client, 'curl', '-s', '--max-time', '10',
                        '--proxy', PROXY, '--cacert', CA_FILE, '-H', 'Cache-Control: no-store',
                        'https://ipinfo.io/json'], capture_output=True, text=True)
    try:
        d = json.loads(p.stdout)
        return f"{d['ip']} ({d.get('org', 'unknown network')}, {d.get('country', '??')})"
    except (ValueError, KeyError):
        return 'unknown (the ipinfo.io lookup failed)'


# --- one site ----------------------------------------------------------------

def split_response(raw):
    """curl -D - -o - output into its header blocks and the final body."""
    blocks, pos = [], 0
    while raw.startswith(b'HTTP/', pos):
        ends = [(i, n) for i, n in ((raw.find(b'\r\n\r\n', pos), 4), (raw.find(b'\n\n', pos), 2)) if i >= 0]
        end, sep = min(ends) if ends else (len(raw), 0)
        blocks.append(raw[pos:end].decode('latin-1'))
        pos = end + sep
    return blocks, raw[pos:]


def parse_headers(blocks):
    """The final response's headers, and every cookie name set along the way."""
    final, cookies = {}, set()
    for block in blocks:
        lines = re.split(r'\r?\n', block)
        fields = [line.split(':', 1) for line in lines[1:] if ':' in line]
        for name, value in fields:
            if name.strip().lower() == 'set-cookie':
                cookies.add(value.split('=', 1)[0].strip().lower())
        if not re.match(r'HTTP/\S+ 1\d\d\b', lines[0]):
            final = {name.strip().lower(): value.strip() for name, value in fields}
    return final, cookies


def page_title(body, content_type):
    """The <title>, decoded in the charset the page declares, else UTF-8."""
    m = re.search(rb'<title[^>]*>(.*?)</title>', body[:200_000], re.S | re.I)
    if not m:
        return ''
    declared = (re.search(r'charset=["\']?([\w.:-]+)', content_type, re.I)
                or re.search(rb'<meta[^>]+charset=["\']?([\w.:-]+)', body[:4096], re.I))
    charset = declared.group(1) if declared else 'utf-8'
    charset = charset.decode('ascii', 'replace') if isinstance(charset, bytes) else charset
    try:
        codecs.lookup(charset)
    except LookupError:
        charset = 'utf-8'
    return ' '.join(html.unescape(m.group(1).decode(charset, 'replace')).split())[:120]


def antibot_vendors(cookies):
    vendors = []
    for vendor, names in ANTIBOT_COOKIES:
        if any(c == n or (n.endswith('_') and c.startswith(n)) for c in cookies for n in names):
            vendors.append(vendor)
    return vendors


def classify(status, curl_error, exit_code, headers, body, title):
    if status == 0:
        return 'error', curl_error or 'no response'

    squid_error = headers.get('x-squid-error', '').split()
    if squid_error:
        code = squid_error[0]
        return ('no-website' if code == 'ERR_DNS_FAIL' else 'proxy-error'), code

    lowered_title = title.lower()
    text = body.decode('utf-8', 'replace').lower() if len(body) < MARKER_PAGE_LIMIT else ''
    for verdict, markers in (('challenge', CHALLENGE_MARKERS), ('blocked', BLOCK_MARKERS)):
        for vendor, substrings, titles in markers:
            if lowered_title in titles or (text and any(s in text for s in substrings)):
                return verdict, vendor
    for verdict, phrases in (('challenge', CHALLENGE_TEXT), ('blocked', BLOCK_TEXT)):
        for phrase in phrases:
            if phrase in lowered_title:
                return verdict, f'title: {phrase}'
            if text and len(body) < TEXT_PAGE_LIMIT and phrase in text:
                return verdict, f'text: {phrase}'

    if status in (404, 410):
        return 'no-homepage', f'http-{status}'
    if status in (202, 428):
        return 'challenge', f'http-{status}'
    if 400 <= status < 500:
        return 'blocked', f'http-{status}'
    if status >= 500:
        return 'server-error', f'http-{status}'
    if exit_code or 300 <= status < 400:
        return 'error', curl_error or f'http-{status}'
    if re.match(r'(404\b|(page )?not found\b)', lowered_title):
        return 'no-homepage', 'soft 404'
    if len(body) < 1024:
        return 'suspect', f'{len(body)} bytes'
    # www.naukri.com: 14 kB, Akamai cookies, no title. A real homepage has one.
    if not title and len(body) < 20_000:
        return 'suspect', f'no title, {len(body)} bytes'
    return 'ok', ''


def check_one(client, position, entry, headers, timeout):
    url = to_url(entry)
    cmd = ['docker', 'exec', client, 'curl', '-sS', '-L', '--max-time', str(timeout),
           '--proxy', PROXY, '--cacert', CA_FILE, '--suppress-connect-headers']
    for header in headers:
        cmd += ['-H', header]
    cmd += ['--compressed', '-D', '-', '-o', '-',
            '-w', '%{stderr}' + MARK + '%{http_code} %{num_redirects} %{time_total} %{url_effective}',
            url]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout + 30)
        raw, stderr, exit_code = p.stdout, p.stderr.decode('utf-8', 'replace'), p.returncode
    except subprocess.TimeoutExpired:
        raw, stderr, exit_code = b'', 'docker exec did not return', -1

    status, redirects, seconds, final_url = 0, 0, 0.0, ''
    message, _, out = stderr.rpartition(MARK) if MARK in stderr else (stderr, '', '')
    parts = out.strip().split(' ', 3)
    if len(parts) == 4:
        status = int(parts[0]) if parts[0].isdigit() else 0
        redirects = int(parts[1]) if parts[1].isdigit() else 0
        seconds, final_url = float(parts[2]), parts[3]

    m = re.search(r'curl: \((\d+)\) ([^\n]*)', message)
    if m:
        curl_error = f"curl-{m.group(1)}: {m.group(2).split(' after ')[0].split(':')[0].lower()}"
    else:
        curl_error = message.strip().splitlines()[-1][:80] if exit_code and message.strip() else ''

    blocks, body = split_response(raw)
    headers_final, cookies = parse_headers(blocks)
    title = page_title(body, headers_final.get('content-type', ''))

    verdict, detail = classify(status, curl_error, exit_code, headers_final, body, title)
    return {
        'position': position, 'input': entry, 'url': url, 'verdict': verdict, 'detail': detail,
        'http_status': status, 'final_url': final_url, 'title': title, 'body_bytes': len(body),
        'redirects': redirects, 'seconds': round(seconds, 2),
        'antibot_cookies': ';'.join(antibot_vendors(cookies)),
    }


# --- a list ------------------------------------------------------------------

def duration(seconds):
    seconds = int(round(seconds))
    return f'{seconds // 60}m {seconds % 60:02d}s' if seconds >= 60 else f'{seconds}s'


def check_list(client, name, entries, headers, jobs, timeout):
    rows, counts, started = [], Counter(), time.monotonic()
    tty, step = sys.stderr.isatty(), max(1, len(entries) // 10)
    pool = concurrent.futures.ThreadPoolExecutor(jobs)
    try:
        futures = [pool.submit(check_one, client, i, entry, headers, timeout)
                   for i, entry in enumerate(entries, 1)]
        for done, future in enumerate(concurrent.futures.as_completed(futures), 1):
            row = future.result()
            rows.append(row)
            counts[row['verdict']] += 1
            if tty or done % step == 0 or done == len(entries):
                line = (f'{name}: {done}/{len(entries)}  ok {counts["ok"]}  '
                        f'walled {counts["challenge"] + counts["blocked"]}  '
                        f'{duration(time.monotonic() - started)}')
                sys.stderr.write(f'\r\x1b[K{line}' if tty else f'{line}\n')
                sys.stderr.flush()
        if tty:
            sys.stderr.write('\n')
    except KeyboardInterrupt:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown()
    return sorted(rows, key=lambda r: r['position']), time.monotonic() - started


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


# --- the report ----------------------------------------------------------------

def paint(text, code):
    return f'\x1b[{code}m{text}\x1b[0m' if COLOUR and code else text


def bar(fraction, width=30):
    eighths = round(fraction * width * 8)
    return '█' * (eighths // 8) + ('', '▏', '▎', '▍', '▌', '▋', '▊', '▉')[eighths % 8]


def print_table(title, header, rows, right=()):
    """rows: lists of cells. The first cell is coloured by its own value."""
    widths = [max(len(str(row[i])) for row in [header, *rows]) for i in range(len(header))]

    def line(cells, first_colour=None):
        out = [str(c).rjust(w) if i in right else str(c).ljust(w)
               for i, (c, w) in enumerate(zip(cells, widths))]
        out[0] = paint(out[0], first_colour)
        return ('  ' + '  '.join(out)).rstrip()

    print(f'\n{paint(title, "1")}')
    print(paint(line(header), '2'))
    print(paint('  ' + '  '.join('─' * w for w in widths), '2'))
    for row in rows:
        print(line(row, COLOURS.get(str(row[0]))))


def report(name, rows, elapsed, jobs, images, egress, csv_path):
    n = len(rows)
    counts = Counter(r['verdict'] for r in rows)
    pct = lambda k: f'{100 * k / n:.1f}%' if n else '-'

    print(f'\n{paint("bot-wall check: " + name, "1")}')
    print(f'  {n} entries, {jobs} in parallel, {duration(elapsed)}')
    print(f'  through {images[0]}, curl from {images[1]}')
    print(f'  leaving from {egress}')
    try:
        print(f'  csv: {csv_path.relative_to(Path.cwd())}')
    except ValueError:
        print(f'  csv: {csv_path}')

    print_table('Verdicts', ['verdict', 'sites', 'share', '', 'meaning'],
                [[v, counts[v], pct(counts[v]), bar(counts[v] / n if n else 0), meaning]
                 for v, meaning in VERDICTS.items()], right=(1, 2))

    group_counts = {g: sum(counts[v] for v in members) for g, members in GROUPS.items()}
    print_table('Summary', ['group', 'sites', 'share', ''],
                [[g, k, pct(k), bar(k / n if n else 0)] for g, k in group_counts.items()],
                right=(1, 2))
    websites = n - group_counts['not a website']
    if websites:
        print(f'\n  {paint("walled", "31")} among the {websites} entries that are websites: '
              f'{group_counts["walled"]} ({100 * group_counts["walled"] / websites:.1f}%)')

    walls = defaultdict(list)
    for r in rows:
        if r['verdict'] in GROUPS['walled']:
            walls[(r['verdict'], r['detail'])].append(urlsplit(r['url']).hostname or r['input'])
    if walls:
        ranked = sorted(walls.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        table = []
        for (verdict, detail), hosts in ranked[:15]:
            examples = ', '.join(hosts[:3]) + (f' +{len(hosts) - 3}' if len(hosts) > 3 else '')
            table.append([verdict, detail, len(hosts), examples])
        print_table('What walls them', ['verdict', 'detail', 'sites', 'examples'], table, right=(2,))
        if len(ranked) > 15:
            print(f'  ... and {len(ranked) - 15} rarer kinds, see the csv')

    vendors = defaultdict(Counter)
    for r in rows:
        for vendor in filter(None, r['antibot_cookies'].split(';')):
            vendors[vendor]['sites'] += 1
            vendors[vendor][r['verdict']] += 1
    if vendors:
        print_table('Bot-management cookies (a vendor watching, not necessarily blocking)',
                    ['vendor', 'sites', 'still ok', 'walled'],
                    [[v, c['sites'], c['ok'], c['challenge'] + c['blocked']]
                     for v, c in sorted(vendors.items(), key=lambda kv: -kv[1]['sites'])],
                    right=(1, 2, 3))
    print()


# --- main ----------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description='Check lists of sites for bot walls, through the scenarios\' squid.',
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument('lists', nargs='+', type=Path,
                   help='one site per line, a domain or a URL; blank lines and # comments ignored')
    p.add_argument('--jobs', type=int, default=16, help='sites fetched in parallel (default 16)')
    p.add_argument('--timeout', type=int, default=30, help='seconds allowed per site (default 30)')
    p.add_argument('--limit', type=int, help='check only the first N entries of each list')
    p.add_argument('--out-dir', type=Path, default=HERE / 'results',
                   help='where the csv files go (default bot-wall-check/results)')
    args = p.parse_args()

    if not shutil.which('docker'):
        sys.exit('docker is not on PATH')
    lists = []
    for path in args.lists:
        if not path.is_file():
            sys.exit(f'no such list: {path}')
        entries = read_entries(path)[:args.limit] if args.limit else read_entries(path)
        if not entries:
            sys.exit(f'no entries in {path}')
        lists.append((path, entries))

    headers = chrome_headers()
    images = scenario_images()
    tag = f'bot-wall-check-{os.getpid()}'
    try:
        client = start_containers(tag, *images)
        wait_for_squid(client)
        egress = egress_of(client)
        for path, entries in lists:
            stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
            csv_path = (args.out_dir / f'{path.stem}-{stamp}.csv').resolve()
            rows, elapsed = check_list(client, path.name, entries, headers, args.jobs, args.timeout)
            write_csv(csv_path, rows)
            report(path.name, rows, elapsed, args.jobs, images, egress, csv_path)
    except KeyboardInterrupt:
        print('\ninterrupted; nothing written for the list in progress', file=sys.stderr)
        return 130
    finally:
        stop_containers(tag)
    return 0


if __name__ == '__main__':
    sys.exit(main())
