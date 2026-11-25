#!/usr/bin/env python3
"""Receive the benchmark's screenshots, show them in one gallery, and review them.

    screenshot-server/server.py --host 127.0.0.1 --data-dir ./screenshots

A run on the GMT cluster keeps no files, so after a page has loaded the run
uploads a PNG of what it rendered to this server. The gallery puts the latest
shot of every site next to each other, one per driver and variant, so that a bot
wall, an error page or a blank window stands out while scrolling through a few
hundred sites. A run whose screenshot shows one of those measured that page and
not the site.

The gallery is also where the shots are reviewed. Every site can carry one tag,
such as cookie-banner or bot-wall, and a note. Tags are kept append-only in
tags.jsonl, and /export.txt turns the sites with a tag into a URL list that
tools/batch.py can measure again.

There is no authentication, by design: anyone who can reach the server can
upload, tag and view. It is meant to live for one benchmark run, behind the
operator's own HTTPS reverse proxy, and to be deleted with its screenshots
afterwards. The server itself speaks plain HTTP only.

Standard library only and Python 3.9 or newer, so it runs on whatever box has a
python3. screenshot-server/README.md has the HTTP API and docker-compose.yml.
"""
import argparse
import base64
import csv
import hashlib
import html
import io
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
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

DRIVERS = ('parrot', 'playwright', 'prescan')
# The variants the benchmark started with. Any other name that matches
# VARIANT_RE is accepted too and sorts after these.
VARIANTS = ('xvfb', 'headful', 'headless')
VARIANT_RE = re.compile(r'[a-z0-9][a-z0-9-]{0,31}')
# Review tags, in the order of their keyboard shortcuts 1 to 6.
TAGS = ('ok', 'cookie-banner', 'bot-wall', 'error-page', 'blank', 'other')
UNTAGGED = 'untagged'
PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'
MAX_TITLE = 300
MAX_SITE = 150
MAX_NOTE = 500
# Long enough for any real page URL, short enough to keep index lines sane.
MAX_PAGE = 2048
# A tag form is a site, a tag, a note of 500 characters and two short fields.
# Percent-encoded, a 500 character note is at most 4500 bytes.
MAX_FORM_BYTES = 16_384
INDEX = 'index.jsonl'
TAGS_FILE = 'tags.jsonl'
# The gallery filters that survive a tag form's redirect back.
FILTERS = ('q', 'driver', 'variant', 'tag')

# What derive_site() can produce. Checked again on every path that comes from a
# request or from the index, so neither can name a file outside the data dir.
SITE_RE = re.compile(r'[A-Za-z0-9-][A-Za-z0-9._-]{0,149}')
SHOT_RE = re.compile(r'[A-Za-z0-9-][A-Za-z0-9._-]{0,200}\.png')

# A rejected client may still be sending its body. See Handler._linger().
LINGER_SECONDS = 2.0

CSS = """
:root{color-scheme:light dark}
body{font:14px/1.4 system-ui,sans-serif;margin:0;padding:12px 16px}
a{color:inherit}
h1{font-size:20px;margin:0 0 4px}
h2{font-size:16px;margin:0;overflow-wrap:anywhere}
form{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0 4px}
input,select,button{font:inherit}
p{margin:4px 0}
section{border-top:1px solid #8886;padding:12px 0}
.meta{margin:2px 0 8px;color:#888;overflow-wrap:anywhere}
.shots{display:flex;flex-wrap:wrap;gap:12px}
figure{margin:0;width:360px;max-width:100%}
img,.none{display:block;width:100%;height:auto;aspect-ratio:16/9;object-fit:cover;
  object-position:top;border:1px solid #8886;background:#8882;box-sizing:border-box}
.none{display:flex;align-items:center;justify-content:center;color:#888}
figcaption{font-size:12px;margin-top:4px;overflow-wrap:anywhere}
.counts{position:sticky;top:0;z-index:1;background:Canvas;padding:6px 0;
  border-bottom:1px solid #8886;display:flex;flex-wrap:wrap;gap:4px 14px}
.counts a{text-decoration:none}
.counts a.on{text-decoration:underline}
.keys,.exports{color:#888;font-size:12px}
.row{padding-left:10px;border-left:4px solid transparent;scroll-margin-top:44px}
.row.cur{border-left-color:#2a7de1;background:#2a7de114}
.row.failed{border-left-color:#d33}
.tag{display:inline-block;font-size:12px;font-weight:normal;padding:0 6px;
  border-radius:8px;margin-left:6px;vertical-align:middle;background:#8883}
.t-ok{background:#2a24}.t-cookie-banner{background:#e906}.t-bot-wall{background:#d335}
.t-error-page{background:#a3c5}.t-blank{background:#8886}.t-other{background:#39f5}
.t-untagged{background:none;border:1px dashed #888}
.tagger{align-items:center;margin:6px 0 8px}
.tagger input[name=note]{width:22em;max-width:100%}
.tagger button.on{font-weight:bold;outline:2px solid #2a7de1}
kbd{font:11px monospace;color:#888}
.status{color:#888;font-size:12px}
.failed .status{color:#d33}
"""

