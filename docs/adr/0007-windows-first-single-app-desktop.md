# ADR-0007 — Windows-first single-app Desktop and node provisioning

- **Status:** Proposed for coordinator acceptance; planning only. Supersedes no accepted ADR until merged and approved. Read [pre-implementation audit](../desktop/PREIMPLEMENTATION_AUDIT.md) and [fresh-session handoff](../desktop/SESSION_HANDOFF.md) first; unresolved choices are guarded by the listed DX issues.
- **Date:** 2026-10-01
- **Scope:** Threads Desktop v1; does not grant permission to activate unverified Threads API/browser capabilities.
- **Related:** [Desktop delivery plan](../desktop/DELIVERY_PLAN.md), [ADR-0003](0003-distributed-hybrid-execution.md), [ADR-0004](0004-persistent-worker-affinity-and-worker-jobs.md), [Windows Worker](../WORKER_AGENT_WINDOWS.md).

## Context

The repository currently contains a Python 3.14 modular-monolith Control Plane, PostgreSQL authoritative state, standalone scheduler, headed Windows Playwright Worker, durable WorkerJob/Command recovery, and Linux/Docker deployment. The customer has no integrated desktop UI. Target customers use continuously running dedicated Windows PCs; developers/IT must retain Linux/Docker deployments.

The product owner selected one installed Windows application that hides to tray while running; the user does **not** want a separate customer-visible Controller service/manager app. A physical single-process binary is **not** a requirement: PostgreSQL, Python and Chromium may be bundled helper processes in one installer.

## Accepted product decisions for the planning baseline

1. **One installer, one visible Threads Desktop app**, built with Tauri 2, Rust native core and React + TypeScript + Vite. Packaged runtime helpers remain private implementation details, not separate customer products.
2. At first run provision exactly one of: **Controller**, **Worker**, **Console**. Controller/Worker roles are effectively fixed until an explicit destructive reset/decommission; Console may reconnect freely. Controller + Worker on one PC is internal/demo only and deferred.
3. **One Controller = one Workspace/organization**, with multiple operator users, accounts and Workers. No multi-tenancy, organization ACL framework or workspace_id propagation across every existing table.
4. Controller holds PostgreSQL, HTTP Control Plane and scheduler. Python still owns all business policy; Rust owns local provisioning, process supervision and secure desktop-to-API transport; React owns presentation.
5. Worker keeps existing Python Agent, device identity/current-user DPAPI key, profile root, headed Chromium and durable local journal. Browser profiles and cookie material never leave the owning Worker; the Desktop does not recreate Worker authentication or routing.
6. Console is client-only; it starts no local PostgreSQL, scheduler, Worker or Chromium.
7. Close/X hides window; the Tauri process and managed runtime continue in tray. Explicit **Quit** confirms, requests graceful shutdown and exits. App autostarts after Windows user sign-in; lock screen does not stop it. **Windows logout and reboot-before-login are unsupported in v1**. No Windows Service and no Windows auto-login workaround.
8. Controller and Worker each run under **one stable dedicated interactive Windows user** on their 24/7 PC. Current-user DPAPI protects their separate secrets; the login principal must remain stable. Threads operator identities are distinct from Windows and device identities.
9. LAN first: stable Controller LAN address (static IP/DHCP reservation recommended and acknowledged); HTTPS verified end-to-end and application-scoped Controller trust; manual address entry, no discovery in v1. Internet reachability comes later without changing the worker business protocol.
10. OWNER, ADMIN, OPERATOR, VIEWER fixed roles from early delivery. Human login is mandatory even on the Controller PC and Worker PC; no localhost administrator bypass. On Worker, human operator authentication authorizes browser onboarding; Worker device authentication independently authorizes runtime transport. Exact role action matrix remains a product policy checkpoint before RBAC implementation.
11. Worker-centric human-assisted browser login is the primary onboarding UX, with Controller-authoritative account identity, assignment, policy and audit. API onboarding is Controller-centric. No cookies/passwords/storage state/raw DOM to Controller; no guessing a verified Threads identity from a local browser login.
12. Decommission/re-pair is mandatory to change a Worker's Controller/workspace. Drain and revoke prior authorization; prior browser profiles are quarantined, never automatically reused across workspaces.
13. Defer backup/recovery and owner email recovery, auto-updates, Internet mode, MFA/SSO, HA, LAN discovery and production release certification. Keep a stable Controller data/identity boundary so later portable backup remains possible.

## Architecture

```text
                       Threads Desktop.exe
                    Tauri 2 / Rust / React
                              |
                    mode (provisioned once)
                 +------------+--------------+
                 |            |              |
             Controller      Worker         Console
                 |            |              |
         PostgreSQL       existing        remote
         CP HTTP          Worker Agent     Operator API
         Scheduler        Chromium             |
                 +------------+----------------+
                              |
                HTTPS + distinct operator/device auth
                              |
                 one Controller / one Workspace
```

A private helper such as `threads-runtime.exe` with narrowly defined `migrate`, `http`, `scheduler`, `bootstrap` and `worker` modes is a **candidate packaging design**, not an accepted guarantee until a Windows packaging spike proves binary compatibility, predictable startup and reproducible licensing. Reuse the existing Worker PyInstaller `onedir` evidence rather than assuming every mode bundles trivially. PostgreSQL remains separately managed as a bundled private database engine.

```text
React -> semantic Tauri commands -> Rust transport/supervisor
Rust -> authenticated Operator API or local process management
Worker Agent -> authenticated Worker protocol
Control Plane -> PostgreSQL authoritative business state
```

