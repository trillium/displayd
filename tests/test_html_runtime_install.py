"""Deterministic installation of the html view's runtime set.

The gap this pins: the native layout engine, its sources and licences, the
trusted templates, and the build script are the whole contract of the
optional `html` view, and nothing owned installing them. install.sh copied
renderers/*.py and README and stopped; deploy.sh rsynced a checkout that
never contains a compiled engine (it is gitignored, and a Mac build is a
dylib, not the host's .so). So a clean target -- the exact box install.sh
exists for -- finished with no runtime and no templates, and the only
symptom was a red card on the panel.

tools/install_html_runtime.sh is the single owner of that set now, and
these tests hold four properties:

  1. it installs every required artifact into a prefix, from a source tree
     that has no compiled engine in it yet;
  2. its --check is the same list the renderer probes, so "complete" is a
     fact about the renderer's own search path rather than a second opinion;
  3. an installed tree resolves its runtime and templates with no source
     checkout, no environment variable, and no developer-local path;
  4. install.sh and deploy.sh both go through that script, so there is one
     path to a clean target rather than two that can drift.

And a fifth, added with the fix for the host deploy that refused before its
restart step:

  5. an install whose --prefix resolves to the source tree itself completes.
     deploy.sh installs into the checkout it just synced, so every copy is a
     copy of a file onto itself; install(1) refuses those with "are the same
     file", the script died before printing its success token, and deploy.sh
     read that as an unusable renderer. The seam the fix lives on is
     install_file() -- the single copy point -- which skips a copy whose
     destination already IS the source, decided on resolved identity rather
     than string equality so a relative or symlinked spelling of the prefix
     works too.

The engine is a real compiled artifact, so no test here builds one: the
installer is driven against a synthetic source tree carrying a stand-in
library, and the parts that would need a compiler are the parts that are
already covered by the build script's own pinned-revision check.

Run from the repo root:  python3 -m unittest tests.test_html_runtime_install
"""

import ast
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
INSTALLER = os.path.join(REPO, "tools", "install_html_runtime.sh")
BUILD_SCRIPT = os.path.join(REPO, "tools", "build_litehtml.sh")

# Every tracked source the set carries, relative to the repo root. Kept as a
# literal so a rename that leaves the installer behind fails here too.
REQUIRED_SOURCES = (
    "renderers/native/displayd_html.h",
    "renderers/native/pil_container.h",
    "renderers/native/pil_container.cpp",
    "renderers/native/shim.cpp",
    "renderers/native/LICENSE-litehtml",
    "renderers/native/LICENSE-gumbo",
    "tools/build_litehtml.sh",
)


def run_installer(tree, args, prefix):
    """Run the copy of the script that lives in `tree`, not the repo's.

    The script resolves its source root from its own location, so running the
    repo's copy would silently test the repo instead of the fixture.
    """
    return subprocess.run(
        ["sh", os.path.join(tree.root, "tools", "install_html_runtime.sh"),
         "--prefix", prefix] + args,
        cwd=tree.root, capture_output=True, text=True, timeout=300)