# The gallery's only script. Without it every tag button is a plain form post.
GALLERY_JS = """
(function () {
  'use strict';
  var TAGS = TAGS_JSON;
  var rows = Array.prototype.slice.call(document.querySelectorAll('section.row'));
  var cur = -1;
  if (!rows.length) { return; }

  function onScreen(row) {
    var box = row.getBoundingClientRect();
    return box.bottom > 60 && box.top < window.innerHeight;
  }
  function firstOnScreen() {
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].getBoundingClientRect().bottom > 60) { return i; }
    }
    return rows.length - 1;
  }
  function focusRow(i, scroll) {
    if (i < 0 || i >= rows.length) { return; }
    if (cur >= 0) { rows[cur].classList.remove('cur'); }
    cur = i;
    rows[i].classList.add('cur');
    if (scroll) { rows[i].scrollIntoView({block: 'start'}); }
    history.replaceState(null, '', '#' + rows[i].id);
  }
  function bump(tag, delta) {
    var count = document.querySelector('[data-count="' + tag + '"]');
    if (count) { count.textContent = String(Number(count.textContent) + delta); }
  }
  function show(row, tag, note) {
    var old = row.getAttribute('data-tag') || 'untagged';
    if (old !== tag) { bump(old, -1); bump(tag, 1); }
    row.setAttribute('data-tag', tag);
    var badge = row.querySelector('.tag');
    badge.textContent = tag;
    badge.className = 'tag t-' + tag;
    var form = row.querySelector('form.tagger');
    if (document.activeElement !== form.elements.note) { form.elements.note.value = note; }
    var save = form.querySelector('button.save');
    save.value = tag;
    save.disabled = false;
    Array.prototype.forEach.call(form.querySelectorAll('button.set'), function (button) {
      button.classList.toggle('on', button.value === tag);
      button.setAttribute('aria-pressed', String(button.value === tag));
    });
  }
  function setTag(row, tag) {
    var form = row.querySelector('form.tagger');
    var status = row.querySelector('.status');
    var body = new URLSearchParams();
    body.set('site', row.getAttribute('data-site'));
    body.set('tag', tag);
    body.set('note', form.elements.note.value);
    row.classList.remove('failed');
    status.textContent = 'saving ' + tag;
    fetch('/tag', {method: 'POST', body: body, headers: {'Accept': 'application/json'}})
      .then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (data) {
          if (!response.ok) { throw new Error(data.error || 'HTTP ' + response.status); }
          return data;
        });
      })
      .then(function (data) {
        show(row, data.tag, data.note);
        status.textContent = 'saved';
      })
      .catch(function (error) {
        row.classList.add('failed');
        status.textContent = 'not saved: ' + error.message;
      });
  }

  rows.forEach(function (row, i) {
    var form = row.querySelector('form.tagger');
    form.addEventListener('submit', function (event) {
      // A browser that cannot say which button was pressed posts the form.
      if (!('submitter' in event)) { return; }
      event.preventDefault();
      var button = event.submitter;
      if (!button || !button.value) { return; }
      focusRow(i, false);
      setTag(row, button.value);
      if (document.activeElement === form.elements.note) { form.elements.note.blur(); }
    });
    row.addEventListener('click', function () {
      if (cur !== i) { focusRow(i, false); }
    });
  });

  document.addEventListener('keydown', function (event) {
    if (event.ctrlKey || event.metaKey || event.altKey || event.isComposing) { return; }
    var target = event.target;
    if (target.name === 'note' && target.form && target.form.classList.contains('tagger')) {
      // Enter saves the note with the row's tag. An untagged row has no tag to
      // save it with, so Enter and Escape both hand the keys back to the page.
      var save = target.form.querySelector('button.save');
      if (event.key === 'Escape' || (event.key === 'Enter' && save.disabled)) {
        event.preventDefault();
        target.blur();
      }
      return;
    }
    if (/^(INPUT|SELECT|TEXTAREA)$/.test(target.tagName) || target.isContentEditable) { return; }
    var key = event.key;
    var digit = key.length === 1 && key >= '1' && key <= String(TAGS.length);
    if (key !== 'j' && key !== 'k' && key !== 'n' && !digit) { return; }
    event.preventDefault();
    // After scrolling away with the mouse, the first key picks up where the
    // reader is instead of acting on a row out of sight.
    if (cur < 0 || !onScreen(rows[cur])) { focusRow(firstOnScreen(), true); return; }
    if (key === 'j') {
      focusRow(cur + 1, true);
    } else if (key === 'k') {
      focusRow(cur - 1, true);
    } else if (key === 'n') {
      var note = rows[cur].querySelector('form.tagger').elements.note;
      note.focus();
      note.select();
    } else {
      setTag(rows[cur], TAGS[Number(key) - 1]);
      focusRow(cur + 1, true);
    }
  });

  for (var i = 0; i < rows.length; i++) {
    if ('#' + rows[i].id === location.hash) { focusRow(i, false); break; }
  }
}());
""".replace('TAGS_JSON', json.dumps(list(TAGS)))
GALLERY_JS_HASH = base64.b64encode(hashlib.sha256(GALLERY_JS.encode('utf-8')).digest()).decode()

