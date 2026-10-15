# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | Yes |

## Design guarantees

- **Read-only except running your listed commands.** done-guard never
  writes, stages, deletes, or transmits anything. No network, no deps.
- **Commands come only from your criteria.** Review criteria files from
  untrusted sources like shell scripts — they execute with your
  permissions, bounded by `--timeout`.
- **Output tails may contain secrets** if your commands print them. Tails
  are bounded (20 lines) and suppressible with `--no-output`. The JSON
  report contains the same evidence — treat it accordingly.
- **No semantic claims.** A passing gate means the declared evidence was
  observed, not that the work is correct.

## Reporting a vulnerability

Open a GitHub issue with the `security` label. Never include real secrets.
