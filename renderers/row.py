"""Concept2 rowing streak for the jumbotron: current streak + last row.

File-polled archetype (cf. services.py): this renderer re-reads
row_tracker's `rows.txt` on its own interval and draws from the parsed
snapshot. A read never blocks a draw, a missing or malformed log never
kills the frame, and a cold start renders a sensible waiting frame --
never blank, never broken.

Streak math mirrors `row.sh`'s rest-day bank rule exactly:
  * every row beyond the first on one calendar day deposits one credit;
  * a zero-row day withdraws one credit and the streak carries through
    (a "covered" rest day: day streak grows, row streak unchanged);
  * an empty bank plus a missed day breaks the streak, fresh start at
    1 day on the next row (no inheritance);
  * streaks reset at the year boundary (rows < year never mix).

Log lines are ISO timestamps (`YYYY-MM-DDTHH:MM:SS+HH:MM`); anything
else in the file (blank lines, `??` placeholders) is ignored.

Remote source: by default the streak is sourced live from the mini1 PM5
bridge WebSocket that also feeds OBS (`ws://mini1:8765/obs/ws`) -- the
same feed, never a second serving path. A poll that observes a real
workout underway (rower connected, distance past warm-up) journals the
calendar day to a small local journal file; streaks are computed over
the union of the remote journal and the local rows.txt fallback, so
history survives and a dead remote degrades to a stale-marked
last-known streak instead of a blank frame.
"""

import base64
import datetime
import hashlib
import json
import os
import socket
import ssl
import tempfile
import time
import urllib.parse
import urllib.request

from PIL import ImageDraw, ImageFont

NAME = "row"
DESCRIPTION = "Concept2 rowing streak: current day/row streak, last row, year pace (from row_tracker rows.txt)"
STATIC = False
# Playlist progress-bar colour for this view (see playlist.accent_for).
ACCENT = "#5CFF9D"
PARAMS = {
    "path": {"type": "string", "help": "local rows.txt fallback; default $DISPLAYD_ROW_FILE, else sibling row_tracker checkout"},
    "source": {"type": "string", "help": "primary row source: ws(s) URL of the mini1 PM5 stats feed, http(s) URL of rows.txt text, or a file path; default $DISPLAYD_ROW_SOURCE, else ws://mini1:8765/obs/ws"},
    "timeout": {"type": "integer", "help": "remote fetch seconds, default 10 (clamped 2..60)"},
    "journal": {"type": "string", "help": "sightings journal path; default $DISPLAYD_ROW_JOURNAL, else next to the local log"},
    "title": {"type": "string", "help": "header text, default ROWING"},
    "interval": {"type": "integer", "help": "poll seconds, default 60"},
    "background": {"type": "string", "help": "background colour, default near-black"},
}

ENV_VAR = "DISPLAYD_ROW_FILE"
ENV_SOURCE = "DISPLAYD_ROW_SOURCE"
ENV_JOURNAL = "DISPLAYD_ROW_JOURNAL"
# Primary row source: the mini1 PM5 bridge WebSocket that feeds OBS (same
# feed, no second serving path). Reachable from lnx-server over the tailnet.
DEFAULT_SOURCE = "ws://mini1:8765/obs/ws"
FETCH_TIMEOUT_DEFAULT = 10
FETCH_TIMEOUT_MIN = 2
FETCH_TIMEOUT_MAX = 60
# A remote sighting counts as a row only past this distance: filters the
# paired-but-idle PM5 (0 m) while catching any real workout within a poll.
MIN_ROW_DISTANCE_M = 100.0
HTTP_MAX_BYTES = 512 * 1024
WS_MAX_BYTES = 1024 * 1024
WS_MAX_MESSAGES = 50
POLL_DEFAULT_INTERVAL = 60
DRAW_REFRESH = 60  # re-render at least this often so the age line stays honest

C_BG = (8, 8, 12)
C_TEXT = (235, 235, 240)
C_DIM = (140, 140, 150)
C_LINE = (60, 60, 70)
C_FIRE = (255, 150, 50)
C_UP = (80, 220, 120)
C_FAILED = (255, 90, 90)
C_WARN = (255, 180, 60)

