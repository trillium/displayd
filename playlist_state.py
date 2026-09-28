"""Playlist read model: config, hold reasons, progress, status.

Single concept: answering "what would the scheduler do / show right now"
without advancing anything. Mixed into :class:`playlist.Playlist`; every
attribute accessed via ``self`` (``daemon``, ``clock``, ``_lock``,
``index``, ``item_started``, ``dwell``, ``current_ok``, ``paused_by``,
``last_error``, ``history``) is owned by that class.
"""


class PlaylistStateMixin:
    """Read-only view over the scheduler's state (no advancement)."""

    def _config(self):
        try:
            return self.daemon.policy.get_config()["playlist"]
        except Exception:
            return {}

    def _views(self, cfg):
        views = cfg.get("views") or []
        return views if isinstance(views, list) else []

    def _hold_reason(self, cfg):
        """Why the scheduler must not advance right now (None = run)."""
        if not cfg.get("enabled"):
            return "disabled"
        if not self._views(cfg):
            return "empty"
        with self._lock:
            if self.paused_by is not None:
                return "paused:manual"
        try:
            if self.daemon.policy.transient_status().get("active") is not None:
                return "transient"
        except Exception:
            pass
        try:
            if getattr(self.daemon.fb, "blanked", False):
                return "screen-off"
        except Exception:
            pass
        try:
            # A static-region layout owns the panel region by region:
            # rotation holds (and the bar hides) until the layout is
            # cleared, exactly like a manual choice. getattr-guarded so
            # test doubles without layout state still work.
            if getattr(self.daemon, "layout", None) is not None:
                return "layout-active"
        except Exception:
            pass
        return None

    def progress(self):
        """Filled share of the current dwell in [0, 1], or None when the
        bar should be hidden (held, error dwell, or misconfigured)."""
        cfg = self._config()
        if self._hold_reason(cfg) is not None:
            return None
        with self._lock:
            if self.item_started is None or not self.current_ok:
                return None
            if self.dwell <= 0:
                return None
            return max(0.0, min(1.0, (self.clock() - self.item_started)
                                / self.dwell))

    def status(self):
        cfg = self._config()
        views = self._views(cfg)
        with self._lock:
            idx = self.index if views else 0
            item = views[idx % len(views)] if views else None
            frac = self.progress()
            history = list(self.history[-10:])
            paused = self.paused_by
            err = self.last_error
        return {
            "enabled": bool(cfg.get("enabled", False)),
            "hold": self._hold_reason(cfg),
            "paused_by": paused,
            "placement": cfg.get("placement"),
            "thickness": cfg.get("thickness"),
            "direction": cfg.get("direction"),
            "index": idx,
            "view": item,
            "progress": round(frac, 4) if frac is not None else None,
            "last_error": err,
            "history": [{"at": at, "renderer": name} for at, name in history],
        }
