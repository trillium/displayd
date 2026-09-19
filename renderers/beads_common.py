"""Shared model for the beads views. NOT a renderer: no run(), so the daemon's
loader skips this file. Both beads (parade overview) and beads-detail (single
bead card) import it, which keeps bucket derivation in exactly one place.

Covers loading (file mirrors, live store export, single-bead show), the Mardi
Gras bucket classification with blocked derived from dependency edges, and
the shared poll cache. All I/O happens in the poll thread, off the draw path.
"""

import json
import os
import shutil
import subprocess
import textwrap
import threading
import time

from PIL import ImageDraw, ImageFont

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
C_ERR = (255, 90, 90)

BUCKETS = (
    ("rolling", "\u25cf", "Rolling", C_ROLLING, "in progress"),
    ("linedup", "\u266a", "Lined Up", C_LINEDUP, "open, nothing in the way"),
    ("stalled", "\u2298", "Stalled", C_STALLED, "waiting on something"),
    ("past", "\u2713", "Past the Stand", C_PAST, "closed"),
)
BUCKET_BY_KEY = {key: (glyph, label, color) for key, glyph, label, color, _ in BUCKETS}

PAD = 60

# Resident process-wide poll state: survives view switches, so re-selecting
# any beads view is instantly populated, never empty.
_POLL = {
    "lock": threading.Lock(),
    "thread": None,
    "wake": threading.Event(),
    "cfg": {},
    "snapshot": None,   # dict with buckets, attention, stores, index, bare
    "updated": 0.0,
    "health": "cold",   # cold | warm | stale | error
    "error": None,
    "source": None,
    "focus_cache": {},  # bead_id -> normalized issue (CLI-fetched extras)
    "focus_fetching": set(),
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
    """Shrink text to fit max_w pixels wide (and max_chars characters).

    Guarantee: with a working font the return value is never wider than
    max_w. Text that fits is returned unchanged; longer text is shaved
    until it fits. When even a single glyph overflows max_w, the result
    is an ellipsis if it fits, else an empty string -- a cut is always
    signalled without overflowing. Callers (e.g. the beads attention
    rows, which fit the title into the width left over by the reason)
    rely on this: a fitted string always honours the budget it was
    given, so measuring a fitted string gives a truthful remainder.

    No-font fallback is deliberate: with font None there are no pixel
    metrics, so _fit keeps the character budget only (text[:max_chars])
    and ignores max_w. A missing font must truncate, never blank a row.
    """
    text = str(text or "")
    if not text:
        return ""
    if len(text) > max_chars:
        text = text[:max_chars]
    if font is None:
        return text  # intentional: no metrics, character budget only
    try:
        if max_w <= 0:
            return ""
        while draw.textlength(text, font=font) > max_w and len(text) > 1:
            text = text[:-2] if len(text) > 8 else text[:-1]
        if draw.textlength(text, font=font) <= max_w:
            return text
        # Even one glyph overflows: ellipsis if it fits, else empty.
        try:
            return ("\u2026" if draw.textlength("\u2026", font=font)
                    <= max_w else "")
        except Exception:
            return ""
    except Exception:
        return text[:max_chars] if len(text) > max_chars else text


def _wrap(draw, text, font, max_w, max_rows=3):
    """Greedy word-wrap to pixel width; returns at most max_rows rows.

    When words are dropped to respect max_rows, the final row keeps a
    trailing ellipsis marker: room for the marker is reserved before
    fitting, so the fit can never shave the marker itself back off.
    Rows that fit without dropping anything carry no marker. A single
    word wider than max_w stays whole on its own row (it cannot wrap),
    so _wrap always terminates with a non-empty result.

    Guarantee: with a working font no returned row is wider than max_w.
    Whole words that already fit pass through untouched; only a row that
    still overflows (a single word wider than the budget) is rescued via
    _fit, which may cut it mid-word rather than let it run off-panel."""
    words = str(text or "").split()
    if not words:
        return [""]
    rows, cur = [], ""
    truncated = False
    for word in words:
        trial = (cur + " " + word).strip()
        try:
            too_wide = font is not None and draw.textlength(trial, font=font) > max_w
        except Exception:
            too_wide = len(trial) > 90
        if too_wide and cur:
            rows.append(cur)
            cur = word
            if len(rows) >= max_rows:
                truncated = True
                break
        else:
            cur = trial
    else:
        rows.append(cur)
    rows = rows[:max_rows]
    if font is not None:
        # Rescue-fit only: _fit returns fitting text unchanged, so whole
        # words pass through byte-identical and only an overflowing row
        # (a single word wider than the budget) gets cut down to size.
        # max_chars=len(row) keeps the character budget out of the way:
        # this pass is purely about pixel width.
        for i in range(len(rows)):
            if truncated and i == len(rows) - 1:
                continue  # marker fit below owns the final row
            rows[i] = _fit(draw, rows[i], font, max_w, len(rows[i]))
    if truncated and rows:
        rows[-1] = _fit_with_marker(draw, rows[-1], font, max_w)
    return rows or [""]


def _fit_with_marker(draw, text, font, max_w, marker=" \u2026"):
    """Fit text reserving room for a trailing marker, then append it.

    Used for the final wrapped row: unlike _fit(text + marker), which
    would shave the marker itself back off a full row, this guarantees
    the marker survives whenever it fits at all."""
    try:
        marker_w = (draw.textlength(marker, font=font)
                    if font is not None else len(marker))
    except Exception:
        marker_w = len(marker)
    if font is None:
        # No metrics: character budget only, marker always survives.
        return (text + marker)[:len(text) + len(marker)]
    if marker_w > max_w:
        return _fit(draw, "\u2026", font, max_w)
    budget = len(text) + len(marker)
    return _fit(draw, text, font, max_w - marker_w, budget) + marker


# ---- loading -----------------------------------------------------------

def _as_labels(raw):
    out = set()
    for item in raw or []:
        name = item.get("name") if isinstance(item, dict) else item
        if name:
            out.add(str(name).strip().lower())
    return out


def _trim_comment(raw):
    if not isinstance(raw, dict):
        return None
    text = str(raw.get("text") or "").strip()
    if not text:
        return None
    return {
        "author": str(raw.get("author") or "?"),
        "text": text[:220],
        "created_at": raw.get("created_at"),
    }


def _norm_issue(store, raw):
    """Trim a store issue dict to what the views need. Tolerates mirrors
    that predate the detail fields (they arrive as None)."""
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
    try:
        ccount = int(raw.get("comment_count") or 0)
    except (TypeError, ValueError):
        ccount = 0
    comments = []
    for c in raw.get("comments") or []:
        t = _trim_comment(c)
        if t is not None:
            comments.append(t)
    comments = comments[-4:]  # latest few earn wall space; history does not
    desc = raw.get("description")
    return {
        "store": store,
        "id": str(raw.get("id")),
        "title": str(raw.get("title") or "(untitled)"),
        "status": str(raw.get("status") or "open").strip().lower(),
        "priority": prio,
        "issue_type": str(raw.get("issue_type") or "task"),
        "labels": _as_labels(raw.get("labels")),
        "deps": deps,
        "description": str(desc)[:1500] if desc else "",
        "owner": raw.get("owner"),
        "assignee": raw.get("assignee"),
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "closed_at": raw.get("closed_at"),
        "close_reason": raw.get("close_reason"),
        "comment_count": ccount,
        "comments": comments,
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


def _show_bead(cli, bead_id):
    """Fetch one bead live: `<cli> show <id> --json`. Returns (store, raw)
    or None. Runs in a worker thread, never on the draw path."""
    exe = shutil.which(cli)
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [exe, "show", bead_id, "--json"],
            capture_output=True, text=True, timeout=CLI_TIMEOUT)
    except Exception:
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return None
    cands = data if isinstance(data, list) else [data]
    for cand in cands:
        if isinstance(cand, dict) and str(cand.get("id") or "") == bead_id:
            return (cli, cand)
    return None


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
            "attention": [], "stores": {}, "total": 0, "closed": 0,
            "index": by_id, "bare": bare}
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
        waiters = _waiters(issue, by_id, bare)
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


