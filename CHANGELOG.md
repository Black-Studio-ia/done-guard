# Changelog

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
