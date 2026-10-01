# Threads Desktop v1 — Detailed Delivery Plan

**Planning baseline:** 2026-10-01; repository `main@73681a4b7a74826915ccea86eff918edf4497bd5`. This is an implementation roadmap, **not implementation completion**, production approval, or automatic authorization for an agent to implement subsequent slices. See [ADR-0007](../adr/0007-windows-first-single-app-desktop.md).

## 0. Desired product outcome

Deliver a single Windows customer app and installer, powered by Tauri 2 + Rust + React/TypeScript, that lets a dedicated PC provision as Controller, Worker, or client-only Console; keeps the selected node running in the tray after the window closes; offers mandatory per-human operator login/RBAC; provisions Controller PostgreSQL/Python runtime transparently; pairs LAN Workers securely; supports Worker-centric human-assisted Threads browser account onboarding; and survives ordinary app restarts without losing authoritative or local identity state.

**Customer-visible promise:** install one app; choose this PC's role once; configure Controller or join existing workspace; operate via one UI. Bundled private helper processes remain allowed and necessary. Controller/Worker dedicated Windows user must sign in after reboot. Windows Service, boot-before-login availability, cloud relay and one-file physical executable are **not** MVP requirements.

**Target M1 vertical slice:** clean Windows x64 VM, no preinstalled Python/PostgreSQL/Docker; launch a **provisional, one-app, loopback-only engineering test bundle** with a **disposable** private Controller cluster, close UI to tray, verify database/API/scheduler continue, Quit, restart and prove the same cluster and durable state. M1 has **no public LAN listener**, no fake persisted Owner, and no production authentication promise. Real first OWNER bootstrap is DX-05, public HTTPS LAN is DX-06 and the final single customer installer is DX-12. This proof must precede broad UI work.

## 1. Existing implementation and non-negotiable reuse

- FastAPI Python 3.14 Control Plane, PostgreSQL, Alembic, standalone scheduler, durable Commands, WorkerJobs, leases/fencing, activity recurrence and outbox. Never reproduce business routing/retry in Rust.
- Existing Windows Worker Agent, current-user DPAPI device identity, `%LOCALAPPDATA%\ThreadsOperations` state and SQLite recovery journal, headed Playwright Chromium, account/worker/profile affinity, HTTPS device protocol v2, durable DRAINING. Keep existing Task Scheduler deployment supported until an explicit tested migration.
- Linux/Docker Controller development/IT profile stays maintained against **the same** Python schema, Operator API and Worker protocol. New Desktop features must not silently make the backend Windows-only.
- Existing `AccountStatus`, `AccountExecutionMode`, `BrowserSessionState`, `WorkerStatus` remain independent axes; do not invent a persisted UI-only omnibus READY/BROKEN state.
- Existing `ThreadsAccount.threads_user_id` and `.username` are currently required. Nullable remote identity/label work is a specifically reviewed migration slice, not something the UI should fake.
- External production gates: #3 live Meta verification, #80 reviewed discovery permissions, #62 CRMResultSink transport, #11 final production certification and #1 owner security follow-up. Desktop demo evidence does **not** close or bypass them.

## 2. Product decisions and pending policy

| Topic | Decision for v1 | Status |
|---|---|---|
| UI/runtime | One installed app, X hides to tray, explicit Quit stops node gracefully | Confirmed |
| Architecture | Tauri 2 + React/TS/Vite + Rust supervisor; Python business runtime | Confirmed |
| Modes | Controller, Worker, Console; Controller/Worker nearly fixed | Confirmed |
| Workspace | Exactly one Workspace per Controller, multiple human users and Workers | Confirmed |
| Host | Dedicated stable Windows user; current-user DPAPI; autostart after login | Confirmed |
| Availability | No Windows Service or runtime before Windows login | Confirmed |
| LAN | Stable Controller address, no discovery, HTTPS/private trust | Confirmed direction; cryptographic protocol requires security review |
| Human access | Mandatory login on Controller, Worker and Console | Confirmed |
| Roles | OWNER, ADMIN, OPERATOR, VIEWER fixed | Confirmed direction; exact per-action matrix below is a **proposal** awaiting product sign-off |
| Browser onboarding | Login locally on Worker; Controller owns Account and assignment | Confirmed |
| Re-pair | Explicit Worker decommission/drain/re-pair; do not reuse old browser profiles | Confirmed |
| Data recovery | Backup, portable restore, owner recovery email deferred | Confirmed deferral; durability risk explicitly documented |
| Release | No auto-updater/Internet/SSO/HA/auto-discovery in v1 | Confirmed |