# The one script allowed is the gallery's, by its hash, and it may talk to this
# server only.
CSP = ("default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; "
       f"script-src 'sha256-{GALLERY_JS_HASH}'; connect-src 'self'; "
       "form-action 'self'; base-uri 'none'; frame-ancestors 'none'")


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


def has_control_chars(text):
    return any(ord(char) < 32 or ord(char) == 127 for char in text)


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
    if has_control_chars(page):
        return None, 'page contains control characters'
    if driver not in DRIVERS:
        return None, 'driver must be one of: ' + ', '.join(DRIVERS)
    # The variant goes into the file name, so it is held to a pattern that
    # cannot leave the site directory or hide the file.
    if variant is None or not VARIANT_RE.fullmatch(variant):
        return None, ('variant must be 1 to 32 lowercase letters, digits or dashes, '
                      'starting with a letter or digit, such as ' + ', '.join(VARIANTS))
    title = (first('title') or '').strip()[:MAX_TITLE]
    return {'page': page, 'driver': driver, 'variant': variant, 'title': title}, None


def parse_tag_form(body):
    """Return (params, None) for a valid tag form body, or (None, problem)."""
    try:
        fields = parse_qs(body.decode('utf-8'), keep_blank_values=True,
                          errors='strict', max_num_fields=20)
    except ValueError:
        # UnicodeDecodeError is a ValueError, and so is a form with too many fields.
        return None, 'the form is not valid UTF-8 or has too many fields'

    def first(name):
        values = fields.get(name)
        return values[0] if values else ''

    site, tag = first('site'), first('tag')
    note = first('note').strip()
    if not SITE_RE.fullmatch(site):
        return None, 'site is not a valid site name'
    if tag not in TAGS:
        return None, 'tag must be one of: ' + ', '.join(TAGS)
    if len(note) > MAX_NOTE:
        return None, f'note is longer than {MAX_NOTE} characters'
    if has_control_chars(note):
        return None, 'note contains control characters'
    # Where a browser without JavaScript goes back to. Both are only ever used
    # rebuilt from known parts, see gallery_location().
    following = first('next')
    anchor = following if SITE_RE.fullmatch(following) else site
    return {'site': site, 'tag': tag, 'note': note, 'back': first('back'), 'anchor': anchor}, None


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