def _waiters(issue, by_id, bare):
    out = []
    for dep in issue["deps"]:
        if dep["type"] not in WAITS_ON:
            continue
        target = by_id.get((issue["store"], dep["target"])) or bare.get(dep["target"])
        if target is not None and target["status"] != "closed":
            out.append(target)
    return out


def bucket_of(issue, snap):
    """Which parade bucket an issue is in, plus the waiters that put it
    there. Single source of truth for overview and detail alike."""
    if issue["status"] == "closed":
        return "past", []
    waiters = _waiters(issue, snap.get("index") or {}, snap.get("bare") or {})
    if waiters:
        return "stalled", waiters
    if issue["status"] == "in_progress":
        return "rolling", []
    return "linedup", []


def dependents_of(issue, snap):
    """Beads waiting on this one (reverse edges), unfinished first."""
    out = []
    for other in (snap.get("index") or {}).values():
        if other is issue:
            continue
        for dep in other["deps"]:
            if dep["type"] not in WAITS_ON:
                continue
            if dep["target"] == issue["id"]:
                out.append(other)
                break
    out.sort(key=lambda i: (i["status"] == "closed", i["priority"], i["id"]))
    return out


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


def find_in_snapshot(snap, bead_id):
    """Resolve a bead id against the snapshot index. Exact id wins; a
    unique prefix or suffix match is accepted for wall use."""
    if not snap or not bead_id:
        return None
    bare = snap.get("bare") or {}
    if bead_id in bare:
        return bare[bead_id]
    cands = [i for bid, i in bare.items()
             if bid.startswith(bead_id) or bid.endswith(bead_id)]
    if len(cands) == 1:
        return cands[0]
    return None