**Proposed minimal RBAC matrix — authorize and audit on server, never only hide React buttons:**

| Action | OWNER | ADMIN | OPERATOR | VIEWER |
|---|---:|---:|---:|---:|
| View allowed workspace/fleet/account operational summary | Yes | Yes | Yes | Yes |
| Create/change OWNER or ADMIN; recovery-sensitive settings | Yes | No | No | No |
| Create/disable OPERATOR or VIEWER | Yes | Yes | No | No |
| Provision/revoke/re-pair Worker; add/disable/reassign Account; attach API | Yes | Yes | No | No |
| Drain assigned Worker, acknowledge intervention, browser re-login/challenge | Yes | Yes | Yes | No |
| Submit approved operational Commands in permission scope | Yes | Yes | Yes | No |
| Read-only diagnostics | Yes | Yes | Yes | Yes |

Guard: >=1 enabled OWNER. Hard deletion of authoritative Account and destructive cleanup of browser data are separate reviewed actions. The product owner previously agreed to Worker-local Operator login, not to every exact cell here. Freeze this matrix at the RBAC issue gate.

## 3. Source layout and implementation discipline

```text
thread/
  apps/desktop/
    package.json                    # standalone pnpm app; no workspace/Nx/Turbo yet
    pnpm-lock.yaml
    vite.config.ts
    tsconfig.json
    src/
      app/                           # providers, routes, mode-aware shell
      features/auth/
      features/controller/
      features/worker/
      features/accounts/
      features/diagnostics/
      components/
      lib/                           # typed command/read model clients; no token persistence
    src-tauri/
      Cargo.toml
      Cargo.lock
      tauri.conf.json
      capabilities/                  # allowlist minimal native permissions
      src/
        provisioning/
        supervisor/
        native_auth/
        transport/
        commands/
  src/threads_platform/            # current Python; only additive bounded changes
  packaging/                       # existing Worker packaging plus reviewed Desktop pipeline
  tests/                           # new Python/auth/onboarding/recovery integration tests
  docs/desktop/                    # this plan + contracts + matrix + runbook
```

Use React Router + TanStack Query and local React state where enough. Keep one typed Tauri command surface with semantic operations, e.g. `get_runtime_status`, `open_browser_login`, `operator_login`; no unrestricted URL/file/process execution from the webview. Rust owns in-memory operator bearer and private secrets; React never receives raw operator tokens, DPAPI material or database credentials.

Each implementation issue is a focused PR or explicitly coordinated small batch, based on latest main. Avoid one mega PR for scaffold + Postgres + migrations + pairing + onboarding. Acceptance is separate from green CI; merge only after exact-head review and the coordinator's acceptance.

## 4. Work breakdown / critical path

Stable identifiers `DX-01`…`DX-14` map to GitHub child issues; actual GitHub numbers are recorded in [issue map](ISSUE_MAP.md). A phase describes dependencies, not dates or parallel work promises.

### Phase 0 — approved contract and risk register

**DX-01 — Desktop architecture/contract baseline**
- Approve ADR-0007, repo folder boundaries, role provisioning, Supervisor/Operator/Worker transport authority.
- Freeze in-scope/out-of-scope, proposed RBAC matrix, data-root/Windows-login limitation, single-installer definition, security and account onboarding threat models.
- Decide packaging candidate experimentally rather than treating `threads-runtime.exe` as proven.
- Deliverables: architecture ADR; delivery plan; trust/auth/onboarding contract; acceptance matrix; Windows runbook; epic/issue dependencies.
- Gate: coordinator signs off scope, unresolved security questions are recorded as explicit blockers for respective slices.

