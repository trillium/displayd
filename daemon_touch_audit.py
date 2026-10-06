"""The drawn-vs-live touch region gate.

Single concept: the touch service's heartbeat and the assertion that the
regions it announced match the geometry the showing UI actually draws
(touch_audit.py does the comparison; the host touch.json is read-only
context). Blind (no heartbeat) and stale (expired heartbeat) are alarms, never
a pass.
"""

import json
import os
import time

try:
    import touch_audit
except Exception:
    touch_audit = None

# Host touch.json (host-local, never overwritten by deploys): read-only
# context for /touch/check, never the live set (see touch_audit.py).
TOUCH_JSON_PATH = os.environ.get(
    "DISPLAYD_TOUCH_JSON",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "touch.json"),
)


class TouchAuditMixin:
    """Touch heartbeat and region audit gate."""

    # Freshness window for the touch-service heartbeat (POST
    # /touch/announce): a heartbeat older than this makes
    # touch_check() report blind ("stale"), never a pass. The touch
    # service re-announces every ANNOUNCE_INTERVAL_SECONDS (touch.py),
    # far inside this window, so a healthy gate never ages out; only a
    # dead or wedged announcer goes blind. deploy.sh's post-restart
    # freshness proof (announced_at newer than pre-restart) is
    # unaffected: a restart still announces at startup first.
    TOUCH_HEARTBEAT_MAX_AGE = 1800.0

    def announce_touch(self, body):
        """Touch-service heartbeat (POST /touch/announce): the region
        set the running service is actually dispatching. Best-effort on
        the touch side, validated here. Never touches the policy clock:
        a machine heartbeat is not operator activity."""
        if touch_audit is None:
            raise ValueError("touch audit helper unavailable")
        cleaned = touch_audit.validate_announce(body or {})
        with self.lock:
            self.touch_live = {
                "announced": cleaned,
                "regions_sha": touch_audit.regions_sha(cleaned),
                "announced_at": time.time(),
            }
            sha = self.touch_live["regions_sha"]
            count = (len(cleaned["regions"])
                     + sum(len(v) for v in
                           cleaned["view_regions"].values()))
        return {"ok": True, "regions": count, "regions_sha": sha}

    def _expected_picker_views(self, params, name="picker"):
        """Mirror of _picker_live_params for the check path: explicit
        views win, else the live advertised set (minus self for the
        unified home screen). None only when the module itself is
        unloadable (then not assertable)."""
        mod = (self.renderers.get(name) or {}).get("module")
        if mod is None:
            return None
        params = params if isinstance(params, dict) else {}
        try:
            if "views" in params:
                return mod.coerce_views(params)
            live = getattr(mod, "live_tile_views", None)
            if live is None and hasattr(mod, "live_views"):
                live = mod.live_views
            if live is not None:
                return live(self.renderers)
            return mod.coerce_views(params)
        except Exception:
            return None

    def touch_check(self):
        """GET /touch/check body: DRAWN geometry vs the LIVE announced
        region set, evaluated per view (matrix) plus the showing view in
        detail. ok False is blind -- "unknown" (no heartbeat yet) or
        "stale" (heartbeat older than TOUCH_HEARTBEAT_MAX_AGE) -- or
        "mismatch" (exact moved/missing rects). Blind carries
        blind True (the monitor reporting its OWN blindness: an alarm,
        not a pass); drift carries no blind flag (a block). Undrawn
        live ids are
        reported, never failed. Non-showing views assume default params."""
        if touch_audit is None:
            return {"ok": False, "status": "error",
                    "error": "touch audit helper unavailable"}
        live = self.touch_live
        if live is None:
            return {"ok": False, "status": "unknown", "blind": True,
                    "current_view": self.current,
                    "error": "no touch heartbeat: restart displayd-touch "
                    "(it announces at startup and re-announces every "
                    "5 minutes) or run touch.py --announce",
                    "coverage": touch_audit.COVERAGE}
        try:
            age = time.time() - float(live.get("announced_at") or 0)
        except (TypeError, ValueError):
            age = float("inf")
        if age > self.TOUCH_HEARTBEAT_MAX_AGE:
            return {"ok": False, "status": "stale", "blind": True,
                    "current_view": self.current,
                    "error": ("touch heartbeat expired (%.0fs old, "
                                "max %.0fs): the touch service stopped "
                                "re-announcing; restart displayd-touch "
                                "or run touch.py --announce" %
                                (age, self.TOUCH_HEARTBEAT_MAX_AGE)),
                    "age_seconds": round(age, 1),
                    "max_age_seconds": self.TOUCH_HEARTBEAT_MAX_AGE,
                    "announced_at": live.get("announced_at"),
                    "regions_sha": live.get("regions_sha"),
                    "coverage": touch_audit.COVERAGE}
        announced = live.get("announced") or {}
        scoped = announced.get("view_regions") or {}
        global_regions = announced.get("regions") or []
        w, h = self.screen.W, self.screen.H
        views = sorted(name for name, entry in self.renderers.items()
                       if isinstance(entry, dict) and "module" in entry)
        results = {}
        for name in views:
            if name in ("picker", "unified"):
                params = (self.current_params
                          if name == self.current else {})
                picker_views = self._expected_picker_views(params, name)
                if picker_views is None:
                    results[name] = {"ok": True, "checkable": False,
                                     "reason": "%s unloadable" % name}
                    continue
            else:
                params, picker_views = (self.current_params
                                        if name == self.current else {}), None
            expected = touch_audit.expected_for_view(
                name, params, w, h, picker_views=picker_views)
            if not expected["checkable"]:
                results[name] = {"ok": True, "checkable": False,
                                 "reason": expected["reason"]}
                continue
            live_norm = touch_audit.normalize_live(
                touch_audit.candidates(global_regions, scoped, name))
            compared = touch_audit.compare_exact(
                expected["exact"], live_norm,
                force_strict=name in scoped)
            presence = touch_audit.compare_presence(expected["presence"],
                                                    live_norm)
            results[name] = {
                "ok": compared["ok"] and presence["ok"],
                "reason": expected["reason"],
                "missing": compared["missing"],
                "moved": compared["moved"],
                "unwired": compared["unwired"],
                "presence_missing": presence["missing"],
                "unasserted": compared["unasserted"]}
        file_info = {"present": False}
        try:
            with open(TOUCH_JSON_PATH, "r", encoding="utf-8") as fh:
                doc = json.load(fh) or {}
            file_sha = touch_audit.regions_sha(doc)
            file_info = {"present": True,
                         "regions_sha": file_sha,
                         "matches_live": file_sha == live["regions_sha"]}
        except Exception as exc:
            file_info = {"present": False, "error": str(exc)}
        current = None
        if self.current is not None and self.current in results:
            current = {"view": self.current,
                       "params": self.current_params,
                       **results[self.current]}
            if self.current in ("picker", "unified"):
                current["expected"] = touch_audit.expected_for_view(
                    self.current, self.current_params, w, h,
                    picker_views=self._expected_picker_views(
                        self.current_params, self.current))["exact"]
        ok = all(r.get("ok", True) for r in results.values())
        report = {"ok": ok, "status": "ok" if ok else "mismatch",
                  "current_view": self.current,
                  "display": [w, h], "current": current,
                  "views": results,
                  "unknown_views": sorted(set(scoped) - set(views)),
                  "announced_at": live["announced_at"],
                  "regions_sha": live["regions_sha"],
                  "touch_json": file_info,
                  "coverage": touch_audit.COVERAGE}
        if self.layout_state():
            report["note"] = ("layout active: matrix still evaluates "
                                "single-view wiring; no per-view taps "
                                "asserted while the layout owns the panel")
        return report
