# TP-000: Legacy credential remediation

Date: 2026-09-25

## Inventory and remediation

- The legacy report in the current default branch contains redacted MongoDB and proxy credential examples.
- The repository history contains a MongoDB URI with embedded authentication in the initial legacy report commit (`63fecc0`). Its values are intentionally omitted here. Gitleaks' built-in rules did not detect this URI, so `.gitleaks.toml` adds a rule for authenticated MongoDB and proxy URIs.
- The current working tree scan with Gitleaks 8.30.1 and the custom rule reports no findings. The full-history scan reports two matches for the same historical URI (raw and percent-decoded), with redacted output. Its values' test/production status and any rotation or revocation cannot be established from repository artifacts.
- No credential value was added to the current working tree by this remediation.

## Ongoing scan

`.github/workflows/secret-scan.yml` scans the complete Git history for pull requests, pushes to `main`, weekly, and on manual dispatch. It pins Gitleaks 8.30.1, verifies the downloaded archive against the release checksum file, and runs the scan with redaction enabled. The history scan will continue to fail on the unverified historical URI until its status is resolved.

## Follow-up required

The repository does not establish whether the historical MongoDB or proxy values were real or test-only, or whether potentially real values were rotated or revoked. The repository owner/reviewer must confirm that status without sharing any values. If any value was real, confirm rotation or revocation before accepting TP-000.