### Phase 1 — prove the Windows appliance before investing in broad UI

**DX-02 — Tauri foundation and single-app lifecycle** (depends DX-01)
- Scaffold `apps/desktop`, standalone lockfiles, separate cargo/JS lint/typecheck/unit checks, dev/test build.
- First-run Controller/Worker/Console choice; persist non-secret role; no normal role-switch dropdown; explicit reset design and confirmation.
- Implement tray, close-to-hide, focus existing single instance, autostart only after sign-in, explicit Quit confirmation.
- Rust Supervisor fixed-process interface with **fake helpers** for deterministic lifecycle tests; React runtime-status snapshot and unprovisioned/provisioned UI.
- Tests: X keeps helper alive, Quit requests orderly stop, no duplicate launcher, lock/reopen behavior, no secret exposed to webview.
- Gate: Windows runner/manual smoke demonstrates tray behavior without production Python/Postgres integration.

**DX-03 — native Windows runtime packaging feasibility** (depends DX-01; in parallel with DX-02)
- Inventory Python 3.14 entrypoints including migrations, HTTP, scheduler, Worker; compare single multi-mode `onedir` sidecar versus a shared-runtime package. Reuse existing Worker PyInstaller/Chromium success.
- Validate pinned PostgreSQL Windows distribution/license/architecture and private `initdb`/server/`pg_ctl` invocations; no system-wide PostgreSQL install/service.
- Validate immutable binaries + durable ACL-scoped Controller root, DPAPI decryption under stable Windows user, executable signing/AV false-positive considerations, deterministic build manifest.
- Prove backend and scheduler start from bundled runtime without uv/Python on Windows test VM. No business changes.
- Deliver a recorded packaging decision, build scripts, checksums and prototype artifact with no production claim.
- **Stop** if Python/PostgreSQL bundled runtime is not reproducible, license incompatible or cannot pass clean-machine smoke; revise packaging design before further work.

**DX-04 — native Controller provisioning and supervisor vertical slice** (depends DX-02 and DX-03)
- Preflight disk, OS, user context, ports, ACL, migration/schema compatibility; never erase an existing DB.
- On provision create one protected durable Controller identity/data root; generate DB credential; private loopback Postgres on persisted non-default app-managed port; `initdb` only for missing cluster on explicit first-run path.
- Fixed startup DAG: PostgreSQL -> bounded readiness -> one-shot Alembic -> HTTP -> /ready -> scheduler -> Ready. Initial test may use a synthetic owner/bootstrap stub; do not expose unauthenticated LAN operator routes.
- Bounded per-helper restart/backoff/circuit-break, per-profile process/data-root locks, process-tree ownership, critical failure surfaced locally. No schema auto-rollback.
- Quit: reject new admin work, stop scheduler, stop HTTP, graceful PostgreSQL shutdown; crash/restart must preserve WAL/data and detect orphan/duplicate processes.
- Verification: fresh Windows VM without dependencies, close-to-tray continuity, restart same cluster, DB outage/recovery, repeated migration, collision prevention, power/sleep warning.
- This is the **first milestone demo**. It proves runtime ownership, not authenticated remote control, safe Internet access or production-readiness.

### Phase 2 — human authorization and secure LAN