PAD = 60
HEADER_SIZE = 72
BIG_SIZE = 300
LABEL_SIZE = 44
ROW_SIZE = 48
SUB_SIZE = 36
FOOT_SIZE = 30


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


# ---- path resolution (no hardcoded home paths) ---------------------------

def resolve_path(explicit=None):
    """Where to read the row log from. Precedence: explicit `path`
    param, `$DISPLAYD_ROW_FILE`, then a sibling `row_tracker/rows.txt`
    next to (up to three levels above) this displayd checkout -- so the
    pairing survives checkout moves on any host. Returns None when no
    candidate exists; the panel says so instead of going blank."""
    if explicit:
        return str(explicit)
    env = os.environ.get(ENV_VAR)
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))  # .../displayd/renderers
    root = os.path.dirname(here)  # .../displayd
    node = root
    for _ in range(4):
        cand = os.path.join(os.path.dirname(node), "row_tracker", "rows.txt")
        if os.path.exists(cand):
            return cand
        direct = os.path.join(node, "row_tracker", "rows.txt")
        if os.path.exists(direct):
            return direct
        node = os.path.dirname(node)
    return None


# ---- remote source resolution -------------------------------------------

def classify_source(src):
    """Split a `source` value into (kind, target). Kind is one of:
    `ws` (live PM5 stats feed: ws(s) URL, or http(s) URL whose path ends
    in /ws -- the same OBS socket either way), `text-url` (http(s) URL
    serving rows.txt text), or `file` (local path)."""
    s = str(src or "").strip()
    if s.startswith(("ws://", "wss://")):
        return ("ws", s)
    if s.startswith(("http://", "https://")):
        try:
            path = urllib.parse.urlsplit(s).path or ""
        except Exception:
            path = ""
        if path.rstrip("/").endswith("/ws"):
            return ("ws", s)
        return ("text-url", s)
    return ("file", s)


def resolve_source(explicit=None):
    """Which row source to poll. Precedence: explicit `source` param,
    `$DISPLAYD_ROW_SOURCE`, else the mini1 PM5 feed. Never empty."""
    if explicit:
        return str(explicit)
    env = os.environ.get(ENV_SOURCE)
    if env:
        return env
    return DEFAULT_SOURCE


def parse_timeout(value):
    """Remote fetch seconds, clamped to [2, 60]; garbage means 10."""
    try:
        iv = int(value)
    except (TypeError, ValueError):
        return FETCH_TIMEOUT_DEFAULT
    return max(FETCH_TIMEOUT_MIN, min(FETCH_TIMEOUT_MAX, iv))


def resolve_journal(explicit=None, local_path=None):
    """Where remote sightings are journalled. Precedence: explicit
    `journal` param, `$DISPLAYD_ROW_JOURNAL`, a `row_sightings.json`
    next to the local log, else the system temp dir. No hardcoded home
    paths anywhere on this chain."""
    if explicit:
        return str(explicit)
    env = os.environ.get(ENV_JOURNAL)
    if env:
        return env
    if local_path:
        try:
            return os.path.join(
                os.path.dirname(os.path.abspath(local_path)),
                "row_sightings.json")
        except Exception:
            pass
    return os.path.join(tempfile.gettempdir(),
                        "displayd-row-sightings.json")


def source_label(kind, target, path):
    """One-line footer naming the active source chain for the panel."""
    if kind == "ws":
        base = "live %s" % target
    elif kind == "text-url":
        base = target
    else:
        base = target or ""
    if path and (kind != "file" or path != target):
        if base:
            return "%s + %s" % (base, path)
        return path
    return base or "no log configured"

def parse_log(text):
    """Fold raw rows.txt text into ({date: rows-that-day}, last timestamp
    string or None, total row count). Non-timestamp lines are ignored."""
    counts = {}
    last_ts = None
    total = 0
    for line in str(text or "").splitlines():
        s = line.strip()
        if len(s) < 10 or not s[0].isdigit():
            continue
        try:
            day = datetime.date.fromisoformat(s[:10])
        except ValueError:
            continue
        counts[day] = counts.get(day, 0) + 1
        total += 1
        if last_ts is None or s > last_ts:
            last_ts = s
    return counts, last_ts, total


