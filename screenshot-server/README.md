# screenshot-server

A run on the GMT cluster keeps no files. So after a page has loaded, the
scenarios take a PNG of what the browser rendered and upload it here, and a
gallery shows the latest shot of every site, one per driver and variant, side by
side. Scrolling through it is how a bot wall, an error page or a blank window
gets noticed: a run that shows one of those measured that page, not the site.
The gallery is also where the shots are reviewed. Each site gets a tag such as
`cookie-banner` or `bot-wall`, and the sites with a tag can be exported as a
URL list to measure them again.

`server.py` is a single file, Python 3.9 or newer, standard library only.

**Anyone who knows the URL can upload screenshots, view all of them and set or
change every review tag.** There is no authentication and the server speaks
plain HTTP. Tags are exactly as open as uploads. That is acceptable only
because the server is temporary: start it for one benchmark run, behind your
HTTPS reverse proxy, and delete it and its data afterwards. Nothing a visitor
does can erase a tag, since every change is a new line in `tags.jsonl`, but a
visitor can set a newer one.

## Run it with docker compose

```bash
cd screenshot-server
docker compose up -d --build   # listens on 127.0.0.1:8765
docker compose logs -f         # one line per request
docker compose down            # stop it
rm -rf screenshots             # delete all data
```

`server.py` is built into the image, so after changing it run `up` with
`--build` again. It is not bind-mounted because Coolify and similar hosts run
the compose file from a directory of their own, where `./server.py` does not
exist: Docker then mounts an empty directory in its place and the container
exits with `can't find '__main__' module in '/app/server.py'`. On Coolify,
deploy the repository with the Docker Compose build pack, base directory
`/screenshot-server`, and give the service a domain with port 8765, for example
`https://shots.example.org:8765`.

The container runs as root, so `./screenshots` may belong to root. If `rm`
refuses: `docker run --rm -v "$PWD":/w python:3.13-slim rm -rf /w/screenshots`.

`SCREENSHOT_PORT=9000 docker compose up -d` publishes a different port. The port
is bound to loopback only; `docker-compose.yml` says what to change when the
proxy runs on another host or in a container.

Point an HTTPS site of your reverse proxy at `127.0.0.1:8765` and let request
bodies of 25 MB or more through, since the server accepts up to 20 MB and most
proxies default to less. With Caddy:

```caddyfile
shots.example.org {
    request_body {
        max_size 25MB
    }
    reverse_proxy 127.0.0.1:8765
}
```

With nginx, `client_max_body_size 25m;` next to
`proxy_pass http://127.0.0.1:8765;`.

Behind a proxy, the `remote` field in `index.jsonl` and `tags.jsonl` is the
proxy's address.

## Run it without docker

```bash
screenshot-server/server.py --host 127.0.0.1 --data-dir ~/screenshots
```

| Option | Default | |
| --- | --- | --- |
| `--host` | `0.0.0.0` | Use `127.0.0.1` behind a reverse proxy on the same host. |
| `--port` | `8765` | |
| `--data-dir` | `./screenshots` | Created if missing. |
| `--max-bytes` | `20000000` | Larger uploads get 413 before their body is read. |

Every request logs one line to stderr: time, client address, request line,
status, size. Headers are not logged.

## Storage

Screenshots land in `<data-dir>/<site>/<UTC time>-<driver>-<variant>.png`, for
example `www.amazon.de/20260917T102803Z-parrot-xvfb.png`, with `-2`, `-3`, ...
added when the name is taken. Each upload also appends one JSON line to
`<data-dir>/index.jsonl`, which is what the gallery reads:

```json
{"time": "2026-09-17T10:28:03Z", "page": "https://www.amazon.de", "site": "www.amazon.de", "driver": "parrot", "variant": "xvfb", "title": "Amazon", "file": "www.amazon.de/20260917T102803Z-parrot-xvfb.png", "bytes": 812345, "remote": "127.0.0.1"}
```

The site is the page without its scheme, query and fragment: host lowercased,
path kept, every character other than letters, digits, `.` and `-` turned into
`_`, runs of `_` and of `.` collapsed, `_` and `.` stripped at both ends, at most
150 characters, `unknown` if nothing is left. So `https://www.amazon.de` is
`www.amazon.de`, `https://de.wikipedia.org/wiki/Wikipedia:Hauptseite` is
`de.wikipedia.org_wiki_Wikipedia_Hauptseite`, and no page can name a directory
outside the data dir.

Review tags go to `<data-dir>/tags.jsonl`, one JSON line per change:

```json
{"time": "2026-11-25T14:02:11Z", "site": "www.amazon.de", "tag": "bot-wall", "note": "Cloudflare challenge", "remote": "127.0.0.1"}
```

The file is only ever appended to. The last line of a site is its current tag
and note, so every earlier decision stays in the file. To undo a tag, set
another one. A site cannot go back to `untagged`.

## HTTP API

### `PUT /upload` (or `POST`)

```bash
curl -sS -X PUT --data-binary @shot.png \
  --url-query page=https://www.amazon.de --url-query driver=parrot \
  --url-query variant=xvfb --url-query title=Amazon \
  https://shots.example.org/upload
```

| Query parameter | |
| --- | --- |
| `page` | Required. The URL that was benchmarked, at most 2048 characters. |
| `driver` | Required. `parrot`, `playwright` or `prescan`. |
| `variant` | Required. 1 to 32 lowercase letters, digits or `-`, starting with a letter or digit, for example `xvfb`, `headful`, `headless` or `firefox-com`. The gallery sorts `xvfb`, `headful` and `headless` first and every other variant after them by name. |
| `title` | Optional. The page title, cut to 300 characters. |

