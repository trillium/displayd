"""Per-key long-poll conditions: a put/publish for K wakes only K's waiters.

Deterministic: a recording Condition tells us when a waiter has parked and
how many times each key's waiters actually woke, so nothing here sleeps to
synchronise; the only clock is the (long) wait bound that must NOT be hit.
"""

import os
import sys
import threading
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import surface_channels  # noqa: E402
from surface_channels import Channels  # noqa: E402
from surface_streams import EventStreams  # noqa: E402

HOLD = 20  # wait bound (s); every test returns long before it


class RecordingCondition(threading.Condition):
    """Condition that reports parking and counts the wakes of its waiters."""

    made = []

    def __init__(self, lock=None):
        super().__init__(lock)
        self.parked = threading.Semaphore(0)
        self.wakes = 0
        RecordingCondition.made.append(self)

    def wait(self, timeout=None):
        self.parked.release()  # lock still held: a notifier cannot slip in
        got = super().wait(timeout)
        self.wakes += 1
        return got


class Base(unittest.TestCase):
    def setUp(self):
        RecordingCondition.made = []
        fake = types.SimpleNamespace(Lock=threading.Lock,
                                     Condition=RecordingCondition)
        patch = mock.patch.object(surface_channels, "threading", fake)
        patch.start()
        self.addCleanup(patch.stop)

    def park(self, target, *args, shared=None):
        """Start a waiter; returns (thread, out, cond) once it is blocked.
        ``shared`` is the condition an earlier waiter already created."""
        out = {}
        thread = threading.Thread(
            target=lambda: out.update(got=target(*args)), daemon=True)
        before = len(RecordingCondition.made)
        thread.start()
        while shared is None and len(RecordingCondition.made) == before:
            thread.join(0.001)  # creation of its condition, not a sleep-sync
        cond = shared or RecordingCondition.made[-1]
        self.assertTrue(cond.parked.acquire(timeout=10))
        return thread, out, cond


class TestChannelsIsolation(Base):
    def test_put_for_one_surface_never_wakes_another(self):
        ch = Channels()
        ta, outa, conda = self.park(ch.read_content, "a", 0, HOLD)
        tb, outb, condb = self.park(ch.read_content, "b", 0, HOLD)
        ch.put_content("a", "full", {"type": "text", "text": "A"})
        ta.join(10)
        self.assertFalse(ta.is_alive())
        self.assertEqual(outa["got"]["contents"]["full"]["content"]["text"], "A")
        self.assertEqual((conda.wakes, condb.wakes), (1, 0))
        self.assertTrue(tb.is_alive())
        ch.put_content("b", "full", {"type": "text", "text": "B"})
        tb.join(10)
        self.assertEqual(outb["got"]["contents"]["full"]["content"]["text"], "B")

    def test_event_for_one_surface_never_wakes_another(self):
        ch = Channels()
        ta, outa, conda = self.park(ch.read_events, "a", 0, HOLD)
        tb, outb, condb = self.park(ch.read_events, "b", 0, HOLD)
        ch.add_event("b", {"kind": "tap"})
        tb.join(10)
        self.assertEqual(len(outb["got"]["events"]), 1)
        self.assertEqual((conda.wakes, condb.wakes), (0, 1))
        ch.add_event("a", {"kind": "tap"})
        ta.join(10)
        self.assertEqual(len(outa["got"]["events"]), 1)

    def test_every_waiter_on_the_key_wakes(self):
        ch = Channels()
        first = self.park(ch.read_content, "a", 0, HOLD)
        second = self.park(ch.read_events, "a", 0, HOLD, shared=first[2])
        ch.put_content("a", "full", {"type": "text"})
        ch.add_event("a", {"kind": "tap"})
        for thread, out, _ in (first, second):
            thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertTrue(first[1]["got"]["contents"])
        self.assertTrue(second[1]["got"]["events"])

    def test_conditions_do_not_outlive_their_waiters(self):
        ch = Channels()
        thread, out, _ = self.park(ch.read_content, "a", 0, HOLD)
        self.assertEqual(list(ch._conds), ["a"])
        ch.put_content("a", "full", {"type": "text"})
        thread.join(10)
        self.assertEqual(ch._conds, {})
        ch.read_content("ghost", 0, 0.01)  # timed-out wait cleans up too
        self.assertEqual(ch._conds, {})

    def test_drop_wakes_the_waiter_and_reads_as_a_gone_surface(self):
        ch = Channels()
        ch.put_content("a", "full", {"type": "text"})
        thread, out, cond = self.park(ch.read_content, "a", 1, 0.5)
        ch.drop("a")
        thread.join(10)
        self.assertEqual(cond.wakes >= 1, True)
        self.assertEqual(out["got"], {"surface": "a", "seq": 0, "contents": {}})
        self.assertEqual(ch._conds, {})


class TestEventStreamsIsolation(Base):
    def make(self):
        streams = EventStreams()
        for sid in ("a", "b"):
            streams.declare({"id": sid, "name": sid})
        return streams

    def test_publish_to_one_stream_never_wakes_another(self):
        streams = self.make()
        ta, outa, conda = self.park(streams.read, "a", 0, HOLD)
        tb, outb, condb = self.park(streams.read, "b", 0, HOLD)
        streams.publish("a", {"type": "ping", "data": 1})
        ta.join(10)
        self.assertFalse(ta.is_alive())
        self.assertEqual([e["data"] for e in outa["got"]["events"]], [1])
        self.assertEqual((conda.wakes, condb.wakes), (1, 0))
        self.assertTrue(tb.is_alive())
        streams.publish("b", {"type": "ping", "data": 2})
        tb.join(10)
        self.assertEqual([e["data"] for e in outb["got"]["events"]], [2])
        self.assertEqual(streams._conds, {})

    def test_action_wakes_only_its_own_stream(self):
        streams = EventStreams()
        for sid in ("a", "b"):
            streams.declare({"id": sid, "name": sid,
                             "actions": [{"id": "ok", "label": "OK"}]})
        ta, outa, conda = self.park(streams.read_actions, "a", 0, HOLD)
        tb, outb, condb = self.park(streams.read_actions, "b", 0, HOLD)
        streams.add_action("b", {"action": "ok"})
        tb.join(10)
        self.assertEqual(len(outb["got"]["actions"]), 1)
        self.assertEqual((conda.wakes, condb.wakes), (0, 1))
        streams.add_action("a", {"action": "ok"})
        ta.join(10)
        self.assertEqual(len(outa["got"]["actions"]), 1)

    def test_remove_wakes_the_waiter_and_reads_as_a_gone_stream(self):
        streams = self.make()
        thread, out, cond = self.park(streams.read, "a", 0, 0.5)
        streams.remove("a")
        thread.join(10)
        self.assertGreaterEqual(cond.wakes, 1)
        self.assertEqual(out["got"], {"stream": "a", "seq": 0, "events": []})
        self.assertEqual(streams._conds, {})


if __name__ == "__main__":
    unittest.main()
