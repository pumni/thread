---
name: desktop-acceptance
description: Use this skill when running or preparing Windows Desktop v1 local preflight, non-admin packaging smoke tests, Controller HTTPS/TLS certificate verification, or native supervisor lifecycle evidence. Do NOT use for hosted CI workflow triage, React-only styling changes, or Control Plane backend migrations.
---

# Desktop Acceptance Skill

## Purpose
Guide the local execution and verification of Windows Desktop acceptance criteria, including packaging smoke tests, direct Controller TLS termination, and native supervisor process lifecycle management.

## Trigger Conditions
Use this skill when:
- Executing local Windows preflight via `scripts/windows_local_preflight.ps1`.
- Running or authoring Desktop packaging smoke tests in `packaging/windows_desktop/`.
- Verifying Controller HTTPS/WSS private TLS termination, leaf SAN validation, or DPAPI key protection.
- Validating native Tauri supervisor behavior (child process trees, graceful Quit vs abrupt crash, PostgreSQL WAL preservation).

## Non-Trigger Conditions
Do NOT use this skill for:
- Triaging hosted GitHub Actions runner failures (use `ci-failure-triage`).
- Pure React component development without native bridge interactions (use `apps/desktop/AGENTS.md`).
- Standard Linux/Docker backend Python changes without Desktop packaging implications.

## Canonical References
Inspect these sources before verifying Desktop acceptance:
- `docs/desktop/ACCEPTANCE_MATRIX.md`.
- `docs/desktop/WINDOWS_REHEARSAL.md`.
- `docs/desktop/SECURITY_AND_PROTOCOLS.md`.
- `packaging/windows_desktop/smoke_desktop_lifecycle.ps1`.
- `packaging/windows_desktop/smoke_controller_lifecycle.ps1`.
- `scripts/windows_local_preflight.ps1`.

## Invariants & Design Rules
1. **Local Preflight Discipline:** Always run `scripts/windows_local_preflight.ps1` with the bundled PostgreSQL 17 tree. Never report PASS if PostgreSQL skipped.
2. **Direct TLS Termination:** Uvicorn directly terminates HTTPS/WSS (`proxy_headers=False`). Leaf SAN is strictly configured LAN IPv4 + `127.0.0.1`. No plaintext listeners or reverse proxies in MVP.
3. **Supervisor Process Ownership:** Rust supervisor owns child process lifecycles. Explicit Quit must cleanly stop PostgreSQL and worker sidecars; crash recovery must preserve PostgreSQL WAL integrity.
4. **Dedicated Windows Runtime Principal:** DPAPI CurrentUser protection requires running under the designated interactive runtime user. Never execute runtime operations under the elevated installer context.
5. **Authoritative State over UI Presence:** UI Automation presence in a webview is not proof of operator authentication; verify database session state directly.

## Step-by-Step Procedure
1. **Verify local prerequisites:** Ensure bundled PostgreSQL and Python runtime artifacts are staged or built.
2. **Run local preflight script:**
   ```powershell
   powershell -NoProfile -File scripts/windows_local_preflight.ps1
   ```
3. **Execute packaging smoke:**
   ```powershell
   powershell -NoProfile -File packaging/windows_desktop/smoke_desktop_lifecycle.ps1
   ```
4. **Verify Controller HTTPS/TLS:** Run `packaging/windows_desktop/smoke_controller_lifecycle.ps1` to validate leaf renewal and certificate chain validation.
5. **Check evidence artifacts:** Inspect logs, exit codes, and database state to confirm clean teardown.
