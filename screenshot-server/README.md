# screenshot-server

A run on the GMT cluster keeps no files. So after a page has loaded, the
scenarios take a PNG of what the browser rendered and upload it here, and a
gallery shows the latest shot of every site, one per driver and variant, side by
side. Scrolling through it is how a bot wall, an error page or a blank window
gets noticed: a run that shows one of those measured that page, not the site.

`server.py` is a single file, Python 3.9 or newer, standard library only.

**Anyone who knows the URL can upload screenshots and view all of them.** There
is no authentication and the server speaks plain HTTP. That is acceptable only
because the server is temporary: start it for one benchmark run, behind your
HTTPS reverse proxy, and delete it and its data afterwards.

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

Behind a proxy, the `remote` field in `index.jsonl` is the proxy's address.

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
| `driver` | Required. `parrot` or `playwright`. |
| `variant` | Required. `xvfb`, `headful` or `headless`. |
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

### Viewing

| Path | |
| --- | --- |
| `/` | The gallery: one row per site, sorted by name, with the latest shot per driver and variant and a link to all shots of the site. Filters: `?q=` (part of the site or page, any case), `&driver=`, `&variant=`. |
| `/site/<site>` | Every shot of one site, newest first. |
| `/shots/<site>/<file>.png` | One screenshot. |
| `/healthz` | `200 ok`, for the compose healthcheck and the proxy. |

Thumbnails are the full PNGs scaled by the browser, since the standard library
cannot resize images. They load lazily, so only what is on screen is fetched.

Any other path is `404`, a known path with the wrong method `405`.

## Tests

```bash
python3 -m unittest discover -s screenshot-server -v
```

They start the server on an ephemeral localhost port with a temporary data dir
and need no network.