**DX-05 — Operator identity, fixed RBAC, audit and bootstrap** (depends DX-01; can start while DX-04 develops)
- One Workspace singleton invariant; OperatorUser, password hash, disabled/must-change-password, server-side hashed opaque OperatorSession, expiry/revoke/logout and current-role validation; >=1 enabled OWNER.
- Native initial Owner creation over local secret-safe channel **before** public operator endpoint opens. Repeat bootstrap rejected. **Also support a non-network one-shot Linux/Docker CLI/stdin bootstrap**, without requiring Tauri or an unauthenticated web setup endpoint.
- Operator routes login/logout/me/user management with bounded responses, rate limits, safe lockout recovery policy, generic login errors and redacted audit.
- Implement direct human login on Worker Desktop using Controller Operator API; Worker device credential never substitutes for user credential. Freeze node lifecycle authorization when deliberate Quit/Restart occurs without an active human session. **V1 proposal:** prompt for suitably privileged Operator login to request drain/stop, or separately security-review a device-authenticated self-only Worker drain endpoint; never embed the old global admin token in Rust.
- Operator/Admin role matrix gets explicit product confirmation at this issue gate; each mutating endpoint independently enforces authorization. **Inventory and reconcile existing static `worker_admin_token` routes** (`/v1/workers/enrollments`, admin drain/abort, intervention resolve): new Windows profile must disable/isolate the legacy bypass and derive actor identity server-side; migration-compatible Linux/IT profile requires explicit gated configuration and tests. Existing CRM ingress remains a separate principal. Reopening unattended UI or locking Windows must lock the human session without stopping the node.
- Verify PostgreSQL migration, concurrent last-owner demotion, revoked/disabled user, role downgrade next request, session expiry, no secret in logs/React/storage.

**DX-06 — Controller private HTTPS identity and first-contact trust** (depends DX-03, DX-04, DX-05)
- Provision long-lived Controller trust identity and locally issued leaf certificate with correct stable LAN endpoint identity/SAN; private key current-user DPAPI at rest; expose one HTTPS API endpoint with distinct Operator and Worker auth.
- Controller localhost desktop uses exactly the same Operator API and authentication as remote Console; no localhost admin bypass or DB direct UI path.
- Select and test exact TLS termination boundary for Python HTTP **and Worker WebSocket**, trusted proxy/ASGI scheme behavior if proxied, server certificate key custody and HTTPS enforcement on **new Operator plus existing Worker LAN routes**. On first Worker/Console contact obtain untrusted certificate but **do not send pairing code/password**. Display actual Controller root fingerprint on trusted local Controller UI; display independently calculated peer fingerprint on client; user verifies via trusted path before persisting application-scoped trust. Fail closed on mismatch.
- Persist Controller CA/trust bound to configured endpoint and fingerprint; reject silent CA rotation, expired cert, endpoint mismatch, invalid SAN and downgrade to plaintext. No global CA installation.
- Verify certificates under valid/invalid hostname/IP, no first-contact credential leakage, localhost vs LAN parity, signed identity rotation boundary and negative MITM simulation. Verify Linux/IT trust provisioning/HTTPS ingress using Windows Worker/Console clients, or record an explicit temporary feature gap.
- Security-review protocol before implementation; do not claim a six-digit pairing code solves TLS identity on its own. Keep Linux/Docker Controller interoperable: choose a non-DPAPI key store, trusted HTTPS/WSS ingress and locally verified CLI fingerprint or explicitly document a temporary deployment gap.

**DX-07 — desktop Worker host and safe legacy task cutover** (depends DX-02, DX-03)
- Embed/supervise existing Worker Agent/Chromium, maintain interactive stable Windows principal, current-user DPAPI, stable data root, process lock, durable local journal and existing protocol.
- Implement guarded migration from existing `ThreadsPlatformWorker` scheduled task: detect, explain, drain, disable old launch path, prove one live agent/identity, preserve rollback and never race double launch. **Bound Quit if Controller is unreachable:** do not claim durable OFFLINE without server evidence; document interruption and lease recovery for an operator-confirmed forced exit.
- Worker desktop works in tray, continues using device auth when operator logs out. Operator auth unlocks local account provisioning/intervention UI only.
- Quit invokes durable DRAINING -> quiescence -> OFFLINE; if timeout/failure, explicitly ask for corrective action, never pretend hard-kill is graceful. Once the legacy static admin token is isolated, device authentication **cannot currently initiate** that drain; approved Operator session or a separately reviewed device self-drain is a **hard DX-07 gate**. Human UI logout alone never stops runtime.
- Test existing packaged Worker compatibility, browser foreground usability, same-user reboots and old-task collision.

