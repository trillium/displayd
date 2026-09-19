"""Service and container status for the jumbotron: what is actually running.

Polled-state archetype: this renderer polls on its own interval in a
background thread and draws from cache. A poll never blocks a draw, a
down or malformed source never kills the frame, and a cold start (first
poll not back yet) renders a sensible waiting frame -- never blank, never
broken.

This view reimplements no discovery. It consumes the lnx-viz inventory
API on this same host (`GET <url>` returning `containers`, `systemd`,
`listeners`, `services`, `host_uptime`, `hostname`, `generated_at`) and
formats it for a wall: tallies of up/down/failed, the failed units by
name, container state, a watch list of named services, and the notable
listening ports. If lnx-viz is not answering, the panel says so -- never
a blank frame, never stale data passed off as fresh.
"""

import json
import threading
import time
import urllib.request

from PIL import ImageDraw, ImageFont

NAME = "services"
DESCRIPTION = "Service status: up/down/failed tallies, failed units, containers, watched services, listening ports (via lnx-viz inventory)"
STATIC = False
PARAMS = {
    "title": {"type": "string", "help": "header text, default SERVICES"},
    "url": {"type": "string", "help": "inventory URL, default http://127.0.0.1:8181/api/inventory"},
    "watch": {"type": "string", "help": "comma-separated service names to spotlight, default displayd.service,lnx-viz.service,docker.service,containerd.service"},
    "interval": {"type": "integer", "help": "poll seconds, default 30"},
    "background": {"type": "string", "help": "background colour, default near-black"},
}

DEFAULT_URL = "http://127.0.0.1:8181/api/inventory"
DEFAULT_WATCH = "displayd.service,lnx-viz.service,docker.service,containerd.service"
POLL_DEFAULT_INTERVAL = 30
FETCH_TIMEOUT = 10
DRAW_REFRESH = 15  # re-render at least this often so the age line stays honest

C_BG = (8, 8, 12)
C_TEXT = (235, 235, 240)
C_DIM = (140, 140, 150)
C_LINE = (60, 60, 70)
C_UP = (80, 220, 120)
C_DOWN = (130, 130, 140)
C_FAILED = (255, 90, 90)
C_WARN = (255, 180, 60)

PAD = 60
HEADER_SIZE = 72
LABEL_SIZE = 44
COUNT_SIZE = 130
ROW_SIZE = 40
SUB_SIZE = 36
FOOT_SIZE = 30

# Resident process-wide poll state: survives view switches, so re-selecting
# this view is instantly populated, never empty.
_POLL = {
    "lock": threading.Lock(),
    "thread": None,
    "wake": threading.Event(),
    "cfg": {},
    "snapshot": None,
    "updated": 0.0,
    "health": "cold",  # cold | warm | stale | error
    "error": None,
}


def _font(screen, name, size):
    try:
        path = screen.font_path(name)
    except Exception:
        return None
    if path is None:
        return None
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return None


def _font_or_default(screen, name, size):
    return _font(screen, name, size) or ImageFont.load_default()


def _fit(draw, text, font, max_w, max_chars=90):
    text = str(text or "")
    if font is not None:
        try:
            while len(text) > 4 and draw.textlength(text, font=font) > max_w:
                text = text[:-2]
            return text
        except Exception:
            pass
    return text[:max_chars] if len(text) > max_chars else text


# ---- inventory shaping (pure over decoded JSON: unit-testable) ----------

