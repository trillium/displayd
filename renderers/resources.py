"""Machine resources for the jumbotron: CPU, load, memory, disk, uptime.

Polled-state archetype: this renderer polls on its own interval in a
background thread and draws from cache. A poll never blocks a draw, a
missing or malformed /proc never kills the frame, and a cold start (first
poll not back yet) renders a sensible waiting frame -- never blank, never
broken.

Data: /proc/stat, /proc/meminfo, /proc/loadavg, /proc/uptime, and
os.statvfs for disk. Nothing is shelled out to: /proc is cheap, always
present, and never blocks.

CPU percentage is a *delta between consecutive polls*, never an
instantaneous reading. The host this panel serves has run at very high
load, so a naive single-sample read would show nonsense. The first poll
only banks a sample; the frame says "sampling" until the second poll
lands a real delta.
"""

import os
import socket
import threading
import time

from PIL import ImageDraw, ImageFont

NAME = "resources"
DESCRIPTION = "Machine resources: CPU, load average, memory, swap, disk, uptime"
STATIC = False
PARAMS = {
    "title": {"type": "string", "help": "header text, default RESOURCES"},
    "mounts": {"type": "string", "help": "comma-separated mount points to show, default /"},
    "interval": {"type": "integer", "help": "poll seconds, default 5"},
    "background": {"type": "string", "help": "background colour, default near-black"},
}

POLL_DEFAULT_INTERVAL = 5
DRAW_REFRESH = 5  # re-render at least this often so the age line stays honest

C_BG = (8, 8, 12)
C_TEXT = (235, 235, 240)
C_DIM = (140, 140, 150)
C_LINE = (60, 60, 70)
C_OK = (80, 220, 120)
C_WARN = (255, 180, 60)
C_BAD = (255, 90, 90)

PAD = 60
HEADER_SIZE = 72
LABEL_SIZE = 44
BIG_SIZE = 170
SUB_SIZE = 40
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
    "prev_cpu": None,  # (total, idle) from the previous poll
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


# ---- sampling (pure functions over /proc text: unit-testable) ------------

def _parse_cpu_line(text):
    """First `cpu` line of /proc/stat -> (total, idle). Raises ValueError."""
    for line in str(text or "").splitlines():
        if line.startswith("cpu "):
            parts = line.split()
            nums = [int(v) for v in parts[1:]]
            if len(nums) < 4:
                raise ValueError("short cpu line")
            total = sum(nums)
            idle = nums[3] + (nums[4] if len(nums) > 4 else 0)  # idle + iowait
            return total, idle
    raise ValueError("no aggregate cpu line")


def _cpu_pct(prev, cur):
    """Delta between two (total, idle) samples -> 0..100 float, or None when
    the counters did not advance (first poll, or a wrapped/odd read)."""
    if prev is None or cur is None:
        return None
    d_total = cur[0] - prev[0]
    d_idle = cur[1] - prev[1]
    if d_total <= 0:
        return None
    pct = 100.0 * (1.0 - float(d_idle) / float(d_total))
    return max(0.0, min(100.0, pct))


def _parse_loadavg(text):
    """-> (l1, l5, l15, procs). Raises ValueError."""
    parts = str(text or "").split()
    if len(parts) < 4:
        raise ValueError("short loadavg")
    return float(parts[0]), float(parts[1]), float(parts[2]), parts[3]


def _parse_meminfo(text):
    """-> dict with mem/swap used+total in bytes. Prefers MemAvailable,
    falls back to Free+Buffers+Cached the way `free` does."""
    vals = {}
    for line in str(text or "").splitlines():
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        try:
            vals[key.strip()] = int(rest.strip().split()[0]) * 1024
        except (ValueError, IndexError):
            continue
    if "MemTotal" not in vals:
        raise ValueError("no MemTotal")
    total = vals["MemTotal"]
    avail = vals.get("MemAvailable")
    if avail is None:
        avail = vals.get("MemFree", 0) + vals.get("Buffers", 0) + vals.get("Cached", 0)
    swap_total = vals.get("SwapTotal", 0)
    swap_free = vals.get("SwapFree", 0)
    return {
        "total": total,
        "used": max(0, total - avail),
        "swap_total": swap_total,
        "swap_used": max(0, swap_total - swap_free),
    }


def _read(path):
    with open(path, "r") as fh:
        return fh.read()


