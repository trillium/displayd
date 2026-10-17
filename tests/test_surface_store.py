"""Registry persistence across restart (surface_store.py + hub wiring).

Pure tests drive a fake clock and a temp file; the last class restarts the
real headless daemon against the same state directory. "Restart" means a
second hub / daemon built on the same path.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import surface_store as store_mod
from surface_hub import SurfaceHub
from surface_model import SurfaceError
from surface_store import FORMAT, SurfaceStore


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def wide(**over):
    spec = {"id": "wall", "kind": "sidecar",
            "renderer": {"name": "web", "content_types": ["text"]},
            "viewport": {"width": 1200, "height": 800},
            "regions": [{"name": "left", "rect": {"x": 0, "y": 0, "w": 600, "h": 800}},
                        {"name": "right", "rect": {"x": 600, "y": 0, "w": 600, "h": 800}}],
            "labels": {"room": "studio"}, "ttl_seconds": 30,
            "panel": {"id": "wall-1", "config": {"columns": [
                {"id": "agent", "stream": "agent", "view": "log", "region": "left"},
                {"id": "tools", "stream": "tools", "view": "calls", "region": "right",
                 "options": {"rows": 5}}]}}}
    spec.update(over)
    return spec


STREAM = {"id": "agent", "name": "Agent", "event_types": ["say"],
          "views": ["log"], "actions": [{"id": "approve", "label": "Approve"}]}


def read(path, mode="r"):
    with open(path, mode) as fh:
        return fh.read()


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "state", "surfaces.json")
        self.clock = Clock()

    def boot(self, **kw):
        """A fresh hub on the same file: the daemon restarting."""
        store = SurfaceStore(self.path, clock=self.clock, **kw)
        return SurfaceHub(store=store, clock=self.clock)

    def doc(self):
        with open(self.path, encoding="utf-8") as fh:
            return json.load(fh)


class TestRoundTrip(StoreCase):
    def test_surface_columns_and_stream_survive_restart(self):
        hub = self.boot()
        hub.registry.register(wide())
        hub.streams.declare(STREAM)
        before = hub.registry.get("wall")
        hub = self.boot()
        after = hub.registry.get("wall")
        self.assertEqual(after["panel"], before["panel"])
        self.assertEqual([c["id"] for c in after["panel"]["config"]["columns"]],
                         ["agent", "tools"])
        for key in ("regions", "viewport", "touch", "labels", "revision",
                    "registered_at", "expires_at", "renderer"):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(hub.streams.get("agent")["actions"][0]["id"], "approve")
        self.assertEqual(hub.restored["restored"], 1)
        self.assertEqual(hub.restored["streams"], 1)

    def test_update_and_deregister_are_persisted_immediately(self):
        hub = self.boot()
        hub.registry.register(wide())
        hub.registry.register(wide(id="other"))
        hub.registry.update("wall", {"labels": {"room": "kitchen"}})
        hub.registry.deregister("other")
        ids = {s["id"]: s for s in self.doc()["surfaces"]}
        self.assertEqual(list(ids), ["wall"])
        self.assertEqual(ids["wall"]["labels"], {"room": "kitchen"})
        self.assertEqual(ids["wall"]["revision"], 2)
        hub = self.boot()
        with self.assertRaises(SurfaceError) as err:
            hub.registry.get("other")
        self.assertEqual(err.exception.status, 404)  # left on purpose: no tombstone

    def test_stream_remove_is_persisted(self):
        hub = self.boot()
        hub.streams.declare(STREAM)
        hub.streams.remove("agent")
        self.assertEqual(self.boot().streams.list(), [])

    def test_columns_replace_through_update_persists(self):
        hub = self.boot()
        hub.registry.register(wide())
        hub.registry.update("wall", {"panel": {"config": {"columns": [
            {"id": "solo", "stream": "agent", "view": "log", "region": "left"}]}}})
        cols = self.boot().registry.get("wall")["panel"]["config"]["columns"]
        self.assertEqual([c["id"] for c in cols], ["solo"])

    def test_no_file_means_empty_and_first_boot_commits_the_file(self):
        hub = self.boot()
        self.assertEqual(hub.registry.list(), [])
        self.assertEqual(self.doc()["format"], FORMAT)
        self.assertTrue(hub.persistence()["saved_at"])


class TestLeases(StoreCase):
    def test_lease_still_holding_is_restored_with_remaining_time(self):
        self.boot().registry.register(wide(ttl_seconds=30))
        self.clock.t += 10
        hub = self.boot()
        self.assertEqual(hub.registry.get("wall")["expires_in"], 20.0)
        self.assertEqual(hub.restored["expired"], 0)

    def test_stale_lease_becomes_a_tombstone_not_a_ghost(self):
        self.boot().registry.register(wide(ttl_seconds=30))
        self.clock.t += 31  # the daemon was down past the lease
        hub = self.boot()
        self.assertEqual(hub.restored, {"restored": 0, "expired": 1, "invalid": 0,
                                        "streams": 0, "streams_invalid": 0,
                                        "panels": 0, "panels_invalid": 0})
        self.assertEqual(hub.registry.list(), [])
        with self.assertRaises(SurfaceError) as err:
            hub.registry.get("wall")
        self.assertEqual(err.exception.status, 410)  # re-register, not unknown
        with self.assertRaises(SurfaceError) as err:
            hub.registry.heartbeat("wall")
        self.assertEqual(err.exception.status, 410)
        hub.registry.register(wide())  # and the owner can come straight back
        self.assertEqual(hub.registry.get("wall")["revision"], 1)

    def test_tombstones_survive_later_restarts(self):
        self.boot().registry.register(wide(ttl_seconds=30))
        self.clock.t += 31
        self.boot()  # judged stale here, written back as a tombstone
        with self.assertRaises(SurfaceError) as err:
            self.boot().registry.get("wall")
        self.assertEqual(err.exception.status, 410)

    def test_runtime_expiry_is_also_remembered_across_restart(self):
        hub = self.boot()
        hub.registry.register(wide(ttl_seconds=5))
        self.clock.t += 6
        hub.registry.list()  # sweep -> tombstone in memory
        hub.registry.register(wide(id="late", ttl_seconds=30))  # writes it out
        with self.assertRaises(SurfaceError) as err:
            self.boot().registry.get("wall")
        self.assertEqual(err.exception.status, 410)

    def test_heartbeats_are_coalesced_but_structure_is_immediate(self):
        hub = self.boot(flush_interval=5.0)
        hub.registry.register(wide(ttl_seconds=60))
        writes = []
        real = hub.store.save
        hub.store.save = lambda: writes.append(1) or real()
        hub.registry.heartbeat("wall")
        hub.registry.heartbeat("wall")
        self.assertEqual(writes, [])  # within the interval, only marked dirty
        self.assertTrue(hub.persistence()["pending_write"])
        self.clock.t += 6
        hub.registry.heartbeat("wall")
        self.assertEqual(len(writes), 1)
        self.assertFalse(hub.persistence()["pending_write"])
        hub.registry.update("wall", {"name": "Wall"})
        self.assertEqual(len(writes), 2)  # urgent never waits

    def test_flush_writes_a_waiting_heartbeat(self):
        hub = self.boot(flush_interval=5.0)
        hub.registry.register(wide(ttl_seconds=60))
        hub.registry.heartbeat("wall")
        stored = self.doc()["surfaces"][0]["expires_at"]
        self.clock.t += 1
        hub.registry.heartbeat("wall")
        self.assertEqual(self.doc()["surfaces"][0]["expires_at"], stored)
        hub.store.flush()
        self.assertEqual(self.doc()["surfaces"][0]["expires_at"], stored + 1)


class TestSafeFile(StoreCase):
    def test_failed_write_keeps_the_old_file_and_the_api_running(self):
        hub = self.boot()
        hub.registry.register(wide())
        good = read(self.path, "rb")
        with mock.patch("surface_store.os.replace", side_effect=OSError("disk full")):
            hub.registry.register(wide(id="two"))  # must not raise
            self.assertIn("disk full", hub.persistence()["last_error"])
        self.assertEqual(read(self.path, "rb"), good)
        self.assertEqual([f for f in os.listdir(os.path.dirname(self.path))
                          if ".tmp" in f], [])
        hub.registry.register(wide(id="three"))  # recovers on the next change
        self.assertIsNone(hub.persistence()["last_error"])
        self.assertEqual({s["id"] for s in self.doc()["surfaces"]},
                         {"wall", "two", "three"})

    def test_write_goes_through_a_temp_file_in_the_same_directory(self):
        hub = self.boot()
        seen = []
        real = os.replace
        with mock.patch("surface_store.os.replace",
                        side_effect=lambda a, b: seen.append((a, b)) or real(a, b)):
            hub.registry.register(wide())
        (src, dst), = seen
        self.assertEqual(os.path.dirname(src), os.path.dirname(dst))
        self.assertNotEqual(src, dst)

    def test_corrupt_file_is_kept_aside_never_overwritten(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as fh:
            fh.write("{not json")
        hub = self.boot()
        aside = hub.persistence()["loaded"]["moved_aside"]
        self.assertEqual(read(aside), "{not json")
        self.assertEqual(hub.registry.list(), [])
        self.assertEqual(self.doc()["surfaces"], [])  # a fresh, valid file

    def test_generation_of_an_unreadable_file_is_still_honoured(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as fh:
            json.dump({"format": FORMAT + 1, "generation": 4}, fh)
        self.assertEqual(self.boot().persistence()["generation"], 5)

    def test_newer_format_is_kept_aside_untouched(self):
        os.makedirs(os.path.dirname(self.path))
        future = {"format": FORMAT + 1, "surfaces": [{"id": "x"}], "generation": 4}
        with open(self.path, "w") as fh:
            json.dump(future, fh)
        hub = self.boot()
        aside = hub.persistence()["loaded"]["moved_aside"]
        self.assertEqual(json.loads(read(aside)), future)
        self.assertIn("newer", hub.persistence()["loaded"]["error"])
        self.assertEqual(hub.registry.list(), [])

    def test_two_unreadable_files_do_not_clobber_each_other(self):
        os.makedirs(os.path.dirname(self.path))
        for text in ("one{", "two{"):
            with open(self.path, "w") as fh:
                fh.write(text)
            aside = self.boot().persistence()["loaded"]["moved_aside"]
            self.assertEqual(read(aside), text)
            os.unlink(self.path)
        self.assertEqual(len([f for f in os.listdir(os.path.dirname(self.path))
                              if "unreadable" in f]), 2)

    def test_invalid_records_are_dropped_and_counted_the_rest_load(self):
        hub = self.boot()
        hub.registry.register(wide())
        hub.streams.declare(STREAM)
        doc = self.doc()
        doc["surfaces"].append({"id": "BAD ID", "renderer": 3})
        doc["surfaces"].append("junk")
        doc["streams"].append({"id": "Bad Stream"})
        with open(self.path, "w") as fh:
            json.dump(doc, fh)
        hub = self.boot()
        self.assertEqual((hub.restored["restored"], hub.restored["invalid"]), (1, 2))
        self.assertEqual((hub.restored["streams"], hub.restored["streams_invalid"]),
                         (1, 1))
        self.assertEqual([s["id"] for s in hub.registry.list()], ["wall"])


class TestMigration(StoreCase):
    def test_older_format_is_walked_up_by_the_migration_table(self):
        os.makedirs(os.path.dirname(self.path))
        old = {"format": 0, "generation": 2, "sidecars": [wide()]}  # old name

        def v0_to_v1(doc):
            rec = dict(wide(), revision=1, registered_at=self.clock.t,
                       updated_at=self.clock.t, last_seen=self.clock.t,
                       expires_at=self.clock.t + 30)
            return {"format": 1, "generation": doc["generation"], "surfaces": [rec]}

        with open(self.path, "w") as fh:
            json.dump(old, fh)
        with mock.patch.dict(store_mod.MIGRATIONS, {0: v0_to_v1}):
            hub = self.boot()
        self.assertEqual(hub.registry.get("wall")["panel"]["id"], "wall-1")
        self.assertEqual(self.doc()["format"], FORMAT)  # rewritten at today's format
        self.assertNotIn("sidecars", self.doc())

    def test_older_format_with_no_migration_is_kept_aside(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as fh:
            json.dump({"format": 0, "surfaces": []}, fh)
        hub = self.boot()
        self.assertIn("no migration", hub.persistence()["loaded"]["error"])
        self.assertTrue(os.path.exists(hub.persistence()["loaded"]["moved_aside"]))

    def test_migration_runs_through_every_step(self):
        steps = []

        def bump(n):
            def run(doc):
                steps.append(n)
                return dict(doc, format=n + 1)
            return run

        with mock.patch.object(store_mod, "FORMAT", 3), \
                mock.patch.dict(store_mod.MIGRATIONS, {1: bump(1), 2: bump(2)}):
            self.assertEqual(store_mod.migrate({"format": 1})["format"], 3)
        self.assertEqual(steps, [1, 2])

    def test_panel_config_is_revalidated_by_current_rules_on_load(self):
        hub = self.boot()
        hub.registry.register(wide())
        doc = self.doc()
        doc["surfaces"][0]["panel"]["config"]["columns"][0]["region"] = "gone"
        with open(self.path, "w") as fh:
            json.dump(doc, fh)
        hub = self.boot()  # the column now names a region the surface lacks
        self.assertEqual(hub.restored["invalid"], 1)
        self.assertEqual(hub.registry.list(), [])


class TestSequences(StoreCase):
    def test_stream_seq_never_regresses_across_restart(self):
        hub = self.boot()
        hub.streams.declare(STREAM)
        for _ in range(3):
            last = hub.streams.publish("agent", {"type": "say", "data": "x"})["seq"]
        hub = self.boot()
        new = hub.streams.publish("agent", {"type": "say", "data": "y"})["seq"]
        self.assertGreater(new, last)
        # a consumer holding the old cursor sees only the new event
        seen = hub.streams.read("agent", since=last)["events"]
        self.assertEqual([e["data"] for e in seen], ["y"])

    def test_generation_advances_on_every_boot_even_with_no_changes(self):
        gens = [self.boot().persistence()["generation"] for _ in range(3)]
        self.assertEqual(gens, [0, 1, 2])

    def test_surface_content_and_touch_seq_follow_the_generation(self):
        hub = self.boot()
        hub.registry.register(wide(touch={"supported": True,
                                          "interactions": ["tap"]}))
        first = hub.deliver("wall/left", {"type": "text", "text": "a"})["delivered"][0]
        hub = self.boot()
        again = hub.deliver("wall/left", {"type": "text", "text": "b"})["delivered"][0]
        self.assertGreater(again["seq"], first["seq"])
        ev = hub.registry.touch("wall", {"interaction": "tap", "x": 10, "y": 10})
        self.assertGreater(ev["seq"], first["seq"])

    def test_first_generation_keeps_the_small_seqs_existing_clients_know(self):
        hub = self.boot()
        hub.streams.declare(STREAM)
        self.assertEqual(hub.streams.publish("agent", {"type": "say"})["seq"], 1)

    def test_events_themselves_are_not_persisted(self):
        hub = self.boot()
        hub.streams.declare(STREAM)
        hub.streams.publish("agent", {"type": "say", "data": "x"})
        self.assertEqual(self.boot().streams.read("agent")["events"], [])
        self.assertNotIn("events", json.dumps(self.doc()))


class TestPathRules(unittest.TestCase):
    def test_default_path_is_state_next_to_the_policy_file(self):
        from daemon_surfaces import surfaces_path
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DISPLAYD_SURFACES", None)
            self.assertEqual(surfaces_path("/srv/displayd/policy.json"),
                             "/srv/displayd/state/surfaces.json")

    def test_env_overrides_the_path(self):
        from daemon_surfaces import surfaces_path
        with mock.patch.dict(os.environ, {"DISPLAYD_SURFACES": "/x/s.json"}):
            self.assertEqual(surfaces_path("/srv/displayd/policy.json"), "/x/s.json")

    def test_state_dir_is_ignored_by_git_and_never_deleted_by_deploy(self):
        root = os.path.join(os.path.dirname(__file__), os.pardir)
        self.assertIn("/state/", read(os.path.join(root, ".gitignore")))
        deploy = read(os.path.join(root, "deploy.sh"))
        self.assertNotIn("--delete", [w for w in deploy.split() if w.startswith("--del")])


class TestDaemonRestart(unittest.TestCase):
    """The real headless daemon, twice, on one policy path (and so one
    ``state/`` directory)."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"DISPLAYD_FAKE_FB": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("DISPLAYD_SURFACES", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def daemon(self):
        import displayd
        old = displayd.DAEMON
        self.addCleanup(setattr, displayd, "DAEMON", old)
        daemon = displayd.DisplayDaemon(
            policy_path=os.path.join(self.tmp.name, "policy.json"),
            feedback_path=os.path.join(self.tmp.name, "feedback.jsonl"))
        self.addCleanup(daemon.clear)
        return daemon

    def test_registry_survives_a_daemon_restart(self):
        first = self.daemon()
        first.surface_hub.registry.register(wide())
        first.surface_hub.streams.declare(STREAM)
        second = self.daemon()
        hub = second.surface_hub
        self.assertEqual([s["id"] for s in hub.registry.list()], ["panel", "wall"])
        self.assertEqual(hub.registry.get("wall")["panel"]["id"], "wall-1")
        self.assertEqual(hub.streams.get("agent")["views"], ["log"])
        state = second.surfaces_state()
        self.assertEqual(state["restored"]["restored"], 1)
        self.assertEqual(state["generation"], 1)
        self.assertTrue(state["path"].endswith(os.path.join("state", "surfaces.json")))

    def test_state_key_is_none_until_the_hub_exists(self):
        self.assertIsNone(self.daemon().surfaces_state())

    def test_a_stored_record_cannot_shadow_the_panel(self):
        first = self.daemon()
        first.surface_hub.registry.register(wide())
        path = first.surface_hub.store.path
        doc = json.loads(read(path))
        doc["surfaces"].append(dict(doc["surfaces"][0], id="panel"))
        with open(path, "w") as fh:
            json.dump(doc, fh)
        ids = [s["id"] for s in self.daemon().surface_hub.registry.list()]
        self.assertEqual(sorted(ids), ["panel", "wall"])
        self.assertEqual(self.daemon().surface_hub.registry.get("panel")["builtin"], True)


if __name__ == "__main__":
    unittest.main()
