# displayd

An API-driven display server for a headless Linux box.

`displayd` owns the physical screen of a machine that has no desktop environment
and exposes it through a small JSON API. You drive the screen entirely over the
network: put content up, ask what is showing, turn the panel on and off, and grab
a picture of the current frame.

It exists for the case where you want a screen you can control programmatically
and nothing else — no window manager, no compositor, no display server to
babysit, no input path. The machine boots to a plain console; `displayd` writes
pixels straight to the framebuffer and hands the console back when it exits.

## What it is not

* No windows, no compositing, no z-order. One thing owns the screen at a time.
* No input. It is output only, by design.
* No GPU acceleration. It is plain memory writes to the framebuffer, which is
  plenty for text, images, and modest animation.

## How it fits together

    HTTP client ──▶ displayd ──▶ /dev/fb0  (pixels)
                        │
                        └──▶ /sys/class/backlight/*  (panel brightness)
                        └──▶ /sys/class/graphics/fb0/blank  (blanking)

Everything that appears on screen comes from a **renderer plugin** — one Python
file in `renderers/`. The core knows nothing about any particular renderer, so
adding a new kind of content means dropping in a single file and restarting the
service. The new renderer then advertises itself, with its own parameter schema,
through `GET /renderers`.

## Requirements

* Linux with a framebuffer console (`/dev/fb0`).
* Python 3.8+.
* [Pillow](https://python-pillow.org/) — used by the daemon and by the bundled
  renderers.
* For screen power control: a `/sys/class/backlight/*` device is used when
  present. Without one, blanking still works but the panel backlight is not
  touched.

## Install

    sudo ./install.sh                 # installs into /opt/displayd
    sudo ./install.sh /path/to/prefix # or choose your own prefix

The installer copies the daemon and renderers into place, writes the systemd unit
with that path baked in, and starts the service. It is idempotent — re-run it
after editing a renderer to deploy the change.

By hand, the equivalent is:

    python3 -m pip install -r requirements.txt
    sudo install -d /opt/displayd/renderers
    sudo install -m 0644 displayd.py /opt/displayd/
    sudo install -m 0644 renderers/*.py /opt/displayd/renderers/
    sudo install -m 0644 displayd.service /etc/systemd/system/
    sudo systemctl daemon-reload && sudo systemctl enable --now displayd

It runs as root, because writing to the framebuffer and driving the backlight
both require it.

### Configuration

The defaults listen on **loopback only**. The API has no authentication, so
binding it wider is a deliberate choice you make, not something the daemon does
for you.

| Flag | Variable | Default | Meaning |
| --- | --- | --- | --- |
| `--port` | `DISPLAYD_PORT` | `8980` | TCP port to listen on |
| `--bind` | `DISPLAYD_BIND` | `127.0.0.1` | address to listen on |

Flags win over environment variables, and environment variables win over the
defaults. To reach the daemon from another machine, bind the address you actually
use and pair it with one of:

* a **host firewall** that allows only the clients you expect, or
* a **private network** — a VPN or a Tailscale address — so the port is never
  reachable from the open internet.

For example, to serve a Tailscale address:

    displayd --bind 100.x.y.z

or, in the unit file:

    Environment=DISPLAYD_BIND=100.x.y.z

Do not bind `0.0.0.0` on a machine reachable from an untrusted network: that
publishes an unauthenticated control surface to everything that can route to it.

## API

| Method | Path | Body | Meaning |
| --- | --- | --- | --- |
| GET | `/` | – | web control panel (live preview, renderer picker, power) |
| GET | `/health` | – | liveness |
| GET | `/state` | – | what is showing, screen power, display facts |
| GET | `/renderers` | – | available renderers and their params |
| GET | `/snapshot` | – | PNG of the last frame presented |
| POST | `/show` | `{"renderer":"text","params":{...}}` | switch content |
| POST | `/clear` | – | blank the screen to black |
| POST | `/screen` | `{"power":"on"\|"off"}` | screen power |
| POST | `/screen/on`, `/screen/off` | – | screen power shorthand |

Example — put a message on the screen:

    curl -s -X POST localhost:8980/show -H 'Content-Type: application/json' \
      -d '{"renderer":"text","params":{"text":"hello","color":"#00ff88"}}'

Ask what is showing:

    curl -s localhost:8980/state

Save a picture of the current screen:

    curl -s localhost:8980/snapshot -o screen.png

Every mutating call returns the new `/state` payload, so a caller never has to
poll to find out what happened.

## Bundled renderers

| Name | Static | Params |
| --- | --- | --- |
| `text` | yes | `text` (required), `size`, `font`, `color`, `background` |
| `image` | yes | `path` (required, file or URL), `fit` (contain/cover/stretch), `background` |
| `solid` | yes | `color` |
| `clock` | no | `format`, `color`, `background` |
| `life` | no | `cell`, `density`, `speed` |

Static renderers draw one frame and return; that frame stays on screen.
Animated renderers loop until the daemon stops them.

## Writing a renderer

A renderer is a single Python file in `renderers/`. It declares four module-level
names and a `run` function:

```python
"""Say hello, centred."""

from PIL import ImageDraw, ImageFont

NAME = "hello"
DESCRIPTION = "Show a greeting"
STATIC = True
PARAMS = {"who": {"type": "string", "help": "who to greet, default world"}}


def run(screen, params, stop):
    who = params.get("who") or "world"
    text = "hello " + who
    img = screen.new_image((0, 0, 0))
    draw = ImageDraw.Draw(img)
    centre = (screen.W // 2, screen.H // 2)
    path = screen.font_path()
    if path is None:                 # no TrueType found; use PIL's built-in bitmap font
        draw.text(centre, text, fill=(255, 255, 255), anchor="mm")
    else:
        draw.text(centre, text, font=ImageFont.truetype(path, 180),
                  fill=(255, 255, 255), anchor="mm")
    screen.present(img)
```

Drop that file at `renderers/hello.py`, restart the service, and it is live:

    sudo systemctl restart displayd
    curl -s localhost:8980/renderers
    curl -s -X POST localhost:8980/show -H 'Content-Type: application/json' \
      -d '{"renderer":"hello","params":{"who":"there"}}'

That is the whole extension step. No core edits, no registration, no config.

### The renderer contract

| Name | Required | Meaning |
| --- | --- | --- |
| `NAME` | no | API name; defaults to the filename without `.py` |
| `DESCRIPTION` | no | shown in `GET /renderers` |
| `STATIC` | no | `True` (default) draws once; `False` loops until `stop` is set |
| `PARAMS` | no | parameter schema, surfaced verbatim by `GET /renderers` |
| `run(screen, params, stop)` | yes | does the drawing |

`screen` is the handle to the physical display:

* `screen.W`, `screen.H` — pixel size
* `screen.new_image(rgb)` — a fresh PIL RGB image
* `screen.present(img)` — push it to the screen and remember it as the last frame
* `screen.clear(rgb)` — fill the screen
* `screen.color(value, default)` — parse `#rgb`, `#rrggbb`, a colour name, or an `(r,g,b)` tuple
* `screen.font_path(family)` — locate a TrueType font, or `None` if absent

A renderer that raises is recorded in `/state` under `last_error` and does not
take the daemon down; a plugin that fails to import is reported against its own
name by `GET /renderers`. Neither stops the other renderers from working.

## Screen power

Screen on/off is core state rather than a renderer, so it stays orthogonal to
whatever is showing:

* **Off** — the framebuffer is blanked (`/sys/class/graphics/fb0/blank` = 4) and
  the panel backlight is driven to 0.
* **On** — the framebuffer is unblanked, the remembered brightness is restored,
  and the last frame is re-presented, so whatever was showing comes back.

The daemon and its renderer keep running across a power cycle; only the panel
goes dark. The brightness value in use before the first power-off is what gets
restored.

## The console

On startup the daemon asks the kernel console to stop painting
(`KDSETMODE`/`KD_GRAPHICS`) so nothing draws over the framebuffer, and it hands
the console back on exit. Startup also clears any blanking left over and, if the
panel is at zero brightness, restores it — so a machine that was left powered off
is readable again as soon as the service starts.

## License

MIT — see [LICENSE](LICENSE).