def _poll_once(cfg, prev_cpu):
    """One poll. Returns (snapshot, new_prev_cpu). Raises on failure; a
    missing /proc file is a failed poll, never a crash."""
    total, idle = _parse_cpu_line(_read("/proc/stat"))
    cur = (total, idle)
    pct = _cpu_pct(prev_cpu, cur)
    load = _parse_loadavg(_read("/proc/loadavg"))
    mem = _parse_meminfo(_read("/proc/meminfo"))
    try:
        uptime_s = float(_read("/proc/uptime").split()[0])
    except (ValueError, IndexError, OSError):
        uptime_s = 0.0
    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = "?"
    try:
        ncpu = os.cpu_count() or 1
    except Exception:
        ncpu = 1

    disks = []
    seen_devs = set()
    for mount in [m.strip() for m in str(cfg.get("mounts") or "/").split(",") if m.strip()]:
        try:
            st = os.statvfs(mount)
            dev = None
            try:
                dev = os.stat(mount).st_dev
            except OSError:
                pass
            if dev is not None and dev in seen_devs:
                continue  # same filesystem twice: show it once
            if dev is not None:
                seen_devs.add(dev)
            total_b = st.f_frsize * st.f_blocks
            # f_bfree (not f_bavail): matches `df` Used/Use% exactly, so the
            # wall agrees with the terminal. Reserved blocks count as used.
            used_b = max(0, total_b - st.f_frsize * st.f_bfree)
            disks.append({
                "mount": mount,
                "used": used_b,
                "total": total_b,
                "pct": (100.0 * used_b / total_b) if total_b else 0.0,
            })
        except OSError as err:
            disks.append({"mount": mount, "error": str(err)[:80]})
    if not disks:
        disks.append({"mount": "/", "error": "no mounts configured"})

    snap = {
        "cpu_pct": pct,  # None until the second poll banks a delta
        "load": load[:3],
        "procs": load[3],
        "mem_used": mem["used"],
        "mem_total": mem["total"],
        "swap_used": mem["swap_used"],
        "swap_total": mem["swap_total"],
        "disks": disks,
        "uptime_s": uptime_s,
        "hostname": hostname,
        "ncpu": ncpu,
    }
    return snap, cur


def _poll_loop():
    while True:
        with _POLL["lock"]:
            cfg = dict(_POLL["cfg"])
            interval = cfg.get("interval") or POLL_DEFAULT_INTERVAL
            try:
                interval = max(2, min(3600, int(interval)))
            except (TypeError, ValueError):
                interval = POLL_DEFAULT_INTERVAL
        try:
            with _POLL["lock"]:
                prev = _POLL["prev_cpu"]
            snap, cur = _poll_once(cfg, prev)
            with _POLL["lock"]:
                _POLL["prev_cpu"] = cur
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
        _POLL["wake"].wait(max(2, interval))
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


# ---- formatting ----------------------------------------------------------

def _gb(n):
    return "%.1f" % (float(n) / (1024 ** 3))


def _uptime(s):
    s = max(0, int(s or 0))
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    mins, _ = divmod(s, 60)
    if days:
        return "up %dd %dh" % (days, hours)
    if hours:
        return "up %dh %dm" % (hours, mins)
    if mins:
        return "up %dm" % mins
    return "up %ds" % s


