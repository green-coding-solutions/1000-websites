"""Tests for screenshot-server/server.py.

    python3 -m unittest discover -s screenshot-server -v

Every test that talks HTTP gets its own server on an ephemeral localhost port and
its own data dir, and goes through real sockets, so what is checked is what a
client sees, including the parts http.server decides (keep-alive, 100-continue).
"""
import base64
import csv
import hashlib
import http.client
import io
import json
import os
import socket
import struct
import tempfile
import threading
import unittest
import zlib
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

import server


def make_png():
    def chunk(kind, data):
        return (struct.pack('>I', len(data)) + kind + data
                + struct.pack('>I', zlib.crc32(kind + data)))
    return (server.PNG_SIGNATURE
            + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\x00\xff\x00\x00'))
            + chunk(b'IEND', b''))


PNG = make_png()
FORM = 'application/x-www-form-urlencoded'


def upload_path(page='https://www.amazon.de', driver='parrot', variant='xvfb', title='Amazon'):
    params = {'page': page, 'driver': driver, 'variant': variant, 'title': title}
    return '/upload?' + urlencode({k: v for k, v in params.items() if v is not None})


def at(second):
    moment = datetime(2026, 9, 17, 10, 28, second, tzinfo=timezone.utc)
    return mock.patch.object(server, 'utcnow', return_value=moment)


class SiteNameTest(unittest.TestCase):
    def test_examples(self):
        cases = {
            'https://www.amazon.de': 'www.amazon.de',
            'https://de.wikipedia.org/wiki/Wikipedia:Hauptseite':
                'de.wikipedia.org_wiki_Wikipedia_Hauptseite',
            'http://ribalba.de/': 'ribalba.de',
            'HTTPS://WWW.Example.COM/Path?q=1#top': 'www.example.com_Path',
            'https://user:secret@example.com/': 'example.com',
            'www.heise.de/news/': 'www.heise.de_news',
            '': 'unknown',
            '///...___': 'unknown',
        }
        for page, site in cases.items():
            with self.subTest(page=page):
                self.assertEqual(server.derive_site(page), site)

    def test_hostile_pages_stay_inside_the_data_dir(self):
        root = Path('/srv/shots')
        for page in ('https://evil/../../etc/passwd', '../../etc/passwd', 'https://../..',
                     'https://evil/%2e%2e/%2f/x', 'https://a/' + 'b/' * 200,
                     'https://evil/.hidden'):
            with self.subTest(page=page):
                site = server.derive_site(page)
                self.assertNotIn('/', site)
                self.assertNotIn('..', site)
                self.assertLessEqual(len(site), 150)
                self.assertTrue(server.SITE_RE.fullmatch(site), site)
                self.assertEqual(os.path.dirname(os.path.normpath(root / site)), str(root))
        self.assertEqual(server.derive_site('https://evil/../../etc/passwd'), 'evil_._._etc_passwd')


class ServerTestCase(unittest.TestCase):
    max_bytes = 100_000

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name).resolve()
        self.log = io.StringIO()
        config = server.Config(data_dir=self.data_dir, max_bytes=self.max_bytes)
        self.httpd = server.ScreenshotServer(('127.0.0.1', 0), config, log_stream=self.log)
        self.port = self.httpd.server_address[1]
        thread = threading.Thread(target=self.httpd.serve_forever, kwargs={'poll_interval': 0.05},
                                  daemon=True)
        thread.start()
        self.addCleanup(self.stop, thread)

    def stop(self, thread):
        self.httpd.shutdown()
        self.httpd.server_close()
        thread.join(5)

    def connect(self):
        return http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)

    def request(self, method, path, body=None, headers=None):
        conn = self.connect()
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response, response.read()
        finally:
            conn.close()

    def upload(self, path=None, body=PNG, method='PUT'):
        return self.request(method, path or upload_path(), body, {'Content-Type': 'image/png'})

    def uploaded(self, path=None):
        response, body = self.upload(path)
        self.assertEqual(response.status, 201, body)
        return json.loads(body)['file']

    def view(self, path):
        response, body = self.request('GET', path)
        return response, body.decode('utf-8', 'replace')

    def index(self):
        text = (self.data_dir / 'index.jsonl').read_text(encoding='utf-8')
        return [json.loads(line) for line in text.splitlines()]

    def post_tag(self, fields, accept='application/json', content_type=FORM):
        body = urlencode(fields).encode('utf-8') if isinstance(fields, dict) else fields
        headers = {'Content-Type': content_type}
        if accept:
            headers['Accept'] = accept
        return self.request('POST', '/tag', body, headers)

    def tagged(self, site='www.amazon.de', tag='bot-wall', note=''):
        response, body = self.post_tag({'site': site, 'tag': tag, 'note': note})
        self.assertEqual(response.status, 200, body)
        return json.loads(body)

    def tag_lines(self):
        path = self.data_dir / 'tags.jsonl'
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


