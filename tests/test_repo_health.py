"""Repository health baseline checks.

These tests codify repository invariants and run in the normal test path.
They use disposable fixtures (temp dirs/files) and do not modify the
real checkout.
"""
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


class TestLineBudget(unittest.TestCase):
    def test_line_budget_script_is_clean(self):
        # The existing checker is the source of truth; runnable from repo
        # root, it must find no offender and exit 0. Every authored file is
        # now inside the budget, so this is the real gate rather than a
        # baseline of known exceptions.
        res = subprocess.run(
            [sys.executable, os.path.join(REPO, 'tools', 'check-lines.py')],
            cwd=REPO,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.assertEqual(res.returncode, 0)
        out = res.stdout
        self.assertIn('line budget ok', out)
        self.assertNotIn('over the 250-line budget', out)
        self.assertFalse(any(n in out for n in ('displayd.py', 'touch.py')))


class TestGeneratedArtifactsNotTracked(unittest.TestCase):
    def test_generated_native_not_tracked(self):
        res = subprocess.run(
            [sys.executable, os.path.join(REPO, 'tools', 'check-generated-tracked.py')],
            cwd=REPO,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn('no generated native artifacts tracked', res.stdout)


class TestRepoHealthCheckWrapper(unittest.TestCase):
    def test_check_repo_health_runs_both_checks(self):
        res = subprocess.run(
            [sys.executable, os.path.join(REPO, 'tools', 'check-repo-health.py')],
            cwd=REPO,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        # Both checks pass now: the wrapper exits clean.
        self.assertEqual(res.returncode, 0)
        out = res.stdout
        self.assertIn('line budget ok', out)
        self.assertNotIn('over the 250-line budget', out)
        self.assertIn('no generated native artifacts tracked', out)


class TestNegativeFixtures(unittest.TestCase):
    def test_oversize_authored_file_fails_budget(self):
        with tempfile.TemporaryDirectory() as td:
            py = os.path.join(td, 'big.py')
            with open(py, 'w') as fh:
                for i in range(300):
                    fh.write(f'# line {i}\n')
            # run checker in that dir
            res = subprocess.run(
                [sys.executable, os.path.join(REPO, 'tools', 'check-lines.py')],
                cwd=td,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            self.assertEqual(res.returncode, 1)
            self.assertIn('big.py', res.stdout)
            self.assertIn('over the 250-line budget', res.stdout)

    def test_tests_dir_excluded_from_budget(self):
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, 'tests'))
            py = os.path.join(td, 'tests', 'big_test.py')
            with open(py, 'w') as fh:
                for i in range(400):
                    fh.write(f'# line {i}\n')
            res = subprocess.run(
                [sys.executable, os.path.join(REPO, 'tools', 'check-lines.py')],
                cwd=td,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            self.assertEqual(res.returncode, 0)
            self.assertIn('line budget ok', res.stdout)

    def test_renderers_underscore_excluded(self):
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, 'renderers'))
            py = os.path.join(td, 'renderers', '_vendored.py')
            with open(py, 'w') as fh:
                for i in range(400):
                    fh.write(f'# line {i}\n')
            res = subprocess.run(
                [sys.executable, os.path.join(REPO, 'tools', 'check-lines.py')],
                cwd=td,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            self.assertEqual(res.returncode, 0)
            self.assertIn('line budget ok', res.stdout)

    def test_generated_artifact_tracked_fails_check(self):
        with tempfile.TemporaryDirectory() as td:
            # init git
            subprocess.run(['git', 'init', '-q'], cwd=td, check=True)
            os.makedirs(os.path.join(td, 'renderers', 'native'))
            bad = os.path.join(td, 'renderers', 'native', 'liblitehtmlpil.dylib')
            with open(bad, 'w') as fh:
                fh.write('fake')
            subprocess.run(['git', 'add', 'renderers/native/liblitehtmlpil.dylib'], cwd=td, check=True)
            # copy checker
            import shutil
            shutil.copy(os.path.join(REPO, 'tools', 'check-generated-tracked.py'), os.path.join(td, 'check.py'))
            res = subprocess.run(
                [sys.executable, 'check.py'],
                cwd=td,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            self.assertEqual(res.returncode, 1)
            self.assertIn('tracked generated artifact', res.stdout)

    def test_generated_so_tracked_fails_check(self):
        with tempfile.TemporaryDirectory() as td:
            subprocess.run(['git', 'init', '-q'], cwd=td, check=True)
            os.makedirs(os.path.join(td, 'renderers', 'native'))
            bad = os.path.join(td, 'renderers', 'native', 'liblitehtmlpil.so')
            with open(bad, 'w') as fh:
                fh.write('fake')
            subprocess.run(['git', 'add', 'renderers/native/liblitehtmlpil.so'], cwd=td, check=True)
            import shutil
            shutil.copy(os.path.join(REPO, 'tools', 'check-generated-tracked.py'), os.path.join(td, 'check.py'))
            res = subprocess.run(
                [sys.executable, 'check.py'],
                cwd=td,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            self.assertEqual(res.returncode, 1)
            self.assertIn('tracked generated artifact', res.stdout)

    def test_positive_clean_checkout_checks_pass(self):
        with tempfile.TemporaryDirectory() as td:
            subprocess.run(['git', 'init', '-q'], cwd=td, check=True)
            # create small clean files
            with open(os.path.join(td, 'small.py'), 'w') as fh:
                fh.write('# ok\n' * 10)
            os.makedirs(os.path.join(td, 'src'))
            with open(os.path.join(td, 'src', 'mod.py'), 'w') as fh:
                fh.write('# ok\n' * 20)
            subprocess.run(['git', 'add', 'small.py', 'src/mod.py'], cwd=td, check=True, env={**os.environ, 'GIT_ALLOW_BARE_ADD':'1'})
            import shutil
            shutil.copy(os.path.join(REPO, 'tools', 'check-lines.py'), os.path.join(td, 'check-lines.py'))
            shutil.copy(os.path.join(REPO, 'tools', 'check-generated-tracked.py'), os.path.join(td, 'check-generated-tracked.py'))
            res1 = subprocess.run([sys.executable, 'check-lines.py'], cwd=td, stdout=subprocess.PIPE, text=True)
            self.assertEqual(res1.returncode, 0)
            res2 = subprocess.run([sys.executable, 'check-generated-tracked.py'], cwd=td, stdout=subprocess.PIPE, text=True)
            self.assertEqual(res2.returncode, 0)
