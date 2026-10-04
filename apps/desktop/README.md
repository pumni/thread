# Threads Desktop M1 prototype

This directory contains the DX-02 Tauri shell and the in-progress DX-04 disposable Controller slice. Python remains the business runtime and PostgreSQL remains its source of truth. The Rust layer owns local provisioning and process supervision; React owns presentation and has no filesystem, process, database, or secret access.

## Local development

Requirements: Bun 1.4.2, Rust stable, and the Windows Tauri prerequisites (WebView2 and the Visual Studio C++ build tools).

```powershell
bun install --frozen-lockfile
bun run dev
```

Use `bun run tauri dev` to run the native shell. For Controller mode, set `THREADS_DESKTOP_RUNTIME_DIR` to an absolute `shared` candidate root produced by `packaging/windows_desktop/build_runtime.py --layout shared`; the root contains `threads-runtime/` and `postgresql/`. No host Python/PostgreSQL or Docker fallback is used. Controller uses one current-user data root, a DPAPI-protected database credential, persisted explicit IPv4/HTTPS endpoint, and separate HTTP and scheduler processes. Uvicorn directly terminates TLS; one HTTPS/WSS listener serves Operator, Worker, health, readiness, and metrics. Root and leaf keys are protected by CurrentUser DPAPI; only the temporary leaf serving key is materialized before HTTP startup and removed during shutdown. The local Controller client verifies HTTPS against its own private root.

An unconfigured Controller has no LAN listener; setup explicitly persists a stable IPv4 and HTTPS port before provisioning TLS. The remote first-contact probe sends no HTTP application request or credentials. Operators compare the candidate root's SHA-256 DER fingerprint with the local Controller display, then confirm the opaque probe before credentials are enabled. Product trust is application-private and does not install a Windows CA. The runtime is unavailable before Windows sign-in; Windows logout is unsupported. Portable backup and production durability are not available.

The configured endpoint can be explicitly reconfigured. Before first Owner setup, the local setup flow may correct it; after an Owner exists, native code requires an authenticated OWNER or ADMIN session. Reconfiguration preserves the root fingerprint, issues a fresh leaf for the exact new IP SAN, and keeps PostgreSQL running during the controlled listener/scheduler transition. Linux/Docker uses explicit `provision`, startup `ensure`, endpoint `reissue`, and local `fingerprint`; normal startup never creates or repairs root identity state.

On Windows, startup is serialized by a named mutex and readiness event. The first process keeps startup ownership through Tauri plugin and application setup, then signals readiness. Later launches wait for that signal before entering the official `tauri-plugin-single-instance`, which remains the first Tauri plugin and forwards activation to the primary window. A startup owner that exits before readiness leaves an abandoned mutex for a waiting process to take over; timeout or an unready normal release fails closed.

## Checks

```powershell
bun run lint
bun run format:check
bun run typecheck
bun run test
bun run build
cargo fmt --check --manifest-path src-tauri/Cargo.toml
cargo clippy --manifest-path src-tauri/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path src-tauri/Cargo.toml
```

The reusable `Desktop Diagnostic` component installs the frozen `bun.lock` and runs frontend checks, native Rust checks, the Windows tray lifecycle smoke, DX-03 package feasibility checks, and the DX-04 Controller lifecycle smoke on GitHub-hosted Windows x64. `PR Acceptance` and `Main Verification` call it with an exact source SHA. The DX-04 smoke uses disposable synthetic database state; its artifact contains evidence only, never a PostgreSQL data directory.

## Contract boundary

The exposed Tauri commands are an explicit allowlist in `src/desktop.ts` and `src-tauri/src/lib.rs`. The webview stores no database credential, private key, or bearer and cannot access native filesystem or process APIs. Controller setup and trust use semantic native commands; no generic file, HTTP, or TLS-bypass command is exposed. Linux/Docker uses the repository-local Python TLS admin CLI and direct Uvicorn TLS, with root-admin state unavailable to ordinary HTTP and scheduler containers. This is not the DX-12 installer or a production distribution.
