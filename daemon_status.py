"""Read-only introspection: /state, /snapshot, /renderers, /deploy.

Single concept: describing the daemon without changing it -- the state dict,
the last presented frame as PNG, the advertised renderer list with their
schemas and declared capability, the html runtime build state, and the
delivery stamp.
"""

import io
import time

import capability
from PIL import Image
from daemon_config import APP_VERSION
from deploy_reload import read_deploy_stamp
import playlist as playlist_module


class StatusMixin:
    """Read-only daemon introspection."""

    # ---- state ---------------------------------------------------------

    def state(self):
        return {
            "version": APP_VERSION,
            "renderer": self.current,
            "params": self.current_params,
            "started_at": self.started_at,
            "age_seconds": round(time.time() - self.started_at, 1) if self.started_at else None,
            "screen": {
                "power": "off" if self.fb.blanked else "on",
                "fb_blank": self.fb.get_blank(),
                "backlight": self.fb.backlight_state(),
                "console_handover": self.console_taken,
            },
            "display": self.fb.status(),
            "last_error": self.last_error,
            "feeds": self.feeds.snapshot(),
            "policy": {
                "transient": self.policy.transient_status(),
                "idle_off": self.policy.idle_off,
            },
            "switch": {
                "at": self.last_switch_at,
                "first_pixel_ms": self.last_switch_ms,
                "fresh_frame_ms": self.last_frame_ms,
            },
            "playlist": self.playlist.status(),
            # Static-region composition (opt-in): None in single-view
            # mode, otherwise per-region binding + health. renderer stays
            # None while a layout owns the panel.
            "layout": self.layout_state(),
            # Delivery stamp (deploy.sh host file): date + SHA of the
            # running build, or {"deployed": False} when never recorded.
            "deploy": self.deploy_info(),
            # Reload scan-confirm: pending window + expiry while the
            # reload view shows, else None. No history is kept.
            "reload_confirm": self.reload_confirm_state(),
            # The optional html view's runtime: is the engine installed and
            # loadable from this process, and which templates can it read.
            # None when the renderer module failed to load at all.
            "html": self.html_runtime(),
        }

    def html_runtime(self):
        """The html view's runtime status, or None when it is not loadable.

        Asked of the renderer module rather than recomputed here, so /state
        reports the paths that renderer really resolves instead of a second
        copy of the same rules. Total by construction: a status probe that
        can raise is not a status, and /state must never fail on one.
        """
        entry = self.renderers.get("html") or {}
        module = entry.get("module")
        probe = getattr(module, "runtime_status", None)
        if probe is None:
            return None
        try:
            return probe()
        except Exception as err:  # a probe, not a dependency
            return {"ok": False, "error": str(err)}

    def renderer_list(self):
        out = []
        for name, entry in sorted(self.renderers.items()):
            if "module" not in entry:
                out.append({"name": name, "broken": entry["broken"]})
                continue
            accent = playlist_module.accent_for(entry)
            out.append(
                {
                    "name": name,
                    "description": entry["description"],
                    "params": entry["params"],
                    "inputs": entry.get("inputs", {}),
                    "static": entry["static"],
                    "capability": entry.get("capability",
                                           capability.UNDECLARED),
                    "accent": "#%02x%02x%02x" % accent,
                }
            )
        return out

    def snapshot(self):
        if self.fb.last_frame is None:
            return None
        img = Image.frombytes(
            "RGB", (self.fb.width, self.fb.height), self.fb.last_frame, "raw", "BGRX"
        )
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    # ---- delivery stamp ----------------------------------------------------

    def deploy_info(self):
        """Last delivery stamp (see read_deploy_stamp). Read-only: the
        file is written host-side by deploy.sh, never through the API."""
        return read_deploy_stamp()
