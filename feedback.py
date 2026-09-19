#!/usr/bin/env python3
"""feedback - durable display-feedback log for displayd.

The situation this solves: an agent puts something on the panel, and whether
it actually *worked as a display* -- readable at distance, right colours,
sensible layout -- is knowledge that dies instantly unless recorded. This
module records it so later human-guided work can learn across many notes.

Design decisions (brief asked for each explicitly):

* Free text AND structured. Every entry carries a required 1-5 ``rating``
  plus optional ``categories`` from a fixed taxonomy, so a later reader can
  aggregate ("chat averages 2.1 on readability") instead of reading fifty
  notes. Free-text ``notes`` ride alongside for the why.
* Who may write: any agent that can reach the display API. There is no auth
  on displayd, so no new gate is added; instead every entry stamps
  ``agent`` (caller-supplied, default "anonymous") and ``received_at`` so
  later readers can weigh attribution.
* Frames ARE stored, one PNG per note next to the log. A reference that may
  rot (snapshot at read time shows something else now) or no artifact at
  all makes feedback nearly useless -- the note must show what was judged.
  Cost is bounded in practice: feedback is human-scale annotation, not a
  frame stream, and the list/summary endpoints report per-frame and total
  bytes so growth is visible. If a frame file is deleted by hand, the entry
  stays valid and reads back with ``frame_present: false`` rather than
  erroring -- pruning is deleting files, no migration needed.
* Nothing automatic. Recording never changes rendering; it is evidence for
  later human-guided work, not an input to any control loop.
* Feedback POSTs do NOT reset the policy idle clock. They mutate the log,
  not what is on screen; counting annotation as display activity would keep
  the panel awake while reviewers write notes about it.

Storage: one JSONL file (append-only, grep-able, human-readable) plus a
``feedback_frames/`` directory beside it. Defaults to the daemon directory
(``feedback.jsonl``); ``DISPLAYD_FEEDBACK`` overrides the file path and the
frames directory follows it. Survives restarts: everything is on disk before
the record call returns.
"""

import hashlib
import json
import os
import threading
import time
import uuid

ENV_VAR = "DISPLAYD_FEEDBACK"

# Fixed taxonomy: categories aggregate, prose does not. "other" is the
# escape hatch so a note is never forced into a wrong bucket.
CATEGORIES = (
    "readability",   # legible at viewing distance / size
    "layout",        # arrangement, spacing, alignment
    "color",         # palette, contrast choices
    "content",       # the information itself: right thing shown?
    "timing",        # how long it stayed, animation speed, staleness
    "size",          # too much / too little on screen at once
    "other",
)

RATING_MIN, RATING_MAX = 1, 5


def default_path():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.environ.get(ENV_VAR, os.path.join(here, "feedback.jsonl"))


def frames_dir_for(log_path):
    base = os.path.splitext(log_path)[0]
    return base + "_frames"


def validate_entry_fields(view, rating, categories, notes, params, agent):
    """Raise ValueError on any bad field. Unknown *views* are checked by the
    daemon (which knows the renderer list); here view must just be present."""
    if not isinstance(view, str) or not view.strip():
        raise ValueError("view is required (which renderer the note concerns)")
    if isinstance(rating, bool) or not isinstance(rating, int):
        raise ValueError("rating must be an integer %d-%d"
                         % (RATING_MIN, RATING_MAX))
    if not (RATING_MIN <= rating <= RATING_MAX):
        raise ValueError("rating must be within [%d, %d]"
                         % (RATING_MIN, RATING_MAX))
    categories = categories or []
    if not isinstance(categories, (list, tuple)):
        raise ValueError("categories must be a list")
    for cat in categories:
        if cat not in CATEGORIES:
            raise ValueError("unknown category %r (one of: %s)"
                             % (cat, ", ".join(CATEGORIES)))
    if notes is not None and not isinstance(notes, str):
        raise ValueError("notes must be a string")
    if params is not None and not isinstance(params, dict):
        raise ValueError("params must be an object")
    if agent is not None and not isinstance(agent, str):
        raise ValueError("agent must be a string")
    return list(categories)


