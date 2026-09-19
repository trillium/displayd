"""Beads parade for the jumbotron: what is moving, stuck, and needs the captain.

Mardi Gras model, copied as a model not code -- four buckets, not a status
column:

    Rolling         in progress, nothing in the way
    Lined Up        open, nothing in the way
    Stalled         waiting on something not done (derived from dependency
                    edges, never read from a status field)
    Past the Stand  closed, folded away

Polled-state archetype: this renderer polls on its own interval in a
background thread and draws from cache. A poll never blocks a draw, a missing
or malformed mirror never kills the frame, and a cold start (first poll not
back yet) renders a sensible waiting frame -- never blank, never broken.

Data: file mirrors first (a deployed snapshot the daemon host cannot build
itself), live `<store> export` as a fallback where the CLIs exist. Both paths
run in the poll thread with timeouts, off the draw path.
"""

import json
import os
import shutil
import subprocess
import threading
import time

from PIL import ImageDraw, ImageFont

NAME = "beads"
DESCRIPTION = "Beads parade: rolling / lined up / stalled / past the stand, plus what needs the captain"
STATIC = False
PARAMS = {
    "title": {"type": "string", "help": "header text, default BEADS"},
    "mirror": {"type": "string", "help": "mirror file(s), comma-separated JSON/JSONL; auto-discovered when omitted"},
    "stores": {"type": "string", "help": "live fallback stores, comma-separated CLI names; default task,brain,robots,review,ideas; empty disables"},
    "interval": {"type": "integer", "help": "poll seconds, default 60"},
    "background": {"type": "string", "help": "background colour, default near-black"},
}

# Dependency edge types where "A --type--> B" means A waits on B.
WAITS_ON = {"blocks", "blocked-by", "depends-on"}
# Labels that flag an open bead as needing the captain directly.
CAPTAIN_LABELS = {"human", "captain-hold", "captain", "gate"}

POLL_FALLBACK_INTERVAL = 60
DRAW_REFRESH = 15  # re-render at least this often so the age line stays honest
CLI_TIMEOUT = 25
ATTENTION_ROWS = 6

C_BG = (8, 8, 12)
C_ROLLING = (80, 220, 120)
C_LINEDUP = (110, 180, 255)
C_STALLED = (255, 180, 60)
C_PAST = (130, 130, 140)
C_TEXT = (235, 235, 240)
C_DIM = (140, 140, 150)
C_LINE = (60, 60, 70)

BUCKETS = (
    ("rolling", "\u25cf", "Rolling", C_ROLLING, "in progress"),
    ("linedup", "\u266a", "Lined Up", C_LINEDUP, "open, nothing in the way"),
    ("stalled", "\u2298", "Stalled", C_STALLED, "waiting on something"),
    ("past", "\u2713", "Past the Stand", C_PAST, "closed"),
)