class UploadTest(ServerTestCase):
    def test_upload_stores_the_file_and_an_index_line(self):
        response, body = self.upload(upload_path(title='Amazon.de: Günstige Preise'))
        self.assertEqual(response.status, 201)
        self.assertEqual(response.getheader('Content-Type'), 'application/json')
        name = json.loads(body)['file']
        self.assertRegex(name, r'^www\.amazon\.de/\d{8}T\d{6}Z-parrot-xvfb\.png$')
        self.assertEqual(response.getheader('Location'), '/shots/' + name)
        self.assertEqual((self.data_dir / name).read_bytes(), PNG)
        # The temp file was moved into place, not left behind.
        self.assertEqual(os.listdir(self.data_dir / 'www.amazon.de'), [Path(name).name])
        [entry] = self.index()
        self.assertEqual(list(entry), ['time', 'page', 'site', 'driver', 'variant', 'title',
                                       'file', 'bytes', 'remote'])
        self.assertRegex(entry['time'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
        self.assertEqual(entry['page'], 'https://www.amazon.de')
        self.assertEqual(entry['site'], 'www.amazon.de')
        self.assertEqual((entry['driver'], entry['variant']), ('parrot', 'xvfb'))
        self.assertEqual(entry['title'], 'Amazon.de: Günstige Preise')
        self.assertEqual((entry['file'], entry['bytes'], entry['remote']),
                         (name, len(PNG), '127.0.0.1'))

    def test_post_works_like_put_and_title_is_optional_and_truncated(self):
        response, _ = self.upload(upload_path(driver='playwright', variant='headful', title=None),
                                  method='POST')
        self.assertEqual(response.status, 201)
        self.uploaded(upload_path(title='x' * 500))
        first, second = self.index()
        self.assertEqual(first['title'], '')
        self.assertEqual(second['title'], 'x' * 300)

    def test_body_must_be_a_png(self):
        for body in (b'GIF89a' + PNG, b'', PNG[:7]):
            with self.subTest(body=body[:8]):
                response, _ = self.upload(body=body)
                self.assertEqual(response.status, 400)
        self.assertFalse((self.data_dir / 'index.jsonl').exists())

    def test_query_parameters_are_validated(self):
        cases = {
            'no page': upload_path(page=None),
            'blank page': upload_path(page='  '),
            'page with a newline': upload_path(page='https://a.example/\nx'),
            'no driver': upload_path(driver=None),
            'unknown driver': upload_path(driver='selenium'),
            'no variant': upload_path(variant=None),
            'blank variant': upload_path(variant=''),
            'variant in capitals': upload_path(variant='Firefox'),
            'variant with a leading dash': upload_path(variant='-firefox'),
            'variant with a slash': upload_path(variant='../x'),
            'variant with a dot': upload_path(variant='firefox.com'),
            'variant of 33 characters': upload_path(variant='a' * 33),
            'no query': '/upload',
        }
        for case, path in cases.items():
            with self.subTest(case=case):
                response, body = self.upload(path)
                self.assertEqual(response.status, 400, body)
        self.assertFalse((self.data_dir / 'index.jsonl').exists())

    def test_oversized_content_length_is_refused_before_the_body_is_sent(self):
        conn = self.connect()
        try:
            conn.putrequest('PUT', upload_path())
            conn.putheader('Content-Length', str(self.max_bytes + 1))
            conn.endheaders()
            # No body goes out: the answer must not wait for one.
            response = conn.getresponse()
            self.assertEqual(response.status, 413)
            self.assertEqual(response.getheader('Connection'), 'close')
        finally:
            conn.close()

    def test_oversized_body_gets_413_and_not_a_reset(self):
        response, _ = self.upload(body=PNG + bytes(self.max_bytes * 5))
        self.assertEqual(response.status, 413)

    def test_content_length_is_required(self):
        conn = self.connect()
        try:
            conn.putrequest('PUT', upload_path())
            conn.endheaders()
            self.assertEqual(conn.getresponse().status, 411)
        finally:
            conn.close()
        # http.client sends an iterable body chunked.
        response, _ = self.request('PUT', upload_path(), iter([PNG]))
        self.assertEqual(response.status, 411)

    def test_uploads_in_the_same_second_get_distinct_names(self):
        with at(3):
            names = [self.uploaded() for _ in range(3)]
        stem = 'www.amazon.de/20260917T102803Z-parrot-xvfb'
        self.assertEqual(names, [f'{stem}.png', f'{stem}-2.png', f'{stem}-3.png'])
        for name in names:
            self.assertEqual((self.data_dir / name).read_bytes(), PNG)
        self.assertEqual([entry['file'] for entry in self.index()], names)

    def test_expect_100_continue(self):
        def head(path, length):
            return (f'PUT {path} HTTP/1.1\r\nHost: localhost\r\nContent-Length: {length}\r\n'
                    'Expect: 100-continue\r\n\r\n').encode()

        address = ('127.0.0.1', self.port)
        # Rejected on the headers alone: the client never gets the go-ahead.
        for path, length, status in ((upload_path(), self.max_bytes + 1, b'413'),
                                     (upload_path(driver='selenium'), len(PNG), b'400')):
            with self.subTest(status=status):
                with socket.create_connection(address, timeout=10) as sock, \
                        sock.makefile('rb') as reader:
                    sock.sendall(head(path, length))
                    self.assertEqual(reader.readline().split()[1], status)
        with socket.create_connection(address, timeout=10) as sock, sock.makefile('rb') as reader:
            sock.sendall(head(upload_path(), len(PNG)))
            self.assertEqual(reader.readline().split()[1], b'100')
            self.assertEqual(reader.readline(), b'\r\n')
            sock.sendall(PNG)
            self.assertEqual(reader.readline().split()[1], b'201')

    def test_any_well_formed_variant_and_the_prescan_driver_are_accepted(self):
        with at(4):
            firefox = self.uploaded(upload_path(variant='firefox-com'))
            prescan = self.uploaded(upload_path(driver='prescan', variant='a' * 32))
        self.assertEqual(firefox, 'www.amazon.de/20260917T102804Z-parrot-firefox-com.png')
        self.assertEqual(prescan, 'www.amazon.de/20260917T102804Z-prescan-' + 'a' * 32 + '.png')
        self.assertEqual([(e['driver'], e['variant']) for e in self.index()],
                         [('parrot', 'firefox-com'), ('prescan', 'a' * 32)])

    def test_connection_is_kept_alive_after_a_good_upload(self):
        conn = self.connect()
        try:
            conn.request('PUT', upload_path(), PNG)
            response = conn.getresponse()
            response.read()
            self.assertEqual(response.status, 201)
            conn.request('GET', '/healthz')
            self.assertEqual(conn.getresponse().read(), b'ok')
        finally:
            conn.close()


class ViewTest(ServerTestCase):
    def test_healthz(self):
        response, body = self.request('GET', '/healthz')
        self.assertEqual((response.status, body), (200, b'ok'))

    def test_upload_and_viewing_need_no_authorization_header(self):
        conn = self.connect()
        try:
            # putrequest sends only Host and Accept-Encoding, nothing else.
            conn.putrequest('PUT', upload_path())
            conn.putheader('Content-Length', str(len(PNG)))
            conn.endheaders(PNG)
            response = conn.getresponse()
            name = json.loads(response.read())['file']
            self.assertEqual(response.status, 201)
        finally:
            conn.close()
        for path in ('/', '/site/www.amazon.de', '/shots/' + name):
            with self.subTest(path=path):
                response, _ = self.request('GET', path)
                self.assertEqual(response.status, 200)
                self.assertIsNone(response.getheader('WWW-Authenticate'))

    def test_gallery_lists_sites_and_escapes_what_was_uploaded(self):
        self.uploaded(upload_path(title='<script>alert(1)</script>'))
        self.uploaded(upload_path(page='https://de.wikipedia.org/wiki/Wikipedia:Hauptseite',
                                  driver='playwright', variant='headful', title='Wikipedia'))
        response, page = self.view('/')
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader('Content-Type'), 'text/html; charset=utf-8')
        self.assertIn("default-src 'none'", response.getheader('Content-Security-Policy'))
        self.assertNotIn('<script>alert(1)', page)
        self.assertEqual(page.count('<script>'), 1)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', page)
        self.assertIn('2 sites, 2 screenshots', page)
        self.assertIn('loading="lazy"', page)
        self.assertIn('href="/shots/www.amazon.de/', page)
        self.assertIn('href="/site/www.amazon.de"', page)
        wikipedia = page.index('<h2><a href="/site/de.wikipedia.org_wiki_Wikipedia_Hauptseite"')
        amazon = page.index('<h2><a href="/site/www.amazon.de"')
        self.assertLess(wikipedia, amazon)

    def test_gallery_shows_the_latest_shot_per_driver_and_variant(self):
        with at(1):
            self.uploaded(upload_path(title='the older one'))
        with at(2):
            self.uploaded(upload_path(title='the newer one'))
            self.uploaded(upload_path(variant='headful', title='the headful one'))
        _, page = self.view('/')
        self.assertIn('the newer one', page)
        self.assertIn('the headful one', page)
        self.assertNotIn('the older one', page)
        self.assertIn('3 shots', page)

    def test_gallery_filters(self):
        self.uploaded()
        self.uploaded(upload_path(page='https://de.wikipedia.org/', driver='playwright',
                                  variant='headful', title='Wikipedia'))
        amazon, wikipedia = 'href="/site/www.amazon.de"', 'href="/site/de.wikipedia.org"'
        _, page = self.view('/?q=AMAZON')
        self.assertIn(amazon, page)
        self.assertIn('1 site, 1 screenshot match, of 2 sites and 2 in all', page)
        self.assertNotIn(wikipedia, page)
        _, page = self.view('/?driver=playwright&q=')
        self.assertIn(wikipedia, page)
        self.assertNotIn(amazon, page)
        _, page = self.view('/?variant=xvfb&q=wiki')
        self.assertIn('No screenshots match', page)

    def test_known_variants_come_first_and_others_follow_sorted(self):
        for variant in ('zeta', 'firefox-com', 'headless', 'xvfb'):
            self.uploaded(upload_path(variant=variant))
        _, page = self.view('/')
        options = page[page.index('<select name="variant">'):]
        options = options[:options.index('</select>')]
        self.assertLess(options.index('<option>xvfb'), options.index('<option>headful'))
        self.assertLess(options.index('<option>headless'), options.index('<option>firefox-com'))
        self.assertLess(options.index('<option>firefox-com'), options.index('<option>zeta'))
        columns = [page.index(f'<b>parrot, {v}</b>') for v in ('xvfb', 'headless', 'firefox-com',
                                                              'zeta')]
        self.assertEqual(columns, sorted(columns))
        _, page = self.view('/?variant=firefox-com')
        self.assertIn('<option selected>firefox-com</option>', page)
        self.assertIn('<b>parrot, firefox-com</b>', page)
        self.assertNotIn('<b>parrot, xvfb</b>', page)

    def test_site_page_lists_every_shot_newest_first(self):
        with at(5):
            first = self.uploaded(upload_path(title='first'))
            second = self.uploaded(upload_path(title='second'))
        response, page = self.view('/site/www.amazon.de')
        self.assertEqual(response.status, 200)
        self.assertLess(page.index(second), page.index(first))
        self.assertLess(page.index('second'), page.index('first'))
        for path in ('/site/nope.example', '/site/..', '/site/', '/site/www.amazon.de/x'):
            with self.subTest(path=path):
                self.assertEqual(self.view(path)[0].status, 404)

    def test_shots_serves_the_png(self):
        name = self.uploaded()
        response, body = self.request('GET', '/shots/' + name)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader('Content-Type'), 'image/png')
        self.assertEqual(response.getheader('Cache-Control'), 'private, max-age=86400')
        self.assertEqual(body, PNG)

    def test_shots_refuses_anything_outside_the_data_dir(self):
        name = self.uploaded()
        site_dir = self.data_dir / 'www.amazon.de'
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        secret = Path(outside.name) / 'secret.png'
        secret.write_bytes(PNG)
        os.symlink(secret, site_dir / 'link.png')
        (site_dir / '.hidden.png').write_bytes(PNG)
        paths = [
            '/shots/../index.jsonl',
            '/shots/index.jsonl',
            '/shots/www.amazon.de',
            '/shots/www.amazon.de/../../etc/passwd',
            '/shots/%2e%2e/%2e%2e/etc/passwd',
            '/shots/..%2F..%2Fetc%2Fpasswd',
            '/shots/www.amazon.de/..%2F..%2Fsecret.png',
            '/shots//etc/passwd',
            '/shots/www.amazon.de/missing.png',
            '/shots/www.amazon.de/.hidden.png',
            '/shots/www.amazon.de/link.png',
            '/shots/' + name + '/',
            '/shots/' + name[:-len('.png')],
        ]
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.view(path)[0].status, 404)

    def test_unknown_paths_and_wrong_methods(self):
        self.assertEqual(self.view('/nope')[0].status, 404)
        self.assertEqual(self.request('GET', '/index.html')[0].status, 404)
        response, _ = self.request('GET', upload_path())
        self.assertEqual((response.status, response.getheader('Allow')), (405, 'PUT, POST'))
        for method, path in (('DELETE', '/healthz'), ('POST', '/'), ('BREW', '/healthz'),
                             ('HEAD', '/'), ('PUT', '/shots/x/y.png'),
                             ('POST', '/export.txt'), ('DELETE', '/export.csv')):
            with self.subTest(method=method, path=path):
                response, _ = self.request(method, path)
                self.assertEqual(response.status, 405)
                self.assertEqual(response.getheader('Allow'), 'GET')
        for method in ('GET', 'PUT'):
            with self.subTest(method=method, path='/tag'):
                response, _ = self.request(method, '/tag')
                self.assertEqual((response.status, response.getheader('Allow')), (405, 'POST'))

    def test_log_has_one_line_per_request_and_no_headers(self):
        self.uploaded()
        self.upload(body=b'not a png')
        self.view('/')
        self.request('GET', '/healthz', headers={'X-Private': 'keep-me-out-of-the-log'})
        log = self.log.getvalue()
        self.assertEqual(len(log.splitlines()), 4, log)
        self.assertNotIn('keep-me-out-of-the-log', log)
        self.assertIn('"GET /healthz HTTP/1.1" 200 2', log)


