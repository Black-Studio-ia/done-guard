# done-guard

One sentence: **done-guard verifies an AI agent's finished work against acceptance criteria you declare in advance — required files, required changes, required command results — using only observable evidence, with zero dependencies and no AI model involved.**

## The problem

Coding agents regularly report tasks as finished while acceptance criteria are unmet: the turn ends after an acknowledgement instead of the fix, a required build never ran, or "done" means a subset of the work (open reports across Codex and Claude Code, e.g. `openai/codex#50771`, `#41176`, `#36596`). Reviewing a diff tells you what changed; it doesn't tell you whether the *assigned work* is complete.

## How it works

You declare the finish line **before** (or after) the agent runs — `acceptance.json`:

```json
{
  "files_exist": ["dist/app.js", "CHANGELOG.md"],
  "files_nonempty": ["dist/app.js"],
  "files_changed": ["src/**/*.py"],
  "added_forbid": ["\\.only\\(", "@pytest.mark.skip", "# noqa"],
  "commands": [
    {"run": "python -m pytest -q", "timeout": 300, "expect_exit": 0,
     "expect_output": ["passed"], "forbid_output": ["FAILED", "Traceback"]}
  ]
}
```

Then:

```sh
python doneguard.py --root .
```

```
done-guard: 3/4 checks passed
  [ok ] (files_exist) dist/app.js
  [ok ] (files_changed) src/**/*.py
  [ok ] (command) python -m pytest -q
  [FAIL] (files_exist) CHANGELOG.md
FAIL: 1 check(s) unmet — the task is not verifiably done
```

Exit codes: `0` evidenced, `1` unmet, `2` misconfigured. `--format json` for pipelines. Ad-hoc flags (`--expect-file`, `--expect-change`, `--run`) work without a file.

## Why done-guard instead of …

| Alternative | Difference |
|---|---|
| SWE-bench / terminal-bench harnesses | Full model-evaluation rigs needing Docker, ~120 GB disk, and model runs. done-guard checks *your* task in place, in seconds, with no model. Different job. |
| promptfoo and prompt-eval tools | Evaluate model *outputs* with judges/models. done-guard executes *your repo's* commands and inspects *your* files. Complementary. |
| Plain CI (tests must pass) | CI proves the suite is green, not that the assigned task's artifacts exist or that anything changed at all. done-guard binds checks to the task. |
| Bespoke per-project scripts | Teams already hand-roll "don't say done without evidence" scripts — done-guard is that pattern as a reusable, tested tool. |
| `donegate` (Node, zero-dep) | The closest alternative and genuinely more complete in places: agent Stop-hook integration that bounces the agent back mid-session, tamper guards (deleted/skipped tests, lint-disables, DONE.md edits), receipts, PR comments, `init` scaffolding. Choose donegate when you want hook-gated workflows and anti-gaming guards. Choose done-guard when you want a single Python file with no Node runtime, `files_changed`-vs-HEAD and non-empty-artifact semantics, strict fail-closed config validation, and JSON-first output. |
| `agentlint`, `agent-audits` | Hook/CI-gated acceptance workflows in the same family; evaluate them alongside donegate if you need framework integration. |
| The agent's own "done" message | The thing being verified. Never accept it unexamined. |

## What this proves — and what it does not

**Proves:** the listed files exist; the listed files differ from git HEAD (so *something* was touched); the listed commands exited as required with required/forbidden output observed.

