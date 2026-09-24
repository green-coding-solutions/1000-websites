"""Tests for screenshot-server/server.py.

    python3 -m unittest discover -s screenshot-server -v

Every test that talks HTTP gets its own server on an ephemeral localhost port and
its own data dir, and goes through real sockets, so what is checked is what a
client sees, including the parts http.server decides (keep-alive, 100-continue).
"""
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
            'unknown variant': upload_path(variant='wayland'),
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
        self.assertNotIn('<script>', page)
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
                             ('HEAD', '/'), ('PUT', '/shots/x/y.png')):
            with self.subTest(method=method, path=path):
                response, _ = self.request(method, path)
                self.assertEqual(response.status, 405)
                self.assertEqual(response.getheader('Allow'), 'GET')

    def test_log_has_one_line_per_request_and_no_headers(self):
        self.uploaded()
        self.upload(body=b'not a png')
        self.view('/')
        self.request('GET', '/healthz', headers={'X-Private': 'keep-me-out-of-the-log'})
        log = self.log.getvalue()
        self.assertEqual(len(log.splitlines()), 4, log)
        self.assertNotIn('keep-me-out-of-the-log', log)
        self.assertIn('"GET /healthz HTTP/1.1" 200 2', log)


if __name__ == '__main__':
    unittest.main()