def _build_snapshot(data, watch):
    """Shape raw inventory JSON into what the wall needs. Raises ValueError
    on a missing or malformed payload."""
    if not isinstance(data, dict):
        raise ValueError("inventory is not an object")
    services = data.get("services")
    if not isinstance(services, list):
        raise ValueError("inventory has no services list")
    containers = data.get("containers")
    if not isinstance(containers, list):
        containers = []
    listeners = data.get("listeners")
    if not isinstance(listeners, list):
        listeners = []

    up = down = failed = 0
    failed_units = []
    by_name = {}
    for svc in services:
        if not isinstance(svc, dict):
            continue
        name = str(svc.get("name") or "?")
        state = str(svc.get("state") or "").strip().lower()
        by_name[name] = {
            "state": state,
            "detail": str(svc.get("detail") or ""),
            "uptime": str(svc.get("uptime") or ""),
            "restarts": str(svc.get("restarts") or "0"),
        }
        if state == "up":
            up += 1
        elif state == "failed":
            failed += 1
            failed_units.append(name)
        else:
            down += 1
    failed_units.sort()

    watched = []
    for name in [w.strip() for w in str(watch or "").split(",") if w.strip()]:
        info = by_name.get(name)
        if info is None:
            watched.append({"name": name, "state": "unknown", "detail": "not in inventory"})
        else:
            watched.append({"name": name, **info})

    conts = []
    for c in containers:
        if not isinstance(c, dict):
            continue
        conts.append({
            "name": str(c.get("name") or "?"),
            "state": str(c.get("state") or "?").strip().lower(),
            "status": str(c.get("status") or ""),
        })

    # Notable listening ports: dedupe by port (0.0.0.0:22 and [::]:22 are
    # one line on a wall), skip ephemeral tailscale noise, cap at 8. The
    # panel's own neighbours come first: when the wall runs out of room
    # it is the high-numbered strangers that drop off, never these.
    PRIORITY = (8181, 8980, 7080, 22, 53, 631, 443)
    seen_ports, ports = set(), []
    valid = []
    for l in listeners:
        if not isinstance(l, dict):
            continue
        try:
            port = int(l.get("port") or 0)
        except (TypeError, ValueError):
            continue
        if not port or port in seen_ports or port >= 30000:
            continue
        seen_ports.add(port)
        valid.append({"port": port,
                      "process": str(l.get("process") or "?")[:24]})
    def _rank(p):
        try:
            return PRIORITY.index(p["port"])
        except ValueError:
            return len(PRIORITY) + p["port"] / 100000.0
    ports = sorted(valid, key=_rank)[:8]

    return {
        "up": up,
        "down": down,
        "failed": failed,
        "total": up + down + failed,
        "failed_units": failed_units,
        "watched": watched,
        "containers": conts,
        "ports": ports,
        "host_uptime": str(data.get("host_uptime") or ""),
        "hostname": str(data.get("hostname") or ""),
        "generated_at": data.get("generated_at"),
    }


