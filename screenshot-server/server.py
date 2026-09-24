#!/usr/bin/env python3
"""Receive the benchmark's screenshots and show them in one gallery.

    screenshot-server/server.py --host 127.0.0.1 --data-dir ./screenshots

A run on the GMT cluster keeps no files, so after a page has loaded the run
uploads a PNG of what it rendered to this server. The gallery puts the latest
shot of every site next to each other, one per driver and variant, so that a bot
wall, an error page or a blank window stands out while scrolling through a few
hundred sites. A run whose screenshot shows one of those measured that page and
not the site.

There is no authentication, by design: anyone who can reach the server can
upload and view. It is meant to live for one benchmark run, behind the operator's
own HTTPS reverse proxy, and to be deleted with its screenshots afterwards. The
server itself speaks plain HTTP only.

Standard library only and Python 3.9 or newer, so it runs on whatever box has a
python3. screenshot-server/README.md has the HTTP API and docker-compose.yml.
"""
import argparse
import html
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

DRIVERS = ('parrot', 'playwright')
VARIANTS = ('xvfb', 'headful', 'headless')
PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'
MAX_TITLE = 300
MAX_SITE = 150
# Long enough for any real page URL, short enough to keep index lines sane.
MAX_PAGE = 2048
INDEX = 'index.jsonl'

# What derive_site() can produce. Checked again on every path that comes from a
# request or from the index, so neither can name a file outside the data dir.
SITE_RE = re.compile(r'[A-Za-z0-9-][A-Za-z0-9._-]{0,149}')
SHOT_RE = re.compile(r'[A-Za-z0-9-][A-Za-z0-9._-]{0,200}\.png')

# A rejected client may still be sending its body. See Handler._linger().
LINGER_SECONDS = 2.0

CSP = ("default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; "
       "form-action 'self'; base-uri 'none'; frame-ancestors 'none'")

CSS = """
:root{color-scheme:light dark}
body{font:14px/1.4 system-ui,sans-serif;margin:0;padding:12px 16px}
a{color:inherit}
h1{font-size:20px;margin:0 0 4px}
h2{font-size:16px;margin:0;overflow-wrap:anywhere}
form{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0 4px}
input,select,button{font:inherit}
section{border-top:1px solid #8886;padding:12px 0}
.meta{margin:2px 0 8px;color:#888;overflow-wrap:anywhere}
.shots{display:flex;flex-wrap:wrap;gap:12px}
figure{margin:0;width:360px;max-width:100%}
img,.none{display:block;width:100%;height:auto;aspect-ratio:16/9;object-fit:cover;
  object-position:top;border:1px solid #8886;background:#8882;box-sizing:border-box}
.none{display:flex;align-items:center;justify-content:center;color:#888}
figcaption{font-size:12px;margin-top:4px;overflow-wrap:anywhere}
"""


@dataclass
class Config:
    data_dir: Path
    max_bytes: int = 20_000_000


def utcnow():
    # A module function so that tests can pin the clock.
    return datetime.now(timezone.utc)


def derive_site(page):
    """Name the directory that a page's screenshots go to.

    The name is one directory directly below the data dir, so it holds nothing a
    filesystem or a URL treats specially: no slash, no leading dot, and no "..".
    Runs of dots are collapsed for that last reason, which no host name misses.
    """
    page = page.strip()
    try:
        parts = urlsplit(page if '://' in page else '//' + page)
        # Userinfo is dropped: a password in a page URL does not belong in a
        # directory name.
        rest = parts.netloc.rpartition('@')[2].lower() + parts.path
    except ValueError:
        rest = page.partition('://')[2] or page
    site = re.sub(r'[^A-Za-z0-9.-]', '_', rest)
    site = re.sub(r'_+', '_', site)
    site = re.sub(r'\.+', '.', site)
    site = site.strip('_.')[:MAX_SITE].strip('_.')
    return site or 'unknown'


