# Threads Desktop scaffold

This directory contains the DX-02 Windows-first Tauri 2 shell. Python remains the business runtime and PostgreSQL remains its source of truth. The Rust layer owns the narrow desktop lifecycle and native IPC surface; React owns presentation and has no filesystem, process, database, or secret access.

## Local development

Requirements: Bun 1.4.2, Rust stable, and the Windows Tauri prerequisites (WebView2 and the Visual Studio C++ build tools).

```powershell
bun install --frozen-lockfile
bun run dev
```

Use `bun run tauri dev` to run the native shell. The first role choice is persisted in a versioned native config file. Controller and Worker start the fixed mock helper; Console starts no helper. Closing the window hides it to the tray. The tray Quit action opens an explicit confirmation, asks the mock helper to stop, then exits.

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

The path-filtered `Desktop CI` workflow installs the frozen `bun.lock` and runs frontend checks, native Rust checks, the exact-SHA Windows tray/supervisor lifecycle smoke, and the DX-03 Windows runtime feasibility smoke on GitHub-hosted Windows x64. DX-03 runtime package experiments are in `packaging/windows_desktop`; this scaffold does not start Python or PostgreSQL yet.

## Contract boundary

The exposed Tauri commands are an explicit allowlist in `src/desktop.ts` and `src-tauri/src/lib.rs`. The webview stores no bearer token and cannot access native filesystem or process APIs. Operator sign-in, business endpoints, credential storage, live PostgreSQL, and real Controller/Worker packaging remain outside DX-02.
