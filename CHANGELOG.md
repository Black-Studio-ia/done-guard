# Changelog

## 0.2.0 — 2026-10-15

- New check kind `files_nonempty` (+ `--expect-nonempty`): required
  artifacts must exist and be non-empty (catches placeholder files).
- Fail-closed criteria validation: wrong types, empty strings, invalid
  regexes, non-integer timeouts/exit codes are usage errors, never
  silent passes or tracebacks.
- Robust process handling: spawn failures become findings; POSIX timeouts
  kill the whole process group so orphaned grandchildren cannot survive;
  the direct child is always reaped (no zombies / ResourceWarning).
- Fixed `__version__` lagging behind CHANGELOG (now 0.2.0).

## 0.1.1 — 2026-10-15

Fixed critical defect found by CI: on POSIX systems a command timeout
raised an unhandled `TimeoutExpired` that crashed the whole gate instead
of failing one check (Windows was unaffected — it uses the argv path).
Shell execution now goes through `_run_shell`, which converts timeouts to
`exit 124 + timed_out` findings, with a dedicated regression test.

## 0.1.0 — 2026-10-15

Initial release.

- Declarative acceptance gates: files_exist, files_changed (git-aware),
  commands (exit codes, timeouts, output patterns).
- Fail-closed configuration; bounded output evidence; `--no-output`.
- 21-test stdlib `unittest` suite incl. adversarial cases; CI workflow
  with done-guard self-gate.
