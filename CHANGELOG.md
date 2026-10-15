# Changelog

## 0.1.0 — 2026-10-15

Initial release.

- Declarative acceptance gates: files_exist, files_changed (git-aware),
  commands (exit codes, timeouts, output patterns).
- Fail-closed configuration; bounded output evidence; `--no-output`.
- 21-test stdlib `unittest` suite incl. adversarial cases; CI workflow
  with done-guard self-gate.