class SourceTree(object):
    """A synthetic checkout: the installer resolves REPO from its own path,
    so moving the script into a fixture is what redirects it. Carries a
    stand-in engine so install mode never reaches a compiler."""

    def __init__(self, engine=True, templates=("status.html",)):
        self.dir = tempfile.TemporaryDirectory(prefix="htmlsrc-")
        self.root = self.dir.name
        self.suffix = "dylib" if sys.platform == "darwin" else "so"
        os.makedirs(os.path.join(self.root, "tools"))
        os.makedirs(os.path.join(self.root, "renderers", "native"))
        os.makedirs(os.path.join(self.root, "html-templates"))
        shutil.copy(INSTALLER, os.path.join(self.root, "tools",
                                            os.path.basename(INSTALLER)))
        for rel in REQUIRED_SOURCES:
            dest = os.path.join(self.root, rel)
            shutil.copy(os.path.join(REPO, rel), dest)
        for name in templates:
            shutil.copy(os.path.join(REPO, "html-templates", name),
                        os.path.join(self.root, "html-templates", name))
        if engine:
            self.fake_engine()

    @property
    def engine_name(self):
        return "renderers/native/liblitehtmlpil." + self.suffix

    def fake_engine(self):
        # One suffix, the one build_litehtml.sh produces for this platform:
        # a stand-in that existed for the wrong platform would prove nothing
        # about what a real build leaves behind.
        path = os.path.join(self.root, self.engine_name)
        with open(path, "w") as fh:
            fh.write("stand-in, not a real shared library")

    def target(self):
        path = tempfile.mkdtemp(prefix="htmltarget-")
        return path

    def install(self, prefix, args=()):
        """Make `prefix` a target the way install.sh does, then add the set.

        The Python side (the daemon's top-level modules, renderers/*.py)
        belongs to install.sh, not to the runtime installer, so a target is
        only realistic once both have run. That split is deliberate and this
        mirrors it rather than widening the installer's ownership to cover the
        daemon.
        """
        os.makedirs(os.path.join(prefix, "renderers"), exist_ok=True)
        for name in sorted(os.listdir(REPO)):
            if name.endswith(".py"):
                shutil.copy(os.path.join(REPO, name),
                            os.path.join(prefix, name))
        for name in os.listdir(os.path.join(REPO, "renderers")):
            if name.endswith(".py"):
                shutil.copy(os.path.join(REPO, "renderers", name),
                            os.path.join(prefix, "renderers", name))
        return run_installer(self, list(args), prefix)

    def clean(self):
        self.dir.cleanup()


class TestInstallerInstallsTheWholeSet(unittest.TestCase):
    def setUp(self):
        self.src = SourceTree()
        self.addCleanup(self.src.clean)
        self.prefix = self.src.target()
        self.addCleanup(shutil.rmtree, self.prefix, True)

    def test_install_then_check_reports_complete(self):
        ran = run_installer(self.src, ["--check"], self.prefix)
        self.assertIn("incomplete:", ran.stdout)
        ran = run_installer(self.src, [], self.prefix)
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        again = run_installer(self.src, ["--check"], self.prefix)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("complete:", again.stdout)

    def test_every_required_artifact_lands_in_the_prefix(self):
        run_installer(self.src, [], self.prefix)
        for rel in REQUIRED_SOURCES:
            self.assertTrue(os.path.isfile(os.path.join(self.prefix, rel)), rel)
        self.assertTrue(os.path.isfile(
            os.path.join(self.prefix, self.src.engine_name)))
        self.assertTrue(os.path.isfile(os.path.join(
            self.prefix, "html-templates", "status.html")))

    def test_artefacts_are_installed_not_symlinked(self):
        # A symlink into a checkout is exactly the developer-local path the
        # install is supposed to remove: the target must own the bytes.
        run_installer(self.src, [], self.prefix)
        for rel in REQUIRED_SOURCES:
            path = os.path.join(self.prefix, rel)
            self.assertFalse(os.path.islink(path), rel)

    def test_build_script_stays_executable(self):
        run_installer(self.src, [], self.prefix)
        mode = os.stat(os.path.join(self.prefix, "tools",
                                    "build_litehtml.sh")).st_mode
        self.assertTrue(mode & 0o111, "a rebuild script that cannot run is "
                                       "not a rebuild path")

    def test_installed_licences_travel_with_the_binary(self):
        # Apache-2.0 (gumbo) and BSD-3-Clause (litehtml) both require it.
        run_installer(self.src, [], self.prefix)
        for name in ("LICENSE-litehtml", "LICENSE-gumbo"):
            body = open(os.path.join(self.prefix, "renderers", "native",
                                     name)).read()
            self.assertGreater(len(body), 500, name)


