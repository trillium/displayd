"""Event streams: named, modular feeds that columns subscribe to.

Single concept: the *producer side* of a column. A stream is not a display
surface and not a region: it is a named sequence of events (agent activity,
tool calls, a metric...) that any number of columns, on any number of
surfaces, may show. A producer ``declare``s it once, saying which event
``types`` it emits, which ``views`` (UI modules) can render it and which
follow-on ``actions`` a viewer may take; then it ``publish``es events.

* ``read(since, wait)`` -- consumers/Sidecars pull events newer than ``since``
  (bounded long-poll, same as surface content).
* ``add_action`` / ``read_actions`` -- the way back: a viewer's follow-on
  action (``{"action": "approve", "column": ...}``) is validated against the
  declared actions and queued for the producer, which reads it by ``since``.

Declarations are persisted by the hub (surface_store.py); event and action
rings are in memory, bounded (``MAX_STREAMS``, ring sizes), and start empty
after a restart with their seq counters moved to a new generation, so a
consumer's ``since`` stays valid. Nothing here knows surfaces.
"""

import collections
import json
import threading
import time

from surface_channels import Waiter
from surface_model import ID_RE, SurfaceError, _text, _token

MAX_STREAMS = 64
EVENT_RING = 256
ACTION_RING = 128
MAX_EVENT_BYTES = 64 * 1024
MAX_LIST = 16  # event types / views / actions per stream


def _tokens(raw, where):
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)) or len(raw) > MAX_LIST:
        raise ValueError("%s: expected a list of at most %d" % (where, MAX_LIST))
    return sorted({_token(item, where) for item in raw})


def _actions(raw):
    raw = [] if raw is None else raw
    if not isinstance(raw, (list, tuple)) or len(raw) > MAX_LIST:
        raise ValueError("actions: expected a list of at most %d" % MAX_LIST)
    out = {}
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("actions: expected a list of {id, label}")
        aid = _token(item.get("id"), "actions.id")
        out[aid] = {"id": aid, "label": _text(item.get("label"), "actions.label",
                                              aid, 40)}
    return list(out.values())


def normalize_stream(spec):
    if not isinstance(spec, dict):
        raise ValueError("stream: expected an object")
    sid = _token(spec.get("id"), "id", ID_RE)
    return {"id": sid, "name": _text(spec.get("name"), "name", sid),
            "event_types": _tokens(spec.get("event_types"), "event_types"),
            "views": _tokens(spec.get("views"), "views"),
            "actions": _actions(spec.get("actions"))}