def _age(updated):
    if not updated:
        return "no data yet"
    secs = max(0, time.time() - updated)
    if secs < 60:
        return "updated %ds ago" % int(secs)
    if secs < 3600:
        return "updated %dm ago" % int(secs // 60)
    return "updated %dh ago" % int(secs // 3600)


# ---- drawing --------------------------------------------------------------

def _bar(draw, x, y, w, h, frac, color):
    draw.rectangle([x, y, x + w, y + h], outline=C_LINE, width=2)
    fill_w = (w - 8) * max(0.0, min(1.0, frac))
    if fill_w > 2:
        draw.rectangle([x + 4, y + 4, x + 4 + fill_w, y + h - 4], fill=color)


def _draw(screen, title, bg):
    _, snap, updated, health, error = _get_state()
    img = screen.new_image(bg)
    draw = ImageDraw.Draw(img)
    head_font = _font_or_default(screen, "DejaVuSans-Bold", HEADER_SIZE)
    label_font = _font_or_default(screen, "DejaVuSans-Bold", LABEL_SIZE)
    big_font = _font_or_default(screen, "DejaVuSans-Bold", BIG_SIZE)
    sub_font = _font_or_default(screen, "DejaVuSans", SUB_SIZE)
    small_font = _font_or_default(screen, "DejaVuSans", FOOT_SIZE)

    host = (snap or {}).get("hostname", "") if snap else ""
    header = title + ("  \u00b7  " + host if host else "")
    draw.text((PAD, 24), _fit(draw, header, head_font, screen.W - 2 * PAD),
              font=head_font, fill=C_TEXT)
    dot = {"cold": (120, 120, 130), "warm": C_OK,
           "stale": C_WARN, "error": C_BAD}[health]
    status = "%s \u00b7 %s" % (health, _age(updated))
    try:
        w = draw.textlength(status, font=small_font)
    except Exception:
        w = 0
    draw.ellipse([screen.W - PAD - 22, 52, screen.W - PAD - 2, 72], fill=dot)
    draw.text((screen.W - PAD - w - 36, 34), status, font=small_font, fill=C_DIM)
    draw.line([(PAD, 128), (screen.W - PAD, 128)], fill=C_LINE, width=2)

    if snap is None:
        msg = "waiting for first poll \u2014 reading /proc" if health == "cold" \
            else "no data: last poll failed"
        draw.text((PAD, 260), _fit(draw, msg, label_font, screen.W - 2 * PAD),
                  font=label_font, fill=C_DIM)
        if error:
            draw.text((PAD, 340),
                      _fit(draw, "last error: " + error, sub_font, screen.W - 2 * PAD),
                      font=sub_font, fill=C_BAD)
        screen.present(img)
        return

    col_w = screen.W - 2 * PAD
    y = 170

    # Row 1: CPU -- the giant number. Load matters on this host, so it gets
    # the sub-line in full: 1/5/15 plus core count and thread pressure.
    draw.text((PAD, y), "CPU", font=label_font, fill=C_DIM)
    pct = snap["cpu_pct"]
    if pct is None:
        cpu_big, cpu_color = "sampling\u2026", C_DIM
    else:
        cpu_big = "%d%%" % int(round(pct))
        cpu_color = C_OK if pct < 70 else (C_WARN if pct < 90 else C_BAD)
    draw.text((PAD, y + 30), cpu_big, font=big_font, fill=cpu_color)
    l1, l5, l15 = snap["load"]
    load_line = "load %.2f  %.2f  %.2f   \u00b7   %d cores   \u00b7   %s procs" % (
        l1, l5, l15, snap["ncpu"], snap["procs"])
    draw.text((PAD + 520, y + 110),
              _fit(draw, load_line, sub_font, col_w - 520),
              font=sub_font, fill=C_TEXT)
    y += 250
    draw.line([(PAD, y), (screen.W - PAD, y)], fill=C_LINE, width=2)
    y += 30

    # Row 2: memory + swap.
    draw.text((PAD, y), "MEM", font=label_font, fill=C_DIM)
    mem_line = "%s / %s GB" % (_gb(snap["mem_used"]), _gb(snap["mem_total"]))
    mem_frac = (float(snap["mem_used"]) / snap["mem_total"]) if snap["mem_total"] else 0.0
    draw.text((PAD, y + 30),
              _fit(draw, mem_line, big_font, col_w - 500),
              font=big_font, fill=C_TEXT)
    _bar(draw, PAD + 1300, y + 90, col_w - 1300, 44, mem_frac,
         C_OK if mem_frac < 0.8 else (C_WARN if mem_frac < 0.93 else C_BAD))
    if snap["swap_total"]:
        swap_line = "swap %s / %s GB" % (_gb(snap["swap_used"]), _gb(snap["swap_total"]))
    else:
        swap_line = "no swap"
    draw.text((PAD + 1300, y + 150),
              _fit(draw, swap_line, sub_font, col_w - 1300),
              font=sub_font, fill=C_DIM)
    y += 260
    draw.line([(PAD, y), (screen.W - PAD, y)], fill=C_LINE, width=2)
    y += 30

    # Row 3: disk, one line per mount.
    draw.text((PAD, y), "DISK", font=label_font, fill=C_DIM)
    y += 62
    for disk in snap["disks"][:3]:  # wall space is finite: three mounts max
        if "error" in disk:
            line = "%s: %s" % (disk["mount"], disk["error"])
            draw.text((PAD, y), _fit(draw, line, sub_font, col_w),
                      font=sub_font, fill=C_BAD)
        else:
            frac = disk["pct"] / 100.0
            line = "%s  %s / %s GB  %d%%" % (
                disk["mount"], _gb(disk["used"]), _gb(disk["total"]),
                int(round(disk["pct"])))
            draw.text((PAD, y), _fit(draw, line, sub_font, col_w - 560),
                      font=sub_font, fill=C_TEXT)
            _bar(draw, PAD + col_w - 520, y + 2, 520, 40, frac,
                 C_OK if frac < 0.8 else (C_WARN if frac < 0.92 else C_BAD))
        y += 68

    # Footer: uptime, and the poll error when stale (honest staleness).
    foot = _uptime(snap["uptime_s"])
    if health in ("stale", "error") and error:
        foot += "   [last poll failed: %s]" % error
    draw.text((PAD, screen.H - 56),
              _fit(draw, foot, small_font, col_w),
              font=small_font, fill=C_DIM)
    screen.present(img)


def _snapshot_key():
    _, snap, updated, health, _ = _get_state()
    if snap is None:
        return ("cold", health)
    return (updated, snap["cpu_pct"], snap["load"], snap["mem_used"],
            [(d.get("mount"), d.get("used")) for d in snap["disks"]], health)


def run(screen, params, stop):
    params = params or {}
    title = str(params.get("title") or "RESOURCES").upper()
    bg = screen.color(params.get("background"), C_BG)
    try:
        interval = int(params.get("interval") or POLL_DEFAULT_INTERVAL)
    except (TypeError, ValueError):
        interval = POLL_DEFAULT_INTERVAL
    interval = max(2, min(3600, interval))
    _ensure_poll({"mounts": params.get("mounts") or "/", "interval": interval})

    # First frame goes up immediately from cache (or the cold frame) --
    # switching here never waits on I/O.
    _draw(screen, title, bg)
    last_key = _snapshot_key()
    last_draw = time.time()
    while not stop.is_set():
        key = _snapshot_key()
        now = time.time()
        if key != last_key or (now - last_draw) >= DRAW_REFRESH:
            last_key = key
            last_draw = now
            try:
                _draw(screen, title, bg)
            except Exception:
                # A draw failure must not kill the daemon loop; the last
                # good frame stays on the panel.
                pass
        stop.wait(1.0)