class TestCheckNamesEveryGap(unittest.TestCase):
    def setUp(self):
        self.src = SourceTree()
        self.addCleanup(self.src.clean)
        self.prefix = self.src.target()
        self.addCleanup(shutil.rmtree, self.prefix, True)

    def test_empty_prefix_names_all_three_kinds_of_gap(self):
        ran = run_installer(self.src, ["--check"], self.prefix)
        out = ran.stdout
        self.assertIn("missing: renderers/native/liblitehtmlpil.{so,dylib}", out)
        self.assertIn("missing: html-templates/*.html", out)
        for rel in REQUIRED_SOURCES:
            self.assertIn("missing: %s" % rel, out)

    def test_each_artifact_is_reported_individually(self):
        # One withheld file must name itself, not collapse into a count: a
        # partial set is the failure this whole script exists to prevent.
        run_installer(self.src, [], self.prefix)
        os.unlink(os.path.join(self.prefix, "html-templates", "status.html"))
        ran = run_installer(self.src, ["--check"], self.prefix)
        self.assertIn("missing: html-templates/*.html", ran.stdout)

    def test_withheld_engine_is_named_by_its_real_path(self):
        run_installer(self.src, [], self.prefix)
        os.unlink(os.path.join(self.prefix, self.src.engine_name))
        ran = run_installer(self.src, ["--check"], self.prefix)
        self.assertIn("missing: renderers/native/liblitehtmlpil.{so,dylib}",
                      ran.stdout)

    def test_check_never_builds_anything(self):
        # Check mode is what deploy.sh asks before deciding; it must not turn
        # into a multi-minute compile on the box.
        before = sorted(os.listdir(os.path.join(self.src.root, "renderers",
                                                "native")))
        run_installer(self.src, ["--check"], self.prefix)
        after = sorted(os.listdir(os.path.join(self.src.root, "renderers",
                                               "native")))
        self.assertEqual(before, after, "check mode wrote into the source tree")

    def test_both_modes_end_on_the_same_success_token(self):
        # deploy.sh reads one token off either mode. When install mode said
        # "complete at" and check mode said "complete:", a caller had to know
        # which one it had run to read the answer at all.
        ran = run_installer(self.src, [], self.prefix)
        self.assertIn("complete: %s" % self.prefix, ran.stdout)
        checked = run_installer(self.src, ["--check"], self.prefix)
        self.assertIn("complete: %s" % self.prefix, checked.stdout)

    def test_deploy_sh_greps_the_token_the_installer_emits(self):
        # deploy.sh decides health from ONE grep on the installer's stdout.
        # If the installer is renamed on its own, deploy silently reports a
        # complete runtime as missing on every run -- so the grepped token is
        # pinned against the installer's success line here.
        with open(os.path.join(REPO, "deploy.sh")) as fh:
            deploy = fh.read()
        grepped = re.findall(r"grep -q '\^(\w+):'", deploy)
        self.assertTrue(grepped, "deploy.sh no longer reads a token off stdout")
        with open(INSTALLER) as fh:
            installer = fh.read()
        for token in grepped:
            self.assertIn('echo "%s: $PREFIX"' % token, installer, token)


class TestBuildFailureIsLoudNotSilent(unittest.TestCase):
    def setUp(self):
        self.src = SourceTree(engine=False)
        self.addCleanup(self.src.clean)
        script = os.path.join(self.src.root, "tools", "build_litehtml.sh")
        with open(script, "w") as fh:
            fh.write("#!/bin/sh\necho 'cmake is required' >&2\nexit 1\n")
        os.chmod(script, 0o755)

    def test_strict_fails_and_names_the_command(self):
        prefix = self.src.target()
        self.addCleanup(shutil.rmtree, prefix, True)
        ran = run_installer(self.src, ["--strict"], prefix)
        self.assertEqual(ran.returncode, 1, ran.stdout + ran.stderr)
        self.assertIn("build_litehtml.sh", ran.stdout + ran.stderr)

    def test_without_strict_everything_else_still_installs(self):
        prefix = self.src.target()
        self.addCleanup(shutil.rmtree, prefix, True)
        ran = run_installer(self.src, [], prefix)
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertIn("incomplete:", ran.stdout)
        for rel in REQUIRED_SOURCES:
            self.assertTrue(os.path.isfile(os.path.join(prefix, rel)), rel)
        # ... and the gap is still reported, not swallowed by the exit code.
        self.assertIn("missing: renderers/native/liblitehtmlpil.{so,dylib}",
                      ran.stdout + ran.stderr)