**Does not prove:** semantic correctness, requirement coverage beyond what you wrote, absence of regressions outside the listed commands, or that the agent "understood" anything. A green gate means *evidenced*, not *correct*. Write criteria that actually exercise the requirement (run the app's real checks, not just `echo ok`).

## Features

- `files_exist` (globs supported), `files_nonempty`, `files_changed` (new/untracked/modified vs HEAD; honest non-git fallback), `files_contain` (required content patterns), `added_forbid` (user regexes banned from added lines: catches narrowed tests), `git_branch` (verifies the checked-out state), `commands` (exit code, timeouts, output patterns)
- Fail-closed config: unknown keys, wrong types, invalid regexes, malformed JSON, and empty criteria are usage errors (exit 2), never silent passes
- Timeouts kill the command (whole process group on POSIX, so stray grandchildren die too) and fail the check; spawn failures are findings, not tracebacks
- Output tails are bounded (last 20 lines, truncated); `--no-output` suppresses them entirely
- Cross-platform command execution (Windows-safe); per-command timeouts with kills
- Zero dependencies, offline, read-only (never modifies your tree)

## Requirements

- Python 3.9+ (standard library only), git (for `files_changed` precision), Windows/macOS/Linux

## Installation

```sh
curl -O https://raw.githubusercontent.com/Black-Studio-ia/done-guard/main/doneguard.py
```

Verify: `python doneguard.py --version`

## Suggested workflow

1. Before delegating, write `acceptance.json` (or ask the agent to draft it — then review it yourself).
2. Agent works.
3. Run `python doneguard.py --root .` — locally, in CI, or as a session-close gate.
4. Anything FAIL → send the report back to the agent as the next prompt.

This repo dogfoods it: [`acceptance.json`](acceptance.json) requires the test suite green with no failures.

### Agent Stop hooks (manual wiring)

done-guard ships no installer, but any agent hook that runs a command can
call it — the same pattern hook-based gates use. Example shape for a
Claude Code Stop hook (`.claude/settings.json`; adjust paths to your
checkout — verify the working directory your agent uses for hooks):

```json
{"hooks": {"Stop": [{"hooks": [{"type": "command",
  "command": "python /path/to/doneguard.py --root /path/to/checkout"}]}]}}
```

A red verdict's text goes back to the agent as the next prompt. For
managed hook installation, bounce-back loops, and tamper receipts,
donegate covers that workflow better — see the comparison above.

### GitHub Actions

See [`.github/workflows/ci.yml`](.github/workflows/ci.yml) — test suite plus a done-guard self-gate on every push.

## Usage

```
usage: done-guard [--root DIR] [--criteria FILE] [--expect-file P]
                  [--expect-nonempty P] [--expect-change P]
                  [--forbid-added RE] [--expect-contains FILE:REGEX]
                  [--expect-branch BRANCH] [--run CMD] [--expect-output RE]
                  [--forbid-output RE] [--timeout S] [--no-output]
                  [--format text|json]
```

## Security and privacy

- **Read-only.** Runs only the commands *you* list, with a timeout; never writes, deletes, stages, or transmits anything. No network access, no dependencies.
- **It executes commands** — only from your criteria file/flags, with a bounded timeout. Review criteria files from untrusted sources exactly as you would a shell script.
- **Output tails can contain secrets** if your commands print them: bounded, and suppressible with `--no-output`. See [SECURITY.md](SECURITY.md).

## Known limitations

- `files_changed` needs git for precise diffs; without git it degrades to non-empty-existence (explicitly reported).
- Shell features in `run` (pipes, env vars) use the platform shell; quote accordingly. Commands must be non-interactive.
- Glob `**` matching is Python `fnmatch`-based, not gitignore-based.
- Long commands delay the verdict until `--timeout`; there is no streaming progress.

## Running the tests

```sh
python -m unittest discover -s tests -v
```

 38 tests, stdlib only: criteria validation (incl. wrong types and bad regexes), file/change/nonempty/content/branch checks (real git fixtures), added_forbid (narrowed tests, renames, removed-lines exclusion, unicode paths, binary diffs, no-baseline skip), command pass/fail/timeout/missing-binary/spawn-failure, output-pattern gates (including exit-0-with-FAILED adversarial case), orphan-process handling, CLI exit codes, JSON validity incl. the text-on-stderr usage-error contract, end-to-end criteria file.

## License

MIT — see [LICENSE](LICENSE).

## Support this project

If this tool catches an unfinished agent task before it ships, you may optionally support its continued development with USDT.

Network: USDT TRC20 (Tron)

USDT address: `TXQCrX61CceFxX5gnFg9N3ssZEC7MavwBQ`

Please verify the network and address independently before sending — USDT on the wrong network can be lost permanently. Contributions are voluntary and do not guarantee specific features, services, or financial returns. The software remains fully usable under its MIT license regardless.

Contact: `ia.creative.tn@gmail.com`
