#!/usr/bin/env python3
"""Build the benchmark's site list: the most popular websites that a run can measure, without adult sites.

    # 1. fetch the candidates once, through the scenarios' squid (needs docker)
    tools/build-site-list.py check --candidates 2000

    # 2. filter, deduplicate and write the list and its audit trail
    tools/build-site-list.py build \\
        --check-csv bot-wall-check/results/tranco-JZW4Y-crux-202610-1-2000-<stamp>.csv \\
        --check-network 'AS3320 Deutsche Telekom AG, DE'

THE RULE

1. Ranking. A Tranco list, by its permanent ID, downloaded from
   https://tranco-list.eu/download/<ID>/1000000. Tranco ranks pay-level
   domains by an aggregate of several providers (Le Pochat et al., "Tranco: A
   Research-Oriented Top Sites Ranking Hardened Against Manipulation",
   NDSS 2019).
2. Website filter. A Tranco domain d is a candidate only if one of its
   homepage origins is in the Chrome UX Report global list of that month, in
   any rank bucket. The origins are tried in this order, and the first one
   CrUX has is the one that is checked: https://www.d, https://d,
   http://www.d, http://d. Tranco counts DNS lookups and links as well as
   page loads, so many of its domains are infrastructure with no website.
   CrUX only has origins that people open in Chrome.
3. Measurable. bot-wall-check/check.py fetches every candidate origin once,
   through the squid the scenarios measure through. Only the verdict ok
   stays. Its final_url, after redirects, is the URL to measure, so that no
   redirect lands in the measured load. A query string made only of tracking
   parameters is removed, and so is a fragment, because the list format
   treats # as a comment.
4. Not adult. Four signals are collected for every candidate.
     cf-family  Cloudflare's family resolver 1.1.1.3 answers 0.0.0.0 for the
                domain, its origin host or its final host, and the
                malware-only resolver 1.1.1.2 does not
     ut1        the domain or a parent is in the adult category of the UT1
                blacklist (Universite Toulouse Capitole)
     oisd       the domain or a parent is on the OISD nsfw list
     rta        the fetched page carries the RTA label
                RTA-5042-1996-1400-1577-RTA
   No signal means not adult, all four mean porn. Where they disagree, the
   decision is a hand-made one by the page title and the domain, read from
   tools/adult-overrides.csv (domain,decision,reason), which may also overrule
   the two automatic cases. Decisions are porn, adult (adult but not porn:
   escort and sex-work ads, hookup and swinger sites, erotic stories, fetish
   communities) and no. Both porn and adult are excluded.
5. Not an endpoint. Domains in tools/endpoint-overrides.csv with the decision
   exclude are dropped (connectivity checks and the like), those with flag
   stay and are flagged.
6. Once each. Two candidates whose final URL has the same host count once, at
   the better Tranco rank.
7. The first --count survivors in Tranco order are the list.

The candidates CSV holds every candidate considered, with the reason it was
left out, so that the list can be audited and refilled from it.

check runs check.py itself, unchanged, with one addition made at run time: it
notes whether the page it fetched carries the RTA label, as an extra
rta_label column. That way the adult signal needs no second fetch of an adult
page. A check CSV without that column still works, with the rta signal
counted as not fired.

Python 3.9 or newer, standard library only.
"""
import argparse
import concurrent.futures
import csv
import datetime
import gzip
import hashlib
import importlib.util
import json
import os
import random
import re
import socket
import struct
import sys
import tarfile
import tempfile
import textwrap
import threading
import urllib.request
from collections import Counter, OrderedDict
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit, urlunsplit

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
CHECK_PY = REPO / 'bot-wall-check' / 'check.py'

TRANCO_URL = 'https://tranco-list.eu/download/{id}/1000000'
TRANCO_META_URL = 'https://tranco-list.eu/api/lists/id/{id}'
CRUX_URL = 'https://raw.githubusercontent.com/zakird/crux-top-lists/main/data/global/{month}.csv.gz'
UT1_URL = 'https://dsi.ut-capitole.fr/blacklists/download/adult.tar.gz'
OISD_URL = 'https://nsfw.oisd.nl/domainswild2'

FAMILY_RESOLVER = '1.1.1.3'
MALWARE_RESOLVER = '1.1.1.2'
RTA_LABEL = b'RTA-5042-1996-1400-1577-RTA'
SIGNALS = ('cf-family', 'ut1', 'oisd', 'rta')

