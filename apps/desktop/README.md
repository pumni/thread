# Threads Desktop scaffold

This directory contains the DX-02 Windows-first Tauri 2 shell. Python remains the business runtime and PostgreSQL remains its source of truth. The Rust layer owns the narrow desktop lifecycle and native IPC surface; React owns presentation and has no filesystem, process, database, or secret access.

## Local development

Requirements: Node.js 24, pnpm 10.34.6 through Corepack, Rust stable, and the Windows Tauri prerequisites (WebView2 and the Visual Studio C++ build tools).

```powershell
corepack pnpm install --frozen-lockfile
corepack pnpm dev
```

Use `corepack pnpm tauri dev` to run the native shell. The first role choice is persisted in a versioned native config file. Controller and Worker start the fixed mock helper; Console starts no helper. Closing the window hides it to the tray. The tray Quit action opens an explicit confirmation, asks the mock helper to stop, then exits.

## Checks

```powershell
corepack pnpm lint
corepack pnpm format:check
corepack pnpm typecheck
corepack pnpm test
corepack pnpm build
cargo fmt --check --manifest-path src-tauri/Cargo.toml
cargo clippy --manifest-path src-tauri/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path src-tauri/Cargo.toml
```

The path-filtered `Desktop CI` workflow runs the frontend checks and native Rust checks on Windows. DX-03 runtime package experiments are in `packaging/windows_desktop`; this scaffold does not start Python or PostgreSQL yet.

## Contract boundary

The exposed Tauri commands are an explicit allowlist in `src/desktop.ts` and `src-tauri/src/lib.rs`. The webview stores no bearer token and cannot access native filesystem or process APIs. Operator sign-in, business endpoints, credential storage, live PostgreSQL, and real Controller/Worker packaging remain outside DX-02.
