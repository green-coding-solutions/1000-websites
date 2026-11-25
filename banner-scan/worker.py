#!/usr/bin/env python3
"""Visit one site in one Firefox variant and say whether a banner covers it.

    python3 /tmp/repo/banner-scan/worker.py --url https://www.ikea.com/de/de/ --variant com

Runs inside ribalba/parrot-browsers with Python's standard library only. scan.py
starts it with docker exec, once per site and variant, and reads one JSON line
from its stdout. The screenshot travels inside that line as base64, so nothing
is written to a mounted directory as root.

The variants are the two browsers the benchmark can measure with:

    plain   a profile from parrot/common/setup-profile.sh with PARROT_CONSENT unset
    com     a copy of the Consent-O-Matic template, made by fresh-profile.sh from
            what make-template-profile.sh left in /tmp/parrot-profile-template

Firefox is started through parrot/common/launch-browser.sh, so its command line
is the benchmark's. Marionette is switched on with the MOZ_MARIONETTE
environment variable, which Firefox reads like --marionette, because
launch-browser.sh takes no extra flags. The profile gets two more prefs that
switch off Marionette's "recommended prefs", so the browser keeps the
benchmark's configuration and not the one Marionette would set for testing.
"""
import argparse
import base64
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.environ.get('BANNER_SCAN_REPO', '/tmp/repo')
COMMON = os.path.join(REPO, 'parrot', 'common')
DETECT = os.path.join(HERE, 'detect.js')
WORK = '/tmp/banner-scan'
PORT = 2828
WINDOW = (1440, 900)


class MarionetteError(Exception):
    pass


class Marionette:
    """The smallest Marionette client that does the job.

    The wire format is "<length>:<json>". Commands are [0, id, name, params]
    and answers are [1, id, error, result].
    """

    def __init__(self, port, deadline):
        last = None
        while time.monotonic() < deadline:
            try:
                self.sock = socket.create_connection(('127.0.0.1', port), timeout=5)
                break
            except OSError as e:
                last = e
                time.sleep(0.3)
        else:
            raise MarionetteError(f'Marionette did not open port {port}: {last}')
        self.buf = b''
        self.msg_id = 0
        self.sock.settimeout(30)
        self._read()  # the server's hello

    def _read(self):
        while b':' not in self.buf:
            self._recv()
        length, _, rest = self.buf.partition(b':')
        length = int(length)
        self.buf = rest
        while len(self.buf) < length:
            self._recv()
        data, self.buf = self.buf[:length], self.buf[length:]
        return json.loads(data)

    def _recv(self):
        chunk = self.sock.recv(1 << 20)
        if not chunk:
            raise MarionetteError('Marionette closed the connection')
        self.buf += chunk

    def call(self, name, params=None, timeout=30):
        self.msg_id += 1
        data = json.dumps([0, self.msg_id, name, params or {}]).encode()
        self.sock.settimeout(timeout)
        self.sock.sendall(str(len(data)).encode() + b':' + data)
        while True:
            msg = self._read()
            if msg[0] == 1 and msg[1] == self.msg_id:
                break
        if msg[2]:
            err = msg[2]
            raise MarionetteError(f"{err.get('error')}: {str(err.get('message', ''))[:200]}")
        return msg[3]

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def run_quiet(cmd, env, timeout=60):
    p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError(f'{os.path.basename(cmd[1])} failed: {(p.stderr or p.stdout).strip()[-300:]}')


def make_profile(variant, profile, url):
    env = dict(os.environ, PARROT_URL=url)
    env.pop('PARROT_CONSENT', None)
    shutil.rmtree(profile, ignore_errors=True)
    if variant == 'plain':
        run_quiet(['bash', os.path.join(COMMON, 'setup-profile.sh'), 'firefox', profile], env)
    else:
        run_quiet(['bash', os.path.join(COMMON, 'fresh-profile.sh'), profile], env)
    with open(os.path.join(profile, 'user.js'), 'a', encoding='utf-8') as f:
        f.write('// banner-scan/worker.py: keep the benchmark prefs, not Marionette\'s test prefs\n')
        f.write('user_pref("remote.prefs.recommended", false);\n')
        f.write('user_pref("marionette.prefs.recommended", false);\n')
        f.write(f'user_pref("marionette.port", {PORT});\n')


def kill_firefox(proc, profile):
    if proc and proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            pass
    subprocess.run(['pkill', '-KILL', '-f', '--', profile], capture_output=True)