# Resident process-wide poll state: survives view switches, so re-selecting
# this view is instantly populated, never empty.
_POLL = {
    "lock": threading.Lock(),
    "thread": None,
    "cfg": {},
    "snapshot": None,   # dict with buckets, attention, stores, total, closed
    "updated": 0.0,
    "health": "cold",   # cold | warm | stale | error
    "error": None,
    "source": None,
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


def _fit(draw, text, font, max_w, max_chars=90):
    text = str(text or "")
    if font is not None:
        try:
            while len(text) > 4 and draw.textlength(text, font=font) > max_w:
                text = text[:-2]
            if draw.textlength(text, font=font) > max_w:
                text = text[:4]
            return text
        except Exception:
            pass
    return text[:max_chars] if len(text) > max_chars else text


# ---- loading -----------------------------------------------------------

def _as_labels(raw):
    out = set()
    for item in raw or []:
        name = item.get("name") if isinstance(item, dict) else item
        if name:
            out.add(str(name).strip().lower())
    return out


def _norm_issue(store, raw):
    """Trim a store issue dict down to what classification needs."""
    if not isinstance(raw, dict) or not raw.get("id"):
        return None
    deps = []
    for dep in raw.get("dependencies") or []:
        if not isinstance(dep, dict):
            continue
        target = dep.get("depends_on_id") or dep.get("target") or dep.get("id")
        dtype = str(dep.get("type") or "").strip().lower()
        if target and dtype:
            deps.append({"type": dtype, "target": str(target)})
    try:
        prio = int(raw.get("priority") or 0)
    except (TypeError, ValueError):
        prio = 0
    return {
        "store": store,
        "id": str(raw.get("id")),
        "title": str(raw.get("title") or "(untitled)"),
        "status": str(raw.get("status") or "open").strip().lower(),
        "priority": prio,
        "labels": _as_labels(raw.get("labels")),
        "deps": deps,
    }


def _load_mirror_file(path):
    """Read one mirror file: JSON array, JSONL, or {stores:{name:[...]}}."""
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    try:
        data = json.loads(text)
    except ValueError:
        data = None
        issues = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            issues.append(json.loads(line))  # raises on malformed -> caller
        store = os.path.splitext(os.path.basename(path))[0]
        return [(store, issue) for issue in issues]
    if isinstance(data, dict) and isinstance(data.get("stores"), dict):
        out = []
        for store, issues in data["stores"].items():
            for issue in issues or []:
                out.append((str(store), issue))
        return out
    if isinstance(data, dict) and isinstance(data.get("issues"), list):
        return [("mirror", issue) for issue in data["issues"]]
    if isinstance(data, list):
        store = os.path.splitext(os.path.basename(path))[0]
        return [(store, issue) for issue in data]
    raise ValueError("unrecognised mirror shape in %s" % path)


def _candidate_mirrors(explicit):
    cands = []
    if explicit:
        cands.extend(p.strip() for p in str(explicit).split(",") if p.strip())
    env = os.environ.get("DISPLAYD_BEADS_MIRROR")
    if env:
        cands.extend(p.strip() for p in env.split(",") if p.strip())
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        root = None
    names = (".beads-mirror-fleet.json", ".beads-mirror-ready.json",
             ".beads-mirror-inflight.json", "beads-mirror.json")
    search = []
    if root:
        search.append(os.path.join(root, "state"))
        search.append(root)
    search.extend((
        "/home/trillium/displayd/state",
        "/opt/displayd/state",
        "/var/lib/displayd",
    ))
    for directory in search:
        for name in names:
            cands.append(os.path.join(directory, name))
    seen, out = set(), []
    for path in cands:
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out


def _export_store(cli):
    exe = shutil.which(cli)
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [exe, "export"], capture_output=True, text=True, timeout=CLI_TIMEOUT)
    except Exception:
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    issues = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            issues.append(json.loads(line))
        except ValueError:
            continue
    return [(cli, issue) for issue in issues]


def _poll_once(cfg):
    """One poll: mirrors first, live CLI export as fallback. Returns
    (pairs, source) or raises."""
    errors = []
    for path in _candidate_mirrors(cfg.get("mirror")):
        if not os.path.isfile(path):
            continue
        try:
            pairs = _load_mirror_file(path)
            if pairs:
                return pairs, path
            errors.append("%s: empty" % path)
        except Exception as err:
            errors.append("%s: %s" % (path, err))
    if errors:
        raise ValueError("; ".join(errors))
    stores = [s.strip() for s in str(cfg.get("stores") or "").split(",") if s.strip()]
    collected = []
    for store in stores:
        try:
            pairs = _export_store(store)
        except Exception:
            pairs = None
        if pairs:
            collected.extend(pairs)
    if collected:
        return collected, "live:" + ",".join(
            sorted({store for store, _ in collected}))
    raise ValueError("no mirror file and no live store answered")


# ---- classification (the Mardi Gras model) ------------------------------

def _classify(pairs):
    by_id, bare = {}, {}
    for store, raw in pairs:
        norm = _norm_issue(store, raw)
        if norm is not None:
            by_id[(norm["store"], norm["id"])] = norm
            bare.setdefault(norm["id"], norm)
    snap = {"rolling": [], "linedup": [], "stalled": [], "past": [],
            "attention": [], "stores": {}, "total": 0, "closed": 0}
    for issue in by_id.values():  # one entry per (store, id)
        store_stat = snap["stores"].setdefault(
            issue["store"], {"open": 0, "total": 0})
        store_stat["total"] += 1
        snap["total"] += 1
        status = issue["status"]
        if status == "closed":
            snap["past"].append(issue)
            snap["closed"] += 1
            continue
        # Blocked is DERIVED from dependency edges: an unfinished bead this
        # one waits on. Never read from a status field.
        waiters = []
        for dep in issue["deps"]:
            if dep["type"] not in WAITS_ON:
                continue
            target = (by_id.get((issue["store"], dep["target"]))
                        or bare.get(dep["target"]))
            if target is not None and target["status"] != "closed":
                waiters.append(target)
        if waiters:
            issue["waiters"] = waiters
            snap["stalled"].append(issue)
            store_stat["open"] += 1
        elif status == "in_progress":
            snap["rolling"].append(issue)
            store_stat["open"] += 1
        else:  # open, deferred, anything else unfinished: nothing in the way
            snap["linedup"].append(issue)
            store_stat["open"] += 1
    snap["attention"] = _attention(snap)
    return snap