**DX-08 — Worker pairing, revoke and Console trust UX** (depends DX-05, DX-06, DX-07)
- Owner/Admin creates single-use bounded enrollment with attempt limits, expiry and server-derived human audit. **Preserve the existing 256-bit `secrets.token_urlsafe(32)` Worker enrollment secret** as the actual protocol credential: first shipping UX may convey the full secret securely (copy/paste) after verified trust. A six-digit UX is optional **only through a reviewed, rate-limited short-code redemption** over verified TLS, never by replacing the high-entropy token in existing `/enroll`. Pair Worker after independently verified Controller trust. Keep current Worker key generation/challenge/device session; enrollment code transient child environment, never file/CLI/log.
- Console first-connect performs Controller trust verification then human login; no Worker enrollment for read/write Console users.
- Worker Controller changes only through explicit drain/decommission/revoke -> quarantine previous-workspace profiles -> clear old trust -> pair anew. Handle disconnected old Controller as documented forced offline path with separate security warning and eventual server revoke by old workspace owner.
- Tests: replay/expiry/brute-force bounds, wrong Controller fingerprint, untrusted TLS without credentials, wrong Worker identity, revoked credential denied, offline force-revoke on reconnect, workspace profile isolation.

### Phase 3 — account domain and usable operations

**DX-09 — browser-only account identity and Worker-centric onboarding** (depends DX-05, DX-07, DX-08)
- Reviewed Alembic migration: introduce internal operator label and nullable canonical `threads_user_id`/`username` for unverified browser accounts; partial uniqueness when known; explicit local/verified identity semantics; preserve API-only existing records.
- Worker UI Add Account available after OWNER/ADMIN Operator login. Create bounded local pending profile **using a reviewed new provisioning-only browser path**: existing `LocalBrowserSessionManager.open()` requires an already-registered account/assignment and cannot open this profile unmodified. Human opens Threads directly; implement and evidence a bounded login-state validation contract rather than equating the user button, an already accepted read-only browser capability or a fixture with authentic session readiness.
- Controller authorizes a one-time worker-bound onboarding intent under human session; Worker completes only using existing device auth + intent; transaction creates Account + BrowserProfile ref + active assignment with idempotency, audit and no partial rows; return canonical account UUID. Intent is not a candidate business Account. **Version/advertise any additive onboarding API/Worker protocol capability** and fail closed for old Worker versions; preserve Worker v2 business compatibility.
- On Controller failure, pending local profile remains isolated and retryable with bounded idempotency; on cancellation/expiry explicit cleanup. Reject double submit, account affinity mismatch, wrong Worker, stale intent and duplicate verified remote ID.
- Never send cookies, storage, password, OTP, raw DOM, browser path, entire URL or local files. `AUTHENTICATED` requires bounded evidence from approved adapter; UI button alone cannot assert it.
- Capability requiring verified remote ID remains unavailable for local-only accounts. Legacy API-only tests and migration roundtrip remain green.

**DX-10 — account maintenance and intervention lifecycle** (depends DX-09)
- Session expiry/challenge re-login **on owning Worker/same profile**; Operator may perform intervention under explicit RBAC; Controller stores only state summary and action/audit.
- Worker offline affects execution availability, not authoritative AccountStatus. HYBRID API behavior stays controlled by existing Capability Router and first-attempt/recovery rules.
- Owner/Admin account disable/enable; no normal hard-delete. Reassignment is explicit, drains old browser work, ends prior affinity, quarantines old profile and requires human login on new Worker; never auto-copy Chromium profiles.
- Separate remove-browser-access from delete-local-profile; offline cleanup is pending and never falsely reported complete. API_ONLY + browser may become HYBRID only after identity-link assurance. Removing one executor does not remove business Account/history.
- Test reassignment vs concurrent WorkerJob claim/lease, stale state report, disabled account/new-command rejection, offline Worker, timeout ambiguity and retention.