def _step(state, count):
    """One calendar day through the rest-day bank rule. `state` is
    [day_streak, row_streak, bank]; mirrors row.sh `_streak_step`."""
    ds, rs, bank = state
    if count > 0:
        if ds == 0:
            state[0], state[1], state[2] = 1, count, count - 1
        else:
            state[0], state[1], state[2] = ds + 1, rs + count, bank + count - 1
    elif ds > 0 and bank > 0:
        # Covered rest day: spend one credit, streak holds. Day streak
        # grows (calendar days), row streak unchanged, bank drops by 1.
        state[0], state[2] = ds + 1, bank - 1
    else:
        state[0], state[1], state[2] = 0, 0, 0


def compute_streaks(counts, as_of):
    """Walk the per-day counts forward to `as_of` (a date, inclusive)
    and return (day_streak, row_streak, bank). Future days are ignored;
    streaks reset at the year boundary. Matches row.sh compute_streaks."""
    state = [0, 0, 0]
    past = sorted((d, c) for d, c in counts.items() if d <= as_of)
    year = None
    prev = None
    for day, count in past:
        if prev is not None:
            gap = prev + datetime.timedelta(days=1)
            while gap < day:
                if gap.year != year:
                    state[0] = state[1] = state[2] = 0
                    year = gap.year
                _step(state, 0)
                gap += datetime.timedelta(days=1)
        if day.year != year:
            state[0] = state[1] = state[2] = 0
            year = day.year
        _step(state, count)
        prev = day
    if prev is not None:
        gap = prev + datetime.timedelta(days=1)
        while gap <= as_of:
            if gap.year != year:
                state[0] = state[1] = state[2] = 0
                year = gap.year
            _step(state, 0)
            gap += datetime.timedelta(days=1)
    return tuple(state)


def summarize(counts, last_ts, total, as_of=None):
    """Shape parsed log data into what the wall needs."""
    today = as_of or datetime.date.today()
    ds, rs, bank = compute_streaks(counts, today)
    year = today.year
    rows_year = sum(c for d, c in counts.items() if d.year == year)
    day_of_year = today.timetuple().tm_yday
    days_in_year = 366 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 365
    last_day = None
    if last_ts:
        try:
            last_day = datetime.date.fromisoformat(last_ts[:10])
        except ValueError:
            last_day = None
    if not counts or (last_day is not None and last_day.year != year and rows_year == 0):
        status = "no rows yet" if not counts else "season not started"
    elif ds == 0:
        status = "streak broken"
    elif last_day == today:
        status = "rowed today"
    else:
        status = "rest — streak held" if bank >= 0 and counts.get(today, 0) == 0 else "rowed today"
        if counts.get(today, 0) > 0:
            status = "rowed today"
    return {
        "day_streak": ds,
        "row_streak": rs,
        "bank": bank,
        "last_ts": last_ts,
        "last_day": last_day.isoformat() if last_day else None,
        "rows_year": rows_year,
        "day_of_year": day_of_year,
        "days_in_year": days_in_year,
        "pace": rows_year - day_of_year,
        "today_count": counts.get(today, 0),
        "status": status,
    }


