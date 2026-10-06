"""The mutating HTTP surface: POST routes.

Single concept: every POST the daemon serves -- feeds, notify, reload and its
confirm, touch announce/resolve/tap, the Mac-side command queues, policy,
playlist control, feedback, show/layout/clear, and screen power. Only these
count as activity for the idle clock.
"""

from daemon_touch_resolve import _resolve_coords


class POSTRoutesMixin:
    """POST route table."""

    def do_POST(self):
        path = self.path.split("?")[0]
        auth_error = self._check_token()
        if auth_error is not None:
            return self._send(401, auth_error)
        if path.startswith("/feed/"):
            parts = path.split("/")
            if len(parts) != 4 or not parts[2] or not parts[3]:
                return self._send(404, {"error": "use /feed/<renderer>/<input>"})
            payload, err = self._body_raw()
            if err:
                return self._send(400, {"error": err})
            try:
                result = self.daemon.feed(parts[2], parts[3], payload)
            except KeyError as exc:
                return self._send(404, {"error": str(exc)})
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})
            return self._send(200, {"ok": True, "renderer": parts[2],
                                    "input": parts[3], "feed": result["feed"],
                                    "attention": result["attention"]})
        if path == "/notify":
            body = self._body()
            if not self.daemon.policy.get_config()["notifications"]["enabled"]:
                return self._send(409, {"error": "notifications are disabled"})
            try:
                result = self.daemon.notify(
                    body.get("title"), body.get("body", ""),
                    body.get("severity", "info"), body.get("color"),
                    body.get("duration"))
            except KeyError as exc:
                return self._send(404, {"error": str(exc)})
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})
            return self._send(200, result)
        if path == "/reload":
            body = self._body()
            try:
                result = self.daemon.reload(body.get("sha"), body.get("duration"),
                                       body.get("highlights"))
            except KeyError as exc:
                return self._send(404, {"error": str(exc)})
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})
            return self._send(200, result)
        if path == "/touch/tap":
            # Touchscreen tap dismissal: clears only an active reload
            # transient (saved base view, or clock after a fresh restart).
            # Always 200 -- dismissing anything else is a harmless no-op.
            self._body()  # drained for keep-alive; no fields read
            return self._send(200, self.daemon.dismiss_reload())
        if path == "/touch/announce":
            # Touch-service heartbeat: the region set it is actually
            # dispatching. 200 + count/sha, or 400 naming the defect.
            # A machine heartbeat, not operator activity: the idle clock
            # is untouched.
            try:
                return self._send(200,
                                    self.daemon.announce_touch(self._body()))
            except ValueError as exc:
                return self._send(400, {"ok": False,
                                         "error": str(exc)})
        if path == "/touch/resolve":
            # Read-only tap question: region + semantic action the
            # CURRENT UI would resolve to, view/mode-gated like the
            # touch path. No dispatch, no state change, no idle-clock
            # touch. 400 on malformed coordinates; unknown-heartbeat
            # and refused-tap answers are 200 (correct answers, not
            # request errors).
            body = self._body()
            try:
                x, y = _resolve_coords(
                    body, self.daemon.screen.W, self.daemon.screen.H)
            except ValueError as exc:
                return self._send(400, {"ok": False,
                                         "error": str(exc)})
            try:
                return self._send(200, self.daemon.resolve_touch(x, y))
            except ValueError as exc:
                return self._send(400, {"ok": False,
                                         "error": str(exc)})
        if path == "/macbook/mouse":
            # Panel tap -> MacBook cursor + fullscreen zoom: touch.py
            # posts panel pixels (closed `macbook_mouse` action); the
            # Mac-side poller fetches the queued Quartz point via GET
            # below, and the showing view is re-pinned to AIM (the tap
            # is the zoom entry; no AIM button remains). 200 +
            # {ok: True, command, mode} on queue; 400 on malformed or
            # off-panel coordinates; 409 on any refusal (wrong view,
            # stale feed, tap outside the map) -- never a mis-move and
            # never a mode change on a miss.
            body = self._body()
            try:
                result = self.daemon.request_mouse_move(body.get("x"),
                                                   body.get("y"))
            except ValueError as exc:
                return self._send(400, {"ok": False,
                                         "error": str(exc)})
            if result.get("ok"):
                return self._send(200, result)
            return self._send(409, result)
        if path == "/macbook/click":
            # Panel second tap on the review image: touch.py posts
            # panel pixels (closed `macbook_click` action); the
            # Mac-side poller fetches the queued Quartz point via GET
            # above and posts one CG down+up pair. 200 + {ok: True,
            # command} on queue; 400 on malformed coordinates; 409 on
            # any refusal (wrong view, stale feed, no fresh capture,
            # tap off the reviewed point, cursor moved) -- never a
            # blind click.
            body = self._body()
            try:
                result = self.daemon.request_click_move(body.get("x"),
                                                   body.get("y"))
            except ValueError as exc:
                return self._send(400, {"ok": False,
                                         "error": str(exc)})
            if result.get("ok"):
                return self._send(200, result)
            return self._send(409, result)
        if path == "/talon/focus":
            # Panel tap on an app row: queue ONE focus command for the
            # Mac-side poller. View-gated on talon_apps showing plus a
            # fresh feed; a miss is a 409 refusal, never a view change.
            # The body carries panel pixels only -- the app name comes
            # from the feed, so a tap can only select a listed app.
            body = self._body()
            try:
                result = self.daemon.request_focus_move(body.get("x"),
                                                   body.get("y"))
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})
            if result.get("ok"):
                return self._send(200, result)
            return self._send(409, result)
        if path == "/macbook/mode":
            # Header control: pin GLANCE/AIM on the showing merged
            # feature, keeping the tab. 400 on a bad mode, 409 unless
            # macbook shows -- never a view change on a miss.
            body = self._body()
            try:
                result = self.daemon.request_mode_move(body.get("mode"))
            except ValueError as exc:
                return self._send(400, {"ok": False,
                                         "error": str(exc)})
            if result.get("ok"):
                return self._send(200, result)
            return self._send(409, result)
        if path == "/talon/tab":
            # Header stepper: page the app-strip window with a slide,
            # clamped at both ends. 400 on a bad direction, 409 on
            # any refusal (including a press at the first/last page).
            body = self._body()
            try:
                result = self.daemon.request_tab_step(
                    body.get("dir", body.get("direction")))
            except ValueError as exc:
                return self._send(400, {"ok": False,
                                         "error": str(exc)})
            if result.get("ok"):
                return self._send(200, result)
            return self._send(409, result)
        if path == "/reload/confirm":
            # Tap/scan confirm path for the reload view only: with no
            # reload showing this is a 409 miss, never a view change.
            body = self._body()
            result = self.daemon.confirm_reload(body.get("via", "tap"),
                                           body.get("token"))
            if result.get("confirmed"):
                return self._send(200, result)
            return self._send(409, result)
        if path == "/policy":
            try:
                return self._send(200, self.daemon.set_policy(self._body()))
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})
        if path == "/playlist/pause":
            self.daemon.policy.note_api()
            self.daemon.playlist.pause()
            return self._send(200, self.daemon.playlist.status())
        if path == "/playlist/resume":
            self.daemon.policy.note_api()
            self.daemon.playlist.resume()
            return self._send(200, self.daemon.playlist.status())
        if path == "/playlist/next":
            self.daemon.policy.note_api()
            self.daemon.playlist.next()
            return self._send(200, self.daemon.playlist.status())
        if path == "/feedback":
            body = self._body()
            try:
                stored = self.daemon.record_feedback(
                    body.get("view"), body.get("rating"),
                    categories=body.get("categories"),
                    notes=body.get("notes", ""),
                    params=body.get("params"),
                    agent=body.get("agent", "anonymous"),
                    include_frame=body.get("include_frame", True))
            except KeyError as exc:
                return self._send(404, {"error": str(exc)})
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})
            except TypeError as exc:
                return self._send(400, {"error": str(exc)})
            return self._send(201, stored)
        body = self._body()
        try:
            if path == "/show":
                name = body.get("renderer") or body.get("name")
                if not name:
                    return self._send(400, {"error": "renderer is required"})
                # A bare /show exits layout mode: single-renderer
                # behaviour is unchanged when no layout is active.
                return self._send(200, self.daemon.show(name, body.get("params")))
            if path == "/layout":
                return self._send(200, self.daemon.set_layout(body))
            if path == "/clear":
                return self._send(200, self.daemon.clear())
            if path in ("/screen", "/screen/off", "/screen/on"):
                power = body.get("power")
                if path.endswith("/off"):
                    power = "off"
                elif path.endswith("/on"):
                    power = "on"
                return self._send(200, self.daemon.set_power(power))
        except KeyError as err:
            return self._send(404, {"error": str(err)})
        except ValueError as err:
            return self._send(400, {"error": str(err)})
        return self._send(404, {"error": "not found"})