def _attention(snap):
    """What needs the captain: review queue, stalled work, open decisions."""
    picked, seen = [], set()

    def take(issue, reason):
        if issue["id"] not in seen:
            seen.add(issue["id"])
            picked.append({"issue": issue, "reason": reason})

    review = [i for i in snap["linedup"] + snap["stalled"]
              if i["store"] == "review"]
    review.sort(key=lambda i: (i["priority"], i["id"]))
    for issue in review[:3]:
        take(issue, "review queue")
    stalled = sorted(snap["stalled"], key=lambda i: (i["priority"], i["id"]))
    for issue in stalled:
        if len(picked) >= ATTENTION_ROWS:
            break
        first = (issue.get("waiters") or [{}])[0]
        take(issue, "waits on %s" % (first.get("title") or first.get("id") or "?"))
    gates = [i for i in snap["linedup"] + snap["rolling"]
             if i["labels"] & CAPTAIN_LABELS
             or i["title"].lower().startswith(("gate:", "decide:", "decision:"))]
    gates.sort(key=lambda i: (i["priority"], i["id"]))
    for issue in gates:
        if len(picked) >= ATTENTION_ROWS:
            break
        take(issue, "needs a captain call")
    return picked[:ATTENTION_ROWS]


def _poll_loop():
    while True:
        with _POLL["lock"]:
            cfg = dict(_POLL["cfg"])
            interval = cfg.get("interval") or POLL_FALLBACK_INTERVAL
        try:
            pairs, source = _poll_once(cfg)
            snap = _classify(pairs)
            with _POLL["lock"]:
                _POLL["snapshot"] = snap
                _POLL["updated"] = time.time()
                _POLL["health"] = "warm"
                _POLL["error"] = None
                _POLL["source"] = source
        except Exception as err:
            with _POLL["lock"]:
                _POLL["error"] = str(err)[:160]
                # A failed poll never discards the last good frame's data:
                # keep the snapshot, mark it stale (or error when cold).
                if _POLL["snapshot"] is None:
                    _POLL["health"] = "error"
                else:
                    _POLL["health"] = "stale"
        time.sleep(max(5, interval))


def _ensure_poll(cfg):
    with _POLL["lock"]:
        _POLL["cfg"] = cfg
        alive = _POLL["thread"] is not None and _POLL["thread"].is_alive()
        if not alive:
            thread = threading.Thread(target=_poll_loop, daemon=True)
            _POLL["thread"] = thread
            thread.start()


# ---- drawing -------------------------------------------------------------

PAD = 60
HEADER_SIZE = 72
COUNT_SIZE = 150
BUCKET_LABEL = 44
BUCKET_SUB = 32
ROW_SIZE = 40
FOOT_SIZE = 30