def _fetch(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        if resp.status != 200:
            raise ValueError("HTTP %s" % resp.status)
        return json.loads(resp.read().decode("utf-8", "replace"))


def _poll_once(cfg):
    data = _fetch(cfg.get("url") or DEFAULT_URL)
    return _build_snapshot(data, cfg.get("watch") or DEFAULT_WATCH)


def _poll_loop():
    while True:
        with _POLL["lock"]:
            cfg = dict(_POLL["cfg"])
            interval = cfg.get("interval") or POLL_DEFAULT_INTERVAL
            try:
                interval = max(5, min(3600, int(interval)))
            except (TypeError, ValueError):
                interval = POLL_DEFAULT_INTERVAL
        try:
            snap = _poll_once(cfg)
            with _POLL["lock"]:
                _POLL["snapshot"] = snap
                _POLL["updated"] = time.time()
                _POLL["health"] = "warm"
                _POLL["error"] = None
        except Exception as err:
            with _POLL["lock"]:
                _POLL["error"] = str(err)[:160]
                # A failed poll never discards the last good frame's data:
                # keep the snapshot, mark it stale (or error when cold).
                if _POLL["snapshot"] is None:
                    _POLL["health"] = "error"
                else:
                    _POLL["health"] = "stale"
        _POLL["wake"].wait(max(5, interval))
        _POLL["wake"].clear()


def _ensure_poll(cfg):
    with _POLL["lock"]:
        _POLL["cfg"] = dict(cfg)
        alive = _POLL["thread"] is not None and _POLL["thread"].is_alive()
        if not alive:
            thread = threading.Thread(target=_poll_loop, daemon=True)
            _POLL["thread"] = thread
            thread.start()
        else:
            _POLL["wake"].set()


def _get_state():
    with _POLL["lock"]:
        return (dict(_POLL["cfg"]), _POLL["snapshot"], _POLL["updated"],
                _POLL["health"], _POLL["error"])


def _age(updated):
    if not updated:
        return "no data yet"
    secs = max(0, time.time() - updated)
    if secs < 60:
        return "updated %ds ago" % int(secs)
    if secs < 3600:
        return "updated %dm ago" % int(secs // 60)
    return "updated %dh ago" % int(secs // 3600)


def _short_name(unit):
    for suffix in (".service",):
        if unit.endswith(suffix):
            return unit[: -len(suffix)]
    return unit


# ---- drawing --------------------------------------------------------------

def _draw(screen, title, bg, url):
    _, snap, updated, health, error = _get_state()
    img = screen.new_image(bg)
    draw = ImageDraw.Draw(img)
    head_font = _font_or_default(screen, "DejaVuSans-Bold", HEADER_SIZE)
    label_font = _font_or_default(screen, "DejaVuSans-Bold", LABEL_SIZE)
    count_font = _font_or_default(screen, "DejaVuSans-Bold", COUNT_SIZE)
    row_font = _font_or_default(screen, "DejaVuSans", ROW_SIZE)
    sub_font = _font_or_default(screen, "DejaVuSans", SUB_SIZE)
    small_font = _font_or_default(screen, "DejaVuSans", FOOT_SIZE)

    host = snap.get("hostname", "") if snap else ""
    header = title + ("  \u00b7  " + host if host else "")
    draw.text((PAD, 24), _fit(draw, header, head_font, screen.W - 2 * PAD),
              font=head_font, fill=C_TEXT)
    dot = {"cold": (120, 120, 130), "warm": C_UP,
           "stale": C_WARN, "error": C_FAILED}[health]
    status = "%s \u00b7 %s" % (health, _age(updated))
    try:
        w = draw.textlength(status, font=small_font)
    except Exception:
        w = 0
    draw.ellipse([screen.W - PAD - 22, 52, screen.W - PAD - 2, 72], fill=dot)
    draw.text((screen.W - PAD - w - 36, 34), status, font=small_font, fill=C_DIM)
    draw.line([(PAD, 128), (screen.W - PAD, 128)], fill=C_LINE, width=2)

    if snap is None:
        # Cold start or a source that has never answered: say so plainly.
        if health == "error":
            big = "SOURCE NOT ANSWERING"
            color = C_FAILED
            sub = "lnx-viz inventory unreachable"
        else:
            big = "waiting for first poll"
            color = C_DIM
            sub = "fetching lnx-viz inventory"
        draw.text((PAD, 220), _fit(draw, big, label_font, screen.W - 2 * PAD),
                  font=label_font, fill=color)
        draw.text((PAD, 300), _fit(draw, sub, row_font, screen.W - 2 * PAD),
                  font=row_font, fill=C_DIM)
        draw.text((PAD, 360), _fit(draw, url, sub_font, screen.W - 2 * PAD),
                  font=sub_font, fill=C_DIM)
        if error:
            draw.text((PAD, 430),
                      _fit(draw, "last error: " + error, sub_font, screen.W - 2 * PAD),
                      font=sub_font, fill=C_FAILED)
        screen.present(img)
        return

    col_w = screen.W - 2 * PAD

    # Tally strip: three glanceable counts.
    tallies = (("UP", snap["up"], C_UP), ("DOWN", snap["down"], C_DOWN),
               ("FAILED", snap["failed"], C_FAILED if snap["failed"] else C_DIM))
    third = col_w / 3.0
    for idx, (label, count, color) in enumerate(tallies):
        x = PAD + idx * third
        draw.text((x, 150), "%d" % count, font=count_font, fill=color)
        draw.text((x + 6, 300), label, font=label_font, fill=C_TEXT)
    draw.text((PAD, 372),
              _fit(draw, "%d services tracked" % snap["total"], sub_font, col_w),
              font=sub_font, fill=C_DIM)
    y = 440
    draw.line([(PAD, y), (screen.W - PAD, y)], fill=C_LINE, width=2)
    y += 26

    # Failed units by name -- the thing the captain actually needs.
    if snap["failed_units"]:
        draw.text((PAD, y), "FAILED", font=label_font, fill=C_FAILED)
        y += 60
        for unit in snap["failed_units"][:3]:
            draw.text((PAD, y), _fit(draw, "\u25cf " + unit, row_font, col_w),
                      font=row_font, fill=C_FAILED)
            y += 56
    else:
        draw.text((PAD, y), "no failed units", font=row_font, fill=C_DIM)
        y += 56

    # Watched services: dots, never a table.
    y += 10
    x = PAD
    for item in snap["watched"][:4]:
        state = item["state"]
        color = C_UP if state == "up" else (C_FAILED if state == "failed" else C_WARN)
        glyph = "\u25cf"
        text = "%s %s" % (glyph, _short_name(item["name"]))
        try:
            tw = draw.textlength(text, font=row_font)
        except Exception:
            tw = len(text) * 20
        if x + tw > screen.W - PAD and x > PAD:
            x = PAD
            y += 56
        draw.text((x, y), text, font=row_font, fill=color)
        x += tw + 60
    y += 62
    draw.line([(PAD, y), (screen.W - PAD, y)], fill=C_LINE, width=2)
    y += 26

    # Containers: one per segment, green when running, amber otherwise.
    if snap["containers"]:
        x = PAD
        try:
            head_w = draw.textlength("CONTAINERS  ", font=row_font)
        except Exception:
            head_w = 0
        draw.text((x, y), "CONTAINERS", font=row_font, fill=C_DIM)
        x += head_w
        for c in snap["containers"][:4]:
            running = c["state"] == "running"
            seg = "%s %s (%s)" % ("\u25cf" if running else "\u25cb",
                                     c["name"], c["state"])
            color = C_UP if running else C_WARN
            try:
                seg_w = draw.textlength(seg + "   ", font=row_font)
            except Exception:
                seg_w = len(seg) * 20
            if x + seg_w > screen.W - PAD and x > PAD + head_w:
                break  # wall space is finite: show fewer, never truncate
            draw.text((x, y), seg, font=row_font, fill=color)
            x += seg_w
        y += 58
    if snap["ports"]:
        # Drop trailing ports until the whole line fits: a cut "pyth"
        # helps nobody, fewer complete entries do.
        shown = list(snap["ports"])
        while shown:
            line = "PORTS  " + "   ".join(
                "%d/%s" % (p["port"], p["process"]) for p in shown)
            try:
                fits = draw.textlength(line, font=sub_font) <= col_w
            except Exception:
                fits = len(line) <= 120
            if fits:
                break
            shown.pop()
        if shown:
            draw.text((PAD, y), line, font=sub_font, fill=C_DIM)
            y += 56

    # Footer: host uptime, and the poll error when stale (honest staleness).
    foot = snap["host_uptime"]
    if health in ("stale", "error") and error:
        foot += "   [inventory poll failed: %s]" % error
    draw.text((PAD, screen.H - 56), _fit(draw, foot, small_font, col_w),
              font=small_font, fill=C_DIM)
    screen.present(img)


def _snapshot_key():
    _, snap, updated, health, _ = _get_state()
    if snap is None:
        return ("cold", health)
    return (updated, snap["up"], snap["down"], snap["failed"],
            [c["state"] for c in snap["containers"]],
            [(w["name"], w["state"]) for w in snap["watched"]], health)


def run(screen, params, stop):
    params = params or {}
    title = str(params.get("title") or "SERVICES").upper()
    bg = screen.color(params.get("background"), C_BG)
    url = str(params.get("url") or DEFAULT_URL)
    try:
        interval = int(params.get("interval") or POLL_DEFAULT_INTERVAL)
    except (TypeError, ValueError):
        interval = POLL_DEFAULT_INTERVAL
    interval = max(5, min(3600, interval))
    _ensure_poll({"url": url,
                  "watch": params.get("watch") or DEFAULT_WATCH,
                  "interval": interval})

    # First frame goes up immediately from cache (or the cold frame) --
    # switching here never waits on I/O.
    _draw(screen, title, bg, url)
    last_key = _snapshot_key()
    last_draw = time.time()
    while not stop.is_set():
        key = _snapshot_key()
        now = time.time()
        if key != last_key or (now - last_draw) >= DRAW_REFRESH:
            last_key = key
            last_draw = now
            try:
                _draw(screen, title, bg, url)
            except Exception:
                # A draw failure must not kill the daemon loop; the last
                # good frame stays on the panel.
                pass
        stop.wait(1.0)