def store_tag(data_dir, lock, params, remote):
    """Append one tag line to tags.jsonl and return it. Nothing is ever rewritten."""
    with lock:
        entry = {
            'time': f'{utcnow():%Y-%m-%dT%H:%M:%SZ}',
            'site': params['site'],
            'tag': params['tag'],
            'note': params['note'],
            'remote': remote,
        }
        with open(data_dir / TAGS_FILE, 'a', encoding='utf-8') as tags:
            tags.write(json.dumps(entry, ensure_ascii=False) + '\n')
    return entry


def read_jsonl(path):
    """Every JSON object in a JSONL file, skipping lines that do not parse."""
    entries = []
    try:
        with open(path, encoding='utf-8', errors='replace') as lines:
            for line in lines:
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    # A line still being appended, or a hand edit gone wrong.
                    continue
    except FileNotFoundError:
        pass
    return entries


def read_index(data_dir):
    """All index entries that are safe to render, in upload order."""
    return [entry for entry in read_jsonl(data_dir / INDEX) if usable_entry(entry)]


def usable_entry(entry):
    keys = ('time', 'page', 'site', 'driver', 'variant', 'title', 'file')
    if not isinstance(entry, dict) or not all(isinstance(entry.get(k), str) for k in keys):
        return False
    site, _, name = entry['file'].partition('/')
    return site == entry['site'] and bool(SITE_RE.fullmatch(site) and SHOT_RE.fullmatch(name))


def read_tags(data_dir):
    """The current tag line of every site. The last line of a site wins."""
    return {entry['site']: entry for entry in read_jsonl(data_dir / TAGS_FILE)
            if usable_tag(entry)}


def usable_tag(entry):
    keys = ('time', 'site', 'tag', 'note')
    return (isinstance(entry, dict) and all(isinstance(entry.get(k), str) for k in keys)
            and entry['tag'] in TAGS and bool(SITE_RE.fullmatch(entry['site'])))


def tag_of(tags, site):
    entry = tags.get(site)
    return entry['tag'] if entry else UNTAGGED


def combo_key(combo):
    driver, variant = combo
    return (DRIVERS.index(driver) if driver in DRIVERS else len(DRIVERS), driver,
            VARIANTS.index(variant) if variant in VARIANTS else len(VARIANTS), variant)


def variant_options(entries):
    """The known variants first, then every other one that was uploaded, sorted."""
    others = sorted({entry['variant'] for entry in entries} - set(VARIANTS))
    return VARIANTS + tuple(others)


def summarize(entries):
    """Per site, in name order: its latest page, its shot count and its latest
    shot per driver and variant."""
    sites = {}
    for entry in entries:
        site = sites.setdefault(entry['site'], {'page': '', 'shots': 0, 'latest': {}})
        site['page'] = entry['page']
        site['shots'] += 1
        site['latest'][(entry['driver'], entry['variant'])] = entry
    return dict(sorted(sites.items()))


def html_page(title, content, script=''):
    script = f'<script>{script}</script>' if script else ''
    return ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{html.escape(title)}</title><style>{CSS}</style></head>'
            f'<body>{content}{script}</body></html>\n')


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


def tag_badge(tag):
    return f'<span class="tag t-{html.escape(tag)}">{html.escape(tag)}</span>'


def select_html(name, current, options):
    choices = [f'<option value="">any {name}</option>']
    for option in options:
        selected = ' selected' if option == current else ''
        choices.append(f'<option{selected}>{html.escape(option)}</option>')
    return f'<select name="{name}">{"".join(choices)}</select>'


