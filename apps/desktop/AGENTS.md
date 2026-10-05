# Desktop Subtree Instructions (apps/desktop)

This document provides localized architecture boundaries, responsibilities, and validation guidance for agents working within the Windows Desktop application (`apps/desktop/`).

## Architectural Boundaries & Ownership

1. **Rust / Native Supervisor (`src-tauri/`):**
   - Owns native process lifecycle, process-tree / Job Object ownership and cleanup, child process management (PostgreSQL, Python Controller, Worker sidecars), and graceful application Quit.
   - Owns Windows security boundaries: DPAPI CurrentUser custody, TLS root/leaf private keys, file permissions, and system tray integration.
   - Owns local Controller HTTPS termination configuration and TLS probe validation.

2. **React / Webview Frontend (`src/`):**
   - Restricted to user interface rendering, operator interaction, and presentation state.
   - **Strict prohibition:** React code must never connect directly to the database, store unredacted secrets/keys, write directly to arbitrary filesystem paths, or expose unrestricted native execution escape hatches.
   - State displayed in the UI (e.g. "Signed in") does not constitute authoritative authentication proof; server/database session authorization remains authoritative.

3. **Python Runtime / Control Plane Boundary:**
   - Business state machines, data modeling, database migrations, and domain invariants remain in Python/PostgreSQL.
   - Desktop manages Python execution as a managed sidecar; it does not replace or bypass Python business validation.

4. **Package & Runtime Manager:**
   - Frontend toolchain uses Bun/Vite.
   - Rust toolchain uses Cargo / Tauri 2 CLI.

## Progressive Disclosure & Documentation Routing

Do not preload the entire Desktop documentation suite for routine tasks. Retrieve specialized documents only when required by the task:

- **Architecture Decisions:** Read [`docs/adr/0007-windows-first-single-app-desktop.md`](../../docs/adr/0007-windows-first-single-app-desktop.md) when modifying native application topology or supervisor boundaries.
- **Security & Private TLS:** Read [`docs/desktop/SECURITY_AND_PROTOCOLS.md`](../../docs/desktop/SECURITY_AND_PROTOCOLS.md) when altering HTTPS/WSS endpoints, certificates, or DPAPI storage.
- **Acceptance & Verification:** Read [`docs/desktop/ACCEPTANCE_MATRIX.md`](../../docs/desktop/ACCEPTANCE_MATRIX.md) when preparing acceptance evidence or checking platform gates.
- **Packaging Smoke & Rehearsal:** Use the `desktop-acceptance` skill or inspect [`docs/desktop/WINDOWS_REHEARSAL.md`](../../docs/desktop/WINDOWS_REHEARSAL.md) when testing packaged binaries.

## Local Validation Entrypoints

For the canonical command list, consult [`apps/desktop/README.md`](README.md#checks). Execute the checks matching the files changed:

- **Frontend changes:** Run Bun linting, formatting, typechecking, test, and build checks from `apps/desktop/`:
  ```powershell
  bun run lint
  bun run format:check
  bun run typecheck
  bun run test
  bun run build
  ```
- **Native Rust changes:** Run Cargo formatting, clippy, and unit tests from `apps/desktop/`:
  ```powershell
  cargo fmt --check --manifest-path src-tauri/Cargo.toml
  cargo clippy --manifest-path src-tauri/Cargo.toml --all-targets -- -D warnings
  cargo test --manifest-path src-tauri/Cargo.toml
  ```
- **Full Windows Desktop Preflight:** From repository root, execute the bundled preflight script:
  ```powershell
  powershell -NoProfile -File scripts/windows_local_preflight.ps1
  ```
