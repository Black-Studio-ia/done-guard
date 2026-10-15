#!/usr/bin/env python3
"""done-guard: verify an AI agent's work against declared acceptance criteria.

Coding agents sometimes report a task as finished while acceptance criteria
are unmet — or stop after zero actions. done-guard does not judge meaning;
it checks observable evidence you declare in advance:

* required files exist (the artifacts the task must produce),
* required files actually changed (new or modified vs git HEAD),
* required commands exit as expected, with required/forbidden output.

No models, no APIs, no network. Zero dependencies, offline. If a check
cannot be evidenced, it fails loudly instead of passing quietly.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import signal
import subprocess
import sys

__version__ = "0.3.0"

DEFAULT_TIMEOUT = 300
OUTPUT_TAIL_LINES = 20
OUTPUT_LINE_LIMIT = 500

TOP_LEVEL_KEYS = {"files_exist", "files_nonempty", "files_changed",
                  "added_forbid", "commands"}
COMMAND_KEYS = {"run", "timeout", "expect_exit", "expect_output",
                "forbid_output", "cwd"}


def _run_shell(cmd: str, cwd: str,
               timeout: int) -> tuple[int, str, str, bool]:
    """Run a shell string with a timeout. Never raises for timeouts or
    spawn failures; callers always get a verdict tuple."""
    new_session = {"start_new_session": True} if os.name != "nt" else {}
    try:
        proc = subprocess.Popen(
            cmd, shell=True, cwd=cwd, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, errors="replace",
            **new_session)  # type: ignore[arg-type]
    except OSError as exc:
        return 127, "", f"could not start command: {exc}", False
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out or "", err or "", False
    except subprocess.TimeoutExpired:
        # Kill the whole process group on POSIX so grandchildren (a runaway
        # `sleep`, a hung child server, …) die with the shell, not after it.
        if os.name != "nt":
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        try:
            proc.kill()
        except OSError:
            pass
        try:
            out, err = proc.communicate(timeout=5)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            # A surviving grandchild may hold the pipes open, so the
            # re-drain can time out. The direct child is already dead from
            # kill(); reap it explicitly so no zombie (or ResourceWarning)
            # is left behind.
            out, err = "", ""
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001 - best effort reap only
                pass
        return 124, out or "", err or "", True


def _run(cmd: list[str], cwd: str,
         timeout: int) -> tuple[int, str, str, bool]:
    """Run an argv list with a timeout. Never raises on timeout."""
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                              errors="replace", timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr, False
    except FileNotFoundError:
        return 127, "", f"command not found: {cmd[0]}", False
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        err = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        return 124, out, err, True
    except OSError as exc:
        return 127, "", str(exc), False


def _git(root: str, args: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(["git", *args], cwd=root, capture_output=True,
                              text=True, errors="replace", timeout=60)
        return proc.returncode, proc.stdout
    except (OSError, subprocess.SubprocessError):
        return 127, ""


def _glob_match(pattern: str, root: str) -> list[str]:
    """Repo-relative paths matching a glob (recursive ``**`` supported)."""
    matches: list[str] = []
    if any(ch in pattern for ch in "*?["):
        for dirpath, _, filenames in os.walk(root):
            for name in filenames:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(
                        "/" + rel, "/" + pattern):
                    matches.append(rel)
        return sorted(matches)
    return [pattern] if os.path.exists(os.path.join(root, pattern)) else []


def check_files_exist(root: str, patterns: list[str]) -> list[dict]:
    findings: list[dict] = []
    for pattern in patterns:
        matched = _glob_match(pattern, root)
        if matched:
            findings.append({"check": "files_exist", "ok": True,
                             "target": pattern,
                             "evidence": f"found: {', '.join(matched[:5])}"
                             + ("…" if len(matched) > 5 else "")})
        else:
            findings.append({"check": "files_exist", "ok": False,
                             "target": pattern,
                             "evidence": "no such file"})
    return findings


def changed_set(root: str) -> tuple[set[str], bool]:
    """Return ``(changed_paths, is_git)`` vs HEAD (tracked + untracked)."""
    code, _ = _git(root, ["rev-parse", "--is-inside-work-tree"])
    if code != 0:
        return set(), False
    changed: set[str] = set()
    code, out = _git(root, ["diff", "--name-only", "HEAD", "--", "."])
    if code == 0:
        changed.update(p for p in out.splitlines() if p)
    code, out = _git(root, ["ls-files", "--others", "--exclude-standard"])
    if code == 0:
        changed.update(p for p in out.splitlines() if p)
    return changed, True


def check_files_nonempty(root: str, patterns: list[str]) -> list[dict]:
    """Required artifacts must exist AND be non-empty."""
    findings: list[dict] = []
    for pattern in patterns:
        matched = _glob_match(pattern, root)
        nonempty = [p for p in matched if _is_nonempty(root, p)]
        if nonempty:
            findings.append({"check": "files_nonempty", "ok": True,
                             "target": pattern,
                             "evidence": f"non-empty: "
                             f"{', '.join(nonempty[:5])}"})
        elif matched:
            findings.append({"check": "files_nonempty", "ok": False,
                             "target": pattern,
                             "evidence": "file exists but is empty — "
                             "the agent may have created a placeholder"})
        else:
            findings.append({"check": "files_nonempty", "ok": False,
                             "target": pattern,
                             "evidence": "no such file"})
    return findings


def _is_nonempty(root: str, rel: str) -> bool:
    try:
        return os.path.getsize(os.path.join(root, rel)) > 0
    except OSError:
        return False


def _added_lines(root: str) -> tuple[list[tuple[str, int]], bool]:
    """Added lines vs HEAD as ``[(path, line)]``. Untracked files count
    fully. Returns ``([], False)`` when there is no git baseline."""
    code, _ = _git(root, ["rev-parse", "--is-inside-work-tree"])
    if code != 0:
        return [], False
    code, _ = _git(root, ["rev-parse", "--verify", "HEAD"])
    if code != 0:
        return [], False
    out_lines: list[tuple[str, int]] = []
    code, out = _git(root, ["diff", "-U0", "HEAD", "--", "."])
    if code == 0:
        path: str | None = None
        for raw in out.splitlines():
            if raw.startswith("diff --git "):
                path = None
                continue
            if raw.startswith("+++ "):
                target = raw[4:].strip()
                if target == "/dev/null":
                    path = None
                    continue
                if target.startswith('"') and target.endswith('"'):
                    target = target[1:-1]
                path = (target[2:] if target.startswith("b/") else target)
            elif raw.startswith("@@ ") or raw.startswith("--- "):
                continue
            elif path is not None and raw.startswith("+") \
                    and not raw.startswith("+++"):
                out_lines.append((path, raw[1:]))
    code, untracked = _git(root, ["ls-files", "--others",
                                  "--exclude-standard"])
    if code == 0:
        for rel in untracked.splitlines():
            if not rel:
                continue
            try:
                with open(os.path.join(root, rel), "r", encoding="utf-8",
                          errors="replace") as handle:
                    for line in handle:
                        out_lines.append((rel, line.rstrip("\n")))
            except OSError:
                continue
    return out_lines, True


def check_added_forbid(root: str, patterns: list[str]) -> list[dict]:
    """Fail when user-forbidden patterns appear in added lines.

    Catches the classic gate-gaming move: narrowing tests to pass
    (``describe.only``, ``@pytest.mark.skip``, ``# noqa`` …) instead of
    fixing the code. Without a git baseline this check is explicitly
    skipped (reported, not silently passed).
    """
    added, has_baseline = _added_lines(root)
    if not has_baseline:
        return [{"check": "added_forbid", "ok": True, "target": "(repo)",
                 "evidence": "skipped: needs a git baseline with at "
                 "least one commit to know which lines are added"}]
    compiled = [(p, re.compile(p)) for p in patterns]
    hits: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for path, line in added:
        if "commit-guard-ignore" in line or "done-guard-ignore" in line:
            continue
        for pattern, regex in compiled:
            if regex.search(line) and (path, pattern) not in seen:
                seen.add((path, pattern))
                hits.append({"check": "added_forbid", "ok": False,
                             "target": pattern,
                             "evidence": f"forbidden pattern added in "
                             f"{path} (line content withheld)"})
    if not hits:
        hits.append({"check": "added_forbid", "ok": True, "target": "(repo)",
                     "evidence": f"no forbidden patterns in added lines"})
    return hits


def check_files_changed(root: str, patterns: list[str]) -> list[dict]:
    findings: list[dict] = []
    changed, is_git = changed_set(root)
    for pattern in patterns:
        matched = _glob_match(pattern, root)
        if not matched:
            findings.append({"check": "files_changed", "ok": False,
                             "target": pattern,
                             "evidence": "no such file"})
            continue
        if not is_git:
            # Without git there is no baseline: non-empty existence is the
            # documented fallback, flagged so nobody mistakes it for a diff.
            ok = any(os.path.getsize(os.path.join(root, p)) > 0
                     for p in matched)
            findings.append({"check": "files_changed", "ok": ok,
                             "target": pattern,
                             "evidence": "no git baseline; "
                             + ("non-empty file exists" if ok
                                else "file missing or empty")})
            continue
        hit = [p for p in matched if p in changed]
        if hit:
            findings.append({"check": "files_changed", "ok": True,
                             "target": pattern,
                             "evidence": f"changed vs HEAD: "
                             f"{', '.join(hit[:5])}"})
        else:
            findings.append({"check": "files_changed", "ok": False,
                             "target": pattern,
                             "evidence": "exists but identical to HEAD — "
                             "the agent may not have touched it"})
    return findings


def _tail(text: str) -> str:
    lines = text.splitlines()[-OUTPUT_TAIL_LINES:]
    return "\n".join(line[:OUTPUT_LINE_LIMIT] for line in lines)


def check_command(root: str, spec: dict, show_output: bool) -> dict:
    run = spec.get("run", "")
    timeout = spec.get("timeout", DEFAULT_TIMEOUT)
    expect_exit = spec.get("expect_exit", 0)
    cwd = os.path.join(root, spec.get("cwd", "."))
    if not isinstance(run, str) or not run.strip():
        return {"check": "command", "ok": False, "target": run,
                "evidence": "invalid 'run' (must be a non-empty string)"}
    try:
        timeout = int(timeout)
        if timeout < 1:
            raise ValueError
    except (ValueError, TypeError):
        return {"check": "command", "ok": False, "target": run,
                "evidence": f"invalid timeout: {spec.get('timeout')!r}"}
    if os.name == "nt":
        cmd: list[str] | str = ["cmd", "/c", run]
        shell = False
    else:
        cmd = run
        shell = True
    try:
        if shell:
            code, out, err, timed_out = _run_shell(cmd, cwd, timeout)
        else:
            code, out, err, timed_out = _run(cmd, cwd, timeout)
    except OSError as exc:
        return {"check": "command", "ok": False, "target": run,
                "evidence": f"could not execute: {exc}"}
    combined = (out or "") + ("\n" + err if err else "")
    problems: list[str] = []
    if timed_out:
        problems.append(f"timed out after {timeout}s (killed)")
    if code != expect_exit:
        problems.append(f"exit {code}, expected {expect_exit}")
    for wanted in spec.get("expect_output", []) or []:
        if not re.search(wanted, combined):
            problems.append(f"output missing required pattern: {wanted!r}")
    for banned in spec.get("forbid_output", []) or []:
        if re.search(banned, combined):
            problems.append(f"output contains forbidden pattern: {banned!r}")
    evidence = "; ".join(problems) if problems else f"exit {code} as expected"
    if show_output and (out or err):
        evidence += "\n--- output tail ---\n" + _tail(combined)
    return {"check": "command", "ok": not problems, "target": run,
            "evidence": evidence, "exit_code": code,
            "timed_out": timed_out}


def _check_string_list(data: dict, key: str) -> str:
    value = data.get(key, [])
    if not isinstance(value, list):
        return f"'{key}' must be a list of paths/globs"
    for i, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            return f"'{key}[{i}]' must be a non-empty string"
    return ""


def load_criteria(path: str) -> tuple[dict | None, str]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None, f"criteria file not found: {path}"
    except (OSError, ValueError) as exc:
        return None, f"criteria file unreadable/invalid JSON: {exc}"
    if not isinstance(data, dict):
        return None, "criteria must be a JSON object"
    unknown = set(data) - TOP_LEVEL_KEYS
    if unknown:
        return None, f"unknown top-level key(s): {sorted(unknown)} " \
                     f"(allowed: {sorted(TOP_LEVEL_KEYS)})"
    for key in ("files_exist", "files_nonempty", "files_changed"):
        problem = _check_string_list(data, key)
        if problem:
            return None, problem
    added_forbid = data.get("added_forbid", [])
    if not isinstance(added_forbid, list) or not all(
            isinstance(p, str) and p.strip() for p in added_forbid):
        return None, "'added_forbid' must be a list of non-empty regex " \
                     "strings"
    for pattern in added_forbid:
        try:
            re.compile(pattern)
        except re.error as exc:
            return None, f"added_forbid has an invalid regex " \
                         f"{pattern!r}: {exc}"
    commands = data.get("commands", [])
    if not isinstance(commands, list):
        return None, "'commands' must be a list"
    for i, spec in enumerate(commands):
        if not isinstance(spec, dict):
            return None, f"commands[{i}] must be an object"
        unknown = set(spec) - COMMAND_KEYS
        if unknown:
            return None, f"commands[{i}] unknown key(s): {sorted(unknown)}"
        run = spec.get("run", "")
        if not isinstance(run, str) or not run.strip():
            return None, f"commands[{i}].run must be a non-empty string"
        if "expect_exit" in spec and (
                not isinstance(spec["expect_exit"], int)
                or isinstance(spec["expect_exit"], bool)):
            return None, f"commands[{i}].expect_exit must be an integer"
        if "timeout" in spec:
            try:
                if int(spec["timeout"]) < 1:
                    raise ValueError
            except (ValueError, TypeError):
                return None, f"commands[{i}].timeout must be an integer >= 1"
        for list_key in ("expect_output", "forbid_output"):
            patterns = spec.get(list_key, [])
            if not isinstance(patterns, list) or not all(
                    isinstance(p, str) for p in patterns):
                return None, f"commands[{i}].{list_key} must be a list " \
                              "of regex strings"
            for pattern in patterns:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    return None, f"commands[{i}].{list_key} has an invalid " \
                                 f"regex {pattern!r}: {exc}"
        if "cwd" in spec and not isinstance(spec["cwd"], str):
            return None, f"commands[{i}].cwd must be a string"
    if not data.get("files_exist") and not data.get("files_nonempty") \
            and not data.get("files_changed") \
            and not data.get("added_forbid") and not commands:
        return None, "no checks declared (nothing to verify)"
    return data, ""


def run_all(root: str, criteria: dict, show_output: bool) -> list[dict]:
    findings: list[dict] = []
    for pattern in criteria.get("files_exist", []) or []:
        findings.extend(check_files_exist(root, [pattern]))
    for pattern in criteria.get("files_nonempty", []) or []:
        findings.extend(check_files_nonempty(root, [pattern]))
    for pattern in criteria.get("files_changed", []) or []:
        findings.extend(check_files_changed(root, [pattern]))
    added_forbid = criteria.get("added_forbid", []) or []
    if added_forbid:
        findings.extend(check_added_forbid(root, added_forbid))
    for spec in criteria.get("commands", []) or []:
        findings.append(check_command(root, spec, show_output))
    return findings


def format_text(findings: list[dict]) -> str:
    passed = sum(1 for f in findings if f["ok"])
    out = [f"done-guard: {passed}/{len(findings)} checks passed"]
    for finding in findings:
        mark = "ok " if finding["ok"] else "FAIL"
        out.append(f"  [{mark}] ({finding['check']}) {finding['target']}")
        for line in finding["evidence"].splitlines():
            out.append(f"        {line}")
    out.append("PASS: work is evidenced" if passed == len(findings)
               else f"FAIL: {len(findings) - passed} check(s) unmet — "
                    "the task is not verifiably done")
    return "\n".join(out) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="done-guard",
        description="Verify an agent's work against declared acceptance "
                    "criteria (files, changes, commands). Observable "
                    "evidence only — never a claim of semantic correctness.",
    )
    parser.add_argument("--root", default=".",
                        help="workspace to verify (default: .)")
    parser.add_argument("--criteria", default=None,
                        help="path to acceptance JSON file "
                             "(default: <root>/acceptance.json)")
    parser.add_argument("--expect-file", action="append", default=[],
                        help="require a file/glob to exist (repeatable)")
    parser.add_argument("--expect-nonempty", action="append", default=[],
                        help="require a file/glob to exist and be non-empty "
                             "(repeatable)")
    parser.add_argument("--expect-change", action="append", default=[],
                        help="require a file/glob changed vs HEAD "
                             "(repeatable)")
    parser.add_argument("--forbid-added", action="append", default=[],
                        help="regex that must not appear in added lines, "
                             "e.g. focused-test markers (repeatable)")
    parser.add_argument("--run", action="append", default=[],
                        help="command that must exit 0 (repeatable)")
    parser.add_argument("--expect-output", action="append", default=[],
                        help="regex required in command output (repeatable)")
    parser.add_argument("--forbid-output", action="append", default=[],
                        help="regex that must not appear (repeatable)")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help=f"per-command timeout in seconds "
                             f"(default: {DEFAULT_TIMEOUT})")
    parser.add_argument("--no-output", action="store_true",
                        help="omit output tails from the report")
    parser.add_argument("--format", choices=("text", "json"), default="text",
                        help="output format (default: text)")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.timeout < 1:
        parser.error("--timeout must be at least 1")
    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print(f"done-guard: error: --root is not a directory: {args.root}",
              file=sys.stderr)
        return 2

    criteria: dict = {"files_exist": [], "files_nonempty": [],
                   "files_changed": [], "added_forbid": [], "commands": []}
    crit_path = (os.path.abspath(args.criteria) if args.criteria
                 else os.path.join(root, "acceptance.json"))
    if args.criteria or os.path.isfile(crit_path):
        data, err = load_criteria(crit_path)
        if data is None:
            print(f"done-guard: error: {err}", file=sys.stderr)
            return 2
        criteria = data
    for pattern in args.expect_file:
        criteria.setdefault("files_exist", []).append(pattern)
    for pattern in args.expect_nonempty:
        criteria.setdefault("files_nonempty", []).append(pattern)
    for pattern in args.forbid_added:
        try:
            re.compile(pattern)
        except re.error as exc:
            print(f"done-guard: error: invalid --forbid-added regex "
                  f"{pattern!r}: {exc}", file=sys.stderr)
            return 2
        criteria.setdefault("added_forbid", []).append(pattern)
    for pattern in args.expect_change:
        criteria.setdefault("files_changed", []).append(pattern)
    if args.run and (args.expect_output or args.forbid_output):
        if len(args.run) > 1:
            print("done-guard: error: --expect-output/--forbid-output apply "
                  "to a single --run; use a criteria file for several",
                  file=sys.stderr)
            return 2
    for command in args.run:
        criteria.setdefault("commands", []).append({
            "run": command, "timeout": args.timeout,
            "expect_exit": 0, "expect_output": args.expect_output,
            "forbid_output": args.forbid_output})
    if not criteria.get("files_exist") and not criteria.get("files_nonempty") \
            and not criteria.get("files_changed") \
            and not criteria.get("added_forbid") \
            and not criteria.get("commands"):
        print("done-guard: error: no checks declared (pass flags or a "
              "criteria file)", file=sys.stderr)
        return 2

    findings = run_all(root, criteria, show_output=not args.no_output)
    ok = all(f["ok"] for f in findings)
    if args.format == "json":
        print(json.dumps({"ok": ok, "root": root, "findings": findings},
                         indent=2))
    else:
        print(format_text(findings), end="")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