def read_urls(text):
    # What tools/submit.py's read_urls(), which tools/batch.py uses, does with a list.
    return [line.split('#', 1)[0].strip() for line in text.splitlines()
            if line.split('#', 1)[0].strip()]


def row_of(page, site):
    """The gallery row of one site."""
    start = page.index(f'<section class="row" id="s-{site}"')
    return page[start:page.index('</section>', start)]


class TagTest(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.uploaded()
        self.uploaded(upload_path(page='https://de.wikipedia.org/', driver='playwright',
                                  variant='headful', title='Wikipedia'))

    def test_tag_is_appended_and_the_last_line_per_site_wins(self):
        with at(7):
            first = self.tagged(tag='bot-wall', note='  Cloudflare challenge  ')
        self.assertEqual(first, {'site': 'www.amazon.de', 'tag': 'bot-wall',
                                 'note': 'Cloudflare challenge', 'time': '2026-09-17T10:28:07Z'})
        with at(8):
            self.tagged(tag='ok')
        lines = self.tag_lines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(list(lines[0]), ['time', 'site', 'tag', 'note', 'remote'])
        self.assertEqual([(line['tag'], line['note']) for line in lines],
                         [('bot-wall', 'Cloudflare challenge'), ('ok', '')])
        self.assertEqual(lines[1]['remote'], '127.0.0.1')
        current = server.read_tags(self.data_dir)
        self.assertEqual(list(current), ['www.amazon.de'])
        self.assertEqual(current['www.amazon.de']['tag'], 'ok')
        self.assertEqual(current['www.amazon.de']['time'], '2026-09-17T10:28:08Z')

    def test_tag_lines_that_are_broken_or_unsafe_are_ignored(self):
        self.tagged(tag='blank')
        with open(self.data_dir / 'tags.jsonl', 'a', encoding='utf-8') as tags:
            tags.write('{"half a line\n')
            tags.write(json.dumps({'time': 't', 'site': '../etc', 'tag': 'ok', 'note': ''}) + '\n')
            tags.write(json.dumps({'time': 't', 'site': 'www.amazon.de', 'tag': 'great',
                                   'note': ''}) + '\n')
            tags.write('[1, 2]\n')
        self.assertEqual({site: entry['tag'] for site, entry in
                          server.read_tags(self.data_dir).items()}, {'www.amazon.de': 'blank'})

    def test_tag_form_is_validated(self):
        good = {'site': 'www.amazon.de', 'tag': 'other', 'note': ''}
        cases = {
            'no site': ({**good, 'site': ''}, 400),
            'site outside the data dir': ({**good, 'site': '../etc'}, 400),
            'site with a slash': ({**good, 'site': 'www.amazon.de/x'}, 400),
            'site without shots': ({**good, 'site': 'nope.example'}, 404),
            'no tag': ({**good, 'tag': ''}, 400),
            'unknown tag': ({**good, 'tag': 'great'}, 400),
            'untagged is a filter, not a tag': ({**good, 'tag': 'untagged'}, 400),
            'note of 501 characters': ({**good, 'note': 'x' * 501}, 400),
            'note with a newline': ({**good, 'note': 'a\nb'}, 400),
            'too many fields': ('&'.join(f'f{n}=1' for n in range(30)).encode() + b'&site=x', 400),
            'body that is not UTF-8': (b'site=www.amazon.de&tag=ok&note=\xff', 400),
            'escape that is not UTF-8': (b'site=www.amazon.de&tag=ok&note=%ff', 400),
        }
        for case, (fields, status) in cases.items():
            with self.subTest(case=case):
                response, body = self.post_tag(fields)
                self.assertEqual(response.status, status, body)
                self.assertIn('error', json.loads(body))
        self.assertEqual(self.tag_lines(), [])
        self.assertEqual(self.tagged(note='é' * 500)['note'], 'é' * 500)

    def test_tag_needs_a_form_body_of_bounded_size(self):
        fields = {'site': 'www.amazon.de', 'tag': 'ok'}
        for content_type in ('text/plain', 'application/json', 'multipart/form-data'):
            with self.subTest(content_type=content_type):
                response, _ = self.post_tag(fields, content_type=content_type)
                self.assertEqual(response.status, 415)
        response, _ = self.post_tag({**fields, 'note': 'x' * 20_000})
        self.assertEqual(response.status, 413)
        response, _ = self.request('POST', '/tag', iter([b'site=www.amazon.de&tag=ok']),
                                   {'Content-Type': FORM})
        self.assertEqual(response.status, 411)
        self.assertEqual(self.tag_lines(), [])
        response, _ = self.post_tag(fields, content_type=FORM + '; charset=UTF-8')
        self.assertEqual(response.status, 200)

    def test_tag_with_a_wrong_content_type_is_refused_before_the_body_is_sent(self):
        head = ('POST /tag HTTP/1.1\r\nHost: localhost\r\nContent-Type: text/plain\r\n'
                'Content-Length: 10\r\nExpect: 100-continue\r\n\r\n').encode()
        with socket.create_connection(('127.0.0.1', self.port), timeout=10) as sock, \
                sock.makefile('rb') as reader:
            sock.sendall(head)
            self.assertEqual(reader.readline().split()[1], b'415')

    def test_form_post_without_javascript_redirects_back_to_the_next_row(self):
        response, body = self.post_tag({'site': 'de.wikipedia.org', 'tag': 'error-page',
                                        'note': '', 'back': 'q=wiki&tag=untagged&evil=1',
                                        'next': 'www.amazon.de'}, accept=None)
        self.assertEqual(response.status, 303, body)
        self.assertEqual(response.getheader('Location'), '/?q=wiki&tag=untagged#s-www.amazon.de')
        self.assertEqual(self.tag_lines()[0]['tag'], 'error-page')
        # The last row has no next one and goes back to itself.
        response, _ = self.post_tag({'site': 'www.amazon.de', 'tag': 'ok'}, accept='text/html')
        self.assertEqual(response.getheader('Location'), '/#s-www.amazon.de')

    def test_redirect_cannot_be_pointed_elsewhere(self):
        response, _ = self.post_tag({'site': 'www.amazon.de', 'tag': 'ok',
                                     'back': '//evil.example/?q=\r\nSet-Cookie: a=b',
                                     'next': '//evil.example'}, accept=None)
        self.assertEqual(response.status, 303)
        location = response.getheader('Location')
        self.assertTrue(location.startswith('/') and not location.startswith('//'), location)
        self.assertNotIn('\n', location)
        self.assertTrue(location.endswith('#s-www.amazon.de'), location)
        self.assertIsNone(response.getheader('Set-Cookie'))

    def test_gallery_shows_tags_counts_and_buttons(self):
        self.tagged(tag='bot-wall', note='"><script>x</script>')
        _, page = self.view('/')
        amazon = row_of(page, 'www.amazon.de')
        self.assertIn('data-tag="bot-wall"', amazon)
        self.assertIn('<span class="tag t-bot-wall">bot-wall</span>', amazon)
        self.assertIn('value="&quot;&gt;&lt;script&gt;x&lt;/script&gt;"', amazon)
        self.assertNotIn('<script>x', page)
        self.assertIn('<button class="set on" name="tag" value="bot-wall" aria-pressed="true">',
                      amazon)
        self.assertIn('<button class="save" name="tag" value="bot-wall">', amazon)
        for number, tag in enumerate(server.TAGS, 1):
            self.assertIn(f'value="{tag}" aria-pressed="{str(tag == "bot-wall").lower()}">'
                          f'<kbd>{number}</kbd> {tag}</button>', amazon)
        wikipedia = row_of(page, 'de.wikipedia.org')
        self.assertIn('<span class="tag t-untagged">untagged</span>', wikipedia)
        self.assertIn('<button class="save" name="tag" value="" disabled>', wikipedia)
        self.assertIn('name="next" value="www.amazon.de"', wikipedia)
        self.assertIn('<b data-count="bot-wall">1</b>', page)
        self.assertIn('<b data-count="untagged">1</b>', page)
        self.assertIn('<b data-count="ok">0</b>', page)
        self.assertIn('href="/export.txt?tag=bot-wall"', page)
        self.assertIn('href="/export.csv"', page)

    def test_gallery_tag_filter_combines_with_the_others(self):
        self.tagged(tag='cookie-banner')
        amazon, wikipedia = 'id="s-www.amazon.de"', 'id="s-de.wikipedia.org"'
        _, page = self.view('/?tag=cookie-banner')
        self.assertIn(amazon, page)
        self.assertNotIn(wikipedia, page)
        self.assertIn('1 site, 1 screenshot match, of 2 sites and 2 in all', page)
        self.assertIn('<option selected>cookie-banner</option>', page)
        self.assertIn('<a href="/?tag=cookie-banner" class="on">', page)
        _, page = self.view('/?tag=untagged')
        self.assertIn(wikipedia, page)
        self.assertNotIn(amazon, page)
        self.assertIn('name="back" value="tag=untagged"', page)
        _, page = self.view('/?tag=untagged&q=amazon')
        self.assertIn('No screenshots match', page)
        _, page = self.view('/?tag=cookie-banner&driver=playwright')
        self.assertIn('No screenshots match', page)
        # The counts cover every site, whatever the filter, and keep the other filters.
        self.assertIn('<b data-count="untagged">1</b>', page)
        self.assertIn('href="/?driver=playwright&amp;tag=ok"', page)

    def test_gallery_script_is_allowed_by_its_hash_and_only_it(self):
        response, page = self.view('/')
        script = page[page.index('<script>') + len('<script>'):page.index('</script>')]
        digest = base64.b64encode(hashlib.sha256(script.encode('utf-8')).digest()).decode()
        csp = response.getheader('Content-Security-Policy')
        self.assertIn(f"script-src 'sha256-{digest}'", csp)
        self.assertIn("connect-src 'self'", csp)
        self.assertNotIn('unsafe-inline', csp.split('script-src')[1].split(';')[0])
        # The keyboard shortcuts the README promises.
        for needle in ("key !== 'j'", "key !== 'k'", "key !== 'n'", "'Escape'", "fetch('/tag'"):
            self.assertIn(needle, script)
        self.assertIn(json.dumps(list(server.TAGS)), script)

    def test_site_page_shows_the_tag_and_the_note(self):
        _, page = self.view('/site/www.amazon.de')
        self.assertIn('<span class="tag t-untagged">untagged</span>', page)
        with at(9):
            self.tagged(tag='error-page', note='502 from the CDN')
        _, page = self.view('/site/www.amazon.de')
        self.assertIn('<span class="tag t-error-page">error-page</span> 502 from the CDN', page)
        self.assertIn('2026-09-17T10:28:09Z', page)
        self.assertIn('href="/#s-www.amazon.de"', page)


class ExportTest(ServerTestCase):
    def setUp(self):
        super().setUp()
        with at(1):
            self.uploaded(upload_path(page='https://www.amazon.de'))
        with at(2):
            self.uploaded(upload_path(page='https://www.amazon.de/', variant='firefox-com'))
            self.uploaded(upload_path(page='https://de.wikipedia.org/', driver='playwright',
                                      variant='headful', title='Wikipedia'))
            self.uploaded(upload_path(page='http://ribalba.de/', title='ribalba'))

    def export(self, query):
        response, body = self.request('GET', '/export.txt?' + query)
        return response, body.decode('utf-8')

    def test_export_txt_lists_the_pages_of_one_tag_like_sites_txt(self):
        self.tagged('www.amazon.de', 'bot-wall')
        self.tagged('de.wikipedia.org', 'error-page')
        with at(9):
            response, text = self.export('tag=bot-wall')
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader('Content-Type'), 'text/plain; charset=utf-8')
        self.assertEqual(text, '# Sites tagged bot-wall in the screenshot review, exported '
                               '2026-09-17T10:28:09Z.\n'
                               '# 1 site. One URL per line, for tools/batch.py generate.\n'
                               '\n'
                               # The page of the site's latest upload.
                               'https://www.amazon.de/\n')
        self.assertEqual(read_urls(text), ['https://www.amazon.de/'])

    def test_export_txt_takes_several_tags_and_untagged(self):
        self.tagged('www.amazon.de', 'bot-wall')
        self.tagged('de.wikipedia.org', 'error-page')
        _, text = self.export('tag=error-page&tag=bot-wall&tag=error-page')
        self.assertIn('# Sites tagged bot-wall or error-page in', text)
        self.assertIn('# 2 sites.', text)
        self.assertEqual(read_urls(text), ['https://de.wikipedia.org/', 'https://www.amazon.de/'])
        _, text = self.export('tag=untagged')
        self.assertEqual(read_urls(text), ['http://ribalba.de/'])
        # Retagged, a site leaves the old tag's list.
        self.tagged('www.amazon.de', 'ok')
        _, text = self.export('tag=bot-wall')
        self.assertEqual(read_urls(text), [])
        self.assertIn('# 0 sites.', text)

    def test_export_txt_needs_a_known_tag(self):
        for query in ('', 'tag=', 'tag=great', 'tag=ok&tag=great', 'q=amazon'):
            with self.subTest(query=query):
                response, text = self.export(query)
                self.assertEqual(response.status, 400)
                self.assertIn('error', json.loads(text))

    def test_export_csv_has_every_site_with_its_review_and_latest_shots(self):
        with at(5):
            self.tagged('www.amazon.de', 'bot-wall', '=HYPERLINK("https://evil.example")')
        response, body = self.request('GET', '/export.csv')
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader('Content-Type'), 'text/csv; charset=utf-8')
        self.assertEqual(response.getheader('Content-Disposition'),
                         'attachment; filename="screenshot-review.csv"')
        rows = list(csv.reader(io.StringIO(body.decode('utf-8'))))
        self.assertEqual(rows[0], ['site', 'page', 'tag', 'note', 'tagged_at', 'shots',
                                   'parrot/xvfb file', 'parrot/xvfb time',
                                   'parrot/firefox-com file', 'parrot/firefox-com time',
                                   'playwright/headful file', 'playwright/headful time'])
        self.assertEqual([row[0] for row in rows[1:]],
                         ['de.wikipedia.org', 'ribalba.de', 'www.amazon.de'])
        amazon = rows[3]
        self.assertEqual(amazon[:6], ['www.amazon.de', 'https://www.amazon.de/', 'bot-wall',
                                      '\'=HYPERLINK("https://evil.example")',
                                      '2026-09-17T10:28:05Z', '2'])
        self.assertEqual(amazon[6:], [
            'www.amazon.de/20260917T102801Z-parrot-xvfb.png', '2026-09-17T10:28:01Z',
            'www.amazon.de/20260917T102802Z-parrot-firefox-com.png', '2026-09-17T10:28:02Z',
            '', ''])
        self.assertEqual(rows[1][2:6], ['', '', '', '1'])
        self.assertEqual(rows[1][10:], ['de.wikipedia.org/20260917T102802Z-playwright-headful.png',
                                        '2026-09-17T10:28:02Z'])

    def test_export_csv_of_an_empty_server_is_its_header(self):
        # The test's own temporary data dir, emptied of its uploads.
        (self.data_dir / 'index.jsonl').unlink()
        response, body = self.request('GET', '/export.csv')
        self.assertEqual(response.status, 200)
        self.assertEqual(body, b'site,page,tag,note,tagged_at,shots\r\n')


if __name__ == '__main__':
    unittest.main()
