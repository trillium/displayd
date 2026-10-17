"""HTTP routes for event streams (``/event-streams...``).

The producer side of columns (surface_streams.py): declare a stream, publish
events, read the follow-on actions viewers took. Sidecars read events here too
(``GET .../events``), which is how a column subscribes.

  GET    /event-streams                       declared streams
  POST   /event-streams                       declare (upsert) -> 201 / 200
  GET    /event-streams/<id>                  one stream (404 unknown)
  DELETE /event-streams/<id>                  remove it
  POST   /event-streams/<id>/events           {"type", "data"} publish
  GET    /event-streams/<id>/events[?since=&wait=]    subscribe (long-poll)
  GET    /event-streams/<id>/actions[?since=&wait=]   producer reads actions

Same bearer-token gate as every other route; publishing and declaring count
as operator activity, reads never do.
"""

from urllib.parse import parse_qs

from daemon_http_surfaces import _number, front
from surface_model import SurfaceError

PREFIX = "/event-streams"


class StreamRoutesMixin:
    """The /event-streams handlers; ``install`` fronts the other routes."""

    def _stream_gate(self, path):
        if path != PREFIX and not path.startswith(PREFIX + "/"):
            return None
        denied = self._check_token()
        if denied is not None:
            self._send(401, denied)
            return False
        return True

    def _stream_call(self, work):
        try:
            return self._send(*work())
        except SurfaceError as err:
            return self._send(err.status, {"error": str(err)})
        except ValueError as err:
            return self._send(400, {"error": str(err)})

    def _stream_get(self, parts):
        streams, segs = self.daemon.surface_hub.streams, parts.path.split("/")[2:]
        query = parse_qs(parts.query)
        since = _number(query.get("since", [0])[0], "since")
        wait = _number(query.get("wait", [0])[0], "wait")
        if not segs:
            return 200, {"streams": streams.list()}
        if len(segs) == 1:
            return 200, streams.get(segs[0])
        if segs[1:] == ["events"]:
            return 200, streams.read(segs[0], since, wait)
        if segs[1:] == ["actions"]:
            return 200, streams.read_actions(segs[0], since, wait)
        return 404, {"error": "not found"}

    def _stream_post(self, parts):
        daemon, segs = self.daemon, parts.path.split("/")[2:]
        streams = daemon.surface_hub.streams
        payload, err = self._body_raw()
        if err:
            raise ValueError(err)
        if not segs:
            stream, created = streams.declare(payload)
            daemon.policy.note_api()
            return (201 if created else 200), stream
        if segs[1:] == ["events"]:
            event = streams.publish(segs[0], payload)
            daemon.policy.note_api()
            return 200, {"ok": True, "event": event}
        return 404, {"error": "not found"}

    def _stream_delete(self, parts):
        segs = parts.path.split("/")[2:]
        if len(segs) != 1:
            return 404, {"error": "not found"}
        result = self.daemon.surface_hub.streams.remove(segs[0])
        self.daemon.policy.note_api()
        return 200, result


def install(handler):
    """Put the /event-streams routes in front of GET/POST/DELETE."""
    gate = lambda h, path: h._stream_gate(path)  # noqa: E731
    call = lambda h, work: h._stream_call(work)  # noqa: E731
    handler.do_GET = front(handler.do_GET, gate, call, lambda h, p: h._stream_get(p))
    handler.do_POST = front(handler.do_POST, gate, call, lambda h, p: h._stream_post(p))
    handler.do_DELETE = front(handler.do_DELETE, gate, call,
                              lambda h, p: h._stream_delete(p))