def parse_upload_query(query):
    """Return (params, None) for a valid upload query string, or (None, problem)."""
    fields = parse_qs(query, keep_blank_values=True)

    def first(name):
        values = fields.get(name)
        return values[0] if values else None

    page = (first('page') or '').strip()
    driver, variant = first('driver'), first('variant')
    if not page:
        return None, 'page is required'
    if len(page) > MAX_PAGE:
        return None, f'page is longer than {MAX_PAGE} characters'
    if any(ord(char) < 32 or ord(char) == 127 for char in page):
        return None, 'page contains control characters'
    if driver not in DRIVERS:
        return None, 'driver must be one of: ' + ', '.join(DRIVERS)
    if variant not in VARIANTS:
        return None, 'variant must be one of: ' + ', '.join(VARIANTS)
    title = (first('title') or '').strip()[:MAX_TITLE]
    return {'page': page, 'driver': driver, 'variant': variant, 'title': title}, None


def store_screenshot(data_dir, lock, params, body, remote):
    """Write one upload into place and index it. Returns the path below data_dir."""
    site = derive_site(params['page'])
    site_dir = data_dir / site
    site_dir.mkdir(exist_ok=True)
    # The leading dot keeps a half-written file out of SHOT_RE, so it is never served.
    fd, tmp = tempfile.mkstemp(dir=site_dir, prefix='.upload-', suffix='.tmp')
    try:
        # mkstemp creates the file 0600, and it keeps that mode through the
        # rename. In the container that means root-only files on the host, which
        # nobody can copy off the server without sudo.
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, 'wb') as out:
            out.write(body)
            out.flush()
            os.fsync(out.fileno())
        # Choosing the name, moving the file and appending its line happen under
        # one lock: two uploads in the same second then cannot pick the same
        # name, and the index stays in upload order, which the gallery relies on.
        with lock:
            now = utcnow()
            stem = f'{now:%Y%m%dT%H%M%SZ}-{params["driver"]}-{params["variant"]}'
            name, n = f'{stem}.png', 2
            while (site_dir / name).exists():
                name, n = f'{stem}-{n}.png', n + 1
            os.replace(tmp, site_dir / name)
            entry = {
                'time': f'{now:%Y-%m-%dT%H:%M:%SZ}',
                'page': params['page'],
                'site': site,
                'driver': params['driver'],
                'variant': params['variant'],
                'title': params['title'],
                'file': f'{site}/{name}',
                'bytes': len(body),
                'remote': remote,
            }
            with open(data_dir / INDEX, 'a', encoding='utf-8') as index:
                index.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return entry['file']


def read_index(data_dir):
    """All index entries that are safe to render, in upload order."""
    entries = []
    try:
        with open(data_dir / INDEX, encoding='utf-8', errors='replace') as index:
            for line in index:
                try:
                    entry = json.loads(line)
                except ValueError:
                    # A line still being appended, or a hand edit gone wrong.
                    continue
                if usable_entry(entry):
                    entries.append(entry)
    except FileNotFoundError:
        pass
    return entries


def usable_entry(entry):
    keys = ('time', 'page', 'site', 'driver', 'variant', 'title', 'file')
    if not isinstance(entry, dict) or not all(isinstance(entry.get(k), str) for k in keys):
        return False
    site, _, name = entry['file'].partition('/')
    return site == entry['site'] and bool(SITE_RE.fullmatch(site) and SHOT_RE.fullmatch(name))


def combo_key(combo):
    driver, variant = combo
    return (DRIVERS.index(driver) if driver in DRIVERS else len(DRIVERS), driver,
            VARIANTS.index(variant) if variant in VARIANTS else len(VARIANTS), variant)


def html_page(title, content):
    return ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{html.escape(title)}</title><style>{CSS}</style></head>'
            f'<body>{content}</body></html>\n')


def figure_html(entry):
    url = html.escape('/shots/' + quote(entry['file']))
    driver, variant = html.escape(entry['driver']), html.escape(entry['variant'])
    when = html.escape(entry['time'].replace('T', ' ').replace('Z', ' UTC'))
    title = html.escape(entry['title']) or '<i>no title</i>'
    alt = html.escape(f"{entry['driver']} {entry['variant']} screenshot of {entry['page']}")
    return (f'<figure><a href="{url}"><img loading="lazy" src="{url}" width="360" alt="{alt}"></a>'
            f'<figcaption><b>{driver}, {variant}</b> {when}<br>{title}</figcaption></figure>')