**DX-11 — Operator read models and mode-aware React product UI** (depends DX-05, DX-08, DX-09; incrementally integrate DX-10)
- Authenticated Operator API read models join account mode/identity, credential status, worker presence, browser session and intervention without persisting fake READY state.
- Controller/Console share dashboard, Workers/Pairing, Accounts, approved Commands/Jobs, interventions, operator users (role-dependent), diagnostics/settings.
- Worker offers status/Controller trust, local profiles, Add Account, browser login/re-login and local diagnostics. Operator logout does not stop node.
- Show only evidence-backed statuses: `last-known session ready, Worker offline` != account failure; disabled account != expired browser session.
- React error/loading/accessibility/keyboard/navigation behavior, no sensitive local paths or secrets. Typed Tauri + Python contract fixtures; negative RBAC UI checks backed by server enforcement.
- Do not surface gated discovery/mentions as production-ready; no new browser capability without existing approval path.

### Phase 4 — release-quality packaging and rehearsal (not production certification)

**DX-12 — reproducible one-installer Windows pipeline and CI** (depends DX-04, DX-07, DX-08, DX-11)
- Build one branded installer containing signed-ready Tauri app, approved runtime bundle(s), private PostgreSQL and Worker Chromium as applicable. NSIS `setup.exe` is candidate; MSI only if an actual enterprise deployment need arises.
- Fresh install, upgrade over durable data without accidental reset, uninstall leaves deliberate data-retention prompt, no system-wide DB/Python dependency, checksums/provenance/third-party licenses and artifact manifests. **Without user-facing backup/restore, only disposable/synthetic pilot data is in scope; no promise of safe destructive schema upgrade or production durable deployment.**
- Add Windows desktop workflow for Rust fmt/clippy/test, pnpm frozen install/lint/typecheck/build, sidecar/packaging smoke, existing secret scan; backend integration suite runs for Python/schema/protocol changes. Keep Linux/Docker smoke official and green.
- Signing/publisher validation is a **distribution gate**; do not call unsigned internal artifact a production installer. Auto-updater deferred.

**DX-13 — three-machine LAN end-to-end and failure rehearsal** (depends DX-04..DX-12)
- Three fresh Windows PCs/VMs: Controller, Worker, Console on stable LAN; dedicated logged-in users. Full first-run: provision Workspace/OWNER, verify HTTPS trust, pair Worker, Console login, Worker Operator login, browser account onboarding, re-login intervention, session summary and allowed Controller operations.
- Inject power interruption, Desktop crash, PostgreSQL outage, network split, invalid TLS/MITM, Worker offline/reconnect, repeated pairing, stale owner/lease and same-data-root double process. Re-run after reboot + sign-in; explicitly demonstrate no runtime before sign-in.
- Verify no secrets in generated logs, telemetry, crashes, fixtures or React; audit key mutations; protocol and schema interoperability with Linux/Docker Controller. Maintain a signed-off evidence packet and outstanding defects/risk register.
- **Exit:** accepted Desktop controlled pilot/demo with stated limitations; never imply Meta live API, CRM transport or enterprise production durability gates were met.

**DX-14 — cutover, security review and operations handoff** (depends DX-06, DX-08, DX-10, DX-13)
- Threat-model review, dependency/license inventory, installer signing/distribution plan, endpoint firewall/ACL/privacy review, documented browser consent and policy-gated capabilities.
- Publish operator runbook: provisioning, static IP, Windows login/autostart/sleep, quit/drain, pairing fingerprint, username/role, user lockout escalation, Worker reassignment, diagnostics, old scheduled-task migration/rollback, release/update limitations.
- Explicitly document deferred backup/recovery as a **hard limit** on real-data reliability; issue follow-ups for portable encrypted backup/Owner Recovery Key/recovery email once customer priority changes.
- Coordinator closes Desktop epic only against explicit pilot definition and cross-checks #3/#80/#62/#11/#1 separately.

## 5. Explicit audit reconciliation / implementation stop gates

