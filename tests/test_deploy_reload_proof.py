"""deploy.sh reload-proof tests: the /reload proof must really run.

The bug this pins: deploy.sh built the /reload proof body with a
backslash-continued run of separate "..." segments. A line continuation
does NOT join separately-quoted words into one argv entry (verified
against sh, dash, bash 3.2/5.3 and zsh), so python3 -c received only the
FIRST segment as its program, the rest landed in sys.argv, and the
segment it did run printed nothing. RELOAD_BODY came out EMPTY, so the
proof POSTed an empty body, the daemon correctly answered 400 "sha is
required", and step 4 warned and skipped on EVERY deploy -- the panel
never proved the build it was running. The same idiom at the
transient-view derivation was masked by its `|| echo` fallback.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""

import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

REPO = os.path.join(os.path.dirname(__file__), os.pardir)
DEPLOY = os.path.join(REPO, "deploy.sh")

# Stubs stand in for the ssh/rsync that would touch a live host. Nothing
# here reaches the network: rsync is a no-op and every ssh invocation is
# answered locally (the restart one restarts the headless daemon, so the
# restart-blanks-the-screen operator path stays faithful).
SSH_STUB = '''#!/usr/bin/env python3
import os, signal, subprocess, sys, time, urllib.request

cmd = sys.argv[-1] if sys.argv else ""
STAMP, PIDF, PORT, REPO = (os.environ["STUB_STAMP"], os.environ["STUB_PIDF"],
                           os.environ["STUB_PORT"], os.environ["STUB_REPO"])

if "is-active" in cmd:
    print("active")
elif "restart displayd-touch" in cmd:
    # No touch unit on the fixture host: deploy.sh has a supported
    # "no heartbeat and no unit to restart" path that continues, which
    # keeps this fixture scoped to the reload proof.
    sys.exit(1)
elif "cat >" in cmd:
    data = sys.stdin.buffer.read()
    sys.stdout.buffer.write(data)
    name = cmd.split("cat >", 1)[1].strip().split("/")[-1]
    with open(STAMP if name == "DEPLOYED"
              else os.path.join(os.path.dirname(STAMP), name), "wb") as fh:
        fh.write(data)
elif "install_html_runtime" in cmd:
    # The fixture host has no deployed tree, so answer the html-runtime step
    # by really running the shipped installer in check mode against the repo.
    # Check mode builds nothing: it answers the question deploy.sh asks --
    # "is the set complete here" -- which for a bare checkout it is not,
    # because no engine has been built. That is a truthful warning, and
    # deploy.sh is expected to continue with it.
    subprocess.run(["sh", os.path.join(REPO, "tools", "install_html_runtime.sh"),
                    "--prefix", REPO, "--check"], check=False)
elif "restart displayd" in cmd:
    try:
        os.kill(int(open(PIDF).read().strip()), signal.SIGTERM)
    except Exception:
        pass
    time.sleep(0.3)
    env = dict(os.environ)
    log = open(os.path.join(os.path.dirname(PIDF), "daemon.log"), "ab")
    proc = subprocess.Popen(
        [sys.executable, "displayd.py", "--bind", "127.0.0.1", "--port", PORT],
        cwd=REPO, env=env, stdout=log, stderr=log)
    open(PIDF, "w").write(str(proc.pid))
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%s/health" % PORT, timeout=2) as resp:
                if json.loads(resp.read()).get("ok"):
                    break
        except Exception:
            time.sleep(0.2)
sys.exit(0)
'''


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class DeployFixture(object):
    """A headless daemon plus a deploy.sh run against it, offline."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="deployproof")
        self.port = free_port()
        self.panel = "127.0.0.1:%d" % self.port
        self.stamp = os.path.join(self.tmp.name, "DEPLOYED")
        self.pidf = os.path.join(self.tmp.name, "daemon.pid")
        self.bin = os.path.join(self.tmp.name, "bin")
        os.makedirs(self.bin)
        self._write_stubs()
        self.proc = self._start()

    def _daemon_env(self):
        env = dict(os.environ)
        env.update({"DISPLAYD_FAKE_FB": "1",
                    "DISPLAYD_DEPLOY_STAMP": self.stamp,
                    "DISPLAYD_POLICY": os.path.join(self.tmp.name,
                                                    "policy.json")})
        return env

    def _start(self):
        log = open(os.path.join(self.tmp.name, "daemon.log"), "ab")
        self._log = log
        proc = subprocess.Popen(
            [sys.executable, "displayd.py", "--bind", "127.0.0.1",
             "--port", str(self.port)], cwd=REPO, env=self._daemon_env(),
            stdout=log, stderr=log)
        with open(self.pidf, "w") as fh:
            fh.write(str(proc.pid))
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        "http://%s/health" % self.panel, timeout=2) as resp:
                    if json.loads(resp.read()).get("ok"):
                        return proc
            except Exception:
                time.sleep(0.2)
        raise AssertionError("headless daemon never became healthy")

    def _write_stubs(self):
        ssh = os.path.join(self.bin, "ssh")
        with open(ssh, "w") as fh:
            fh.write(SSH_STUB)
        os.chmod(ssh, 0o755)
        rsync = os.path.join(self.bin, "rsync")
        with open(rsync, "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(rsync, 0o755)

    def deploy(self):
        env = self._daemon_env()
        env.update({"PATH": self.bin + os.pathsep + env["PATH"],
                    "DISPLAYD_HOST": "fixture@localhost",
                    "DISPLAYD_PANEL": self.panel,
                    "DISPLAYD_REMOTE_DIR": "~/displayd",
                    "DEPLOYER": "test",
                    "STUB_STAMP": self.stamp, "STUB_PIDF": self.pidf,
                    "STUB_PORT": str(self.port), "STUB_REPO": REPO})
        done = subprocess.run([DEPLOY], cwd=REPO, env=env, timeout=600,
                              capture_output=True, text=True)
        return done

    def close(self):
        try:
            self.proc.send_signal(signal.SIGTERM)
            self.proc.wait(timeout=10)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        try:
            self._log.close()
        except Exception:
            pass
        self.tmp.cleanup()


class TestReloadProofRuns(unittest.TestCase):
    """The operator path: deploy.sh proves the build, twice in a row."""

    @classmethod
    def setUpClass(cls):
        cls.fx = DeployFixture()

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    def _assert_proof_ran(self, run, label):
        out = run.stdout + run.stderr
        self.assertNotIn("/reload proof failed", out,
                         "%s: the reload proof was skipped -- empty proof "
                         "body (deploy.sh built RELOAD_BODY with a "
                         "backslash-continued multi-segment python3 -c)\n%s"
                         % (label, out))
        self.assertIn("reload confirmation showing on panel", out,
                      "%s: /reload proof never reached the panel\n%s"
                      % (label, out))
        self.assertIn("reload confirmed, back to", out,
                      "%s: proof was never confirmed off the panel\n%s"
                      % (label, out))

    def test_consecutive_deploys_both_prove_the_build(self):
        # The reported symptom was a warn-and-skip on consecutive deploys.
        # The cause was unconditional (every deploy), so both runs must
        # show the proof; the second run is the consecutive case.
        first = self.fx.deploy()
        self._assert_proof_ran(first, "deploy 1")
        self.assertEqual(first.returncode, 0,
                         "deploy 1 failed:\n%s"
                         % (first.stdout + first.stderr))
        second = self.fx.deploy()
        self._assert_proof_ran(second, "deploy 2 (consecutive)")
        self.assertEqual(second.returncode, 0,
                         "deploy 2 failed:\n%s"
                         % (second.stdout + second.stderr))

    def test_proof_carries_the_deployed_sha(self):
        run = self.fx.deploy()
        with urllib.request.urlopen(
                "http://%s/state" % self.fx.panel, timeout=10) as resp:
            state = json.load(resp)
        # The step confirmed the proof off the panel, so the live view is a
        # real one again -- never left parked on the QR.
        self.assertNotEqual(state.get("renderer"), "reload")
        self.assertIn("stamp written", run.stdout)


class TestProofBodyIsOneShellWord(unittest.TestCase):
    r"""Root-cause guard: `python3 -c` needs ONE program argument.

    A backslash continuation removes the newline but does not join two
    separately-quoted words, so `"a"\` + newline + `"b"` reaches python as
    two arguments and only the first becomes the -c program. The rule is
    therefore purely lexical: the word after `-c` must be one quoted
    string, and no further quoted word may follow it in the same command
    (a separator or redirection may). Read straight off deploy.sh, so it
    needs no daemon and no deploy run.
    """

    def _skip_gap(self, text, i):
        """Advance over whitespace and line continuations."""
        while i < len(text) and (text[i].isspace() or text[i] == "\\"):
            i += 1
        return i

    def _is_one_word(self, text, pos):
        i = self._skip_gap(text, pos)
        if i >= len(text) or text[i] not in "'\"":
            return False
        quote = text[i]
        i += 1
        while i < len(text):
            if quote == '"' and text[i] == "\\":
                i += 2
                continue
            if text[i] == quote:
                break
            i += 1
        else:
            return False                      # unterminated program
        after = self._skip_gap(text, i + 1)
        return after >= len(text) or text[after] not in "'\""

    def _offenders(self, text):
        bad = []
        for match in re.finditer(r"python3\s+-c", text):
            start = text.rfind("\n", 0, match.start()) + 1
            if text[start:].lstrip().startswith("#"):
                continue                      # a comment, not a command
            if not self._is_one_word(text, match.end()):
                bad.append("line %d" % (text.count("\n", 0, match.start())
                                        + 1))
        return bad

    def test_every_deploy_python_c_gets_exactly_one_program(self):
        with open(DEPLOY) as fh:
            offenders = self._offenders(fh.read())
        self.assertEqual(offenders, [],
                         "multi-word python3 -c silently runs only the "
                         "first: " + "; ".join(offenders))

    def test_guard_catches_the_pre_fix_script(self):
        # Disconfirming evidence: the guard must FAIL on the script that
        # had the defect, or it proves nothing.
        old = subprocess.run(["git", "-C", REPO, "show", "HEAD:deploy.sh"],
                             capture_output=True, text=True)
        if old.returncode != 0:
            self.skipTest("HEAD:deploy.sh unavailable")
        self.assertTrue(self._offenders(old.stdout),
                        "the guard must flag the pre-fix deploy.sh, else it "
                        "does not actually detect the defect")


class TestTransientDerivationRuns(unittest.TestCase):
    def test_derivation_is_live_code_not_a_fallback(self):
        # The transient-view set is derived from the repo. Before the fix
        # that derivation printed nothing and the `|| echo` fallback
        # answered instead, so the documented derivation was dead code.
        with open(DEPLOY) as fh:
            src = fh.read()
        self.assertIn('HERE="$HERE" python3 -c', src,
                      "the transient derivation must run python3 itself")
        derived = subprocess.run(
            [sys.executable, "-c",
             "import os,sys;here=os.environ['HERE'];sys.path.insert(0,here);"
             "import policy;"
             "renders={f[:-3] for f in os.listdir(os.path.join(here,"
             "'renderers')) if f.endswith('.py')};"
             "print(' '.join(sorted(k for k in policy.PRIORITY "
             "if k in renders)))"],
            cwd=REPO, env=dict(os.environ, HERE=REPO), timeout=60,
            capture_output=True, text=True)
        self.assertEqual(derived.stdout.strip(), "notice reload",
                         "derived transient set changed; the hardcoded "
                         "fallback and the derivation must agree")


if __name__ == "__main__":
    unittest.main()
