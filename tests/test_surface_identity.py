"""Panel identity and accessibility scale (surface_identity.py, surface_panel.py).

A logical panel (``panel.id``) outlives the surface it appears on: a returning
panel with no config of its own gets its remembered one back, trimmed to the
regions the new surface has. "Restart" means a second hub on the same file.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import surface_panel
from surface_hub import SurfaceHub
from surface_model import SurfaceError
from surface_store import SurfaceStore

TWO = [{"name": "left", "rect": {"x": 0, "y": 0, "w": 600, "h": 800}},
       {"name": "right", "rect": {"x": 600, "y": 0, "w": 600, "h": 800}}]
COLUMNS = [{"id": "agent", "stream": "agent", "view": "log", "region": "left"},
           {"id": "tools", "stream": "tools", "view": "calls", "region": "right"}]


def spec(sid="ipad", panel=None, **over):
    out = {"id": sid, "kind": "sidecar",
           "renderer": {"name": "web", "content_types": ["text"]},
           "viewport": {"width": 1200, "height": 800}, "regions": TWO,
           "ttl_seconds": 30}
    if panel is not None:
        out["panel"] = panel
    out.update(over)
    return out


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "surfaces.json")
        self.hub = self.boot()
        self.reg = self.hub.registry

    def boot(self):
        return SurfaceHub(store=SurfaceStore(self.path))

    def register(self, *a, **kw):
        rec, _ = self.reg.register(spec(*a, **kw))
        return rec


class TestScale(Case):
    def test_scale_is_stored_per_panel_and_defaults_to_one(self):
        rec = self.register("a", {"id": "p", "config": {"scale": 1.5}})
        self.assertEqual(rec["panel"]["config"]["scale"], 1.5)
        self.assertEqual(surface_panel.scale_of(rec["panel"]["config"]), 1.5)
        plain = self.register("b", {"id": "q"})
        self.assertEqual(surface_panel.scale_of(plain["panel"]["config"]), 1.0)

    def test_scale_is_bounded_and_numeric(self):
        for bad in (0, 0.49, 3.01, -1, "big", True, None, [2]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.register("a", {"config": {"scale": bad}})
        for good in (0.5, 1, 3, 2.25):
            self.register("a", {"config": {"scale": good}})

    def test_update_merges_scale_without_touching_columns(self):
        self.register("a", {"id": "p", "config": {"columns": COLUMNS}})
        rec = self.reg.update("a", {"panel": {"config": {"scale": 2}}})
        self.assertEqual(rec["panel"]["config"]["scale"], 2.0)
        self.assertEqual(len(rec["panel"]["config"]["columns"]), 2)

    def test_two_panels_keep_their_own_scale(self):
        self.register("a", {"config": {"scale": 2}})
        self.register("b", {"config": {"scale": 0.75}})
        got = {s["id"]: surface_panel.scale_of(s["panel"]["config"])
               for s in self.reg.list() if s["id"] in ("a", "b")}
        self.assertEqual(got, {"a": 2.0, "b": 0.75})


class TestRecognition(Case):
    def test_anonymous_and_unknown_ids_are_reported_not_guessed(self):
        self.assertEqual(self.register("a")["panel"]["identity"], "anonymous")
        rec = self.register("b", {"id": "never-seen"})
        self.assertEqual(rec["panel"]["identity"], "new")
        self.assertEqual(rec["panel"]["config"]["columns"], [])
        self.assertNotIn("restored", rec["panel"])

    def test_a_returning_panel_gets_its_config_back_on_another_surface(self):
        self.register("ipad", {"id": "studio", "config": {"columns": COLUMNS, "scale": 1.5}})
        self.reg.deregister("ipad")
        rec = self.register("ipad-2", {"id": "studio"})
        self.assertEqual(rec["panel"]["identity"], "restored")
        self.assertEqual(rec["panel"]["restored"]["from_surface"], "ipad")
        self.assertEqual(rec["panel"]["restored"]["dropped"], [])
        self.assertEqual(rec["panel"]["config"]["scale"], 1.5)
        self.assertEqual([c["id"] for c in rec["panel"]["config"]["columns"]],
                         ["agent", "tools"])
        self.assertEqual(self.reg.get("ipad-2")["panel"]["config"]["scale"], 1.5)

    def test_a_config_sent_by_the_caller_wins_and_is_remembered(self):
        self.register("a", {"id": "studio", "config": {"scale": 2}})
        kept = self.register("b", {"id": "studio", "config": {"scale": 1.25}})
        self.assertEqual(kept["panel"]["identity"], "kept")
        self.assertEqual(self.register("c", {"id": "studio"})["panel"]["config"]["scale"],
                         1.25)

    def test_columns_for_missing_regions_are_dropped_not_the_whole_restore(self):
        self.register("big", {"id": "studio", "config": {"columns": COLUMNS, "scale": 2}})
        small = self.register("phone", {"id": "studio"},
                              viewport={"width": 600, "height": 800},
                              regions=[TWO[0]])
        self.assertEqual(small["panel"]["identity"], "restored")
        self.assertEqual(small["panel"]["restored"]["dropped"], ["tools"])
        self.assertEqual([c["id"] for c in small["panel"]["config"]["columns"]], ["agent"])
        self.assertEqual(small["panel"]["config"]["scale"], 2.0)
        # the smaller surface did not shrink what the bigger one configured
        again = self.register("big-2", {"id": "studio"})
        self.assertEqual([c["id"] for c in again["panel"]["config"]["columns"]],
                         ["agent", "tools"])

    def test_the_restore_report_is_told_once(self):
        self.register("a", {"id": "studio", "config": {"scale": 2}})
        self.register("b", {"id": "studio"})
        self.assertNotIn("identity", self.reg.get("b")["panel"])

    def test_update_remembers_the_new_config(self):
        self.register("a", {"id": "studio"})
        self.reg.update("a", {"panel": {"config": {"scale": 2.5}}})
        self.assertEqual(self.register("b", {"id": "studio"})["panel"]["config"]["scale"], 2.5)

    def test_an_empty_registration_does_not_erase_the_memory(self):
        self.register("a", {"id": "studio", "config": {"scale": 2}})
        self.register("a", {"id": "studio"})  # re-register, nothing sent
        self.assertEqual(self.register("b", {"id": "studio"})["panel"]["config"]["scale"], 2.0)


class TestMigration(Case):
    def test_newer_or_unmigratable_configs_are_left_alone(self):
        self.register("a", {"id": "studio", "config": {"scale": 2}})
        stored = self.reg.identities._seen["studio"]
        stored["version"] = surface_panel.CONFIG_VERSION + 1
        rec = self.register("b", {"id": "studio"})
        self.assertEqual(rec["panel"]["identity"], "unusable")
        self.assertEqual(rec["panel"]["config"]["columns"], [])
        self.assertEqual(self.reg.identities._seen["studio"]["config"]["scale"], 2)
        stored["version"] = 0  # no step registered from 0
        self.assertEqual(self.register("c", {"id": "studio"})["panel"]["identity"], "unusable")

    def test_an_older_config_walks_up_the_registered_steps(self):
        self.register("a", {"id": "studio", "config": {"scale": 2}})
        self.reg.identities._seen["studio"]["version"] = 0
        surface_panel.MIGRATIONS[0] = lambda c: dict(c, scale=c["scale"] / 2)
        self.addCleanup(surface_panel.MIGRATIONS.pop, 0, None)
        rec = self.register("b", {"id": "studio"})
        self.assertEqual(rec["panel"]["identity"], "restored")
        self.assertEqual(rec["panel"]["config"]["scale"], 1.0)


class TestPersistence(Case):
    def test_identity_survives_a_restart(self):
        self.register("ipad", {"id": "studio", "config": {"columns": COLUMNS, "scale": 1.75}})
        self.reg.deregister("ipad")
        hub = self.boot()
        self.assertEqual(hub.restored["identities"], 1)
        rec, _ = hub.registry.register(spec("wall", {"id": "studio"}))
        self.assertEqual(rec["panel"]["identity"], "restored")
        self.assertEqual(rec["panel"]["config"]["scale"], 1.75)

    def test_a_malformed_identity_is_dropped_and_counted(self):
        known, bad = self.reg.identities.restore({
            "ok": {"version": 1, "config": {"scale": 2}, "surface": "x", "at": 1},
            "Bad Id": {"version": 1, "config": {}},
            "nover": {"config": {}}, "nocfg": {"version": 1, "config": []}})
        self.assertEqual((known, bad), (1, 3))

    def test_the_memory_is_bounded(self):
        from surface_identity import MAX_IDENTITIES
        for i in range(MAX_IDENTITIES + 5):
            self.reg.identities.remember(
                {"id": "p%d" % i, "version": 1, "config": {"scale": 2, "columns": []}},
                "s", 1.0)
        self.assertEqual(len(self.reg.identities.export()), MAX_IDENTITIES)
        self.assertNotIn("p0", self.reg.identities.export())


class TestBuiltinPanelScale(Case):
    def test_the_panel_accepts_a_scale_like_any_other_panel(self):
        self.reg.register_builtin("panel", lambda: dict(
            name="p", kind="panel",
            renderer={"name": "x", "content_types": ["text"]},
            viewport={"width": 1920, "height": 1080}))
        rec = self.reg.update("panel", {"panel": {"config": {"scale": 2}}})
        self.assertEqual(rec["panel"]["config"]["scale"], 2.0)
        with self.assertRaises(ValueError):
            self.reg.update("panel", {"panel": {"config": {"scale": 9}}})
        with self.assertRaises(SurfaceError):
            self.reg.update("panel", {"name": "renamed"})


if __name__ == "__main__":
    unittest.main()