def filter_query(**filters):
    """The gallery's query string for these filters, without the empty ones."""
    return urlencode([(key, filters[key]) for key in FILTERS if filters.get(key)])


def gallery_location(back, anchor):
    """Where a tag form posted without JavaScript redirects to: the gallery with
    the filters it was posted from, scrolled to the anchor row.

    The location is rebuilt from the known filters and a valid site name, so a
    crafted form cannot redirect anywhere else or inject a header.
    """
    try:
        fields = parse_qs(back, max_num_fields=20)
    except ValueError:
        fields = {}
    query = filter_query(**{key: (fields.get(key) or [''])[0] for key in FILTERS})
    return ('/?' + query if query else '/') + '#s-' + quote(anchor)


def tagger_html(site, entry, back, following):
    """The tag buttons and the note field of one gallery row."""
    current = entry['tag'] if entry else ''
    note = entry['note'] if entry else ''
    # The first submit button is the form's default, which Enter in the note
    # field presses. It saves the note with the current tag, and on an untagged
    # row it is disabled, so Enter there does not tag the site by accident.
    if current:
        save = f'<button class="save" name="tag" value="{html.escape(current)}">save note</button>'
    else:
        save = '<button class="save" name="tag" value="" disabled>save note</button>'
    buttons = []
    for number, tag in enumerate(TAGS, 1):
        on = tag == current
        buttons.append(f'<button class="set{" on" if on else ""}" name="tag" '
                       f'value="{tag}" aria-pressed="{str(on).lower()}">'
                       f'<kbd>{number}</kbd> {tag}</button>')
    return ('<form class="tagger" method="post" action="/tag">'
            f'<input type="hidden" name="site" value="{html.escape(site)}">'
            f'<input type="hidden" name="back" value="{html.escape(back)}">'
            f'<input type="hidden" name="next" value="{html.escape(following)}">'
            f'<input name="note" value="{html.escape(note)}" maxlength="{MAX_NOTE}" '
            'placeholder="note" aria-label="note">'
            + save + ''.join(buttons)
            + '<span class="status" aria-live="polite"></span></form>')


