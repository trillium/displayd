"""deploy.sh step 2b must really reach the installer on the host.

The step is the deploy's LiteHTML gate: it runs
tools/install_html_runtime.sh on the target and reads its
"complete: <prefix>" token. A host whose runtime set is short must fail the
deploy there -- so the step has to reach the installer in the first place.

This file pins that reach for the prefix deploy.sh ships with. REMOTE_DIR
defaults to "~/displayd" (this script's own header, and the value
test_deploy_reload_proof.py deploys with), and a quoted "~" is literal for
the local shell AND for the remote one: `sh '~/displayd/tools/...'` opened
nothing, the step captured only "No such file or directory" (rc 127), saw no
"complete:" token, and refused a deploy whose runtime was fine. The engine
half of the same refusal -- a prefix that resolves to the checkout itself,
where every copy is a self-copy install(1) rejects -- is pinned in
test_html_runtime_install.py, because there the unit under test is the
installer.

Seam: the step's own lines, taken verbatim out of deploy.sh and run under a
real /bin/sh with a stub ssh that does what sshd does -- `sh -c` on the
remote command with HOME set to a fake remote home. A stand-in installer
there records that it RAN and with which prefix, so these tests prove the
path resolved; they are not assertions about quoting.

Run from the repo root:  python3 -m unittest tests.test_deploy_html_step
"""

import os
import shlex
import subprocess
import tempfile
import unittest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
DEPLOY = os.path.join(REPO, "deploy.sh")

# The step's installer call as it stood before the fix. Kept so the harness
# stays disconfirmed: if this spelling started passing, the harness would no
# longer be measuring the thing it claims to.
PRE_FIX_STEP = (
    "HTML_LOG=$($SSH \"$HOST\" \"timeout 1800 sh '$REMOTE_DIR/tools/"
    "install_html_runtime.sh' --prefix '$REMOTE_DIR'\" 2>&1) || true\n")


def step_script():
    """deploy.sh's step 2b, verbatim: the remote prefix resolution and the
    installer call. Fails loudly if either line is renamed, so a refactor
    cannot quietly leave this test driving nothing."""
    with open(DEPLOY) as fh:
        lines = fh.read().splitlines(keepends=True)

    def find(prefix):
        for i, line in enumerate(lines):
            if line.startswith(prefix):
                return i
        raise AssertionError(
            "deploy.sh no longer has a %r line; the step this test drives "
            "was renamed or removed" % prefix)

    return "".join(lines[find("REMOTE_PREFIX="):find("HTML_LOG=") + 1])


class FakeRemoteHost(object):
    """A remote home plus an ssh that behaves the way sshd does.

    The stub takes the command argument exactly as sshd receives it -- the
    last argv word -- and runs it with the login shell, so the tilde and
    $HOME handling under test is the real thing. A timeout(1) shim stands in
    for coreutils (macOS has no /usr/bin/timeout), so the step's
    `timeout 1800 sh ...` runs on any development machine.
    """

    def __init__(self):
        self.dir = tempfile.TemporaryDirectory(prefix="deploystep-")
        self.root = self.dir.name
        self.home = os.path.join(self.root, "home")
        self.bin = os.path.join(self.root, "bin")
        self.marker = os.path.join(self.root, "ran")
        os.makedirs(self.home)
        os.makedirs(self.bin)
        self._write("timeout", "#!/bin/sh\n# drop the duration, run the rest\n"
                               "shift\nexec \"$@\"\n")
        self._write("ssh", "#!/bin/sh\n"
                           "for a in \"$@\"; do cmd=$a; done\n"
                           "exec /bin/sh -c \"$cmd\"\n")

    def _write(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as fh:
            fh.write(body)
        os.chmod(path, 0o755)

    def plant_installer(self, prefix):
        """A stand-in installer at <prefix>/tools/, which prints the success
        token and records its own path and prefix. Nothing here builds
        anything -- the step's calling contract is the subject, not the
        installer."""
        tools = os.path.join(prefix, "tools")
        os.makedirs(tools)
        path = os.path.join(tools, "install_html_runtime.sh")
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\n"
                     "printf 'complete: %s\\n' \"$2\"\n"
                     "printf '%s\\n%s\\n' \"$0\" \"$2\" > '"
                     + self.marker + "'\n")
        os.chmod(path, 0o755)

    def ran_with(self):
        with open(self.marker) as fh:
            path, prefix = fh.read().splitlines()[:2]
        return path, prefix

    def run_step(self, script, remote_dir):
        """Run the step under /bin/sh with REMOTE_DIR spelled as deploy.sh
        has it, and return the captured HTML_LOG."""
        env = dict(os.environ)
        env["HOME"] = self.home
        env["PATH"] = self.bin + os.pathsep + env["PATH"]
        shell = ("REMOTE_DIR=" + shlex.quote(remote_dir) + "\n"
                 "SSH=\"" + os.path.join(self.bin, "ssh")
                 + " -o ConnectTimeout=10 -o BatchMode=yes\"\n"
                 "HOST=fixture@fake\n" + script
                 + "printf '%s' \"$HTML_LOG\"\n")
        return subprocess.run(["/bin/sh", "-c", shell], env=env,
                              capture_output=True, text=True, timeout=120)

    def close(self):
        self.dir.cleanup()


