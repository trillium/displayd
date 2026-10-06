"""Touch heartbeat renewal: the in-process /touch/announce cadence.

Single concept: keeping displayd's view of the live touch region set fresh.
``ANNOUNCE_INTERVAL_SECONDS`` is the renewal cadence (far inside the daemon's
heartbeat window); ``HeartbeatMixin`` adds the renewal thread to the touch
service. Renewal must never depend on a service restart.
"""

import logging
import threading

LOG = logging.getLogger("displayd-touch")

# Re-announce cadence for the touch heartbeat (POST /touch/announce):
# the daemon's /touch/check treats a heartbeat older than its
# TOUCH_HEARTBEAT_MAX_AGE (30 minutes) as blind ("stale"), so renewal
# runs far inside that window -- six consecutive failed renewals before
# the gate could go blind, and one cheap localhost POST every five
# minutes on the panel host. In-process on purpose: the sibling P1
# showed a periodic service restart can wedge the unit and kill touch
# input entirely, so renewal must never depend on restarting.
ANNOUNCE_INTERVAL_SECONDS = 300.0


class HeartbeatMixin:
    """Announce renewal for the touch service (in-process, never a restart)."""

    def reannounce(self):
        """One best-effort heartbeat renewal. Never raises: a failed
        renewal is logged and retried on the next interval, and taps
        keep serving -- the daemon reports blind until one lands."""
        try:
            ack = self.announce()
        except Exception as exc:
            LOG.warning("touch re-announce failed (best-effort, "
                        "retry in %.0fs): %s",
                        self.announce_interval, exc)
            return None
        LOG.info("touch re-announce: %d regions live (%s)",
                 (ack or {}).get("regions", 0),
                 (ack or {}).get("regions_sha"))
        return ack

    def start_heartbeat(self):
        """Renew the announce on an interval, in-process. Idempotent:
        a second start while the thread lives is a no-op."""
        if (self._heartbeat_thread is not None
                and self._heartbeat_thread.is_alive()):
            return
        self._heartbeat_stop.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()

    def stop_heartbeat(self):
        """Stop renewal; safe to call when never started."""
        self._heartbeat_stop.set()
        thread, self._heartbeat_thread = self._heartbeat_thread, None
        if thread is not None:
            thread.join(timeout=5)

    def _heartbeat_loop(self):
        while not self._heartbeat_stop.wait(self.announce_interval):
            if self._stop:
                return
            self.reannounce()
