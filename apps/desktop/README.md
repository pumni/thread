# Threads Desktop M1 prototype

This directory contains the DX-02 Tauri shell and the in-progress DX-04 disposable Controller slice. Python remains the business runtime and PostgreSQL remains its source of truth. The Rust layer owns local provisioning and process supervision; React owns presentation and has no filesystem, process, database, or secret access.

## Local development

Requirements: Bun 1.4.2, Rust stable, and the Windows Tauri prerequisites (WebView2 and the Visual Studio C++ build tools).

```powershell
bun install --frozen-lockfile
bun run dev
```

Use `bun run tauri dev` to run the native shell. For Controller mode, set `THREADS_DESKTOP_RUNTIME_DIR` to an absolute `shared` candidate root produced by `packaging/windows_desktop/build_runtime.py --layout shared`; the root contains `threads-runtime/` and `postgresql/`. No host Python/PostgreSQL or Docker fallback is used. Controller uses one current-user data root, a DPAPI-protected database credential, a persisted loopback endpoint, and separate HTTP and scheduler processes. The M1 bootstrap boundary creates no Owner. Worker remains on the DX-02 mock helper; Console starts no helper. Closing the window hides to tray; explicit Quit stops scheduler, HTTP, then PostgreSQL.

M1 is internal and disposable. It has no Owner account or LAN endpoint. The runtime is unavailable before Windows sign-in; Windows logout is unsupported. Portable backup and production durability are not available.

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

The path-filtered `Desktop CI` workflow installs the frozen `bun.lock` and runs frontend checks, native Rust checks, the Windows tray lifecycle smoke, DX-03 package feasibility checks, and the DX-04 Controller lifecycle smoke on GitHub-hosted Windows x64. The DX-04 smoke uses disposable synthetic database state; its artifact contains evidence only, never a PostgreSQL data directory.

## Contract boundary

The exposed Tauri commands are an explicit allowlist in `src/desktop.ts` and `src-tauri/src/lib.rs`. The webview stores no database credential and cannot access native filesystem or process APIs. M1 HTTP is unauthenticated and loopback-only; Owner/RBAC and LAN HTTPS remain later issues. This is not the DX-12 installer or a production distribution.