No React-to-PostgreSQL connection, no generic IPC HTTP proxy that exposes secrets to webview, and no Rust scheduler/Command/WorkerJob business implementation. Local Controller UI and remote Console UI authenticate against the **same Operator API and RBAC**.

## Windows process and data ownership

- One visible Tauri process is the customer app. Child helpers may be multiple OS processes. Use OS process ownership and bounded restart policy; never allow duplicate scheduler or duplicate Worker host on the same data root.
- Prototype an internal supervisor with a fixed dependency graph: Controller `preflight -> postgres -> migrate -> http -> scheduler`; Worker `identity -> endpoint/trust -> Agent`; Console none. Test migration failure and database corruption as fail-closed conditions.
- Runtime binaries are immutable release content. Controller persistent state belongs to a stable ACL-protected root (target `%PROGRAMDATA%\ThreadsOperations\Controller`, subject to dedicated-user ACL validation); Worker retains its current-user `%LOCALAPPDATA%\ThreadsOperations` root. Never bind identity to release directory or temporary extraction path.
- Controller PostgreSQL binds loopback only; the Controller HTTPS Operator/Worker endpoint is stable and reachable on the configured LAN. Do not silently choose a different public port or Controller identity on restart.
- Check collision/migration path with the existing `ThreadsPlatformWorker` scheduled task. An accepted cutover must prevent old host and Desktop from starting the same Worker simultaneously, preserve original current-user DPAPI identity and data root, and retain rollback instructions.
- `X` hides, `Quit` confirms impact and invokes graceful Worker drain/Controller orderly stop; abnormal supervisor death must not yield an unowned scheduler or silently reinitialize PostgreSQL. Validate Windows Job Object/process-tree cleanup before claiming this guarantee.
- A sleeping Windows PC suspends useful work; preflight warns without changing user power plan. Reboot waits for dedicated Windows user login and app autostart.

## Authentication, trust and account boundaries

- Controller bootstrap creates one Workspace and the first OWNER **before** LAN Operator API is exposed. Pass the initial password over a secret-safe native channel (candidate child stdin); hash server-side. Reject repeat bootstrap. **M1 is disposable loopback-only runtime proof, not authenticated first-run provision**; DX-05 must also offer a non-network bootstrap CLI/stdin on official Linux/Docker Controller without requiring Tauri.
- Operator login uses server-side opaque sessions (hashed at rest), current role/status authorization, explicit revoke/logout, short bounded lifetime and Rust-memory-only bearer storage. Do not expose bearer tokens to React or persistent browser storage. Reopening UI after unattended inactivity or Windows session lock requires a lock-screen policy. Existing static `worker_admin_token` routes for enrollment/drain/intervention must be migrated to canonical Operator authorization or disabled/isolated in the new Windows deployment profile; caller-provided actor strings are not audit identities.
- Controller trust is independent of operator login and Worker device enrollment. First-contact fingerprint verification must be out-of-band anchored in the actual Controller identity displayed on its trusted local screen (or a trusted equivalent); never accept arbitrary untrusted TLS merely because a remote page says it is safe. DX-06 must select the exact LAN TLS terminator and validate Worker WSS upgrades and ASGI scheme/trusted-proxy behavior, **plus HTTPS enforcement on public Operator routes**.
- Worker pairing must preserve authenticated TLS after trust confirmation, issue bounded one-time enrollment, rate-limit guesses, bind enrollment to Worker device identity and audit the initiating OWNER/ADMIN. **Preserve existing 256-bit Worker enrollment tokens.** A six-digit display requires a distinct security-reviewed, rate-limited short-code redemption after verified TLS; first release may convey the full high-entropy token. Never persist enrollment code in config/CLI/log.
- A new browser-only account may not have proven remote Threads identifiers. The required nullable-ID/domain migration is a **planned** change, not implemented. Local label is not a verified username. **Existing browser session opening requires an already assigned Account**, so Worker-first login needs an isolated pending local profile + explicitly reviewed login-state contract before server-side atomic registration. Additive onboarding protocol/capability must version-negotiate with existing Worker v2. Link API and browser identities only on sufficient evidence; avoid silent duplicate/unsafe hybrid merging.

## Alternatives rejected or deferred

- Separate customer-visible Controller UI + Controller service executable in v1: rejected by requested one-app workflow; a future **service-host mode** may be justified if users require reboot-before-login/logout availability.
- Replace PostgreSQL with SQLite: rejected because accepted PostgreSQL transactional claims/leases/locking semantics would change.
- Port Python business logic or headed Worker to Rust: rejected; no customer benefit proportional to risk.
- Install system-wide PostgreSQL and ask users to manage databases, Python or Docker: rejected for Windows customer installer; Linux/Docker remains supported for IT/dev.
- Autodiscovery, generic runtime plugins, background cloud relay, automatic update, owner email recovery, HA, configurable RBAC and backup implementation: deferred.

## Explicit risk acceptance

Until backup/recovery is shipped, loss of the Controller host or Windows DPAPI user context may make data/identity unrecoverable. An internal/demo Windows Desktop acceptance is **not** a claim of production data durability or production release readiness. Record this limitation in installer/runbooks and do not certify a customer's irreplaceable data on an unrecoverable prototype.

## Verification and review

See [test matrix](../desktop/ACCEPTANCE_MATRIX.md) and [operations](../desktop/WINDOWS_REHEARSAL.md). An ADR/planning PR must pass document/consistency review; implementation proceeds only via explicitly authorized issues. Keep the existing `#3` Meta live validation, `#80` discovery permission, `#62` production CRMResultSink and `#11` release-certification gates separate from Desktop vertical-slice evidence.