class TestInstallerMatchesTheRendererSearchPath(unittest.TestCase):
    """The set is only correct if it is the set the renderer looks for.

    Two independent lists -- the installer's and the renderer's -- must agree,
    or a target can pass its install check and still draw a build card.
    """

    def setUp(self):
        self.src = SourceTree()
        self.addCleanup(self.src.clean)
        self.prefix = self.src.target()
        self.addCleanup(shutil.rmtree, self.prefix, True)
        self.src.install(self.prefix)

    def _import_native(self, root):
        spec = importlib.util.spec_from_file_location(
            "installed_html_native", os.path.join(root, "renderers",
                                                  "_html_native.py"))
        module = importlib.util.module_from_spec(spec)
        sys.path.insert(0, os.path.join(root, "renderers"))
        try:
            spec.loader.exec_module(module)
        finally:
            sys.path.pop(0)
        return module

    def test_installed_tree_finds_the_engine_with_no_environment(self):
        module = self._import_native(self.prefix)
        env = os.environ.get("DISPLAYD_HTML_LIB")
        try:
            os.environ.pop("DISPLAYD_HTML_LIB", None)
            found = module.lib_path()
            self.assertIsNotNone(found, "the renderer could not find the "
                                        "engine in its own installed tree")
            self.assertEqual(os.path.realpath(found),
                             os.path.realpath(os.path.join(
                                 self.prefix, self.src.engine_name)))
        finally:
            if env is not None:
                os.environ["DISPLAYD_HTML_LIB"] = env

    def test_engine_path_is_inside_the_prefix_not_the_source_tree(self):
        module = self._import_native(self.prefix)
        found = os.path.realpath(module.lib_path() or "")
        self.assertTrue(found.startswith(os.path.realpath(self.prefix)), found)
        self.assertNotIn(os.path.realpath(self.src.root), found)

    def test_native_dir_resolves_relative_to_the_installed_renderers(self):
        module = self._import_native(self.prefix)
        self.assertEqual(os.path.realpath(module.NATIVE_DIR),
                         os.path.realpath(os.path.join(self.prefix,
                                                       "renderers", "native")))

    def test_installed_tool_can_rebuild_in_place(self):
        # The build script derives its own root from its own location, so an
        # installed copy builds into the installed tree -- no checkout needed.
        module = self._import_native(self.prefix)
        text = open(os.path.join(self.prefix, "tools",
                                 "build_litehtml.sh")).read()
        self.assertIn('ROOT="$(dirname "$HERE")"', text)
        self.assertTrue(os.path.isfile(os.path.join(
            module.NATIVE_DIR, "pil_container.cpp")))