def _fetch_focus_worker(bead_id, stores):
    try:
        for store in stores:
            hit = _show_bead(store, bead_id)
            if hit is not None:
                norm = _norm_issue(hit[0], hit[1])
                if norm is not None:
                    with _POLL["lock"]:
                        _POLL["focus_cache"][bead_id] = norm
                        _POLL["focus_fetching"].discard(bead_id)
                return
    except Exception:
        pass
    finally:
        with _POLL["lock"]:
            _POLL["focus_fetching"].discard(bead_id)


def request_focus(bead_id, stores):
    """Ensure a bead outside the mirror gets fetched live. Non-blocking:
    spawns one worker at most per id; the draw keeps showing cache meanwhile."""
    if not bead_id:
        return
    with _POLL["lock"]:
        if bead_id in _POLL["focus_cache"] or bead_id in _POLL["focus_fetching"]:
            return
        _POLL["focus_fetching"].add(bead_id)
    thread = threading.Thread(target=_fetch_focus_worker,
                              args=(bead_id, list(stores or ())), daemon=True)
    thread.start()


def resolve_focus(bead_id, snap):
    """Best-known normalized issue for a bead id: snapshot first, then the
    live-fetch cache. Returns (issue, source) with issue possibly None."""
    issue = find_in_snapshot(snap, bead_id)
    if issue is not None:
        return issue, "mirror"
    with _POLL["lock"]:
        issue = _POLL["focus_cache"].get(bead_id)
    if issue is not None:
        return issue, "live"
    return None, None


def get_state():
    with _POLL["lock"]:
        return (dict(_POLL["cfg"]), _POLL["snapshot"], _POLL["updated"],
                _POLL["health"], _POLL["error"], _POLL["source"])


def _poll_loop():
    last_sig = None
    while True:
        with _POLL["lock"]:
            cfg = dict(_POLL["cfg"])
            interval = cfg.get("interval") or POLL_FALLBACK_INTERVAL
            interval = max(5, min(3600, int(interval)))
            sig = (cfg.get("mirror"), cfg.get("stores"), interval)
            focus_param = (cfg.get("focus") or "").strip()
            stores = [s.strip() for s in str(cfg.get("stores") or "").split(",")
                      if s.strip()]
        if sig != last_sig:
            last_sig = sig
        try:
            pairs, source = _poll_once(cfg)
            snap = _classify(pairs)
            if focus_param and find_in_snapshot(snap, focus_param) is None:
                request_focus(focus_param, stores)
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
        _POLL["wake"].wait(max(5, interval))
        _POLL["wake"].clear()


def ensure_poll(cfg):
    with _POLL["lock"]:
        _POLL["cfg"] = dict(cfg)
        alive = _POLL["thread"] is not None and _POLL["thread"].is_alive()
        if not alive:
            thread = threading.Thread(target=_poll_loop, daemon=True)
            _POLL["thread"] = thread
            thread.start()
        else:
            _POLL["wake"].set()


def _parse_ts(value):
    try:
        text = str(value).strip().replace("Z", "+00:00")
        import datetime as _dt
        return _dt.datetime.fromisoformat(text).timestamp()
    except Exception:
        return None


def _age(updated):
    if not updated:
        return "no data yet"
    secs = max(0, time.time() - updated)
    if secs < 60:
        return "updated %ds ago" % int(secs)
    if secs < 3600:
        return "updated %dm ago" % int(secs // 60)
    return "updated %dh ago" % int(secs // 3600)


def age_of(iso_ts, now=None):
    """Wall-friendly age of an ISO timestamp: 12d, 3h, 20m."""
    ts = _parse_ts(iso_ts)
    if ts is None:
        return "?"
    secs = max(0, (now if now is not None else time.time()) - ts)
    if secs < 3600:
        return "%dm" % max(1, int(secs // 60))
    if secs < 86400 * 2:
        return "%dh" % int(secs // 3600)
    if secs < 86400 * 60:
        return "%dd" % int(secs // 86400)
    return "%dmo" % int(secs // (86400 * 30))