def _age(updated):
    if not updated:
        return "no data yet"
    secs = max(0, time.time() - updated)
    if secs < 60:
        return "updated %ds ago" % int(secs)
    if secs < 3600:
        return "updated %dm ago" % int(secs // 60)
    return "updated %dh ago" % int(secs // 3600)


def _draw(screen, title, bg):
    with _POLL["lock"]:
        snap = _POLL["snapshot"]
        updated = _POLL["updated"]
        health = _POLL["health"]
        error = _POLL["error"]
        source = _POLL["source"]
    img = screen.new_image(bg)
    draw = ImageDraw.Draw(img)
    head_font = _font(screen, "DejaVuSans-Bold", HEADER_SIZE)
    count_font = _font(screen, "DejaVuSans-Bold", COUNT_SIZE)
    lab_font = _font(screen, "DejaVuSans-Bold", BUCKET_LABEL)
    sub_font = _font(screen, "DejaVuSans", BUCKET_SUB)
    row_font = _font(screen, "DejaVuSans", ROW_SIZE)
    small_font = _font(screen, "DejaVuSans", FOOT_SIZE)
    plain = head_font or row_font

    # Header.
    draw.text((PAD, 24), title, font=plain, fill=C_TEXT)
    health_dot = {"cold": (120, 120, 130), "warm": C_ROLLING,
                  "stale": C_STALLED, "error": (255, 90, 90)}[health]
    status = "%s \u00b7 %s" % (health, _age(updated))
    w = 0
    if small_font is not None:
        try:
            w = draw.textlength(status, font=small_font)
        except Exception:
            pass
    draw.ellipse([screen.W - PAD - 22, 52, screen.W - PAD - 2, 72],
                 fill=health_dot)
    draw.text((screen.W - PAD - w - 36, 34), status,
              font=small_font or plain, fill=C_DIM)
    draw.line([(PAD, 128), (screen.W - PAD, 128)], fill=C_LINE, width=2)

    if snap is None:
        # Cold start: a sensible waiting frame, never blank.
        msg = "waiting for first poll \u2014 mirrors or store export"
        draw.text((PAD, 220), _fit(draw, msg, row_font, screen.W - 2 * PAD),
                  font=row_font or plain, fill=C_DIM)
        if error:
            draw.text((PAD, 300),
                      _fit(draw, "last error: " + error, small_font,
                           screen.W - 2 * PAD),
                      font=small_font or plain, fill=(255, 90, 90))
        screen.present(img)
        return

    # Four buckets, one glanceable strip. Big counts shrink to fit their
    # column so a four-digit tally never collides with its neighbour.
    counts = {key: len(snap[key]) for key, _, _, _, _ in BUCKETS}
    col_w = (screen.W - 2 * PAD) / 4.0
    for idx, (key, glyph, label, color, sub) in enumerate(BUCKETS):
        x = PAD + idx * col_w
        text = "%s %d" % (glyph, counts[key])
        font = count_font
        if font is not None:
            try:
                size = COUNT_SIZE
                while (size > 48 and draw.textlength(text, font=font)
                       > col_w - 12):
                    size -= 8
                    font = _font(screen, "DejaVuSans-Bold", size)
                    if font is None:
                        break
            except Exception:
                font = count_font
        draw.text((x, 150), text, font=font or plain, fill=color)
        draw.text((x + 6, 310), label, font=lab_font or plain, fill=C_TEXT)
        draw.text((x + 6, 362), sub, font=sub_font or plain, fill=C_DIM)

    # Running tally + progress.
    total = snap["total"] or 1
    frac = snap["closed"] / total
    bar_y, bar_h = 420, 26
    draw.rectangle([PAD, bar_y, screen.W - PAD, bar_y + bar_h],
                   outline=C_LINE, width=2)
    fill_w = (screen.W - 2 * PAD - 8) * frac
    if fill_w > 2:
        draw.rectangle([PAD + 4, bar_y + 4, PAD + 4 + fill_w,
                        bar_y + bar_h - 4], fill=C_ROLLING)
    tally = "%d closed of %d \u00b7 %d%% past the stand" % (
        snap["closed"], snap["total"], int(frac * 100))
    draw.text((PAD, bar_y + 38), tally, font=sub_font or plain, fill=C_DIM)

    # What needs the captain.
    draw.text((PAD, 500), "NEEDS THE CAPTAIN", font=lab_font or plain,
              fill=C_STALLED)
    y = 562
    for item in snap["attention"]:
        issue, reason = item["issue"], item["reason"]
        # The reason is the actionable part: never truncate it, fit the
        # title into whatever width is left.
        tag = "[%s %s] " % (issue["store"], issue["id"])
        width = screen.W - 2 * PAD - 24  # slack for rasterizer rounding
        # Cap the reason first (~40% of the row), then fit the title
        # into the remainder: the full line always fits the panel.
        reason = _fit(draw, reason, row_font, width * 0.40, max_chars=56)
        tail = " \u2014 " + reason
        title_w = width
        if row_font is not None:
            try:
                fixed = (draw.textlength(tag, font=row_font)
                         + draw.textlength(tail, font=row_font))
                title_w = max(120, width - fixed)
            except Exception:
                pass
        title = _fit(draw, issue["title"], row_font, title_w)
        draw.text((PAD, y), tag + title + tail,
                  font=row_font or plain, fill=C_TEXT)
        y += 72
    if not snap["attention"]:
        draw.text((PAD, y), "nothing waiting on the captain",
                  font=row_font or plain, fill=C_DIM)

    # Footer: per-store open counts + source.
    parts = []
    for store in sorted(snap["stores"]):
        stat = snap["stores"][store]
        parts.append("%s %d/%d" % (store, stat["open"], stat["total"]))
    foot = "  \u00b7  ".join(parts)
    if source:
        foot += "   (%s)" % source
    if health in ("stale", "error") and error:
        foot += "   [last poll failed: %s]" % error
    draw.text((PAD, screen.H - 56),
              _fit(draw, foot, small_font, screen.W - 2 * PAD),
              font=small_font or plain, fill=C_DIM)
    screen.present(img)


def _snapshot_key():
    with _POLL["lock"]:
        snap = _POLL["snapshot"]
        updated = _POLL["updated"]
        health = _POLL["health"]
    if snap is None:
        return ("cold", health)
    return (updated, len(snap["rolling"]), len(snap["linedup"]),
            len(snap["stalled"]), len(snap["past"]), health)


def run(screen, params, stop):
    params = params or {}
    title = str(params.get("title") or "BEADS").upper()
    bg = screen.color(params.get("background"), C_BG)
    try:
        interval = int(params.get("interval") or POLL_FALLBACK_INTERVAL)
    except (TypeError, ValueError):
        interval = POLL_FALLBACK_INTERVAL
    interval = max(5, min(3600, interval))
    stores = params.get("stores")
    if stores is None:
        stores = "task,brain,robots,review,ideas"
    _ensure_poll({"mirror": params.get("mirror") or "",
                  "stores": stores, "interval": interval})

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