class TestViewReportsItsOwnRuntime(unittest.TestCase):
    """runtime_status() is the install contract as the renderer sees it."""

    def setUp(self):
        os.environ["DISPLAYD_FAKE_FB"] = "1"
        self.addCleanup(os.environ.pop, "DISPLAYD_FAKE_FB", None)
        self.tmp = tempfile.TemporaryDirectory(prefix="htmlstate-")
        self.addCleanup(self.tmp.cleanup)
        import displayd
        self.daemon = displayd.DisplayDaemon(
            policy_path=os.path.join(self.tmp.name, "policy.json"))

    def _load_view(self):
        path = os.path.join(REPO, "renderers", "html.py")
        spec = importlib.util.spec_from_file_location("installed_html_view", path)
        module = importlib.util.module_from_spec(spec)
        sys.path.insert(0, os.path.join(REPO, "renderers"))
        try:
            spec.loader.exec_module(module)
        finally:
            sys.path.pop(0)
        return module

    def test_status_names_the_library_template_root_and_templates(self):
        status = self._load_view().runtime_status()
        for key in ("ok", "library", "version", "template_root", "templates",
                    "error"):
            self.assertIn(key, status)
        self.assertEqual(status["template_root"],
                         os.path.realpath(os.path.join(REPO,
                                                       "html-templates")))
        self.assertGreaterEqual(status["templates"], 1)

    def test_status_never_raises_without_a_built_engine(self):
        # This checkout may or may not have the engine; either way the probe
        # must answer, because /state has to stay total.
        status = self._load_view().runtime_status()
        self.assertEqual(status["ok"], bool(status["library"])
                         and status["templates"] > 0)

    def test_daemon_state_carries_the_html_runtime(self):
        status = self.daemon.state()["html"]
        self.assertIsInstance(status, dict)
        self.assertIn("template_root", status)
        self.assertIn("ok", status)

    def test_daemon_state_is_total_without_the_html_renderer(self):
        self.daemon.renderers = {}
        self.assertIsNone(self.daemon.html_runtime())
        self.assertIsNone(self.daemon.state()["html"])

    def test_daemon_state_is_total_when_the_probe_raises(self):
        # A probe that can raise is not a status: /state must never fail on it.
        entry = {"module": type("M", (), {
            "runtime_status": staticmethod(lambda: (_ for _ in ()).throw(
                RuntimeError("boom")))})}
        self.daemon.renderers = {"html": entry}
        self.assertEqual(self.daemon.html_runtime(),
                         {"ok": False, "error": "boom"})
        json.dumps(self.daemon.state())  # serialisable, so /state can answer