class TestTheStepReachesTheInstaller(unittest.TestCase):
    def setUp(self):
        self.host = FakeRemoteHost()
        self.addCleanup(self.host.close)
        self.script = step_script()

    def _assert_installer_ran(self, ran, prefix):
        out = ran.stdout + ran.stderr
        self.assertIn("complete: %s" % prefix, out,
                      "the step never reached the installer:\n%s" % out)
        path, reported = self.host.ran_with()
        self.assertEqual(os.path.realpath(path), os.path.realpath(
            os.path.join(prefix, "tools", "install_html_runtime.sh")),
            "the installer that ran was not the one under the fake "
            "remote home")
        self.assertEqual(reported, prefix,
                         "the installer was handed a different prefix")

    def test_default_remote_dir_resolves_against_the_remote_home(self):
        # REMOTE_DIR="~/displayd" is both the default in deploy.sh and the
        # value the deploy fixture uses.
        prefix = os.path.join(self.host.home, "displayd")
        self.host.plant_installer(prefix)
        self._assert_installer_ran(self.host.run_step(self.script, "~/displayd"),
                                   prefix)

    def test_absolute_remote_dir_still_works(self):
        prefix = os.path.join(self.host.root, "opt", "displayd")
        self.host.plant_installer(prefix)
        self._assert_installer_ran(self.host.run_step(self.script, prefix),
                                   prefix)

    def test_a_remote_dir_with_a_space_still_works(self):
        # Resolving the tilde must not cost the quoting that keeps one argv
        # word one argv word.
        prefix = os.path.join(self.host.root, "displayd dir")
        self.host.plant_installer(prefix)
        self._assert_installer_ran(self.host.run_step(self.script, prefix),
                                   prefix)

    def test_bare_tilde_resolves_to_the_remote_home(self):
        self.host.plant_installer(self.host.home)
        self._assert_installer_ran(self.host.run_step(self.script, "~"),
                                   self.host.home)


class TestHarnessIsDisconfirmedByThePreFixStep(unittest.TestCase):
    """The harness must fail on the spelling that had the defect, or it
    proves nothing about the fix."""

    def setUp(self):
        self.host = FakeRemoteHost()
        self.addCleanup(self.host.close)

    def test_the_pre_fix_quoting_never_reached_the_file(self):
        self.host.plant_installer(os.path.join(self.host.home, "displayd"))
        ran = self.host.run_step(PRE_FIX_STEP, "~/displayd")
        self.assertNotIn("complete:", ran.stdout + ran.stderr)
        self.assertFalse(os.path.exists(self.host.marker),
                         "the pre-fix step ran the installer: the harness is "
                         "no longer measuring the tilde")


if __name__ == "__main__":
    unittest.main()