# Query parameters that only say where a visitor came from. A final URL whose
# query is made of nothing else loses its query.
TRACKING_PARAMS = {'gclid', 'gclsrc', 'dclid', 'fbclid', 'msclkid', 'yclid', 'igshid',
                   '_ga', '_gl', 'mc_cid', 'mc_eid', 'ref', 'ref_src', 'referrer', 'src'}

LOGIN_PATH = re.compile(r'/(log-?in|sign-?in|signon|auth|sso|oauth2?|session|account/login|'
                        r'accounts?/(log-?in|sign-?in))\b', re.I)
LOGIN_HOST = re.compile(r'^(login|signin|auth|sso|accounts?|id|idp)\.', re.I)
# Consent gates that a site redirects to on another host, with a session in the URL.
CONSENT_HOST = re.compile(r'^(consent\.|myprivacy\.)', re.I)
GEO_TITLE = re.compile(r'(not|un)available in your (region|country|location)', re.I)

AUDIT_FIELDS = ['tranco_rank', 'domain', 'crux_origin', 'crux_bucket', 'check_verdict',
                'check_detail', 'final_url', 'measured_url', 'title', 'adult_signals',
                'adult_decision', 'adult_reason', 'dup_of', 'flags', 'excluded_reason',
                'included', 'list_position']

UA = 'build-site-list/1 (python urllib)'


# --- downloads -----------------------------------------------------------------

def default_cache():
    base = os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache'
    return Path(base) / '1000-websites-site-list'


def fetch(url, path, refresh=False):
    """Download url to path once. Later calls use the cached copy unless refresh."""
    path = Path(path)
    if path.is_file() and not refresh:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    print(f'downloading {url}', file=sys.stderr)
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    tmp = path.with_suffix(path.suffix + '.part')
    with urllib.request.urlopen(req, timeout=300) as r, tmp.open('wb') as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
    tmp.replace(path)
    return path


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def file_date(path):
    return datetime.datetime.fromtimestamp(Path(path).stat().st_mtime).strftime('%Y-%m-%d')


def load_tranco(list_id, cache, refresh=False):
    path = fetch(TRANCO_URL.format(id=list_id), cache / f'tranco-{list_id}.csv', refresh)
    meta_path = fetch(TRANCO_META_URL.format(id=list_id), cache / f'tranco-{list_id}.json', refresh)
    ranking = []
    with path.open(encoding='utf-8', newline='') as f:
        for row in csv.reader(f):
            if len(row) >= 2 and row[0].strip().isdigit():
                ranking.append((int(row[0]), row[1].strip().lower()))
    if not ranking:
        sys.exit(f'{path} holds no ranking')
    return ranking, json.loads(meta_path.read_text(encoding='utf-8')), path


def load_crux(month, cache, refresh=False):
    path = fetch(CRUX_URL.format(month=month), cache / f'crux-global-{month}.csv.gz', refresh)
    buckets = {}
    with gzip.open(path, 'rt', encoding='utf-8', newline='') as f:
        for row in csv.DictReader(f):
            buckets[row['origin'].strip().lower()] = int(row['rank'])
    if not buckets:
        sys.exit(f'{path} holds no origins')
    return buckets, path


def load_ut1(cache, refresh=False):
    path = fetch(UT1_URL, cache / 'ut1-adult.tar.gz', refresh)
    domains = set()
    with tarfile.open(path, 'r:gz') as tar:
        for member in tar:
            if member.isfile() and member.name.endswith('/domains'):
                text = tar.extractfile(member).read().decode('utf-8', 'replace')
                domains.update(line.strip().lower() for line in text.splitlines()
                               if line.strip() and not line.startswith('#'))
    if not domains:
        sys.exit(f'{path} holds no adult/domains')
    return domains, path


def load_oisd(cache, refresh=False):
    path = fetch(OISD_URL, cache / 'oisd-nsfw-domainswild2.txt', refresh)
    domains = set()
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
        line = line.strip().lower()
        if line and not line.startswith('#'):
            domains.add(line.lstrip('*.'))
    if not domains:
        sys.exit(f'{path} holds no domains')
    return domains, path


# --- candidates ----------------------------------------------------------------

def origin_variants(domain):
    return (f'https://www.{domain}', f'https://{domain}', f'http://www.{domain}', f'http://{domain}')


