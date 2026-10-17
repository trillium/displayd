"""HTTP routes for registered display surfaces (``/surfaces...``).

Single concept: the wire shape of the surface API. It is a mixin placed in
front of the existing route tables (``Handler`` MRO), so every other path
falls straight through to them untouched.

  GET    /surfaces[?kind=&renderer=&content_type=&touch=&min_width=
                    &min_height=&region=&label.<k>=<v>]   discover
  POST   /surfaces                       register (upsert) -> 201 / 200
  GET    /surfaces/<id>                  one surface (404 unknown, 410 expired)
  POST   /surfaces/<id>                  update (merge a partial spec)
  DELETE /surfaces/<id>                  deregister
  POST   /surfaces/<id>/heartbeat        extend the lease (410 -> re-register)
  POST   /surfaces/<id>/content          {"region"?, "content"} send to one
  POST   /surfaces/content               {"target", "content"} send anywhere
  GET    /surfaces/<id>/content[?since=&wait=&region=]  a Sidecar pulls
  POST   /surfaces/<id>/touch            a Sidecar reports a touch
  GET    /surfaces/<id>/events[?since=&wait=]           consumers read touches
  GET    /surfaces/<id>/columns          its columns + what their streams offer
  POST   /surfaces/<id>/columns          {"columns": [...]} replace the columns
  POST   /surfaces/<id>/columns/<col>/action   a viewer's follow-on action

Same bearer-token gate as the rest of the API. Mutating sends count as
operator activity; heartbeats and reads never touch the idle clock.
"""

import functools
from urllib.parse import parse_qs, urlsplit

import surface_columns as columns
from surface_model import SurfaceError
from surface_target import check_query


def _filter(query_string):
    """Query string -> select query (numeric/label keys coerced)."""
    raw = {k: v[0] for k, v in parse_qs(query_string).items()
           if k not in ("since", "wait")}
    query = {}
    for key, value in raw.items():
        if key.startswith("label."):
            query.setdefault("labels", {})[key[len("label."):]] = value
        elif key in ("min_width", "min_height"):
            try:
                query[key] = int(value)
            except ValueError:
                raise ValueError("%s: expected an integer" % key)
        else:
            query[key] = value
    return check_query(query)


def _number(value, name):
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError("%s: expected a number" % name)


def front(verb, gate, call, handle):
    """Wrap an existing ``do_<VERB>`` so the paths ``gate`` claims are
    answered by ``handle`` (run through ``call``) and everything else falls
    straight through to it. ``wraps`` keeps ``inspect.getsource`` (which
    several route tests read) pointing at the original handler."""
    @functools.wraps(verb)
    def wrapper(self):
        parts = urlsplit(self.path)
        ok = gate(self, parts.path)
        if ok is None:
            return verb(self)
        if ok:
            call(self, lambda: handle(self, parts))
    return wrapper


def install(handler):
    """Put the /surfaces routes in front of ``handler``'s GET/POST/DELETE."""
    gate = lambda h, path: h._surface_gate(path)  # noqa: E731
    call = lambda h, work: h._surface_call(work)  # noqa: E731
    handler.do_GET = front(handler.do_GET, gate, call, lambda h, p: h._surface_get(p))
    handler.do_POST = front(handler.do_POST, gate, call,
                            lambda h, p: h._surface_post(p.path))
    handler.do_DELETE = front(handler.do_DELETE, gate, call,
                              lambda h, p: h._surface_delete(p.path))


class SurfaceRoutesMixin:
    """The /surfaces handlers; ``install`` puts them in front of the others."""

    def _surface_call(self, work):
        try:
            return self._send(*work())
        except SurfaceError as err:
            return self._send(err.status, {"error": str(err)})
        except ValueError as err:
            return self._send(400, {"error": str(err)})

    def _surface_gate(self, path):
        """None when this is not a surface path (fall through); True when
        it is and the request may proceed; False once a 401 was sent."""
        if path != "/surfaces" and not path.startswith("/surfaces/"):
            return None
        denied = self._check_token()
        if denied is not None:
            self._send(401, denied)
            return False
        return True

    # ---- handlers ------------------------------------------------------

    def _segments(self, path):
        return [p for p in path.split("/")[2:]]

    def _surface_get(self, parts):
        hub, segs = self.daemon.surface_hub, self._segments(parts.path)
        query = parse_qs(parts.query)
        since = _number(query.get("since", [0])[0], "since")
        wait = _number(query.get("wait", [0])[0], "wait")
        if not segs:
            return 200, {"surfaces": hub.registry.list(_filter(parts.query))}
        if len(segs) == 1:
            return 200, hub.registry.get(segs[0])
        if len(segs) == 2 and segs[1] == "content":
            hub.registry.get(segs[0])  # 404/410 before holding a poll open
            return 200, hub.registry.channels.read_content(
                segs[0], since, wait, query.get("region", [None])[0])
        if len(segs) == 2 and segs[1] == "events":
            hub.registry.get(segs[0])
            return 200, hub.registry.channels.read_events(segs[0], since, wait)
        if len(segs) == 2 and segs[1] == "columns":
            return 200, columns.describe(hub.registry.get(segs[0]), hub.streams)
        return 404, {"error": "not found"}

    def _surface_post(self, path):
        daemon, segs = self.daemon, self._segments(path)
        hub = daemon.surface_hub
        if len(segs) == 2 and segs[1] == "heartbeat":
            self._drain_body()
            return 200, hub.registry.heartbeat(segs[0])
        payload, err = self._body_raw()
        if err:
            raise ValueError(err)
        if not segs:
            record, created = hub.registry.register(payload)
            daemon.policy.note_api()
            return (201 if created else 200), record
        if segs == ["content"]:
            self._need_object(payload)
            daemon.policy.note_api()
            return 200, hub.deliver(payload.get("target"), payload.get("content"))
        if len(segs) == 1:
            record = hub.registry.update(segs[0], payload)
            daemon.policy.note_api()
            return 200, record
        self._need_object(payload)
        if segs[1] == "content":
            target = segs[0] + ("/" + payload["region"] if payload.get("region") else "")
            daemon.policy.note_api()
            return 200, hub.deliver(target, payload.get("content"))
        if segs[1] == "columns" and len(segs) == 2:
            record = hub.registry.update(
                segs[0], {"panel": {"config": {"columns": payload.get("columns")}}})
            daemon.policy.note_api()
            return 200, columns.describe(record, hub.streams)
        if segs[1] == "columns" and len(segs) == 4 and segs[3] == "action":
            daemon.policy.note_api()
            return 200, columns.act(hub.registry, hub.streams, segs[0], segs[2], payload)
        if segs[1] == "touch":
            event = hub.registry.touch(segs[0], payload)
            daemon.policy.note_api()
            return 200, {"ok": True, "event": event}
        return 404, {"error": "not found"}

    def _surface_delete(self, path):
        segs = self._segments(path)
        if len(segs) != 1:
            return 404, {"error": "not found"}
        result = self.daemon.surface_hub.registry.deregister(segs[0])
        self.daemon.policy.note_api()
        return 200, result

    @staticmethod
    def _need_object(payload):
        if not isinstance(payload, dict):
            raise ValueError("expected a JSON object body")

    def _drain_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