def render_gallery(entries, q='', driver='', variant='', tag='', tags=None):
    tags = tags or {}
    shot_counts = Counter(entry['site'] for entry in entries)
    needle = q.strip().lower()
    matching = [e for e in entries
                if (not needle or needle in e['site'].lower() or needle in e['page'].lower())
                and (not driver or e['driver'] == driver)
                and (not variant or e['variant'] == variant)
                and (not tag or tag_of(tags, e['site']) == tag)]
    # Later lines are later uploads, so the last one per combination wins.
    latest, pages = {}, {}
    for entry in matching:
        latest.setdefault(entry['site'], {})[(entry['driver'], entry['variant'])] = entry
        pages[entry['site']] = entry['page']
    # Only combinations that occur at all get a column, and every site gets the
    # same columns in the same order, so a missing or odd shot is easy to spot.
    combos = sorted({combo for shots in latest.values() for combo in shots}, key=combo_key)

    totals = f"{plural(len(latest), 'site')}, {plural(len(matching), 'screenshot')}"
    if needle or driver or variant or tag:
        totals += f" match, of {plural(len(shot_counts), 'site')} and {len(entries)} in all"
    # How far the review is, over every site and not only the filtered ones.
    tag_counts = Counter(tag_of(tags, site) for site in shot_counts)
    counts = []
    for name in ('',) + TAGS + (UNTAGGED,):
        query = filter_query(q=q, driver=driver, variant=variant, tag=name)
        href = '/?' + query if query else '/'
        on = ' class="on"' if name == tag else ''
        count = (f'<b data-count="{name}">{tag_counts[name]}</b>' if name
                 else f'<b>{len(shot_counts)}</b>')
        counts.append(f'<a href="{html.escape(href)}"{on}>{name or "all"} {count}</a>')
    exports = ', '.join(f'<a href="/export.txt?tag={name}">{name}</a>'
                        for name in TAGS + (UNTAGGED,))
    parts = [
        '<header><h1>Screenshots</h1>',
        f'<p>{html.escape(totals)}</p>',
        '<form method="get" action="/">',
        f'<input type="search" name="q" value="{html.escape(q)}" placeholder="site or page">',
        select_html('driver', driver, DRIVERS),
        select_html('variant', variant, variant_options(entries)),
        select_html('tag', tag, TAGS + (UNTAGGED,)),
        '<button>Filter</button></form>',
        '<p class="keys">Keys: j and k move to the next and previous site, 1 to 6 tag it '
        'and move on, n edits its note, Enter saves the note and Esc leaves it.</p>',
        f'<p class="exports">Export <a href="/export.csv">every site as CSV</a>, or the URLs '
        f'of the sites tagged {exports}.</p>',
        f'<nav class="counts">{"".join(counts)}</nav></header>',
    ]
    if not latest:
        parts.append('<p>No screenshots match.</p>' if entries else '<p>No screenshots yet.</p>')
    back = filter_query(q=q, driver=driver, variant=variant, tag=tag)
    ordered = sorted(latest)
    for position, site in enumerate(ordered):
        shots = latest[site]
        entry = tags.get(site)
        following = ordered[position + 1] if position + 1 < len(ordered) else ''
        count = plural(shot_counts[site], 'shot')
        parts.append(f'<section class="row" id="s-{html.escape(site)}" '
                     f'data-site="{html.escape(site)}" '
                     f'data-tag="{html.escape(entry["tag"]) if entry else ""}">'
                     f'<h2>{site_link(site, site)}{tag_badge(tag_of(tags, site))}</h2>'
                     f'<p class="meta">{site_link(site, count)}, {html.escape(pages[site])}</p>'
                     + tagger_html(site, entry, back, following)
                     + '<div class="shots">')
        for combo in combos:
            if combo in shots:
                parts.append(figure_html(shots[combo]))
            else:
                missing = html.escape(f'no {combo[0]}, {combo[1]} shot')
                parts.append(f'<figure><div class="none">{missing}</div></figure>')
        parts.append('</div></section>')
    return html_page('Screenshots', ''.join(parts), GALLERY_JS)


def render_site(entries, site, tags=None):
    """Every shot of one site, newest first, or None for a site with no shots."""
    shots = [entry for entry in entries if entry['site'] == site]
    if not shots:
        return None
    shots.reverse()
    entry = (tags or {}).get(site)
    review = tag_badge(tag_of(tags or {}, site))
    if entry and entry['note']:
        review += ' ' + html.escape(entry['note'])
    if entry:
        review += f' <span class="meta">({html.escape(entry["time"])})</span>'
    back = '/#s-' + quote(site)
    content = (f'<header><p><a href="{html.escape(back)}">All sites</a></p>'
               f'<h1>{html.escape(site)}</h1>'
               f"<p>{plural(len(shots), 'screenshot')}</p><p>Review: {review}</p></header>"
               '<div class="shots">' + ''.join(figure_html(entry) for entry in shots) + '</div>')
    return html_page(site, content)


def export_txt(entries, tags, wanted, now):
    """The page URLs of the sites that carry one of the wanted tags, in the format
    of sites.txt, so that tools/batch.py generate can take the file as it is."""
    sites = summarize(entries)
    chosen = [site for site in sites if tag_of(tags, site) in wanted]
    lines = [
        f'# Sites tagged {" or ".join(wanted)} in the screenshot review, '
        f'exported {now:%Y-%m-%dT%H:%M:%SZ}.',
        f'# {plural(len(chosen), "site")}. One URL per line, for tools/batch.py generate.',
        '',
    ]
    lines += [sites[site]['page'] for site in chosen]
    return '\n'.join(lines) + '\n'