def crux_candidates(ranking, buckets):
    """Every Tranco domain with a homepage origin in CrUX, in Tranco order."""
    for rank, domain in ranking:
        for origin in origin_variants(domain):
            if origin in buckets:
                yield rank, domain, origin, buckets[origin]
                break


# --- check.py, with the RTA label noted -----------------------------------------

def run_check(list_path, jobs, timeout):
    """Run check.py's main on list_path, adding an rta_label column to its CSV."""
    spec = importlib.util.spec_from_file_location('bot_wall_check', CHECK_PY)
    check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(check)

    seen = threading.local()
    original_split, original_check_one = check.split_response, check.check_one

    def split_response(raw):
        blocks, body = original_split(raw)
        seen.rta = RTA_LABEL in body
        return blocks, body

    def check_one(*args, **kwargs):
        seen.rta = False
        row = original_check_one(*args, **kwargs)
        row['rta_label'] = 'yes' if seen.rta else 'no'
        return row

    check.split_response, check.check_one = split_response, check_one
    check.FIELDS = check.FIELDS + ['rta_label']
    sys.argv = [str(CHECK_PY), str(list_path), '--jobs', str(jobs), '--timeout', str(timeout)]
    return check.main()


def cmd_check(args):
    cache = Path(args.cache_dir)
    ranking, _, _ = load_tranco(args.list_id, cache, args.refresh)
    buckets, _ = load_crux(args.crux_month, cache, args.refresh)
    chosen = []
    for i, cand in enumerate(crux_candidates(ranking, buckets), 1):
        if i > args.skip + args.candidates:
            break
        if i > args.skip:
            chosen.append(cand)
    first, last = args.skip + 1, args.skip + len(chosen)
    name = f'tranco-{args.list_id}-crux-{args.crux_month}-{first}-{last}.txt'
    with tempfile.TemporaryDirectory() as tmp:
        list_path = Path(tmp) / name
        list_path.write_text(
            f'# CrUX-matched Tranco candidates {first} to {last}, '
            f'Tranco ranks {chosen[0][0]} to {chosen[-1][0]}\n'
            + ''.join(f'{origin}\n' for _, _, origin, _ in chosen), encoding='utf-8')
        print(f'checking candidates {first} to {last} (Tranco ranks {chosen[0][0]} '
              f'to {chosen[-1][0]})', file=sys.stderr)
        return run_check(list_path, args.jobs, args.timeout)


# --- DNS -------------------------------------------------------------------------

def dns_query(name, server, timeout=3.0, tries=3):
    """A records for name from server, over UDP. None when the server did not answer."""
    labels = [l for l in name.rstrip('.').split('.') if l]
    qname = b''.join(bytes([len(l)]) + l.encode('ascii') for l in labels) + b'\x00'
    for _ in range(tries):
        qid = random.randrange(65536)
        packet = struct.pack('>HHHHHH', qid, 0x0100, 1, 0, 0, 0) + qname + struct.pack('>HH', 1, 1)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        try:
            s.sendto(packet, (server, 53))
            while True:
                data, _ = s.recvfrom(4096)
                if len(data) >= 12 and struct.unpack('>H', data[:2])[0] == qid:
                    break
        except (socket.timeout, OSError):
            continue
        finally:
            s.close()
        return parse_dns(data)
    return None


def skip_name(data, pos):
    while True:
        n = data[pos]
        if n == 0:
            return pos + 1
        if n & 0xC0 == 0xC0:
            return pos + 2
        pos += n + 1


def parse_dns(data):
    _, flags, qd, an, _, _ = struct.unpack('>HHHHHH', data[:12])
    pos = 12
    for _ in range(qd):
        pos = skip_name(data, pos) + 4
    ips = []
    for _ in range(an):
        pos = skip_name(data, pos)
        rtype, _, _, rdlen = struct.unpack('>HHIH', data[pos:pos + 10])
        pos += 10
        if rtype == 1 and rdlen == 4:
            ips.append('.'.join(str(b) for b in data[pos:pos + 4]))
        pos += rdlen
    return {'rcode': flags & 0xF, 'a': ips}