def plural(count, word):
    return f'{count} {word}' if count == 1 else f'{count} {word}s'


def site_link(site, text):
    return f'<a href="/site/{html.escape(quote(site))}">{html.escape(text)}</a>'


def select_html(name, current, options):
    choices = [f'<option value="">any {name}</option>']
    for option in options:
        selected = ' selected' if option == current else ''
        choices.append(f'<option{selected}>{html.escape(option)}</option>')
    return f'<select name="{name}">{"".join(choices)}</select>'


def render_gallery(entries, q='', driver='', variant=''):
    shot_counts = Counter(entry['site'] for entry in entries)
    needle = q.strip().lower()
    matching = [e for e in entries
                if (not needle or needle in e['site'].lower() or needle in e['page'].lower())
                and (not driver or e['driver'] == driver)
                and (not variant or e['variant'] == variant)]
    # Later lines are later uploads, so the last one per combination wins.
    latest, pages = {}, {}
    for entry in matching:
        latest.setdefault(entry['site'], {})[(entry['driver'], entry['variant'])] = entry
        pages[entry['site']] = entry['page']
    # Only combinations that occur at all get a column, and every site gets the
    # same columns in the same order, so a missing or odd shot is easy to spot.
    combos = sorted({combo for shots in latest.values() for combo in shots}, key=combo_key)

    totals = f"{plural(len(latest), 'site')}, {plural(len(matching), 'screenshot')}"
    if needle or driver or variant:
        totals += f" match, of {plural(len(shot_counts), 'site')} and {len(entries)} in all"
    parts = [
        '<header><h1>Screenshots</h1>',
        f'<p>{html.escape(totals)}</p>',
        '<form method="get" action="/">',
        f'<input type="search" name="q" value="{html.escape(q)}" placeholder="site or page">',
        select_html('driver', driver, DRIVERS),
        select_html('variant', variant, VARIANTS),
        '<button>Filter</button></form></header>',
    ]
    if not latest:
        parts.append('<p>No screenshots match.</p>' if entries else '<p>No screenshots yet.</p>')
    for site in sorted(latest):
        shots = latest[site]
        count = plural(shot_counts[site], 'shot')
        parts.append(f'<section><h2>{site_link(site, site)}</h2>'
                     f'<p class="meta">{site_link(site, count)}, {html.escape(pages[site])}</p>'
                     '<div class="shots">')
        for combo in combos:
            if combo in shots:
                parts.append(figure_html(shots[combo]))
            else:
                missing = html.escape(f'no {combo[0]}, {combo[1]} shot')
                parts.append(f'<figure><div class="none">{missing}</div></figure>')
        parts.append('</div></section>')
    return html_page('Screenshots', ''.join(parts))


def render_site(entries, site):
    """Every shot of one site, newest first, or None for a site with no shots."""
    shots = [entry for entry in entries if entry['site'] == site]
    if not shots:
        return None
    shots.reverse()
    content = (f'<header><p><a href="/">All sites</a></p><h1>{html.escape(site)}</h1>'
               f"<p>{plural(len(shots), 'screenshot')}</p></header>"
               '<div class="shots">' + ''.join(figure_html(entry) for entry in shots) + '</div>')
    return html_page(site, content)