class EventStreams(Waiter):
    """Declared streams, their event rings and their action queues."""

    def __init__(self, clock=time.time, seq_base=0, on_change=None):
        self._clock = clock
        self._base = seq_base
        self._on_change = on_change or (lambda urgent=True: None)
        self._listeners = []
        self._cond = threading.Condition()
        self._decl = {}  # id -> declaration
        self._log = {}   # id -> {"events": {"seq","ring"}, "actions": {...}}

    def subscribe(self, fn):
        """``fn()`` after an event is published or a declaration changes, with
        no lock held (so it may read the streams); a failing listener never
        fails the publisher."""
        self._listeners.append(fn)

    def _fire(self):
        for fn in list(self._listeners):
            try:
                fn()
            except Exception:
                pass

    def _ring(self, size):
        return {"seq": self._base, "ring": collections.deque(maxlen=size)}

    def export(self):
        with self._cond:
            return [dict(self._decl[s]) for s in sorted(self._decl)]

    def restore(self, decls):
        """Boot-time load of validated declarations (rings start empty)."""
        with self._cond:
            for decl in decls[:MAX_STREAMS]:
                self._decl[decl["id"]] = decl
                self._log.setdefault(decl["id"], {"events": self._ring(EVENT_RING),
                                                  "actions": self._ring(ACTION_RING)})

    def declare(self, spec):
        """Upsert a declaration; returns ``(stream, created)``. Events and
        queued actions survive a re-declaration."""
        decl = normalize_stream(spec)
        with self._cond:
            created = decl["id"] not in self._decl
            if created and len(self._decl) >= MAX_STREAMS:
                raise SurfaceError(409, "at most %d streams" % MAX_STREAMS)
            self._decl[decl["id"]] = decl
            self._log.setdefault(decl["id"], {"events": self._ring(EVENT_RING),
                                              "actions": self._ring(ACTION_RING)})
            out = self._view(decl["id"]), created
        self._on_change()
        self._fire()
        return out

    def _view(self, sid):
        log = self._log[sid]
        return dict(self._decl[sid], seq=log["events"]["seq"],
                    action_seq=log["actions"]["seq"])

    def get(self, sid):
        with self._cond:
            if sid not in self._decl:
                raise SurfaceError(404, "unknown stream %r" % sid)
            return self._view(sid)

    def find(self, sid):
        """The stream or None (columns may name one not declared yet)."""
        with self._cond:
            return self._view(sid) if sid in self._decl else None

    def list(self):
        with self._cond:
            return [self._view(s) for s in sorted(self._decl)]

    def remove(self, sid):
        with self._cond:
            self.get_locked(sid)
            del self._decl[sid], self._log[sid]
            self._cond.notify_all()
        self._on_change()
        self._fire()
        return {"ok": True, "stream": sid}

    def get_locked(self, sid):
        if sid not in self._decl:
            raise SurfaceError(404, "unknown stream %r" % sid)

    # ---- events (producer -> columns) ------------------------------------

    def publish(self, sid, event):
        if not isinstance(event, dict):
            raise ValueError("event: expected an object")
        etype = _token(event.get("type"), "event.type")
        data = event.get("data")
        if len(json.dumps(data)) > MAX_EVENT_BYTES:
            raise ValueError("event.data: over %d bytes" % MAX_EVENT_BYTES)
        with self._cond:
            self.get_locked(sid)
            types = self._decl[sid]["event_types"]
            if types and etype not in types:
                raise SurfaceError(415, "stream %r emits %s, not %r"
                                   % (sid, ", ".join(types), etype))
            ring = self._log[sid]["events"]
            ring["seq"] += 1
            stored = {"seq": ring["seq"], "type": etype, "data": data,
                      "at": self._clock()}
            ring["ring"].append(stored)
            self._cond.notify_all()
            out = dict(stored)
        self._fire()
        return out

    def _read(self, sid, kind, since, wait):
        def newer():
            if sid not in self._log:
                return None
            return [dict(e) for e in self._log[sid][kind]["ring"] if e["seq"] > since]

        with self._cond:
            self.get_locked(sid)
            found = self._wait_until(newer, wait)
            alive = sid in self._log
            return {"stream": sid, "seq": self._log[sid][kind]["seq"] if alive else 0,
                    "items": found or []}

    def read(self, sid, since=0, wait=0):
        out = self._read(sid, "events", since, wait)
        return {"stream": sid, "seq": out["seq"], "events": out["items"]}

    # ---- actions (viewer -> producer) ------------------------------------

    def add_action(self, sid, action):
        """Queue a follow-on action; it must be one the stream declared."""
        with self._cond:
            self.get_locked(sid)
            declared = [a["id"] for a in self._decl[sid]["actions"]]
            if action.get("action") not in declared:
                raise SurfaceError(409, "stream %r offers actions: %s"
                                   % (sid, ", ".join(declared) or "none"))
            ring = self._log[sid]["actions"]
            ring["seq"] += 1
            stored = dict(action, seq=ring["seq"], at=self._clock())
            ring["ring"].append(stored)
            self._cond.notify_all()
            return dict(stored)

    def read_actions(self, sid, since=0, wait=0):
        out = self._read(sid, "actions", since, wait)
        return {"stream": sid, "seq": out["seq"], "actions": out["items"]}
