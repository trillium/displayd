"""Control-surface tests for displayd. No framebuffer needed: nothing here
instantiates DisplayDaemon or touches /dev/fb0.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import displayd

# Selection params go through displayd.validate_params/validate_value, so the
# advertised PARAMS types are exactly the types the daemon validates
# (touch_confidence.regions is an array).
KNOWN_TYPES = set(displayd.INPUT_TYPES)


class TestRenderers(unittest.TestCase):
    def test_bundled_renderers_load(self):
        found = displayd.load_renderers(displayd.RENDERER_DIR)
        for name in ("text", "image", "solid", "clock", "life"):
            self.assertIn(name, found, "bundled renderer %r missing" % name)
            self.assertIn("module", found[name], "%r failed to import: %s"
                          % (name, found[name].get("broken")))

    def test_param_schemas_are_well_formed(self):
        found = displayd.load_renderers(displayd.RENDERER_DIR)
        for name, entry in found.items():
            if "module" not in entry:
                continue
            params = entry["params"]
            self.assertIsInstance(params, dict, "%r PARAMS must be a dict" % name)
            for pname, spec in params.items():
                self.assertIsInstance(spec, dict, "%r.%r spec must be a dict"
                                      % (name, pname))
                self.assertIn(spec.get("type"), KNOWN_TYPES,
                              "%r.%r has unknown type %r" % (name, pname, spec.get("type")))

    def test_new_renderer_appears_with_no_ui_edit(self):
        """Dropping a file into renderers/ is the whole extension step: the
        control page reads /renderers at load, so no UI change is needed."""
        plugin = ('NAME = "beacon"\n'
                  'DESCRIPTION = "Test beacon"\n'
                  'STATIC = True\n'
                  'PARAMS = {"note": {"type": "string", "help": "what to say"}}\n'
                  'def run(screen, params, stop):\n'
                  '    pass\n')
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "beacon.py")
            with open(path, "w") as fh:
                fh.write(plugin)
            found = displayd.load_renderers(tmp)
            self.assertIn("beacon", found)
            self.assertIn("module", found["beacon"])
            self.assertEqual(found["beacon"]["params"]["note"]["type"], "string")
        # And the page itself names no renderer: it builds the list dynamically.
        for hard_coded in ('value="beacon"', ">beacon<"):
            self.assertNotIn(hard_coded, displayd.CONTROL_PAGE)


class TestControlPage(unittest.TestCase):
    def test_page_is_self_contained(self):
        page = displayd.CONTROL_PAGE
        self.assertIn("<!DOCTYPE html>", page)
        self.assertNotIn('src="http', page.replace("https://", ""))
        self.assertNotIn("href=\"http", page)

    def test_page_drives_everything_through_the_api(self):
        page = displayd.CONTROL_PAGE
        for endpoint in ("/health", "/state", "/renderers", "/snapshot",
                         "/show", "/clear", "/screen/on", "/screen/off"):
            self.assertIn(endpoint, page, "page never talks to %s" % endpoint)

    def test_page_builds_renderer_list_dynamically(self):
        page = displayd.CONTROL_PAGE
        self.assertIn("createElement(\"option\")", page)
        self.assertIn("/renderers", page)
        for name in ("life", "clock", "text", "image", "solid"):
            self.assertNotIn('value="%s"' % name, page,
                             "renderer %r looks hardcoded in the page" % name)

    def test_page_covers_required_controls(self):
        page = displayd.CONTROL_PAGE.lower()
        for token in ("preview", "power", "last error", "backlight", "blank"):
            self.assertIn(token, page, "page has no %r control" % token)

    def test_handler_routes_root_to_html(self):
        import inspect
        src = inspect.getsource(displayd.Handler.do_GET)
        self.assertIn('"/"', src)
        self.assertIn("text/html", src)


class TestPhoneFirstRebuild(unittest.TestCase):
    """The v1 phone-first rebuild: one-tap views, playback, proof,
    feedback, deploy stamp without scrolling, tap-action list.

    Run from the repo root:  python3 -m unittest tests.test_control -v
    """

    def test_one_tap_view_grid(self):
        page = displayd.CONTROL_PAGE
        self.assertIn('id="viewgrid"', page)
        # Buttons are built from the live /renderers list, never hardcoded.
        self.assertIn('createElement("button")', page)
        self.assertIn("oneTapShow", page)
        for name in ("life", "clock", "text", "image", "solid"):
            self.assertNotIn('value="%s"' % name, page)
            self.assertNotIn('>%s<' % name, page)

    def test_big_four_pinned_top(self):
        import re
        m = re.search(r"PINNED\s*=\s*\[(.*?)\]", displayd.CONTROL_PAGE)
        self.assertIsNotNone(m, "page has no PINNED order list")
        pinned = re.findall(r'"([^"]+)"', m.group(1))
        self.assertEqual(pinned, ["clock", "chat", "row", "stream"])

    def test_current_view_highlighted(self):
        page = displayd.CONTROL_PAGE
        self.assertIn("markCurrent", page)
        self.assertIn('"active"', page)
        # The sticky top bar names the current view on every load.
        self.assertIn('id="tb-view"', page)

    def test_playback_controls(self):
        page = displayd.CONTROL_PAGE
        for token in ('id="plpause"', 'id="plresume"', 'id="plnext"',
                      'id="pl-status"',
                      '"/playlist/pause"', '"/playlist/resume"',
                      '"/playlist/next"', '"/playlist"'):
            self.assertIn(token, page, "playback missing %r" % token)

    def test_proof_controls(self):
        page = displayd.CONTROL_PAGE
        for token in ('id="rl-sha"', 'id="reload"', 'id="reload-result"',
                      '"/reload"', 'commit_url',
                      'id="dep-when"', 'id="dep-sha"', 'id="dep-who"'):
            self.assertIn(token, page, "proof section missing %r" % token)

    def test_deploy_stamp_visible_without_scrolling(self):
        page = displayd.CONTROL_PAGE
        self.assertIn('id="topbar"', page)
        self.assertIn('id="tb-dep"', page)
        self.assertIn("sticky", page)
        # The top bar (with the deploy stamp) precedes all sections.
        self.assertLess(page.index('id="topbar"'), page.index("<h2>"))
        self.assertLess(page.index('id="tb-dep"'), page.index("<h2>"))

    def test_feedback_rate_and_summary(self):
        page = displayd.CONTROL_PAGE
        for token in ('id="fb-view"', 'id="ratebtns"', 'id="fbsend"',
                      'id="fb-notes"', 'id="fb-summary"',
                      'data-rating', '"/feedback"', '"/feedback/summary"'):
            self.assertIn(token, page, "feedback section missing %r" % token)

    def test_tap_action_list_matches_touch_allowlist(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__),
                                        os.pardir))
        import touch
        page = displayd.CONTROL_PAGE
        for action in touch.ALLOWED_ACTIONS:
            self.assertIn(action, page,
                          "tap-action list omits %r" % action)

    def test_layout_style_picker(self):
        """Styles, their slots and each slot's views come from the daemon;
        nothing in the layout script hardcodes a renderer."""
        import control_page_script_layout as lay
        page = displayd.CONTROL_PAGE
        for token in ('id="laystyles"', 'id="layslots"', 'id="layapply"',
                      'id="layclear"', 'id="lay-status"',
                      '"/layout/presets"', '"/layout"'):
            self.assertIn(token, page, "layout section missing %r" % token)
        self.assertIn(lay._SCRIPT_LAYOUT, page)
        # No preset name and no renderer name is spelled out: the buttons
        # and every slot's options come from GET /layout/presets.
        for name in ("full", "split-50-50", "split-50-50-columns",
                     "15-70-15"):
            self.assertNotIn('data-style="%s"' % name, page)
            self.assertNotIn(">%s<" % name, page)
        for name in ("picker", "row", "clock", "chat"):
            self.assertNotIn(">%s<" % name, lay._SCRIPT_LAYOUT)
        self.assertIn("slot.views", lay._SCRIPT_LAYOUT)
        self.assertNotIn("SCHEMAS", lay._SCRIPT_LAYOUT,
                         "the layout slots must not fall back to all views")

    def test_layout_applies_and_clears_through_existing_shapes(self):
        import control_page_script_layout as lay
        src = lay._SCRIPT_LAYOUT
        self.assertIn('preset: style.name', src)
        self.assertIn('views: views', src)
        self.assertIn('method: "DELETE"', src)
        # Geometry and capability fit stay the daemon's: the page never
        # sends regions or rects of its own.
        self.assertNotIn("regions:", src)
        self.assertNotIn("rect", src)

    def test_layout_shows_the_live_style(self):
        import control_page_script_layout as lay
        src = lay._SCRIPT_LAYOUT
        self.assertIn('classList.add("live")', src)
        self.assertIn("LAY_LIVE.regions", src)
        self.assertIn("layout: loading", displayd.CONTROL_PAGE)

    def test_client_script_parses(self):
        """The whole page is one <script>; a syntax error in any half
        would leave the phone with dead controls and no server-side
        signal. Skipped where node is unavailable."""
        import shutil
        import subprocess
        import tempfile
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        js = displayd.CONTROL_PAGE.split("<script>", 1)[1]
        js = js.rsplit("</script>", 1)[0]
        self.assertTrue(js.strip(), "no client script in the page")
        with tempfile.NamedTemporaryFile("w", suffix=".js",
                                         delete=False) as fh:
            fh.write(js)
            path = fh.name
        try:
            done = subprocess.run([node, "--check", path],
                                  capture_output=True, text=True)
        finally:
            os.unlink(path)
        self.assertEqual(done.returncode, 0,
                         "client script does not parse:\n" + done.stderr)

    def test_thumb_targets_single_column(self):
        page = displayd.CONTROL_PAGE
        self.assertIn("max-width: 520px", page)
        self.assertTrue("min-height: 48px" in page
                        or "min-height: 52px" in page,
                        "no thumb-sized button targets")
        self.assertIn("min-height: 56px", page)

    def test_page_uses_only_existing_endpoints(self):
        """Every path the page fetches must already exist on the daemon:
        presentation-only means no new endpoints."""
        import inspect
        import re
        page = displayd.CONTROL_PAGE
        paths = set(re.findall(r'"(/(?:health|state|renderers|snapshot|show|'
                               r'clear|screen/[a-z]+|policy|playlist(?:/[a-z]+)?|'
                               r'layout(?:/[a-z]+)?|'
                               r'notify|reload|feedback(?:/[a-z]+)?))"', page))
        self.assertTrue(paths, "no API paths found in page")
        routes = (inspect.getsource(displayd.Handler.do_GET)
                  + inspect.getsource(displayd.Handler.do_POST))
        for path in sorted(paths):
            base = path.split("?")[0]
            self.assertIn('"%s"' % base, routes,
                          "page calls %r which has no daemon route" % base)


class TestExposureDefaults(unittest.TestCase):
    def test_default_bind_is_loopback(self):
        self.assertEqual(displayd.BIND, "127.0.0.1",
                         "default bind must stay loopback: the API has no auth")


if __name__ == "__main__":
    unittest.main()
