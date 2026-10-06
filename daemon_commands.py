"""The command long-poll hold.

Single concept: the optional ``?wait=`` hold the GET command endpoints share.
wait=0 is one take_* sample and an immediate reply; wait>0 holds until a tap
queues a newer command or the hold expires, bounded by CMD_WAIT_MAX. The TTL
slot stays the truth, so an unacked command is simply the next poll's answer.
"""

import time


class CommandsMixin:
    """Bounded long-poll wait for command endpoints."""

    # ---- command long-poll (tap-latency fast lane) --------------------
    # The three GET command endpoints accept an optional ?wait= (seconds).
    # wait=0 (or absent/garbage) is exactly today's behavior: one take_*
    # sample, immediate reply. wait>0 holds the reply until a tap queues
    # a newer command or the hold expires -- the TTL slot stays the truth
    # (socket-is-a-fast-lane shape from the transport report): an unacked
    # command simply sits for the next poll, so the fallback is the status
    # quo, not a second code path. Bounded by CMD_WAIT_MAX so a turn can
    # never hang; the held GET runs on its own handler thread and never
    # touches the idle clock (observation, like every GET). take_*
    # semantics (read-only, since= idempotency, TTL expiry) are unchanged.
    CMD_WAIT_MAX = 5.0

    def wait_command(self, kind, since=None, wait=0.0):
        """take_* with an optional bounded hold. `kind` is one of
        "mouse" / "focus" / "click". Returns the pending command
        newer than `since`, or None when idle at hold expiry -- the same
        shape as take_*, so callers and the wire format never change."""
        takes = {"mouse": self.take_mouse_move,
                 "focus": self.take_focus_move,
                 "click": self.take_click_move}
        take = takes[kind]  # internal callers only; unknown kind raises
        try:
            wait = float(wait) if wait is not None else 0.0
        except (TypeError, ValueError):
            wait = 0.0
        if not wait > 0:  # covers zero, negatives, and NaN alike
            wait = 0.0
        wait = min(wait, self.CMD_WAIT_MAX)
        cmd = take(since)
        if cmd is not None or wait <= 0:
            return cmd
        deadline = time.monotonic() + wait
        with self.cmd_cond:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.cmd_cond.wait(timeout=remaining)
                cmd = take(since)  # re-sample: wakes can be spurious
                if cmd is not None:
                    return cmd
        return take(since)
