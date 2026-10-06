"""Feed delivery and the policy reaction to it.

Single concept: a bridge push into a view's buffer, and the one policy-driven
consequence of a push -- a chat event may pull the panel to the chat view for
a bounded time. Feed storage/health is feed_store.py.
"""

from renderer_registry import talon_apps_module


class FeedsMixin:
    """Feed delivery plus chat attention."""

    # ---- feeds + policy-driven behaviour -------------------------------

    def feed(self, renderer, input_name, payload):
        """Deliver a validated payload to a view's buffer, then consult
        the policy: a chat event may pull the panel to the chat view.
        Pure cache write first -- never disturbs what is on screen.
        Raises KeyError (unknown renderer/input) or ValueError (schema
        mismatch); both map to HTTP errors without side effects."""
        # Feed compat for the retired talon_apps view: the Mac-side poller
        # still posts /feed/talon_apps/state, and that namespace is owned
        # by the merged macbook feature now (the app list lives in its
        # header). Validated against the helper's schema and stored under
        # the same key every reader already uses -- no Mac-side change.
        if renderer == "talon_apps" and input_name == "state":
            if talon_apps_module is None:
                raise KeyError("unknown renderer: %s" % renderer)
            try:
                pushed = self.feeds.push(renderer, input_name, payload,
                                         talon_apps_module.STATE_SCHEMA)
            except ValueError as exc:
                self.feeds.note_error(renderer, input_name, exc)
                raise
            self.policy.note_feed()
            with self.lock:
                woke = self._wake_if_idle()
            if woke:
                self._exit_sleep_view()
            return {"feed": pushed,
                    "attention": self._maybe_attention(renderer,
                                                       input_name)}
        entry = self.renderers.get(renderer)
        if not entry or "module" not in entry:
            raise KeyError("unknown renderer: %s" % renderer)
        spec = (entry.get("inputs") or {}).get(input_name)
        if spec is None:
            raise KeyError("unknown input: %s.%s" % (renderer, input_name))
        try:
            pushed = self.feeds.push(renderer, input_name, payload, spec)
        except ValueError as exc:
            # Schema mismatch: mark the feed errored (until a later push
            # succeeds) before mapping to the HTTP error. Raising KeyError
            # paths stay untouched -- an unknown feed has no entry to mark.
            self.feeds.note_error(renderer, input_name, exc)
            raise
        self.policy.note_feed()
        with self.lock:
            woke = self._wake_if_idle()
        if woke:
            # The only wake path with no follow-up view switch: land the
            # sleep return now, or the panel lights up on the sleep view.
            self._exit_sleep_view()
        return {"feed": pushed,
                "attention": self._maybe_attention(renderer, input_name)}

    def _maybe_attention(self, fed_renderer, fed_input):
        """Chat-attention decision point. OFF by default; when enabled, a
        chat event pulls the panel to the configured view for return_after
        seconds (re-armed by each new event), then back to the base view.
        Only a feed to the configured view+input counts as a chat event;
        manual selections always win; a notice in flight suppresses."""
        cfg = self.policy.get_config()["chat_attention"]
        if not cfg["enabled"]:
            return {"switched": False, "reason": "disabled"}
        if fed_renderer != cfg["view"] or fed_input != cfg["input"]:
            return {"switched": False, "reason": "not-a-chat-event"}
        # A layout owns the panel region by region: a feed just updates
        # the bound region's buffer (it is already visible), so a
        # full-screen pull would destroy the layout to show what is
        # already on screen. Feeds route; the panel does not switch.
        with self.layout_lock:
            if self.layout is not None:
                return {"switched": False, "reason": "layout-active"}
        view = cfg["view"]
        if self.current == view:
            active = self.policy.active
            if active is not None and active["kind"] == "attention":
                token, _ = self.policy.begin_transient("attention", cfg["return_after"])
                with self.lock:
                    self._arm_transient("attention", token, cfg["return_after"])
                return {"switched": True, "reason": "re-armed"}
            return {"switched": False, "reason": "already-showing"}
        if self.fb.blanked:
            return {"switched": False, "reason": "screen-off"}
        entry = self.renderers.get(view)
        if not entry or "module" not in entry:
            return {"switched": False, "reason": "unknown-view"}
        token, active_kind = self.policy.begin_transient("attention", cfg["return_after"])
        if token is None:
            # A higher-or-equal transient holds the screen: the attention
            # pull is suppressed. The reason names the holder ("notice" in
            # the long-standing case), so the panel state stays legible.
            return {"switched": False, "reason": "%s-active" % active_kind}
        with self.lock:
            self._arm_transient("attention", token, cfg["return_after"])
        try:
            self._start_view(view, {})
        except (KeyError, ValueError):
            return {"switched": False, "reason": "unknown-view"}
        out = {"switched": True, "reason": "pulled", "view": view}
        if active_kind:
            out["superseded"] = active_kind
        return out