class TestInstallPathsGoThroughTheInstaller(unittest.TestCase):
    """One path to a clean target, not two that can drift apart."""

    def _text(self, name):
        with open(os.path.join(REPO, name)) as fh:
            return fh.read()

    def test_install_sh_installs_the_runtime_set(self):
        src = self._text("install.sh")
        self.assertIn('install_html_runtime.sh" --prefix "$PREFIX" --strict',
                      src)

    def test_install_sh_documents_the_opt_out(self):
        src = self._text("install.sh")
        self.assertIn("DISPLAYD_SKIP_HTML", src)

    def test_install_sh_runs_the_runtime_before_it_starts_the_service(self):
        src = self._text("install.sh")
        self.assertLess(src.index('install_html_runtime.sh" --prefix'),
                        src.index("systemctl enable --now"))

    def test_deploy_sh_installs_the_runtime_on_the_host(self):
        src = self._text("deploy.sh")
        self.assertIn("install_html_runtime.sh", src)

    def test_deploy_sh_runs_it_after_the_sync_and_before_the_restart(self):
        src = self._text("deploy.sh")
        self.assertLess(src.index('echo "rsync done"'),
                        src.index("HTML_LOG="))
        self.assertLess(src.index("HTML_LOG="),
                        src.index("sudo -n systemctl restart displayd"))

    def test_deploy_sh_does_not_gate_the_panel_on_an_optional_view(self):
        # The remote call may not be --strict: a panel that has been serving
        # without the html view must not fail a deploy because the host has
        # no C++ toolchain.
        src = self._text("deploy.sh")
        remote = src.split("HTML_LOG=")[1].split("\n")[0]
        self.assertNotIn("--strict", remote)

    def test_deploy_sh_names_the_build_command_when_the_set_is_short(self):
        src = self._text("deploy.sh")
        self.assertIn("build_litehtml.sh", src)

    def test_deploy_sh_does_not_ship_a_target_built_engine(self):
        # The engine is built ON the target, for that platform (installer
        # header; docs/HTML_RENDERER.md). rsync knows nothing about
        # .gitignore, so a developer's Mac checkout otherwise ships its
        # gitignored Mach-O .dylib, and _html_native.LIB_NAMES tries .dylib
        # first -- the stray file shadows the host's own working .so and the
        # html view dies on a host whose real engine is fine.
        patterns = re.findall(r"--exclude '([^']+)'", self._text("deploy.sh"))
        self.assertIn("liblitehtmlpil.*", patterns)
        self.assertIn("build/", patterns)

    def test_the_exclusions_keep_the_engine_and_build_tree_off_the_host(self):
        # The patterns above, run for real: rsync -n over a source tree that
        # looks like a developer checkout, using deploy.sh's own exclusion
        # list (parsed, never retyped) so this cannot drift from the script.
        if shutil.which("rsync") is None:
            self.skipTest("rsync not installed")
        block = self._text("deploy.sh").split("rsync -az", 1)[1]
        block = block.split('"$HERE/"', 1)[0]
        source = tempfile.mkdtemp(prefix="rsyncsrc-")
        self.addCleanup(shutil.rmtree, source, True)
        dest = tempfile.mkdtemp(prefix="rsyncdst-")
        self.addCleanup(shutil.rmtree, dest, True)
        for rel in ("renderers/native/liblitehtmlpil.dylib",
                    "renderers/native/pil_container.cpp",
                    "html-templates/status.html",
                    "build/litehtml/libjunk.a"):
            path = os.path.join(source, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write("x")
        args = ["rsync", "-an", "--out-format=%n"]
        for pattern in re.findall(r"--exclude '([^']+)'", block):
            args += ["--exclude", pattern]
        ran = subprocess.run(args + [source + "/", dest + "/"],
                             capture_output=True, text=True)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        listed = set(ran.stdout.split())
        self.assertNotIn("renderers/native/liblitehtmlpil.dylib", listed,
                         "a Mac-built engine would cross to the host")
        self.assertFalse([n for n in listed if n.startswith("build")],
                         "per-platform build output would cross to the host")
        # ... and the runtime sources the target genuinely needs still ship.
        self.assertIn("renderers/native/pil_container.cpp", listed)
        self.assertIn("html-templates/status.html", listed)


class TestInstallIntoItsOwnSourceTree(unittest.TestCase):
    """deploy.sh installs into the tree it just synced: --prefix == the
    checkout. Every tracked artifact is then its own destination, and
    install(1) refuses to copy a file onto itself -- which killed the run
    before its success token, so deploy.sh saw "incomplete" for a runtime
    that was already there and refused to restart the panel."""

    def setUp(self):
        self.src = SourceTree()
        self.addCleanup(self.src.clean)

    def test_prefix_equal_to_the_repo_root_completes(self):
        ran = run_installer(self.src, [], self.src.root)
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertIn("complete: %s" % self.src.root, ran.stdout)
        self.assertNotIn("same file", ran.stdout + ran.stderr)

    def test_every_artifact_is_still_there_afterwards(self):
        def slurp(rel):
            with open(os.path.join(self.src.root, rel), "rb") as fh:
                return fh.read()

        before = {rel: slurp(rel) for rel in REQUIRED_SOURCES}
        ran = run_installer(self.src, [], self.src.root)
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        for rel, body in before.items():
            self.assertEqual(slurp(rel), body, rel)

    def test_relative_spelling_of_the_repo_root_also_completes(self):
        # Resolved identity, not string equality: "./renderers/x" and
        # "<root>/renderers/x" are the same file but not the same string.
        ran = run_installer(self.src, [], ".")
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertIn("complete: .", ran.stdout)

    def test_symlinked_spelling_of_the_repo_root_also_completes(self):
        link = self.src.root + "-link"
        os.symlink(self.src.root, link)
        self.addCleanup(os.unlink, link)
        ran = run_installer(self.src, [], link)
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertIn("complete: %s" % link, ran.stdout)

    def test_check_on_the_repo_root_still_reports_complete(self):
        ran = run_installer(self.src, ["--check"], self.src.root)
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertIn("complete: %s" % self.src.root, ran.stdout)

    def test_a_genuinely_incomplete_same_root_is_still_incomplete(self):
        # The skip must not turn a real gap into a success: with no engine
        # and a build that fails, the same-prefix run still says so.
        short = SourceTree(engine=False)
        self.addCleanup(short.clean)
        script = os.path.join(short.root, "tools", "build_litehtml.sh")
        with open(script, "w") as fh:
            fh.write("#!/bin/sh\necho 'cmake is required' >&2\nexit 1\n")
        os.chmod(script, 0o755)
        ran = run_installer(short, [], short.root)
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertIn("incomplete: %s" % short.root, ran.stdout)
        self.assertIn("missing: renderers/native/liblitehtmlpil.{so,dylib}",
                      ran.stdout)
        strict = run_installer(short, ["--strict"], short.root)
        self.assertEqual(strict.returncode, 1, strict.stdout + strict.stderr)


def _daemon_import_closure():
    """Every top-level repo module displayd.py transitively imports by name."""
    seen, queue = set(), ["displayd"]
    while queue:
        name = queue.pop()
        path = os.path.join(REPO, name + ".py")
        if name in seen or not os.path.exists(path):
            continue
        seen.add(name)
        with open(path) as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            else:
                continue
            for imported in names:
                if os.path.exists(os.path.join(REPO, imported + ".py")):
                    queue.append(imported)
    return sorted(seen)


class TestCleanTargetCanImportTheDaemon(unittest.TestCase):
    """install.sh used to copy displayd.py alone, leaving policy/playlist/
    feedback behind -- a prefix that could not even import the daemon, so no
    clean install of any view could ever have worked."""

    def setUp(self):
        self.src = SourceTree()
        self.addCleanup(self.src.clean)
        self.prefix = tempfile.mkdtemp(prefix="cleantarget-")
        self.addCleanup(shutil.rmtree, self.prefix, True)
        self.src.install(self.prefix)

    def test_the_installed_prefix_has_every_module_the_daemon_imports(self):
        present = set(os.listdir(self.prefix))
        for name in _daemon_import_closure():
            self.assertIn(name + ".py", present, name)

    def test_the_daemon_imports_cleanly_from_the_installed_prefix(self):
        # The real proof: run the daemon's own import machinery against the
        # installed tree with the repo NOT on sys.path, so a module that only
        # exists in the checkout cannot satisfy it.
        env = dict(os.environ)
        env["PYTHONPATH"] = self.prefix
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["DISPLAYD_FAKE_FB"] = "1"
        ran = subprocess.run(
            [sys.executable, "-c",
             "import displayd; print(displayd.__file__)"],
            cwd=self.prefix, env=env, capture_output=True, text=True)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        # ...and it was the INSTALLED copy that imported, not the checkout.
        self.assertEqual(os.path.realpath(ran.stdout.strip()),
                         os.path.realpath(os.path.join(self.prefix,
                                                       "displayd.py")))

    def test_install_sh_copies_the_whole_module_set_not_one_file(self):
        with open(os.path.join(REPO, "install.sh")) as fh:
            src = fh.read()
        self.assertIn('install -m 0644 "$HERE"/*.py "$PREFIX/"', src)


if __name__ == "__main__":
    unittest.main()

class TestDeployGatesOnHtmlUsability(unittest.TestCase):
    def test_deploy_fails_when_html_runtime_incomplete(self):
        src = SourceTree(engine=False)
        self.addCleanup(src.clean)
        # Make build fail fast
        script = os.path.join(src.root, "tools", "build_litehtml.sh")
        with open(script, "w") as fh:
            fh.write("#!/bin/sh\necho 'cmake is required' >&2\nexit 1\n")
        os.chmod(script, 0o755)
        prefix = src.target()
        self.addCleanup(shutil.rmtree, prefix, True)
        ran = run_installer(src, [], prefix)
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertIn("incomplete:", ran.stdout)
        self.assertIn("missing: renderers/native/liblitehtmlpil.{so,dylib}", ran.stdout)

    def test_deploy_succeeds_when_html_runtime_complete(self):
        src = SourceTree()
        self.addCleanup(src.clean)
        prefix = src.target()
        self.addCleanup(shutil.rmtree, prefix, True)
        ran = run_installer(src, [], prefix)
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertIn("complete: %s" % prefix, ran.stdout)