def read_snapshot(path):
    """Read + parse the log file. Raises OSError when unreadable."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    counts, last_ts, total = parse_log(text)
    return summarize(counts, last_ts, total)


# ---- remote fetching (stdlib only; never blocks the panel) ---------------

def fetch_http_text(url, timeout):
    """GET rows.txt text from an http(s) URL. Raises OSError/ValueError."""
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ValueError("row text url must be http(s)")
    req = urllib.request.Request(url, headers={"User-Agent": "displayd-row/1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if getattr(resp, "status", 200) != 200:
            raise OSError("http %s" % getattr(resp, "status", "?"))
        raw = resp.read(HTTP_MAX_BYTES + 1)
    if len(raw) > HTTP_MAX_BYTES:
        raise ValueError("row text too large")
    return raw.decode("utf-8", errors="replace")


_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_dial_url(url):
    """Map an http(s) .../ws URL onto its ws(s) twin (same OBS socket)."""
    parts = urllib.parse.urlsplit(url)
    scheme = {"http": "ws", "https": "wss"}.get(parts.scheme, parts.scheme)
    return urllib.parse.urlunsplit(
        (scheme, parts.netloc, parts.path, parts.query, parts.fragment))


def _recv_exact(sock, n, deadline, lookahead=None):
    buf = b""
    if lookahead:
        buf = bytes(lookahead[:n])
        del lookahead[:n]
    while len(buf) < n:
        if time.monotonic() > deadline:
            raise TimeoutError("row feed read timed out")
        try:
            chunk = sock.recv(n - len(buf))
        except socket.timeout:
            raise TimeoutError("row feed read timed out")
        if not chunk:
            raise OSError("row feed closed mid-frame")
        buf += chunk
    return buf


def _ws_pong(sock, payload):
    try:
        if len(payload) < 126:
            sock.sendall(b"\x8a" + bytes([len(payload)]) + payload)
        else:
            sock.sendall(b"\x8a\x7e" + len(payload).to_bytes(2, "big") + payload)
    except OSError:
        pass


def fetch_ws_stats(url, timeout):
    """Connect to the PM5 /obs/ws feed and return the first stats dict.
    Minimal stdlib-only WS client: server frames are unmasked, ping is
    answered, close ends the read. Returns None when the feed is reachable
    but quiet (handshake OK, no stats within the timeout: PM5 idle with
    nothing fresh to say) -- that is a live, healthy silence, not a
    failure. Raises OSError/ValueError/TimeoutError on connect/handshake
    failure, an explicit close, or oversize frames."""
    target = _ws_dial_url(url) if url.startswith(("http://", "https://")) else url
    parts = urllib.parse.urlsplit(target)
    if parts.scheme not in ("ws", "wss"):
        raise ValueError("row stats url must be ws(s) or http(s) .../ws")
    host = parts.hostname or ""
    if not host:
        raise ValueError("row stats url has no host")
    port = parts.port or (443 if parts.scheme == "wss" else 80)
    resource = parts.path or "/"
    if parts.query:
        resource += "?" + parts.query
    deadline = time.monotonic() + max(1, timeout)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        sock.settimeout(max(0.5, deadline - time.monotonic()))
        if parts.scheme == "wss":
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(sock, server_hostname=host)
            sock.settimeout(max(0.5, deadline - time.monotonic()))
        req = (
            "GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\nUser-Agent: displayd-row/1\r\n\r\n"
        ) % (resource, parts.netloc or host, key)
        sock.sendall(req.encode("latin-1"))
        head = b""
        status_end = head.find(b"\r\n\r\n")
        lookahead = bytearray()
        while status_end < 0:
            if time.monotonic() > deadline or len(head) > 16384:
                raise OSError("row feed handshake failed")
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                raise TimeoutError("row feed handshake timed out")
            if not chunk:
                raise OSError("row feed handshake failed")
            head += chunk
            status_end = head.find(b"\r\n\r\n")
        header_block = head[:status_end]
        # A fast server pipelines the greeting frame into the same TCP
        # segment as the handshake: keep those bytes for frame parsing.
        lookahead = bytearray(head[status_end + 4:])
        status = header_block.split(b"\r\n", 1)[0]
        if b"101" not in status:
            raise OSError("row feed handshake rejected: %s"
                          % status.decode("latin-1", "replace")[:80])
        accept = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
        ).decode("ascii")
        if accept not in header_block.decode("latin-1", "replace"):
            raise OSError("row feed handshake key mismatch")
        text_parts = []
        waiting_cont = False
        seen = 0
        total = 0
        while seen < WS_MAX_MESSAGES:
            if time.monotonic() > deadline:
                return None  # reachable but quiet: PM5 idle, nothing fresh
            sock.settimeout(max(0.5, deadline - time.monotonic()))
            try:
                hdr = _recv_exact(sock, 2, deadline, lookahead)
            except TimeoutError:
                return None  # reachable but quiet (see above)
            fin = bool(hdr[0] & 0x80)
            opcode = hdr[0] & 0x0F
            masked = bool(hdr[1] & 0x80)
            length = hdr[1] & 0x7F
            if length == 126:
                length = int.from_bytes(_recv_exact(sock, 2, deadline, lookahead), "big")
            elif length == 127:
                length = int.from_bytes(_recv_exact(sock, 8, deadline, lookahead), "big")
            if length > WS_MAX_BYTES:
                raise ValueError("row feed frame too large")
            mask = _recv_exact(sock, 4, deadline, lookahead) if masked else b""
            payload = _recv_exact(sock, length, deadline, lookahead) if length else b""
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:  # close: explicit server action, not quiet
                raise OSError("row feed closed")
            if opcode == 0x9:  # ping -> pong, keep listening
                _ws_pong(sock, payload)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode == 0x1:
                text_parts = [payload]
                waiting_cont = not fin
            elif opcode == 0x0 and text_parts:
                text_parts.append(payload)
                waiting_cont = not fin
            else:
                continue
            if waiting_cont:
                continue
            seen += 1
            total += sum(len(p) for p in text_parts)
            if total > WS_MAX_BYTES:
                raise ValueError("row feed message too large")
            try:
                msg = json.loads(
                    b"".join(text_parts).decode("utf-8", errors="replace"))
            except ValueError:
                text_parts = []
                continue
            text_parts = []
            if isinstance(msg, dict) and (
                    msg.get("type") == "stats" or
                    (msg.get("type") is None and isinstance(msg.get("raw"), dict))):
                return msg
        return None  # messages flowed, but none were stats: quiet
    finally:
        try:
            sock.close()
        except Exception:
            pass


# ---- sightings journal (remote days, without bank inflation) --------------

def stats_sighting(msg, last_sample=None):
    """Decide whether one stats message evidences a row today.
    Returns (sighted, sample). Requires a real workout underway
    (rower connected, distance past warm-up, elapsed ticking) and a
    sample newer than the persisted one, so a frozen feed cannot
    re-journal day after day."""
    if not isinstance(msg, dict):
        return False, None
    if msg.get("connected") is False:
        return False, None
    raw = msg.get("raw")
    if not isinstance(raw, dict):
        return False, None
    try:
        dist = float(raw.get("distance_m"))
        elapsed = float(raw.get("elapsed_time_s"))
    except (TypeError, ValueError):
        return False, None
    if not (dist >= MIN_ROW_DISTANCE_M and elapsed > 0):
        return False, None
    sample = {"distance_m": dist, "elapsed_time_s": elapsed}
    if isinstance(last_sample, dict):
        try:
            if (float(last_sample.get("distance_m")) == dist
                    and float(last_sample.get("elapsed_time_s")) == elapsed):
                return False, sample
        except (TypeError, ValueError):
            pass
    return True, sample


def load_journal(path):
    """(days: set of ISO dates, last_sample: dict|None). A missing or
    corrupt journal reads as empty -- never raises."""
    days = set()
    last = None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return days, last
    if isinstance(data, dict):
        raw_days = data.get("days")
        if isinstance(raw_days, list):
            for entry in raw_days:
                if isinstance(entry, str) and len(entry) == 10:
                    try:
                        datetime.date.fromisoformat(entry)
                        days.add(entry)
                    except ValueError:
                        continue
        if isinstance(data.get("last_sample"), dict):
            last = data["last_sample"]
    return days, last


def save_journal(path, days, last_sample):
    """Atomic journal write (tmp + replace). Raises OSError on failure."""
    tmp = "%s.tmp-%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"days": sorted(days), "last_sample": last_sample}, fh)
        fh.write("\n")
    os.replace(tmp, path)


def note_sighting(path, day_iso, sample):
    """Journal one sighting. Returns True when the day is newly added.
    Never raises: a broken journal must not break the panel."""
    try:
        days, _last = load_journal(path)
        fresh = day_iso not in days
        days.add(day_iso)
        save_journal(path, days, sample)
        return fresh
    except Exception:
        return False


def merge_journal(counts, last_ts, total, journal_days):
    """Fold journal ISO days into parsed-log data. Journal days count one
    row each and only when the log has no entry that day, so the bank is
    never inflated; the newest journal day refreshes last_ts when newer."""
    counts = dict(counts or {})
    for iso in sorted(journal_days or ()):
        try:
            day = datetime.date.fromisoformat(iso)
        except ValueError:
            continue
        if day not in counts:
            counts[day] = 1
            total += 1
            stamp = iso + "T12:00:00"
            if last_ts is None or stamp > last_ts:
                last_ts = stamp
    return counts, last_ts, total


def poll_source(kind, target, timeout, local_path, journal_path, today=None):
    """One poll across remote source + local fallback + journal. Returns
    (counts, last_ts, total, remote_ok, remote_error, sighted). Never
    raises: every failure surfaces as remote_error with whatever the
    fallback and journal still provide."""
    remote_ok = True
    remote_error = None
    sighted = False
    remote_counts, remote_last, remote_total = {}, None, 0
    day = today or datetime.date.today()
    try:
        if kind == "file":
            if not target:
                raise OSError("no log configured")
            with open(target, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            remote_counts, remote_last, remote_total = parse_log(text)
        elif kind == "text-url":
            text = fetch_http_text(target, timeout)
            remote_counts, remote_last, remote_total = parse_log(text)
        else:  # live stats feed: sightings journal the day
            msg = fetch_ws_stats(target, timeout)
            # msg None = reachable but quiet (PM5 idle): healthy, no sighting.
            _days, last = load_journal(journal_path)
            ok, sample = stats_sighting(msg, last)
            if sample is not None:
                try:
                    days, _last = load_journal(journal_path)
                    days.add(day.isoformat())
                    save_journal(journal_path, days, sample)
                except Exception:
                    pass
            sighted = ok
    except Exception as err:
        remote_ok = False
        remote_error = str(err)[:160]
    counts, last_ts, total = dict(remote_counts), remote_last, remote_total
    if local_path and (kind != "file" or local_path != target):
        try:
            with open(local_path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            lcounts, llast, _ltotal = parse_log(text)
            for d, c in lcounts.items():
                if d not in counts:
                    counts[d] = c
                    total += c
        except Exception:
            pass
        else:
            if llast is not None and (last_ts is None or llast > last_ts):
                last_ts = llast
    try:
        jdays, _last = load_journal(journal_path)
    except Exception:
        jdays = set()
    counts, last_ts, total = merge_journal(counts, last_ts, total, jdays)
    return counts, last_ts, total, remote_ok, remote_error, sighted


# ---- drawing --------------------------------------------------------------

def _age(updated):
    if not updated:
        return "no data yet"
    secs = max(0, time.time() - updated)
    if secs < 60:
        return "updated %ds ago" % int(secs)
    if secs < 3600:
        return "updated %dm ago" % int(secs // 60)
    return "updated %dh ago" % int(secs // 3600)


def _draw_message(screen, title, bg, big, sub, foot, color):
    img = screen.new_image(bg)
    draw = ImageDraw.Draw(img)
    head_font = _font_or_default(screen, "DejaVuSans-Bold", HEADER_SIZE)
    label_font = _font_or_default(screen, "DejaVuSans-Bold", LABEL_SIZE)
    row_font = _font_or_default(screen, "DejaVuSans", ROW_SIZE)
    sub_font = _font_or_default(screen, "DejaVuSans", SUB_SIZE)
    small_font = _font_or_default(screen, "DejaVuSans", FOOT_SIZE)
    draw.text((PAD, 24), _fit(draw, title, head_font, screen.W - 2 * PAD),
              font=head_font, fill=C_TEXT)
    draw.line([(PAD, 128), (screen.W - PAD, 128)], fill=C_LINE, width=2)
    draw.text((PAD, 220), _fit(draw, big, label_font, screen.W - 2 * PAD),
              font=label_font, fill=color)
    if sub:
        draw.text((PAD, 300), _fit(draw, sub, row_font, screen.W - 2 * PAD),
                  font=row_font, fill=C_DIM)
    if foot:
        draw.text((PAD, screen.H - 56),
                  _fit(draw, foot, small_font, screen.W - 2 * PAD),
                  font=small_font, fill=C_DIM)
    screen.present(img)


def _draw(screen, title, bg, snap, label, health, error, updated):
    img = screen.new_image(bg)
    draw = ImageDraw.Draw(img)
    head_font = _font_or_default(screen, "DejaVuSans-Bold", HEADER_SIZE)
    big_font = _font_or_default(screen, "DejaVuSans-Bold", BIG_SIZE)
    label_font = _font_or_default(screen, "DejaVuSans-Bold", LABEL_SIZE)
    row_font = _font_or_default(screen, "DejaVuSans", ROW_SIZE)
    sub_font = _font_or_default(screen, "DejaVuSans", SUB_SIZE)
    small_font = _font_or_default(screen, "DejaVuSans", FOOT_SIZE)

    dot = {"cold": (120, 120, 130), "warm": C_UP,
           "stale": C_WARN, "error": C_FAILED}[health]
    status = "%s · %s" % (health, _age(updated))
    try:
        w = draw.textlength(status, font=small_font)
    except Exception:
        w = 0
    draw.text((PAD, 24), _fit(draw, title, head_font, screen.W - 2 * PAD - w - 80),
              font=head_font, fill=C_TEXT)
    draw.ellipse([screen.W - PAD - 22, 52, screen.W - PAD - 2, 72], fill=dot)
    draw.text((screen.W - PAD - w - 36, 34), status, font=small_font, fill=C_DIM)
    draw.line([(PAD, 128), (screen.W - PAD, 128)], fill=C_LINE, width=2)

    col_w = screen.W - 2 * PAD
    ds, rs, bank = snap["day_streak"], snap["row_streak"], snap["bank"]

    # Hero: the day streak, with a fire marker while alive.
    hero = "%d" % ds
    try:
        while len(hero) > 1 and draw.textlength(hero, font=big_font) > col_w * 0.55:
            hero_size = big_font.size - 20
            big_font = _font_or_default(screen, "DejaVuSans-Bold", max(60, hero_size))
            if big_font.size <= 60:
                break
    except Exception:
        pass
    hero_color = C_FIRE if ds > 0 else C_DIM
    draw.text((PAD, 150), hero, font=big_font, fill=hero_color)
    try:
        hw = draw.textlength(hero, font=big_font)
    except Exception:
        hw = 0
    draw.text((PAD + hw + 40, 200),
              _fit(draw, "DAY STREAK" if ds != 1 else "DAY STREAK",
                   label_font, col_w - hw - 40),
              font=label_font, fill=C_TEXT)
    draw.text((PAD + hw + 40, 280),
              _fit(draw, "%d rows · bank %d" % (rs, bank), row_font, col_w - hw - 40),
              font=row_font, fill=C_DIM)
    y = 560
    draw.line([(PAD, y), (screen.W - PAD, y)], fill=C_LINE, width=2)
    y += 26

    # Last row + year pace: the glanceable second line.
    last = snap["last_day"] or "—"
    draw.text((PAD, y), _fit(draw, "last row  %s" % last, row_font, col_w),
              font=row_font, fill=C_TEXT)
    y += 70
    pace = snap["pace"]
    if pace > 0:
        pace_text = "year %d/%d · %d ahead of pace" % (
            snap["rows_year"], snap["days_in_year"], pace)
        pace_color = C_UP
    elif pace < 0:
        pace_text = "year %d/%d · %d behind pace" % (
            snap["rows_year"], snap["days_in_year"], -pace)
        pace_color = C_WARN
    else:
        pace_text = "year %d/%d · on pace" % (snap["rows_year"], snap["days_in_year"])
        pace_color = C_TEXT
    draw.text((PAD, y), _fit(draw, pace_text, row_font, col_w),
              font=row_font, fill=pace_color)
    y += 70
    draw.text((PAD, y), _fit(draw, snap["status"], sub_font, col_w),
              font=sub_font, fill=C_DIM)

    foot = label or "no log configured"
    if health == "stale":
        foot += "   [STALE \u2014 last-known streak]"
    if health in ("stale", "error") and error:
        foot += "   [read failed: %s]" % error
    draw.text((PAD, screen.H - 56), _fit(draw, foot, small_font, col_w),
              font=small_font, fill=C_DIM)
    screen.present(img)


def _snapshot_key(snap, health):
    if snap is None:
        return ("cold", health)
    return (snap["day_streak"], snap["row_streak"], snap["bank"],
            snap["last_ts"], snap["rows_year"], health)


def run(screen, params, stop):
    params = params or {}
    title = str(params.get("title") or "ROWING").upper()
    bg = screen.color(params.get("background"), C_BG)
    try:
        interval = int(params.get("interval") or POLL_DEFAULT_INTERVAL)
    except (TypeError, ValueError):
        interval = POLL_DEFAULT_INTERVAL
    interval = max(5, min(3600, interval))
    timeout = parse_timeout(params.get("timeout"))
    source = resolve_source(params.get("source"))
    kind, target = classify_source(source)
    path = resolve_path(params.get("path"))
    journal_path = resolve_journal(params.get("journal"), path)
    label = source_label(kind, target, path)

    if kind == "file" and not target and path is None:
        _draw_message(screen, title, bg, "NO LOG CONFIGURED",
                      "set source param or $%s" % ENV_SOURCE, None, C_WARN)
        while not stop.is_set():
            stop.wait(interval)
            now_source = resolve_source(params.get("source"))
            now_kind, now_target = classify_source(now_source)
            now_path = resolve_path(params.get("path"))
            if (now_kind != "file" or now_target) or now_path is not None:
                source, kind, target, path = now_source, now_kind, now_target, now_path
                journal_path = resolve_journal(params.get("journal"), path)
                label = source_label(kind, target, path)
                break
        else:
            return

    def _refresh():
        """One poll shaped into (snap|None, health, error)."""
        counts, last_ts, total, ok, err, _sighted = poll_source(
            kind, target, timeout, path, journal_path)
        if counts or last_ts is not None:
            return summarize(counts, last_ts, total), \
                ("warm" if ok else "stale"), err
        if ok:
            # Reachable source, genuinely no rows anywhere yet.
            return summarize(counts, last_ts, total), "warm", err
        return None, "error", err or "no row data"

    snap = None
    health = "cold"
    error = None
    updated = 0.0
    try:
        snap, health, error = _refresh()
        if snap is not None:
            updated = time.time()
        else:
            raise OSError(error or "no row data")
    except Exception as err:
        error = str(err)[:160]
        health = "error"
        _draw_message(screen, title, bg, "LOG NOT READABLE",
                      label, ("last error: %s" % error) if error else None,
                      C_FAILED)

    if snap is not None:
        _draw(screen, title, bg, snap, label, health, error, updated)
    last_key = _snapshot_key(snap, health)
    last_draw = time.time()
    while not stop.is_set():
        stop.wait(interval)
        if stop.is_set():
            break
        try:
            fresh, fresh_health, fresh_error = _refresh()
            if fresh is not None:
                snap = fresh
                updated = time.time()
                error = fresh_error
                health = fresh_health
            else:
                error = fresh_error
                health = "stale" if snap is not None else "error"
        except Exception as err:
            error = str(err)[:160]
            health = "stale" if snap is not None else "error"
        key = _snapshot_key(snap, health)
        now = time.time()
        if key != last_key or (now - last_draw) >= DRAW_REFRESH:
            last_key = key
            last_draw = now
            try:
                if snap is None:
                    _draw_message(screen, title, bg, "LOG NOT READABLE",
                                  label, ("last error: %s" % error) if error else None,
                                  C_FAILED)
                else:
                    _draw(screen, title, bg, snap, label, health, error, updated)
            except Exception:
                # A draw failure must not kill the daemon loop; the last
                # good frame stays on the panel.
                pass