def csv_cell(value):
    # A spreadsheet runs a cell that starts with one of these as a formula, and
    # pages, titles and notes come from unauthenticated clients.
    value = str(value)
    return "'" + value if value[:1] in ('=', '+', '-', '@', '\t', '\r') else value


def export_csv(entries, tags):
    """One line per site: its page, its review and its latest shot per driver and variant."""
    sites = summarize(entries)
    combos = sorted({combo for site in sites.values() for combo in site['latest']},
                    key=combo_key)
    out = io.StringIO()
    writer = csv.writer(out)
    header = ['site', 'page', 'tag', 'note', 'tagged_at', 'shots']
    for driver, variant in combos:
        header += [f'{driver}/{variant} file', f'{driver}/{variant} time']
    writer.writerow(header)
    for name, site in sites.items():
        entry = tags.get(name) or {'tag': '', 'note': '', 'time': ''}
        row = [name, site['page'], entry['tag'], entry['note'], entry['time'], site['shots']]
        for combo in combos:
            shot = site['latest'].get(combo)
            row += [shot['file'], shot['time']] if shot else ['', '']
        writer.writerow([csv_cell(value) for value in row])
    return out.getvalue()


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
        if path == '/tag':
            if self.command != 'POST':
                return self._not_allowed('POST')
            return self._tag()
        if path == '/healthz':
            if self.command != 'GET':
                return self._not_allowed('GET')
            return self._send(200, 'ok', 'text/plain; charset=utf-8')
        if path in ('/', '/export.txt', '/export.csv') or path.startswith(('/site/', '/shots/')):
            if self.command != 'GET':
                return self._not_allowed('GET')
            if path == '/':
                return self._gallery(url.query)
            if path == '/export.txt':
                return self._export_txt(url.query)
            if path == '/export.csv':
                return self._export_csv()
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

    def _read_body(self, length):
        """The request body, or None after a short body has been answered."""
        self._unread_body = False
        try:
            body = self.rfile.read(length)
        except OSError:
            self.close_connection = True
            return None
        if len(body) < length:
            self.close_connection = True
            self._send_json(400, 'the body ended before Content-Length bytes')
            return None
        return body

    # Request checks that need no body

    def handle_expect_100(self):
        # curl sends "Expect: 100-continue" with larger bodies and waits for the
        # go-ahead. Running every check that needs no body first means a
        # rejected request body is never transmitted at all.
        url = urlsplit(self.path)
        error = None
        if url.path == '/upload' and self.command in ('PUT', 'POST'):
            error, _ = self._upload_precheck(url.query)
        elif url.path == '/tag' and self.command == 'POST':
            error, _ = self._tag_precheck()
        if error:
            self._unread_body = self._declares_body()
            self._send_json(*error)
            return False
        return super().handle_expect_100()

    def _length_precheck(self, limit):
        """Return ((status, message), None) for a missing or too large body, else (None, length)."""
        lengths = self.headers.get_all('Content-Length') or []
        # A chunked body has no length to check against the limit up front.
        if 'Transfer-Encoding' in self.headers or not lengths:
            return (411, 'Content-Length is required, chunked bodies are not accepted'), None
        if len(lengths) > 1 or not re.fullmatch(r'[0-9]{1,15}', lengths[0].strip()):
            return (400, 'invalid Content-Length'), None
        length = int(lengths[0])
        if length > limit:
            return (413, f'body of {length} bytes is over the limit of {limit} bytes'), None
        return None, length

    def _upload_precheck(self, query):
        """Return ((status, message), None) for a rejected upload, else (None, (length, params))."""
        error, length = self._length_precheck(self.server.config.max_bytes)
        if error:
            return error, None
        params, problem = parse_upload_query(query)
        if problem:
            return (400, problem), None
        return None, (length, params)

    def _tag_precheck(self):
        """Return ((status, message), None) for a rejected tag form, else (None, length)."""
        error, length = self._length_precheck(MAX_FORM_BYTES)
        if error:
            return error, None
        if self.headers.get_content_type() != 'application/x-www-form-urlencoded':
            return (415, 'the body must be application/x-www-form-urlencoded'), None
        return None, length

    # Upload

    def _upload(self, query):
        error, checked = self._upload_precheck(query)
        if error:
            return self._send_json(*error)
        length, params = checked
        body = self._read_body(length)
        if body is None:
            return
        if not body.startswith(PNG_SIGNATURE):
            return self._send_json(400, 'the body is not a PNG')
        try:
            name = store_screenshot(self.server.config.data_dir, self.server.store_lock,
                                    params, body, self.client_address[0])
        except OSError as error:
            self.server.log(f'storing an upload for {printable(params["page"])} failed: {error}')
            return self._send_json(500, 'could not store the screenshot')
        self._send_json(201, {'file': name}, {'Location': '/shots/' + quote(name)})

    # Review

    def _tag(self):
        error, length = self._tag_precheck()
        if error:
            return self._send_json(*error)
        body = self._read_body(length)
        if body is None:
            return
        params, problem = parse_tag_form(body)
        if problem:
            return self._send_json(400, problem)
        data_dir = self.server.config.data_dir
        if not any(entry['site'] == params['site'] for entry in read_index(data_dir)):
            return self._send_json(404, f'site {params["site"]} has no screenshots')
        try:
            entry = store_tag(data_dir, self.server.store_lock, params, self.client_address[0])
        except OSError as error:
            self.server.log(f'storing a tag for {params["site"]} failed: {error}')
            return self._send_json(500, 'could not store the tag')
        # The gallery's script asks for JSON. A plain form post, from a browser
        # without JavaScript, goes back to the gallery it came from.
        if 'application/json' in self.headers.get('Accept', ''):
            return self._send_json(200, {key: entry[key] for key in ('site', 'tag', 'note', 'time')})
        self._send(303, '', 'text/plain; charset=utf-8',
                   {'Location': gallery_location(params['back'], params['anchor'])})

    def _export_txt(self, query):
        wanted = parse_qs(query).get('tag', [])
        known = TAGS + (UNTAGGED,)
        if not wanted or any(tag not in known for tag in wanted):
            return self._send_json(400, 'tag must be given, one or more of: ' + ', '.join(known))
        # Once each, in the order of the tags, whatever order the query had.
        wanted = [tag for tag in known if tag in wanted]
        data_dir = self.server.config.data_dir
        text = export_txt(read_index(data_dir), read_tags(data_dir), wanted, utcnow())
        self._send(200, text, 'text/plain; charset=utf-8', {'Cache-Control': 'no-store'})

    def _export_csv(self):
        data_dir = self.server.config.data_dir
        text = export_csv(read_index(data_dir), read_tags(data_dir))
        self._send(200, text, 'text/csv; charset=utf-8', {
            'Cache-Control': 'no-store',
            'Content-Disposition': 'attachment; filename="screenshot-review.csv"',
        })

    # Viewing

    def _gallery(self, query):
        fields = parse_qs(query)
        q, driver, variant, tag = ((fields.get(k) or [''])[0] for k in FILTERS)
        data_dir = self.server.config.data_dir
        self._send_html(render_gallery(read_index(data_dir), q, driver, variant, tag,
                                       read_tags(data_dir)))

    def _site(self, raw):
        site = unquote(raw)
        page = None
        if SITE_RE.fullmatch(site):
            data_dir = self.server.config.data_dir
            page = render_site(read_index(data_dir), site, read_tags(data_dir))
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
        description='Receive benchmark screenshots over plain HTTP, show them in a gallery '
                    'and tag them for review.',
        epilog='There is no authentication: run it only for as long as a benchmark needs '
               'it, behind an HTTPS reverse proxy.')
    parser.add_argument('--host', default='0.0.0.0', help='address to listen on (default: 0.0.0.0)')
    parser.add_argument('--port', type=int, default=8765, help='port to listen on (default: 8765)')
    parser.add_argument('--data-dir', type=Path, default=Path('./screenshots'),
                        help='where screenshots, index.jsonl and tags.jsonl go '
                             '(default: ./screenshots)')
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
