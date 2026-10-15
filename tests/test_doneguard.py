"""Tests for done-guard. Run with: python -m unittest discover -s tests -v"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import doneguard


def write(root, rel, content):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    return path


def git(root, *args):
    proc = subprocess.run(["git", *args], cwd=root, capture_output=True,
                          text=True)
    assert proc.returncode == 0, (args, proc.stderr[-300:])
    return proc.stdout


def make_repo(files=None):
    root = tempfile.mkdtemp(prefix="done-guard-test-")
    git(root, "init")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    for rel, content in (files or {}).items():
        write(root, rel, content)
    return root


TRUE_CMD = "exit 0" if os.name != "nt" else "exit /b 0"
FALSE_CMD = "exit 1" if os.name != "nt" else "exit /b 1"


class CriteriaTests(unittest.TestCase):
    def test_unknown_top_key_rejected(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as tmp:
            json.dump({"files_exist!": []}, tmp)
            path = tmp.name
        try:
            data, err = doneguard.load_criteria(path)
            self.assertIsNone(data)
            self.assertIn("unknown", err)
        finally:
            os.unlink(path)

    def test_malformed_json_rejected(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as tmp:
            tmp.write("{not json")
            path = tmp.name
        try:
            data, err = doneguard.load_criteria(path)
            self.assertIsNone(data)
            self.assertTrue(err)
        finally:
            os.unlink(path)

    def test_empty_criteria_rejected(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as tmp:
            json.dump({}, tmp)
            path = tmp.name
        try:
            data, err = doneguard.load_criteria(path)
            self.assertIsNone(data)
            self.assertIn("nothing to verify", err)
        finally:
            os.unlink(path)

    def test_unknown_command_key_rejected(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as tmp:
            json.dump({"commands": [{"run": "x", "bogus": 1}]}, tmp)
            path = tmp.name
        try:
            data, err = doneguard.load_criteria(path)
            self.assertIsNone(data)
            self.assertIn("bogus", err)
        finally:
            os.unlink(path)


class FilesTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_exist_and_glob(self):
        write(self.root, "dist/a.js", "x\n")
        write(self.root, "dist/b.js", "y\n")
        findings = doneguard.check_files_exist(self.root, ["dist/a.js",
                                                           "dist/*.js",
                                                           "missing.txt"])
        self.assertEqual([f["ok"] for f in findings], [True, True, False])

    def test_changed_detects_modification(self):
        write(self.root, "a.txt", "v1\n")
        write(self.root, "b.txt", "v1\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-m", "base")
        write(self.root, "a.txt", "v2\n")  # modified, unstaged
        write(self.root, "c.txt", "new\n")  # untracked
        ok_a = doneguard.check_files_changed(self.root, ["a.txt"])[0]
        ok_b = doneguard.check_files_changed(self.root, ["b.txt"])[0]
        ok_c = doneguard.check_files_changed(self.root, ["c.txt"])[0]
        self.assertTrue(ok_a["ok"])
        self.assertFalse(ok_b["ok"])  # exists but untouched
        self.assertIn("identical to HEAD", ok_b["evidence"])
        self.assertTrue(ok_c["ok"])  # untracked counts as changed

    def test_changed_missing_file(self):
        res = doneguard.check_files_changed(self.root, ["nope.txt"])[0]
        self.assertFalse(res["ok"])

    def test_changed_without_git_degrades_honestly(self):
        plain = tempfile.mkdtemp(prefix="done-guard-nogit-")
        self.addCleanup(shutil.rmtree, plain, True)
        write(plain, "out.txt", "data\n")
        res = doneguard.check_files_changed(plain, ["out.txt"])[0]
        self.assertTrue(res["ok"])
        self.assertIn("no git baseline", res["evidence"])


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_pass(self):
        res = doneguard.check_command(
            self.root, {"run": TRUE_CMD, "timeout": 30}, True)
        self.assertTrue(res["ok"])
        self.assertEqual(res["exit_code"], 0)

    def test_fail_exit(self):
        res = doneguard.check_command(
            self.root, {"run": FALSE_CMD, "timeout": 30}, True)
        self.assertFalse(res["ok"])
        self.assertIn("exit 1", res["evidence"])

    def test_missing_binary_fails(self):
        res = doneguard.check_command(
            self.root, {"run": "definitely-not-a-real-binary-xyz",
                        "timeout": 30}, True)
        self.assertFalse(res["ok"])

    def test_timeout_kills(self):
        sleep = ("sleep 20" if os.name != "nt"
                 else "powershell -command Start-Sleep -Seconds 20")
        res = doneguard.check_command(
            self.root, {"run": sleep, "timeout": 2}, True)
        self.assertFalse(res["ok"])
        self.assertTrue(res["timed_out"])
        self.assertIn("timed out", res["evidence"])

    def test_forbid_output_catches_false_pass(self):
        # Exit 0 but output proves failure: must still FAIL.
        cmd = ("printf 'ok\\nFAILED 3 tests\\n'" if os.name != "nt"
               else "cmd /c \"echo ok & echo FAILED 3 tests\"")
        res = doneguard.check_command(
            self.root, {"run": cmd, "timeout": 30,
                        "forbid_output": ["FAILED"]}, True)
        self.assertFalse(res["ok"])
        self.assertIn("forbidden", res["evidence"])

    def test_expect_output_required(self):
        cmd = ("printf 'all good\\n'" if os.name != "nt"
               else "cmd /c echo all good")
        res = doneguard.check_command(
            self.root, {"run": cmd, "timeout": 30,
                        "expect_output": ["10 passed"]}, True)
        self.assertFalse(res["ok"])
        self.assertIn("missing required pattern", res["evidence"])

    def test_no_output_flag(self):
        cmd = ("printf 'secret-value-xyz\\n'" if os.name != "nt"
               else "cmd /c echo secret-value-xyz")
        res = doneguard.check_command(
            self.root, {"run": cmd, "timeout": 30}, False)
        self.assertTrue(res["ok"])
        self.assertNotIn("secret-value-xyz", res["evidence"])

    def test_invalid_run_and_timeout(self):
        res = doneguard.check_command(self.root, {"run": "  ", "timeout": 5},
                                      True)
        self.assertFalse(res["ok"])
        res = doneguard.check_command(
            self.root, {"run": TRUE_CMD, "timeout": 0}, True)
        self.assertFalse(res["ok"])


class CliTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo({"app.py": "x = 1\n"})
        git(self.root, "add", "-A")
        git(self.root, "commit", "-m", "base")
        write(self.root, "app.py", "x = 2\n")
        self.script = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "doneguard.py")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def run_cli(self, *args):
        return subprocess.run([sys.executable, self.script, *args],
                              capture_output=True, text=True, cwd=self.root)

    def test_cli_flags_pass(self):
        proc = self.run_cli("--root", self.root, "--expect-change", "app.py",
                            "--run", TRUE_CMD)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("2/2", proc.stdout)

    def test_cli_untouched_file_fails(self):
        proc = self.run_cli("--root", self.root, "--expect-change", "app.py",
                            "--expect-file", "missing-artifact.zip")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("FAIL", proc.stdout)

    def test_cli_no_checks_exit_2(self):
        proc = self.run_cli("--root", self.root)
        self.assertEqual(proc.returncode, 2)

    def test_cli_json_valid(self):
        proc = self.run_cli("--root", self.root, "--expect-change", "app.py",
                            "--format", "json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["findings"]), 1)

    def test_criteria_file_end_to_end(self):
        crit = {"files_changed": ["app.py"],
                "commands": [{"run": TRUE_CMD, "timeout": 30,
                              "expect_exit": 0}]}
        path = os.path.join(self.root, "acceptance.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(crit, handle)
        proc = self.run_cli("--root", self.root)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("PASS", proc.stdout)


if __name__ == "__main__":
    unittest.main()