def resolve_all(names, cache_file, refresh=False):
    """Family and malware answers for every name, cached so that a rebuild sees the same."""
    cache_file = Path(cache_file)
    known = {}
    if cache_file.is_file() and not refresh:
        known = json.loads(cache_file.read_text(encoding='utf-8'))
    todo = [(n, s) for n in sorted(names) for s in (FAMILY_RESOLVER, MALWARE_RESOLVER)
            if f'{s} {n}' not in known]
    if todo:
        print(f'resolving {len(todo)} names at {FAMILY_RESOLVER} and {MALWARE_RESOLVER}',
              file=sys.stderr)
        stamp = datetime.date.today().isoformat()
        with concurrent.futures.ThreadPoolExecutor(32) as pool:
            for (n, s), answer in zip(todo, pool.map(lambda t: dns_query(*t), todo)):
                if answer is not None:
                    known[f'{s} {n}'] = dict(answer, date=stamp)
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(known, indent=0, sort_keys=True), encoding='utf-8')
    return known


def cf_family_fires(names, answers):
    for n in names:
        fam = answers.get(f'{FAMILY_RESOLVER} {n}')
        mal = answers.get(f'{MALWARE_RESOLVER} {n}')
        if fam and '0.0.0.0' in fam['a'] and not (mal and '0.0.0.0' in mal['a']):
            return True
    return False


# --- build -----------------------------------------------------------------------

def host_of(url):
    return (urlsplit(url).hostname or '').lower().rstrip('.')


def suffixes(name):
    """name and its parents, down to two labels."""
    parts = name.split('.')
    return ['.'.join(parts[i:]) for i in range(max(1, len(parts) - 1))]


def listed(names, domains):
    return any(s in domains for n in names for s in suffixes(n))


def clean_url(url):
    """The final URL without a tracking-only query and without a fragment, and what was removed."""
    parts = urlsplit(url)
    notes, query = [], parts.query
    if query:
        keys = [k.lower() for k, _ in parse_qsl(query, keep_blank_values=True)]
        if keys and all(k in TRACKING_PARAMS or k.startswith('utm_') for k in keys):
            notes.append(f'tracking query removed: ?{query}')
            query = ''
    if parts.fragment or url.endswith('#'):
        notes.append(f'fragment removed: #{parts.fragment}')
    path = parts.path or '/'
    return urlunsplit((parts.scheme, parts.netloc, path, query, '')), notes


def read_overrides(path, allowed):
    out = {}
    if not path or not Path(path).is_file():
        return out
    with Path(path).open(encoding='utf-8', newline='') as f:
        for row in csv.DictReader(line for line in f if not line.lstrip().startswith('#')):
            domain = (row.get('domain') or '').strip().lower()
            decision = (row.get('decision') or '').strip().lower()
            if not domain:
                continue
            if decision not in allowed:
                sys.exit(f'{path}: {domain}: decision {decision!r} is not one of {", ".join(allowed)}')
            out[domain] = (decision, (row.get('reason') or '').strip())
    return out


def read_check_csvs(paths):
    rows, meta = {}, []
    for p in paths:
        p = Path(p)
        m = re.search(r'-(\d{8})-(\d{6})\.csv$', p.name)
        when = (datetime.datetime.strptime(m.group(1) + m.group(2), '%Y%m%d%H%M%S')
                if m else datetime.datetime.fromtimestamp(p.stat().st_mtime))
        with p.open(encoding='utf-8', newline='') as f:
            n = 0
            for row in csv.DictReader(f):
                rows[row['input'].strip().lower().rstrip('/')] = row
                n += 1
        meta.append((p, when, n))
    return rows, meta


def rel(path):
    try:
        return str(Path(path).resolve().relative_to(REPO))
    except ValueError:
        return str(path)