def printable(text, limit=2000):
    return ''.join(c if c.isprintable() else f'\\x{ord(c):02x}' for c in text[:limit])


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'screenshot-server'
    sys_version = ''
    # Idle keep-alive connections and stalled uploads give their thread back.
    timeout = 60
    _unread_body = False

    def __getattr__(self, name):
        # Every method goes through _dispatch, so an unknown one gets the same
        # 404 or 405 as a known one instead of http.server's 501.
        if name.startswith('do_'):
            return self._dispatch
        raise AttributeError(name)

    def _dispatch(self):
        url = urlsplit(self.path)
        path = url.path
        self._unread_body = self._declares_body()
        if path == '/upload':
            if self.command not in ('PUT', 'POST'):
                return self._not_allowed('PUT, POST')
            return self._upload(url.query)
        if path == '/healthz':
            if self.command != 'GET':
                return self._not_allowed('GET')
            return self._send(200, 'ok', 'text/plain; charset=utf-8')
        if path == '/' or path.startswith(('/site/', '/shots/')):
            if self.command != 'GET':
                return self._not_allowed('GET')
            if path == '/':
                return self._gallery(url.query)
            if path.startswith('/site/'):
                return self._site(path[len('/site/'):])
            return self._shot(path[len('/shots/'):])
        return self._not_found()

    # Responses

    def _start(self, status, content_type, length, headers=None):
        self.log_request(status, length)
        self.send_response_only(status)
        self.send_header('Date', self.date_time_string())
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(length))
        self.send_header('X-Content-Type-Options', 'nosniff')
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        if self._unread_body:
            self.send_header('Connection', 'close')
        self.end_headers()

    def _send(self, status, body, content_type, headers=None):
        body = body.encode('utf-8') if isinstance(body, str) else body
        self._start(status, content_type, len(body), headers)
        if self.command != 'HEAD':
            self.wfile.write(body)
        if self._unread_body:
            self._linger()

    def _send_json(self, status, payload, headers=None):
        if isinstance(payload, str):
            payload = {'error': payload}
        self._send(status, json.dumps(payload) + '\n', 'application/json', headers)

    def _send_html(self, text):
        self._send(200, text, 'text/html; charset=utf-8', {
            'Cache-Control': 'no-store',
            'Content-Security-Policy': CSP,
            'Referrer-Policy': 'no-referrer',
        })

    def _not_found(self):
        self._send(404, 'not found\n', 'text/plain; charset=utf-8')

    def _not_allowed(self, allow):
        self._send(405, 'method not allowed\n', 'text/plain; charset=utf-8', {'Allow': allow})

    def _declares_body(self):
        length = self.headers.get('Content-Length', '').strip()
        return 'Transfer-Encoding' in self.headers or length not in ('', '0')

    def _linger(self):
        # Closing a socket that still holds unread request bytes makes the kernel
        # answer with a RST, and a client that is still sending its body then
        # loses the error response and reports a reset connection instead. So stop
        # writing and throw away whatever arrives for a moment before closing.
        # Nothing is kept and the wait is bounded, so the body is not read.
        try:
            self.connection.shutdown(socket.SHUT_WR)
            deadline = time.monotonic() + LINGER_SECONDS
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.connection.settimeout(remaining)
                if not self.connection.recv(65536):
                    break
        except OSError:
            pass

    # Upload

    def handle_expect_100(self):
        # curl sends "Expect: 100-continue" with larger bodies and waits for the
        # go-ahead. Running every check that needs no body first means a
        # rejected upload is never transmitted at all.
        url = urlsplit(self.path)
        if url.path == '/upload' and self.command in ('PUT', 'POST'):
            error, _ = self._upload_precheck(url.query)
            if error:
                self._unread_body = self._declares_body()
                self._send_json(*error)
                return False
        return super().handle_expect_100()

    def _upload_precheck(self, query):
        """Return ((status, message), None) for a rejected upload, else (None, (length, params))."""
        lengths = self.headers.get_all('Content-Length') or []
        # A chunked body has no length to check against --max-bytes up front.
        if 'Transfer-Encoding' in self.headers or not lengths:
            return (411, 'Content-Length is required, chunked bodies are not accepted'), None
        if len(lengths) > 1 or not re.fullmatch(r'[0-9]{1,15}', lengths[0].strip()):
            return (400, 'invalid Content-Length'), None
        length = int(lengths[0])
        limit = self.server.config.max_bytes
        if length > limit:
            return (413, f'body of {length} bytes is over the limit of {limit} bytes'), None
        params, problem = parse_upload_query(query)
        if problem:
            return (400, problem), None
        return None, (length, params)

    def _upload(self, query):
        error, checked = self._upload_precheck(query)
        if error:
            return self._send_json(*error)
        length, params = checked
        self._unread_body = False
        try:
            body = self.rfile.read(length)
        except OSError:
            self.close_connection = True
            return
        if len(body) < length:
            self.close_connection = True
            return self._send_json(400, 'the body ended before Content-Length bytes')
        if not body.startswith(PNG_SIGNATURE):
            return self._send_json(400, 'the body is not a PNG')
        try:
            name = store_screenshot(self.server.config.data_dir, self.server.store_lock,
                                    params, body, self.client_address[0])
        except OSError as error:
            self.server.log(f'storing an upload for {printable(params["page"])} failed: {error}')
            return self._send_json(500, 'could not store the screenshot')
        self._send_json(201, {'file': name}, {'Location': '/shots/' + quote(name)})

    # Viewing

    def _gallery(self, query):
        fields = parse_qs(query)
        q, driver, variant = ((fields.get(k) or [''])[0] for k in ('q', 'driver', 'variant'))
        entries = read_index(self.server.config.data_dir)
        self._send_html(render_gallery(entries, q, driver, variant))

    def _site(self, raw):
        site = unquote(raw)
        page = None
        if SITE_RE.fullmatch(site):
            page = render_site(read_index(self.server.config.data_dir), site)
        if page is None:
            return self._not_found()
        self._send_html(page)

    def _shot(self, raw):
        parts = [unquote(part) for part in raw.split('/')]
        if len(parts) != 2 or not SITE_RE.fullmatch(parts[0]) or not SHOT_RE.fullmatch(parts[1]):
            return self._not_found()
        root = self.server.config.data_dir
        path = (root / parts[0] / parts[1]).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            return self._not_found()
        try:
            shot = open(path, 'rb')
        except OSError:
            return self._not_found()
        with shot:
            length = os.fstat(shot.fileno()).st_size
            self._start(200, 'image/png', length, {'Cache-Control': 'private, max-age=86400'})
            shutil.copyfileobj(shot, self.wfile)

    # Logging

    def log_request(self, code='-', size='-'):
        code = getattr(code, 'value', code)
        line = printable(getattr(self, 'requestline', ''))
        self.server.log(f'{self.client_address[0]} "{line}" {code} {size}')

    def log_error(self, format, *args):
        # http.server reports its own errors here and again in log_request().
        pass

    def log_message(self, format, *args):
        self.server.log(printable(format % args))


class ScreenshotServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, config, log_stream=None):
        config.data_dir = Path(config.data_dir).resolve()
        self.config = config
        self.log_stream = log_stream if log_stream is not None else sys.stderr
        self.store_lock = threading.Lock()
        if ':' in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, Handler)

    def log(self, message):
        self.log_stream.write(f'{utcnow():%Y-%m-%dT%H:%M:%SZ} {message}\n')
        self.log_stream.flush()


def positive_int(text):
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError(f'{text} is not a positive number')
    return value


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='Receive benchmark screenshots over plain HTTP and show them in a gallery.',
        epilog='There is no authentication: run it only for as long as a benchmark needs '
               'it, behind an HTTPS reverse proxy.')
    parser.add_argument('--host', default='0.0.0.0', help='address to listen on (default: 0.0.0.0)')
    parser.add_argument('--port', type=int, default=8765, help='port to listen on (default: 8765)')
    parser.add_argument('--data-dir', type=Path, default=Path('./screenshots'),
                        help='where screenshots and index.jsonl go (default: ./screenshots)')
    parser.add_argument('--max-bytes', type=positive_int, default=20_000_000,
                        help='largest accepted upload in bytes (default: 20000000)')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        args.data_dir.expanduser().mkdir(parents=True, exist_ok=True)
    except OSError as error:
        sys.exit(f'server.py: cannot create --data-dir {args.data_dir}: {error}')
    config = Config(data_dir=args.data_dir.expanduser(), max_bytes=args.max_bytes)
    try:
        httpd = ScreenshotServer((args.host, args.port), config)
    except OSError as error:
        sys.exit(f'server.py: cannot listen on {args.host} port {args.port}: {error}')
    httpd.log(f'listening on http://{args.host}:{httpd.server_address[1]}, '
              f'screenshots in {config.data_dir}')
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == '__main__':
    main()
