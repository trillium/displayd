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