def visit(url, variant, settle, load_timeout, deadline):
    os.makedirs(WORK, exist_ok=True)
    profile = os.path.join(WORK, f'profile-{variant}')
    log_path = os.path.join(WORK, f'firefox-{variant}.log')
    subprocess.run(['pkill', '-KILL', '-f', '--', profile], capture_output=True)
    result = {'variant': variant, 'url': url, 'final_url': '', 'title': '', 'verdict': 'error',
              'evidence': [], 'error': '', 'load_timeout': False, 'load_seconds': None,
              'detect': None, 'screenshot': ''}
    proc = mn = None
    try:
        make_profile(variant, profile, url)
        env = dict(os.environ, MOZ_MARIONETTE='1', DISPLAY=os.environ.get('DISPLAY', ':99'),
                   PARROT_URL=url)
        with open(log_path, 'w') as log:
            proc = subprocess.Popen(['bash', os.path.join(COMMON, 'launch-browser.sh'), 'firefox',
                                     profile, 'about:blank'],
                                    env=env, stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        mn = Marionette(PORT, min(deadline, time.monotonic() + 40))
        mn.call('WebDriver:NewSession', {'capabilities': {'alwaysMatch': {
            'pageLoadStrategy': 'normal', 'acceptInsecureCerts': False}}})
        mn.call('WebDriver:SetTimeouts', {'pageLoad': int(load_timeout * 1000), 'script': 20000})
        try:
            mn.call('WebDriver:SetWindowRect', {'x': 0, 'y': 0, 'width': WINDOW[0], 'height': WINDOW[1]})
        except MarionetteError:
            pass

        started = time.monotonic()
        try:
            mn.call('WebDriver:Navigate', {'url': url}, timeout=load_timeout + 15)
        except MarionetteError as e:
            if str(e).startswith('timeout'):
                result['load_timeout'] = True
            else:
                result['error'] = f'navigate: {e}'
        except socket.timeout:
            result['load_timeout'] = True
        result['load_seconds'] = round(time.monotonic() - started, 1)

        time.sleep(max(0.0, min(settle, deadline - time.monotonic() - 15)))

        with open(DETECT, encoding='utf-8') as f:
            script = f.read() + '\nreturn detectBanner();'
        try:
            det = mn.call('WebDriver:ExecuteScript', {'script': script, 'args': []}, timeout=30)['value']
            result['detect'] = det
            result['verdict'] = det.get('verdict', 'error')
            result['evidence'] = det.get('evidence', [])
            result['final_url'] = det.get('url', '')
            result['title'] = det.get('title', '')
        except (MarionetteError, socket.timeout) as e:
            result['error'] = (result['error'] + '; ' if result['error'] else '') + f'detect: {e}'
            try:
                result['final_url'] = mn.call('WebDriver:GetCurrentURL')['value']
            except (MarionetteError, socket.timeout):
                pass

        try:
            shot = mn.call('WebDriver:TakeScreenshot', {'full': False, 'hash': False}, timeout=30)
            result['screenshot'] = shot['value']
        except (MarionetteError, socket.timeout) as e:
            result['error'] = (result['error'] + '; ' if result['error'] else '') + f'screenshot: {e}'

        # A navigation error (proxy error page, certificate error) means the
        # page was not the site, whatever the detector saw on the error page.
        if result['error']:
            result['verdict'] = 'error'
        try:
            mn.call('Marionette:Quit', {'flags': ['eForceQuit']}, timeout=10)
        except (MarionetteError, OSError):
            pass
        if proc:
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                pass
    except Exception as e:  # noqa: BLE001 - any failure is one site's error, not the scan's
        result['error'] = f'{type(e).__name__}: {e}'[:400]
        result['verdict'] = 'error'
        if os.path.exists(log_path):
            with open(log_path, errors='replace') as f:
                tail = f.read()[-300:].strip()
            if tail:
                result['error'] += f' | firefox log: {tail}'
    finally:
        if mn:
            mn.close()
        kill_firefox(proc, profile)
        shutil.rmtree(profile, ignore_errors=True)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--url', required=True)
    p.add_argument('--variant', choices=('plain', 'com'), required=True)
    p.add_argument('--settle', type=float, default=12, help='seconds to wait after the load event')
    p.add_argument('--load-timeout', type=float, default=30, help='page load timeout in seconds')
    p.add_argument('--budget', type=float, default=120, help='hard limit for the whole visit')
    args = p.parse_args()
    started = time.monotonic()
    result = visit(args.url, args.variant, args.settle, args.load_timeout, started + args.budget)
    result['seconds'] = round(time.monotonic() - started, 1)
    sys.stdout.write(json.dumps(result) + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