def cmd_build(args):
    cache = Path(args.cache_dir)
    ranking, tranco_meta, tranco_path = load_tranco(args.list_id, cache, args.refresh)
    buckets, crux_path = load_crux(args.crux_month, cache, args.refresh)
    ut1, ut1_path = load_ut1(cache, args.refresh)
    oisd, oisd_path = load_oisd(cache, args.refresh)
    checked, check_meta = read_check_csvs(args.check_csv)
    adult_over = read_overrides(args.adult_overrides, ('porn', 'adult', 'no'))
    endpoint_over = read_overrides(args.endpoint_overrides, ('exclude', 'flag'))

    # Candidates: CrUX-matched Tranco domains, in Tranco order, up to the last one checked.
    cands = []
    for rank, domain, origin, bucket in crux_candidates(ranking, buckets):
        row = checked.get(origin.rstrip('/'))
        if row is None:
            break
        cands.append({'tranco_rank': rank, 'domain': domain, 'crux_origin': origin,
                      'crux_bucket': bucket, 'check': row})
    if not cands:
        sys.exit('none of the CrUX-matched Tranco domains is in the check CSV')
    missing_later = sum(1 for c in crux_candidates(ranking, buckets)
                        if c[0] > cands[-1]['tranco_rank'] and c[2].rstrip('/') in checked)
    if missing_later:
        print(f'warning: {missing_later} checked origins come after the first unchecked candidate '
              f'and are ignored', file=sys.stderr)

    names = set()
    for c in cands:
        final = host_of(c['check'].get('final_url') or '')
        c['names'] = list(OrderedDict.fromkeys(
            n for n in (c['domain'], host_of(c['crux_origin']), final) if n))
        names.update(c['names'])
    answers = resolve_all(names, cache / 'dns-answers.json', args.refresh_dns)

    rows, seen_hosts, position, undecided = [], {}, 0, []
    for c in cands:
        chk = c['check']
        verdict = chk['verdict']
        final_url = chk.get('final_url') or ''
        measured, notes = clean_url(final_url) if final_url else ('', [])
        host = host_of(final_url)
        sig = []
        if cf_family_fires(c['names'], answers):
            sig.append('cf-family')
        if listed(c['names'], ut1):
            sig.append('ut1')
        if listed(c['names'], oisd):
            sig.append('oisd')
        if chk.get('rta_label') == 'yes':
            sig.append('rta')

        flags = list(notes)
        if host and not (host == c['domain'] or host.endswith('.' + c['domain'])):
            flags.append(f'other domain: {host}')
        if CONSENT_HOST.match(host):
            # The gate's URL holds a session, so a later run would load a stale one.
            measured = c['crux_origin'] + '/'
            flags.append('consent gate on another host, the origin is listed instead')
        if GEO_TITLE.search(chk.get('title', '')):
            flags.append('geo-blocked: the title says the site is not available here')
        path = urlsplit(final_url).path if final_url else ''
        if host and (LOGIN_HOST.match(host) or LOGIN_PATH.search(path)
                     or re.search(r'\b(log ?in|sign ?in)\b', chk.get('title', ''), re.I)):
            flags.append('login page')

        out = {'tranco_rank': c['tranco_rank'], 'domain': c['domain'],
               'crux_origin': c['crux_origin'], 'crux_bucket': c['crux_bucket'],
               'check_verdict': verdict, 'check_detail': chk.get('detail', ''),
               'final_url': final_url, 'measured_url': measured if verdict == 'ok' else '',
               'title': chk.get('title', ''), 'adult_signals': ' '.join(sig),
               'adult_decision': '', 'adult_reason': '', 'dup_of': '', 'flags': '',
               'excluded_reason': '', 'included': 'no', 'list_position': ''}
        reason = ''
        ep = endpoint_over.get(c['domain'])
        if ep:
            flags.append(f'endpoint, {ep[0]}: {ep[1]}')
        if verdict != 'ok':
            reason = f'check: {verdict}' + (f' ({chk.get("detail")})' if chk.get('detail') else '')
        elif ep and ep[0] == 'exclude':
            reason = f'not a website: {ep[1]}'
        if not reason:
            if c['domain'] in adult_over:
                decision, why = adult_over[c['domain']]
                why = f'by title: {why}'
            elif not sig:
                decision, why = 'no', 'no signal'
            elif len(sig) == len(SIGNALS):
                decision, why = 'porn', 'all four signals'
            else:
                decision, why = 'undecided', 'signals disagree, no decision in the overrides file'
                undecided.append(out)
            out['adult_decision'], out['adult_reason'] = decision, why
            if decision == 'porn':
                reason = 'adult: porn'
            elif decision == 'adult':
                reason = 'adult: not porn'
            elif decision == 'undecided':
                reason = 'adult: undecided'
        if not reason:
            # The host of what is measured: for a consent gate, the origin's.
            key = host_of(measured) or host
            if key in seen_hosts:
                out['dup_of'] = seen_hosts[key]
                reason = f'duplicate: same final host {key} as {seen_hosts[key]}'
            else:
                seen_hosts[key] = c['domain']
        out['flags'] = ' | '.join(flags)
        if not reason and position < args.count:
            position += 1
            out['included'], out['list_position'] = 'yes', position
        elif not reason:
            out['excluded_reason'] = 'reserve: beyond the first count survivors'
        out['excluded_reason'] = reason or out['excluded_reason']
        rows.append(out)

    if undecided:
        print(f'\n{len(undecided)} candidates have signals that disagree and no decision in '
              f'{args.adult_overrides}:', file=sys.stderr)
        for r in undecided:
            print(f"  {r['domain']},{r['adult_signals']},{r['title']}", file=sys.stderr)
        if not args.allow_undecided:
            return 2
    if position < args.count:
        print(f'only {position} of {args.count} sites survive among {len(cands)} candidates. '
              f'Check more with tools/build-site-list.py check --skip {len(cands)}', file=sys.stderr)
        if not args.allow_short:
            return 3

    write_audit(args.audit, rows)
    write_list(args, rows, tranco_meta, check_meta,
               {'tranco': tranco_path, 'crux': crux_path, 'ut1': ut1_path, 'oisd': oisd_path},
               answers, len(cands))
    print(f'wrote {rel(args.out)} ({position} sites) and {rel(args.audit)} ({len(rows)} candidates)',
          file=sys.stderr)
    return 0


