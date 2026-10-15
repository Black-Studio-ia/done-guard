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

    def test_wrong_typed_fields_rejected(self):
        bad = [
            {"files_exist": [123]},
            {"files_exist": [""]},
            {"files_changed": ["ok", None]},
            {"commands": [{"run": "x", "expect_exit": "zero"}]},
            {"commands": [{"run": "x", "expect_exit": True}]},
            {"commands": [{"run": "x", "timeout": 0}]},
            {"commands": [{"run": "x", "forbid_output": "(unclosed"}]},
            {"commands": [{"run": "x", "expect_output": [7]}]},
            {"commands": [{"run": "x", "cwd": 5}]},
            {"commands": [{"run": "  "}]},
        ]
        for doc in bad:
            with tempfile.NamedTemporaryFile("w", suffix=".json",
                                             delete=False) as tmp:
                json.dump(doc, tmp)
                path = tmp.name
            try:
                data, err = doneguard.load_criteria(path)
                self.assertIsNone(data, doc)
                self.assertTrue(err, doc)
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

    def test_nonempty(self):
        write(self.root, "full.txt", "data\n")
        write(self.root, "empty.txt", "")
        findings = doneguard.check_files_nonempty(
            self.root, ["full.txt", "empty.txt", "missing.txt"])
        self.assertEqual([f["ok"] for f in findings], [True, False, False])
        self.assertIn("placeholder", findings[1]["evidence"])

    def test_added_forbid_catches_narrowed_tests(self):
        root = make_repo({"t.test.js": "it('a', () => {});\n"})
        self.addCleanup(shutil.rmtree, root, True)
        git(root, "add", "-A")
        git(root, "commit", "-m", "base")
        path = os.path.join(root, "t.test.js")
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write("it.only('b', () => {});\n")
        findings = doneguard.check_added_forbid(
            root, [r"\.only\(", "@pytest.mark.skip"])
        self.assertEqual(len(findings), 1)
        self.assertFalse(findings[0]["ok"])
        self.assertIn("t.test.js", findings[0]["evidence"])
        # The matched line content itself must never appear.
        self.assertNotIn("it.only", findings[0]["evidence"])

    def test_added_forbid_clean_tree_passes(self):
        root = make_repo({"t.test.js": "it('a', () => {});\n"})
        self.addCleanup(shutil.rmtree, root, True)
        git(root, "add", "-A")
        git(root, "commit", "-m", "base")
        findings = doneguard.check_added_forbid(root, [r"\.only\("])
        self.assertTrue(all(f["ok"] for f in findings))

    def test_added_forbid_without_git_skips_honestly(self):
        plain = tempfile.mkdtemp(prefix="done-guard-nogit-")
        self.addCleanup(shutil.rmtree, plain, True)
        write(plain, "a.js", "it.only('x', () => {});\n")
        findings = doneguard.check_added_forbid(plain, [r"\.only\("])
        self.assertTrue(findings[0]["ok"])
        self.assertIn("skipped", findings[0]["evidence"])

    def test_added_forbid_bad_regex_rejected_at_load(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as tmp:
            json.dump({"added_forbid": ["(unclosed"]}, tmp)
            path = tmp.name
        try:
            data, err = doneguard.load_criteria(path)
            self.assertIsNone(data)
            self.assertIn("invalid regex", err)
        finally:
            os.unlink(path)

    def test_files_contain(self):
        write(self.root, "report.md", "# Report\nTotal: 42\n")
        write(self.root, "wrong.md", "# Report\nNothing here\n")
        findings = doneguard.check_files_contain(
            self.root, [{"file": "report.md", "patterns": ["Total: \\d+"]},
                        {"file": "wrong.md", "patterns": ["Total:"]},
                        {"file": "absent.md", "patterns": ["x"]}])
        self.assertEqual([f["ok"] for f in findings], [True, False, False])
        self.assertEqual(findings[0]["check"], "files_contain")

    def test_files_contain_bad_spec_rejected(self):
        bad_docs = [
            {"files_contain": [{"file": "a"}]},
            {"files_contain": [{"patterns": ["x"]}]},
            {"files_contain": [{"file": "", "patterns": ["x"]}]},
            {"files_contain": "nope"},
            {"git_branch": 5},
        ]
        bad_regex_doc = {"files_contain": [{"file": "a",
                                             "patterns": ['(unclosed']}]}
        for doc in bad_docs + [bad_regex_doc]:
            with tempfile.NamedTemporaryFile("w", suffix=".json",
                                             delete=False) as tmp:
                json.dump(doc, tmp)
                path = tmp.name
            try:
                data, err = doneguard.load_criteria(path)
                self.assertIsNone(data, doc)
                self.assertTrue(err, doc)
            finally:
                os.unlink(path)

    def test_git_branch(self):
        root = make_repo({"a.txt": "x\n"})
        self.addCleanup(shutil.rmtree, root, True)
        git(root, "add", "-A")
        git(root, "commit", "-m", "b")
        branch = git(root, "branch", "--show-current").strip()
        self.assertTrue(
            doneguard.check_git_branch(root, branch)[0]["ok"])
        res = doneguard.check_git_branch(root, "definitely-not-this")[0]
        self.assertFalse(res["ok"])
        self.assertIn("wrong state", res["evidence"])

    def test_added_forbid_ignores_removed_lines(self):
        root = make_repo({"t.js": "it.only('old', () => {});\nkeep();\n"})
        self.addCleanup(shutil.rmtree, root, True)
        git(root, "add", "-A")
        git(root, "commit", "-m", "b")
        with open(os.path.join(root, "t.js"), "w", encoding="utf-8",
                  newline="\n") as handle:
            handle.write("keep();\n")  # .only line REMOVED, not added
        findings = doneguard.check_added_forbid(root, [r"\.only\("])
        self.assertTrue(all(f["ok"] for f in findings))

    def test_added_forbid_renamed_file(self):
        root = make_repo({"old.js": "a();\n"})
        self.addCleanup(shutil.rmtree, root, True)
        git(root, "add", "-A")
        git(root, "commit", "-m", "b")
        os.rename(os.path.join(root, "old.js"), os.path.join(root, "new.js"))
        with open(os.path.join(root, "new.js"), "a", encoding="utf-8",
                  newline="\n") as handle:
            handle.write("it.only('x', () => {});\n")
        git(root, "add", "-A")
        findings = doneguard.check_added_forbid(root, [r"\.only\("])
        self.assertTrue(any(not f["ok"] for f in findings))

    def test_added_forbid_unicode_path(self):
        name = "täst-ü.js"
        root = make_repo({name: "a();\n"})
        self.addCleanup(shutil.rmtree, root, True)
        git(root, "add", "-A")
        git(root, "commit", "-m", "b")
        with open(os.path.join(root, name), "a", encoding="utf-8",
                  newline="\n") as handle:
            handle.write("fit('x', () => {});\n")
        findings = doneguard.check_added_forbid(root, [r"\bfit\s*\("])
        self.assertTrue(any(not f["ok"] and name in f["evidence"]
                            for f in findings),
                        json.dumps(findings))

    def test_added_forbid_skips_binary_diff(self):
        root = make_repo({})
        self.addCleanup(shutil.rmtree, root, True)
        with open(os.path.join(root, "blob.bin"), "wb") as handle:
            handle.write(b"\x00\x01\x02" * 100)
        git(root, "add", "-A")
        git(root, "commit", "-m", "b")
        with open(os.path.join(root, "blob.bin"), "ab") as handle:
            handle.write(b"\x03\x04")
        findings = doneguard.check_added_forbid(root, [r"."])
        self.assertTrue(all(f["ok"] for f in findings))

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

    def test_run_shell_never_raises_on_timeout(self):
        # Regression: the POSIX shell branch once let TimeoutExpired
        # propagate, crashing the whole gate instead of failing one check.
        wait = ("sleep 20" if os.name != "nt"
                else "powershell -command Start-Sleep -Seconds 20")
        code, out, err, timed_out = doneguard._run_shell(
            wait, self.root, timeout=2)
        self.assertEqual(code, 124)
        self.assertTrue(timed_out)

    def test_run_shell_spawn_failure_is_finding_not_crash(self):
        code, out, err, timed_out = doneguard._run_shell(
            "echo hi", os.path.join(self.root, "no-such-dir"), timeout=10)
        self.assertEqual(code, 127)
        self.assertFalse(timed_out)

    @unittest.skipIf(os.name == "nt" or shutil.which("pgrep") is None,
                     "needs POSIX pgrep")
    def test_timeout_leaves_no_orphan(self):
        marker = "done-guard-orphan-probe-xyz"
        res = doneguard.check_command(
            self.root, {"run": f"sleep 60 # {marker}", "timeout": 2}, True)
        self.assertFalse(res["ok"])
        self.assertTrue(res["timed_out"])
        left = subprocess.run(["pgrep", "-f", marker], capture_output=True,
                              text=True).stdout.strip()
        self.assertEqual(left, "", f"orphan survived: {left}")

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

    def test_cli_usage_error_stays_text_on_stderr(self):
        # Contract: usage/config errors are text on stderr + exit 2,
        # even under --format json (only verdicts are JSON).
        proc = self.run_cli("--root", self.root, "--format", "json")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertIn("no checks declared", proc.stderr)

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
