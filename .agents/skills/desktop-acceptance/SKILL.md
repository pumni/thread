---
name: desktop-acceptance
description: Verify Windows Desktop local preflight, packaging smoke tests, and native supervisor acceptance evidence. Not for hosted CI triage.
---

# Desktop Acceptance Skill

## Purpose
Guide the local execution and verification of Windows Desktop acceptance criteria, packaging smoke tests, and native supervisor evidence without duplicating canonical security and protocol specifications.

## Trigger Conditions
Use this skill when:
- Executing local Windows preflight via `scripts/windows_local_preflight.ps1`.
- Running or authoring Desktop packaging smoke tests in `packaging/windows_desktop/`.
- Preparing or reviewing Controller private TLS, certificate verification, or native supervisor acceptance evidence.
- Verifying native process management (process trees, graceful Quit vs abrupt crash recovery).

## Non-Trigger Conditions
Do NOT use this skill for:
- Triaging hosted GitHub Actions runner failures (use `ci-failure-triage`).
- Pure React component development without native bridge interactions (use `apps/desktop/AGENTS.md`).
- Standard Linux/Docker backend Python changes without Desktop packaging implications.

## Canonical References
Inspect these canonical sources before verifying Desktop acceptance:
- `docs/adr/0007-windows-first-single-app-desktop.md` (canonical Desktop architecture).
- `docs/desktop/SECURITY_AND_PROTOCOLS.md` (authoritative TLS, DPAPI, and protocol contracts).
- `docs/desktop/ACCEPTANCE_MATRIX.md` (acceptance criteria & evidence requirements).
- `docs/desktop/WINDOWS_REHEARSAL.md` (packaging & manual verification runbook).
- `packaging/windows_desktop/smoke_desktop_lifecycle.ps1`.
- `packaging/windows_desktop/smoke_controller_lifecycle.ps1`.
- `scripts/windows_local_preflight.ps1`.

## Invariants & Design Rules
1. **Local Preflight Gate:** Local preflight must run `scripts/windows_local_preflight.ps1` with the bundled PostgreSQL 17 tree. Never report PASS if PostgreSQL was skipped.
2. **Consult Canonical Source of Truth:** Do not infer or invent TLS, SAN, port, or credential rules. Always consult `docs/desktop/SECURITY_AND_PROTOCOLS.md` and ADR-0007 for exact accepted policy.
3. **Authoritative Evidence First:** UI Automation presence in a webview is not proof of operator authentication; inspect database session state directly.
4. **Stop Rather Than Weaken Invariants:** If local tests fail or native behavior diverges from accepted contracts, stop and report rather than weakening security boundaries, disabling TLS checks, or hardcoding bypasses.

## Step-by-Step Procedure
1. **Identify the acceptance surface:** Determine whether the task touches local preflight, packaging smoke, Controller HTTPS/WSS, or supervisor lifecycle.
2. **Consult matching canonical document:** Read the relevant section of `docs/desktop/ACCEPTANCE_MATRIX.md` or `docs/desktop/SECURITY_AND_PROTOCOLS.md`.
3. **Inspect current implementation:** Check current scripts in `packaging/windows_desktop/` and native code in `apps/desktop/src-tauri/`.
4. **Run targeted local verification:**
   - For general preflight:
     ```powershell
     powershell -NoProfile -File scripts/windows_local_preflight.ps1
     ```
   - For packaging/lifecycle smoke:
     ```powershell
     powershell -NoProfile -File packaging/windows_desktop/smoke_desktop_lifecycle.ps1
     ```
   - For Controller HTTPS lifecycle smoke:
     ```powershell
     powershell -NoProfile -File packaging/windows_desktop/smoke_controller_lifecycle.ps1
     ```
5. **Inspect authoritative evidence:** Review log output, exit codes, certificate fingerprints, and PostgreSQL records to ensure requirements are met before reporting results.