def write_audit(path, rows):
    with Path(path).open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=AUDIT_FIELDS)
        w.writeheader()
        w.writerows(rows)


def write_list(args, rows, tranco_meta, check_meta, sources, answers, n_cands):
    chosen = [r for r in rows if r['included'] == 'yes']
    last_rank = chosen[-1]['tranco_rank'] if chosen else 0
    scope = [r for r in rows if r['tranco_rank'] <= last_rank]
    unmatched = last_rank - len(scope)
    drop = Counter()
    verdicts = Counter()
    for r in scope:
        reason = r['excluded_reason']
        if reason.startswith('check: '):
            drop['check'] += 1
            verdicts[r['check_verdict']] += 1
        elif reason.startswith('not a website'):
            drop['endpoint'] += 1
        elif reason == 'adult: porn':
            drop['porn'] += 1
        elif reason == 'adult: not porn':
            drop['adult'] += 1
        elif reason.startswith('duplicate'):
            drop['dup'] += 1
        elif reason == 'adult: undecided':
            drop['undecided'] += 1
    by_title = [r for r in scope if r['adult_reason'].startswith('by title')]
    endpoints = [r for r in scope if 'endpoint, ' in r['flags']]
    flagged_endpoints = [r for r in chosen if 'endpoint, flag' in r['flags']]
    logins = [r for r in chosen if 'login page' in r['flags']]
    other = [r for r in chosen if 'other domain' in r['flags']]
    trimmed = [r for r in chosen if 'removed' in r['flags']]
    consent = [r for r in chosen if 'consent gate' in r['flags']]
    geo = [r for r in chosen if 'geo-blocked' in r['flags']]
    dns_dates = sorted({v.get('date', '') for v in answers.values()} - {''})
    cfg = tranco_meta.get('configuration', {})
    check_days = sorted({f'{w:%Y-%m-%d}' for _, w, _ in check_meta})

    def para(text, indent=''):
        hang = indent + ('   ' if re.match(r'\d\. ', text) else '')
        return '\n'.join(textwrap.wrap(text, 78, initial_indent='# ' + indent,
                                       subsequent_indent='# ' + hang,
                                       break_on_hyphens=False, break_long_words=False))

    decided_no = sum(1 for r in by_title if r['adult_decision'] == 'no')
    parts = [
        para(f'The {len(chosen)} most popular websites in Tranco list {args.list_id} that a '
             f'benchmark run can measure, without adult sites. One URL per line, in Tranco '
             f'order, for tools/submit.py --url-file and bot-wall-check/check.py.'),
        '#',
        '# Sources',
        f"#   Ranking   Tranco list {args.list_id}, created {tranco_meta.get('created_on', '?')[:10]},",
        f"#             providers {', '.join(cfg.get('providers', []))},",
        f"#             {cfg.get('startDate', '?')} to {cfg.get('endDate', '?')}, pay-level domains, "
        f"{cfg.get('combinationMethod', '?')} combination",
        f'#             {TRANCO_URL.format(id=args.list_id)}',
        f"#             sha256 {sha256(sources['tranco'])}",
        '#             Le Pochat et al., "Tranco: A Research-Oriented Top Sites',
        '#             Ranking Hardened Against Manipulation", NDSS 2019',
        f'#   Websites  Chrome UX Report, global, {args.crux_month[:4]}-{args.crux_month[4:]}, '
        f'all rank buckets',
        f'#             {CRUX_URL.format(month=args.crux_month)}',
        f"#             sha256 {sha256(sources['crux'])}",
        f"#   Adult     Cloudflare 1.1.1.3 against 1.1.1.2, queried {', '.join(dns_dates)}",
        f"#             UT1 adult category, downloaded {file_date(sources['ut1'])}",
        f'#             {UT1_URL}',
        f'#             (Universite Toulouse Capitole, https://dsi.ut-capitole.fr/blacklists/)',
        f"#             OISD nsfw, downloaded {file_date(sources['oisd'])}",
        f'#             {OISD_URL}',
        '#             the RTA label RTA-5042-1996-1400-1577-RTA in the page check.py fetched',
        '#',
        '# The rule',
        para('A Tranco domain d is taken if its homepage is a website, a run can measure it, it '
             'is not an adult site and its page is not already on the list. The first '
             f'{args.count} such domains in Tranco order are the list.', '  '),
        para('1. Website means CrUX has one of https://www.d, https://d, http://www.d or '
             'http://d, tried in that order. The first one it has is the origin that is checked.', '  '),
        para('2. Measurable means bot-wall-check/check.py gave that origin the verdict ok. Its '
             'final URL after redirects is what is listed, so the measured load starts without '
             'a redirect. A query made only of tracking parameters is removed, and so is a '
             'fragment. A site that redirects to a consent gate on another host, with a session '
             'in the URL, is listed by its origin instead.', '  '),
        para('3. Adult was decided by four signals. These are the family resolver against the '
             'malware resolver, UT1, OISD and the RTA label. No signal means not adult and all '
             'four mean porn. Every site they disagreed on was decided by its title and domain, '
             'with the reason in tools/adult-overrides.csv. Porn and other adult sites (escort '
             'and sex-work ads, hookup and swinger sites, erotic stories, fetish communities) '
             'are both left out.', '  '),
        para('4. Endpoints with no content of their own, such as connectivity checks, are left '
             'out, as listed in tools/endpoint-overrides.csv.', '  '),
        para('5. Two domains whose final URL has the same host count once, at the better '
             'Tranco rank.', '  '),
        '#',
        para(f"The bot-wall check ran on {', '.join(check_days)} and left from "
             f'{args.check_network}. Bot managers treat addresses differently, so check again '
             f'from the benchmark machine before a sweep.'),
    ]
    for p_, w, n in check_meta:
        parts.append(f'#   {rel(p_)}')
        parts.append(f'#     {n} origins, started {w:%Y-%m-%d %H:%M}')
    parts += [
        '#',
        f'# From Tranco rank 1 to {last_rank}, the rank of the last site here, these are the counts.',
        f'#   {last_rank:5d} Tranco domains',
        f'#   {unmatched:5d} without a homepage origin in CrUX',
        f'#   {len(scope):5d} candidates, of which these are left out',
        f"#   {drop['check']:5d} check verdict not ok",
    ]
    for v, k in sorted(((v, k) for k, v in verdicts.items()), reverse=True):
        parts.append(f'#         {v:5d} {k}')
    parts += [
        f"#   {drop['endpoint']:5d} not a website although the check said ok (connectivity checks and the like)",
        f"#   {drop['porn']:5d} porn",
        f"#   {drop['adult']:5d} adult sites that are not porn",
        f"#   {drop['dup']:5d} duplicates of a better-ranked domain's final host",
    ]
    if drop['undecided']:
        parts.append(f"#   {drop['undecided']:5d} adult signals disagree and no decision was made")
    parts += [
        f'#   {len(chosen):5d} on this list',
        '#',
        para(f'{len(by_title)} candidates in that range were decided by title, {decided_no} of '
             f'them as not adult.'),
        '#',
        para('These are still in and flagged in sites_tranco_candidates.csv.'),
        f'#   {len(logins):5d} final URLs that look like a login page',
        f'#   {len(other):5d} final URLs on another domain than the Tranco one',
        f'#   {len(trimmed):5d} final URLs with a tracking query or a fragment removed',
        f'#   {len(consent):5d} consent gates on another host, listed by their origin',
        f'#   {len(geo):5d} pages whose title says the site is not available here',
        f'#   {len(flagged_endpoints):5d} flagged endpoints',
    ]
    if endpoints:
        parts += ['#', '# Not websites, from tools/endpoint-overrides.csv, with what the check said.']
        for r in endpoints:
            flag = next(f for f in r['flags'].split(' | ') if f.startswith('endpoint, '))
            decision, why = flag[len('endpoint, '):].split(': ', 1)
            done = {'exclude': 'excluded', 'flag': 'flagged'}[decision]
            parts.append(f"#   {r['domain']} (check verdict {r['check_verdict']}, {done})")
            parts.append(para(why, '      '))
    parts += [
        '#',
        para(f'The {len(rows) - len(scope)} candidates checked beyond rank {last_rank} are the '
             f'reserve to refill from. Every candidate and the reason it is in or out are in '
             f'sites_tranco_candidates.csv.'),
        '#',
        para('The sources, and the answers of the two resolvers, are downloaded once into '
             '--cache-dir. Rebuilt from the same cache, the list comes out the same. UT1, OISD '
             'and the resolvers change daily, so a fresh cache can decide a few sites '
             'differently.'),
        '#',
        f'# Built {datetime.date.today().isoformat()} with these commands.',
        f'#   tools/build-site-list.py check --list-id {args.list_id} '
        f'--crux-month {args.crux_month} --candidates {n_cands}',
        f'#   tools/build-site-list.py build --list-id {args.list_id} '
        f'--crux-month {args.crux_month} --count {args.count} \\',
    ]
    for p_, _, _ in check_meta:
        parts.append(f'#       --check-csv {rel(p_)} \\')
    parts.append(f"#       --check-network '{args.check_network}'")
    with Path(args.out).open('w', encoding='utf-8') as f:
        f.write('\n'.join(parts) + '\n\n')
        for r in chosen:
            f.write(r['measured_url'] + '\n')