class FeedbackStore:
    """Append-only JSONL feedback log with per-note PNG frames.

    Thread-safe; every record() is fsynced before returning so a restart
    (clean or not) keeps what was acknowledged.
    """

    def __init__(self, path=None):
        self.path = path or default_path()
        self.frames_dir = frames_dir_for(self.path)
        self._lock = threading.Lock()

    # ---- writes ------------------------------------------------------

    def record(self, view, rating, categories=None, notes="", params=None,
               agent="anonymous", frame_png=None, frame_size=None):
        """Append one entry; returns the stored dict (without PNG bytes).

        ``frame_png`` is raw PNG bytes captured at feedback time (or None
        when the caller explicitly skips the frame). ``frame_size`` is the
        (width, height) tuple for the captured frame.
        """
        cats = validate_entry_fields(view, rating, categories, notes,
                                     params, agent)
        entry_id = "fb-" + uuid.uuid4().hex[:12]
        entry = {
            "id": entry_id,
            "received_at": time.time(),
            "agent": (agent or "").strip() or "anonymous",
            "view": view.strip(),
            "params": params or {},
            "rating": rating,
            "categories": cats,
            "notes": notes or "",
            "frame": None,
        }
        if frame_png is not None:
            os.makedirs(self.frames_dir, exist_ok=True)
            fname = entry_id + ".png"
            fpath = os.path.join(self.frames_dir, fname)
            with open(fpath, "wb") as fh:
                fh.write(frame_png)
            entry["frame"] = {
                "file": fname,
                "bytes": len(frame_png),
                "sha256": hashlib.sha256(frame_png).hexdigest(),
                "width": frame_size[0] if frame_size else None,
                "height": frame_size[1] if frame_size else None,
            }
        with self._lock:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
        return self._with_presence(dict(entry))

    # ---- reads -------------------------------------------------------

    def _frame_present(self, entry):
        frame = entry.get("frame")
        if not frame:
            return False
        return os.path.exists(os.path.join(self.frames_dir, frame["file"]))

    def _with_presence(self, entry):
        entry["frame_present"] = self._frame_present(entry)
        return entry

    def _read_all(self):
        entries = []
        try:
            with open(self.path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except ValueError:
                        continue  # a torn last line never kills the log
        except OSError:
            pass
        return entries

    def list(self, view=None, limit=50):
        """Newest first, optionally filtered to one view."""
        with self._lock:
            entries = self._read_all()
        if view:
            entries = [e for e in entries if e.get("view") == view]
        entries.sort(key=lambda e: e.get("received_at", 0), reverse=True)
        try:
            limit = max(1, int(limit))
        except (TypeError, ValueError):
            limit = 50
        return [self._with_presence(e) for e in entries[:limit]]

    def get(self, entry_id):
        with self._lock:
            entries = self._read_all()
        for entry in entries:
            if entry.get("id") == entry_id:
                return self._with_presence(entry)
        raise KeyError("unknown feedback id: %s" % entry_id)

    def frame_path(self, entry_id):
        """Filesystem path of a note's PNG. Raises KeyError/IOError."""
        entry = self.get(entry_id)
        frame = entry.get("frame")
        if not frame:
            raise IOError("feedback %s has no frame" % entry_id)
        fpath = os.path.join(self.frames_dir, frame["file"])
        if not os.path.exists(fpath):
            raise IOError("frame for feedback %s was pruned" % entry_id)
        return fpath

    def summary(self):
        """Per-view aggregates: counts, mean rating, category histogram.

        This is the "patterns across entries" read: it answers "the chat
        view keeps getting marked unreadable at distance" without opening
        fifty notes.
        """
        with self._lock:
            entries = self._read_all()
        by_view = {}
        total_frame_bytes = 0
        for entry in entries:
            view = entry.get("view", "?")
            agg = by_view.setdefault(view, {
                "count": 0, "rating_sum": 0, "ratings": {},
                "categories": {}, "with_frames": 0,
            })
            agg["count"] += 1
            rating = entry.get("rating")
            if isinstance(rating, int):
                agg["rating_sum"] += rating
                agg["ratings"][str(rating)] = agg["ratings"].get(str(rating), 0) + 1
            for cat in entry.get("categories") or []:
                agg["categories"][cat] = agg["categories"].get(cat, 0) + 1
            frame = entry.get("frame") or {}
            total_frame_bytes += frame.get("bytes") or 0
            if self._frame_present(entry):
                agg["with_frames"] += 1
        views = {}
        for view, agg in sorted(by_view.items()):
            views[view] = {
                "count": agg["count"],
                "avg_rating": (round(agg["rating_sum"] / agg["count"], 2)
                               if agg["count"] else None),
                "ratings": agg["ratings"],
                "categories": agg["categories"],
                "with_frames": agg["with_frames"],
            }
        return {"total": len(entries), "views": views,
                "total_frame_bytes": total_frame_bytes,
                "categories": list(CATEGORIES)}