Read [the pre-implementation audit](PREIMPLEMENTATION_AUDIT.md) before starting any DX issue. It records hard mismatches confirmed in current code: legacy Worker admin-token bypass (DX-05), strong existing Worker enrollment vs proposed short pairing code (DX-08), assigned-Account prerequisite vs first-time browser login (DX-09), and limited M1 prototype vs real Owner/HTTPS/installer (DX-04/05/06/12). These are **issue acceptance gates**, not permission to rewrite accepted C1–C6 semantics. A fresh session starts from [SESSION_HANDOFF.md](SESSION_HANDOFF.md), fetches latest `main`, #93, #94 and authorized issue, and verifies proposed ADR acceptance before coding.

## 6. Dependency graph and progressive gates

```text
DX-01 ADR/contracts
   ├── DX-02 desktop/tray ----------┐
   ├── DX-03 packaging spike ------+--> DX-04 Controller runtime [M1]
   └── DX-05 Operator RBAC --------+--> DX-06 HTTPS trust
                    DX-02 + DX-03 --> DX-07 Worker host
              DX-05+DX-06+DX-07 --> DX-08 pairing [M2]
              DX-05+DX-07+DX-08 --> DX-09 browser onboarding
                           DX-09 --> DX-10 account lifecycle [M3]
                   DX-05+08+09 --> DX-11 Product UX
                 DX-04+07+08+11 --> DX-12 Installer/CI
                      DX-04..12 --> DX-13 three-PC rehearsal [M4]
                DX-06+08+10+13 --> DX-14 security/operations handoff
```

M1 is the **first vertical slice** (no preinstalled dependencies, close-to-tray + restart durable state). M2 is authenticated LAN Controller/Worker/Console. M3 is Worker-centric browser onboarding and re-login. M4 is one-installer Windows pilot evidence. No calendar promises absent team capacity and packaging spike evidence.

## 7. Cross-cutting definition of done for every issue/PR

1. Issue is explicitly authorized before code; PR stays scoped to its DX acceptance criteria.
2. No breach of Control Plane/Worker authority, data-root/DPAPI, browser capability, Command/WorkerJob lease or original Linux/Docker behavior.
3. Meaningful unit/integration/negative/security tests plus relevant Windows clean-room evidence. Schema changes require Alembic constraints and PostgreSQL integration coverage.
4. Rust/TS typed contracts and privacy review; zero secrets in webview, localStorage, diagnostics, process args, fixture/artifacts or Git.
5. Relevant Python quality gate: `uv sync --locked`, `ruff check`, `ruff format --check`, `pyright`, `alembic check`, `pytest`. CI/secret scan and Windows Desktop gates green when applicable; scope-based skips explicitly documented.
6. Docs/runbooks/protocol versions updated in same PR where accepted behavior changes.
7. Review reports exact tested head SHA, test/environment evidence, BLOCKER/MAJOR findings, criterion-by-criterion pass, and separate coordinator acceptance. PASS != ACCEPTED; CI green != permission to merge. Recheck unchanged head and CI before merge; merge with expected SHA; verify main afterwards.

## 8. Sequencing rules and stop conditions

- Prioritize DX-03 packaging feasibility and DX-04 complete local vertical slice before elaborate React dashboards, optional backends or additional account features.
- Freeze security handshakes and RBAC at their respective issues; do not improvise a weak TLS bootstrap or direct desktop access to Worker secrets under time pressure.
- On Windows crash, no silent DB re-init, no orphan duplicate scheduler, no hard-kill advertised as Worker safe-stop. Any failure here is a blocker.
- Browser session state alone does not verify external remote identity. Never infer API/browser cross-account linkage from a label or unverified username.
- Deferred changes require separate later epics: portable encrypted Controller backup/recovery and Owner email channel; pre-login Windows Service hosting; Internet/public deployment; manual/signed updater; dynamic LAN discovery; MFA/SSO; production CRM transport and external Meta gates.
- If one installer cannot meet clean Windows packaging constraints, record evidence and revise the packaging strategy in DX-03 rather than silently install dependencies or rewrite Python.