# --- main ----------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description='Build the benchmark site list from Tranco and CrUX.',
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = p.add_subparsers(dest='cmd', required=True)

    def common(sp):
        sp.add_argument('--list-id', default='JZW4Y', help='Tranco list ID (default JZW4Y)')
        sp.add_argument('--crux-month', default='202610', help='CrUX month as YYYYMM (default 202610)')
        sp.add_argument('--cache-dir', default=str(default_cache()),
                        help='where downloads and DNS answers are kept (default %(default)s)')
        sp.add_argument('--refresh', action='store_true', help='download the sources again')

    c = sub.add_parser('check', help='run bot-wall-check/check.py on the CrUX-matched candidates')
    common(c)
    c.add_argument('--candidates', type=int, default=2000, help='how many to check (default 2000)')
    c.add_argument('--skip', type=int, default=0, help='skip the first N, to extend an earlier check')
    c.add_argument('--jobs', type=int, default=16)
    c.add_argument('--timeout', type=int, default=30)

    b = sub.add_parser('build', help='filter the checked candidates and write the list')
    common(b)
    b.add_argument('--check-csv', action='append', required=True,
                   help='a check.py result CSV, repeatable')
    b.add_argument('--check-network', default='an unrecorded network',
                   help="where the check left from, as check.py's 'leaving from' line says")
    b.add_argument('--count', type=int, default=1000)
    b.add_argument('--adult-overrides', default=str(HERE / 'adult-overrides.csv'))
    b.add_argument('--endpoint-overrides', default=str(HERE / 'endpoint-overrides.csv'))
    b.add_argument('--out', default=str(REPO / 'sites_tranco_top1000.txt'))
    b.add_argument('--audit', default=str(REPO / 'sites_tranco_candidates.csv'))
    b.add_argument('--refresh-dns', action='store_true', help='ask the resolvers again')
    b.add_argument('--allow-undecided', action='store_true',
                   help='write the files although some adult decisions are missing')
    b.add_argument('--allow-short', action='store_true',
                   help='write the files although fewer than --count sites survive')
    args = p.parse_args()
    return cmd_check(args) if args.cmd == 'check' else cmd_build(args)


if __name__ == '__main__':
    sys.exit(main())
