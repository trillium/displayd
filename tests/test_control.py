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

KNOWN_TYPES = {"string", "integer", "number", "boolean"}


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


class TestExposureDefaults(unittest.TestCase):
    def test_default_bind_is_loopback(self):
        self.assertEqual(displayd.BIND, "127.0.0.1",
                         "default bind must stay loopback: the API has no auth")


if __name__ == "__main__":
    unittest.main()
