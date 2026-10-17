"""HTTP routes for the review decision page (``/review...``).

  GET  /review                state, the drawn variables, and whether it shows
  POST /review/load           {"bead": <review show --json>} parse + show it
  POST /review/select         {"slot": 1..5}  choose an option (asks to confirm)
  POST /review/confirm        send the chosen option (publishes a decision)
  POST /review/back           one step back / leave the page
  POST /review/result         {"nonce", "ok", "message"} the executor's answer

select/confirm/back are the tap targets (touch actions ``review_*``) and are
409 unless the review view is showing. Same bearer-token gate as the rest.
"""

from daemon_http_surfaces import front
from surface_model import SurfaceError

PREFIX = "/review"


class ReviewRoutesMixin:
    def _review_route_gate(self, path):
        if path != PREFIX and not path.startswith(PREFIX + "/"):
            return None
        denied = self._check_token()
        if denied is not None:
            self._send(401, denied)
            return False
        return True

    def _review_route_call(self, work):
        try:
            return self._send(*work())
        except SurfaceError as err:
            return self._send(err.status, {"error": str(err)})
        except (ValueError, KeyError) as err:
            return self._send(400, {"error": str(err)})

    def _review_get(self, parts):
        if parts.path != PREFIX:
            return 404, {"error": "not found"}
        return 200, self.daemon.review_state()

    def _review_post(self, parts):
        daemon, verb = self.daemon, parts.path[len(PREFIX) + 1:]
        payload, err = self._body_raw()
        if err and verb in ("load", "select", "result"):
            raise ValueError(err)
        payload = payload if isinstance(payload, dict) else {}
        if verb == "load":
            out = daemon.review_load(payload.get("bead"))
        elif verb == "select":
            out = daemon.review_select(payload.get("slot"))
        elif verb == "confirm":
            out = daemon.review_confirm()
        elif verb == "back":
            out = daemon.review_back()
        elif verb == "result":
            out = daemon.review_result(payload.get("nonce"), payload.get("ok"),
                                       payload.get("message"))
        else:
            return 404, {"error": "not found"}
        daemon.policy.note_api()
        return 200, out


def install(handler):
    """Put the /review routes in front of GET/POST."""
    gate = lambda h, path: h._review_route_gate(path)  # noqa: E731
    call = lambda h, work: h._review_route_call(work)  # noqa: E731
    handler.do_GET = front(handler.do_GET, gate, call, lambda h, p: h._review_get(p))
    handler.do_POST = front(handler.do_POST, gate, call, lambda h, p: h._review_post(p))