The body is the raw PNG with a `Content-Length` (chunked bodies are refused).
A client that sends `Expect: 100-continue`, as curl does for larger files, gets
a rejection for size or parameters before it sends the body.

| Status | |
| --- | --- |
| `201` | Stored. Body `{"file": "<site>/<name>.png"}`, `Location: /shots/<site>/<name>.png`. |
| `400` | A query parameter is missing or invalid, or the body is not a PNG. |
| `411` | No `Content-Length`. |
| `413` | `Content-Length` above `--max-bytes`. |

Errors come as `{"error": "..."}`.

### `POST /tag`

Sets the review tag of one site. The gallery's buttons send it, and so can
curl:

```bash
curl -sS -H 'Accept: application/json' \
  --data-urlencode site=www.amazon.de --data-urlencode tag=bot-wall \
  --data-urlencode 'note=Cloudflare challenge' \
  https://shots.example.org/tag
```

| Form field | |
| --- | --- |
| `site` | Required. A site name as the gallery shows it, which must have at least one screenshot. |
| `tag` | Required. `ok`, `cookie-banner`, `bot-wall`, `error-page`, `blank` or `other`. |
| `note` | Optional. Free text without control characters, at most 500 characters after trimming. An empty note clears the old one, since every tag line carries the whole note. |
| `back`, `next` | Optional. Used by the gallery without JavaScript, see below. |

The body must be `application/x-www-form-urlencoded`, with a `Content-Length` of
at most 16384 bytes.

| Status | |
| --- | --- |
| `200` | Stored, for a request whose `Accept` header contains `application/json`. Body `{"site": ..., "tag": ..., "note": ..., "time": ...}`. |
| `303` | Stored, for any other request, such as a plain form post from a browser without JavaScript. `Location` is the gallery with the filters from `back` and scrolled to the row named in `next`, or to the tagged site. It is rebuilt from the known filters and a valid site name, so it never leaves the gallery. |
| `400` | A field is missing or invalid, or the body is not UTF-8. |
| `404` | The site has no screenshots. |
| `411`, `413`, `415` | No `Content-Length`, a body over 16384 bytes, or another content type. |

### Export

| Path | |
| --- | --- |
| `/export.txt?tag=<tag>` | The page URLs of the sites that carry the tag now, one per line and sorted by site, after a `#` comment header with what was exported, when and how many. `tag` may repeat, as in `?tag=bot-wall&tag=error-page`, and `untagged` lists the sites nobody has tagged yet. The format is that of `sites.txt`, so the file goes straight into `tools/batch.py generate`. Each site's URL is the `page` of its latest upload. |
| `/export.csv` | Every site with screenshots: `site`, `page`, `tag`, `note`, `tagged_at`, `shots`, then the file and time of the latest shot for each driver and variant. Untagged sites have empty tag columns. A cell that starts with `=`, `+`, `-`, `@`, a tab or a carriage return gets a leading `'`, so a spreadsheet does not run what an anonymous client uploaded as a formula. |

### Viewing

| Path | |
| --- | --- |
| `/` | The gallery: one row per site, sorted by name, with its review tag, the tag buttons and note field, the latest shot per driver and variant, and a link to all shots of the site. The header counts the sites per tag. Filters: `?q=` (part of the site or page, any case), `&driver=`, `&variant=`, `&tag=` (a tag or `untagged`), all combinable. |
| `/site/<site>` | Every shot of one site, newest first, with its tag and note. |
| `/shots/<site>/<file>.png` | One screenshot. |
| `/healthz` | `200 ok`, for the compose healthcheck and the proxy. |

Thumbnails are the full PNGs scaled by the browser, since the standard library
cannot resize images. They load lazily, so only what is on screen is fetched.

Any other path is `404`, a known path with the wrong method `405`.

The pages load nothing from elsewhere. Their Content-Security-Policy allows one
script, the gallery's own, by its hash, and lets it talk to this server only.

## Review workflow

After a batch of runs, open the gallery, ideally with `?tag=untagged` so that
only what is left shows, and go through it with the keyboard:

| Key | |
| --- | --- |
| `j`, `k` | Focus the next or previous site. The focused row has a blue bar on the left. After scrolling with the mouse, the first key focuses the row at the top of the screen. |
| `1` to `6` | Tag the focused site `ok`, `cookie-banner`, `bot-wall`, `error-page`, `blank` or `other` and move on to the next one. |
| `n` | Edit the focused site's note. `Enter` saves it with the site's current tag, `Esc` leaves the field. On an untagged site, type the note and then press a number, which sends the note along. |

The tag buttons in each row do the same with the mouse. The page stays where it
is while tagging and only updates the row and the counts, and the URL keeps the
focused row, so a reload continues there. A save that failed marks the row red
and says why.

Without JavaScript the buttons still work: each one posts a plain form, and the
server redirects back to the gallery with the same filters, scrolled to the
next row.

To remeasure what was in the way, export the tags that matter and generate new
batches from them:

```bash
curl -sS 'https://shots.example.org/export.txt?tag=cookie-banner&tag=bot-wall&tag=error-page&tag=blank' \
  > remeasure.txt
tools/batch.py generate remeasure.txt --name remeasure
```

`/export.csv` has everything in one table, including the notes.

## Tests

```bash
python3 -m unittest discover -s screenshot-server -v
```

They start the server on an ephemeral localhost port with a temporary data dir
and need no network.
