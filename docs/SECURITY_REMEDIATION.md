# TP-000: Legacy credential remediation

Date: 2026-09-25

## Inventory and remediation

- The obsolete legacy report has been removed from the current tree, and README no longer references it. The planning, parity, architecture, and ADR documents remain the source of truth.
- A full-history Gitleaks scan found two matches for the same MongoDB URI with embedded authentication in commit `63fecc06b387e595acdff69e28c634acb5d0250c`: the raw URI and a percent-decoded match. Values are intentionally omitted. The built-in rules missed it, so `.gitleaks.toml` adds a rule for authenticated MongoDB and proxy URIs.
- The credential's test/production status and any rotation or revocation are not confirmed. No allowlist or history rewrite has been applied.
- No credential value was added to the current tree by this remediation.

## Current scanning decision

Until the owner confirms whether the historical value was test-only or was rotated/revoked, `.github/workflows/secret-scan.yml` scans the current tree and only commits introduced by the pull request or push. Scheduled and manual runs scan the current tree. This keeps checks effective for current and newly introduced secrets without allowlisting the unresolved historical finding.

The workflow pins Gitleaks 8.30.1, verifies the downloaded archive against the release checksum file, and runs with output redaction. The full-history audit command remains `gitleaks git --config=.gitleaks.toml --redact --no-banner --log-opts="--all" .`; it still reports the unresolved URI.

## Follow-up required

The repository owner/reviewer must confirm without sharing any values whether the historical MongoDB and proxy values were test-only or, if any were real, rotated/revoked. After confirmation, baseline only the known finding using the `embedded-service-uri-credentials` rule and exact historical commit above. Do not add a global allowlist. TP-000 remains unaccepted until the owner decision is recorded and GitHub Actions passes.
