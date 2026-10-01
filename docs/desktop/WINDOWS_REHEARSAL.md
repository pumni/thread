# Windows Desktop v1 — Packaging, Operations and Rehearsal Runbook

**Planning document, not an installed product runbook.** Commands/paths below are design contracts or test targets; implementers must replace illustrative invocations with real supported CLI only after DX-03/DX-04 confirm exact binaries.

## 1. Deployment profiles

| Profile | Customer/user interaction | Processes | State owner |
|---|---|---|---|
| Windows Controller | Same single Desktop setup, Controller first-run | Tauri tray supervisor, bundled PostgreSQL, Python HTTP, standalone scheduler | One Workspace PostgreSQL + Controller DPAPI trust |
| Windows Worker | Same setup, Worker first-run | Tauri tray supervisor, existing Python Worker, headed Chromium | One user-scoped Worker identity/profiles/journal |
| Windows Console | Same setup, Console first-run | Tauri UI + secure HTTPS client only | Client-specific trusted endpoint, in-memory human session |
| Linux/Docker Controller | Existing developer/IT Compose/server workflow | PostgreSQL + FastAPI + scheduler | Same database/schema/Operator API/Worker protocol |

Current legacy Windows Worker Task Scheduler host remains supported until explicit DX-07 cutover. Do not run it and the Desktop supervisor against the same data root.

## 2. Prototype proof vs production-shaped first run

- **M1 / DX-04:** provisional local engineering runtime **on loopback only**, using a disposable database and proven bundled test helpers. Does **not** expose an unauthenticated LAN API, create a fake durable Owner, imply production auth, or require final NSIS/MSI installer. Proves Controller PostgreSQL -> migrations -> HTTP -> scheduler -> X-to-tray -> Quit -> recovery.
- **M2 / DX-05 + DX-06:** actual Owner bootstrap and authenticated HTTPS/WSS Controller endpoint. Before these gates, no remote Console login or LAN Worker pairing is permitted.
- **M4 / DX-12:** one final installable Windows setup artifact for Controller/Worker/Console; package signing/licensing and real Windows test evidence are separate release checks.

## 3. Installation vs first run

Installer installs branded Desktop and bundled dependencies under immutable release directories; it does not ask the customer to install PostgreSQL/Python/Docker or manage credentials/ports. The first-run wizard obtains role and provisioning details, performs preflight and initializes state. No backup, owner email setup or auto-update in MVP.

Controller first-run wizard:
1. Check OS/architecture, running dedicated interactive Windows user, supported data-root ACL, free disk and LAN addresses; warn if power plan allows sleep.
2. **At M2 or later** ask Workspace display name, first OWNER username/password, preferred Controller LAN IP/hostname and HTTPS port; recommend static IP/DHCP reservation. User acknowledges that the router configuration is outside the app. M1 is a restricted loopback-only engineering proof with no persisted fake Owner.
3. Show and verify planned immutable runtime version and data root. Explicitly disclose **data backup/recovery not yet available** and no runtime before Windows login. Accept only **disposable/synthetic pilot data** until a separately reviewed backup/recovery release gate exists.
4. Create provisioned Controller identity and private PostgreSQL cluster; store generated DB password and trust keys encrypted for the same current Windows account.
5. Migrate schema exactly once per eligible version; bootstrap first Owner locally; start HTTP/scheduler behind bounded health gates; show trusted Controller fingerprint plus Worker pairing entry.
6. Autostart Tauri at dedicated user's Windows sign-in (single instance), start configured node automatically and minimize to tray by user-selected preference (default on).

Worker wizard:
1. Run under one stable dedicated interactive Windows user; preserve existing Worker DPAPI root if migrating an installed Worker.
2. Enter Controller stable HTTPS endpoint; fetch bootstrap-only TLS identity, verify root fingerprint independently against Controller's local UI before sending pairing code.
3. Enter time-limited one-use code, enroll existing Worker UUID/key, report presence, then show Worker-local browser capabilities, login/intervention and diagnostics.
4. If old `ThreadsPlatformWorker` scheduled task exists, stop and drain safely, disable old start trigger and prove only the Desktop host owns it; provide rollback without moving current-user DPAPI keys.

Console wizard: enter Controller endpoint -> first-contact identity verification by independent trusted channel -> user/password -> role-aware Dashboard. No local business runtime.

## 4. Internal Controller root and permissions

Target immutable binaries: `C:\Program Files\Threads Operations\...`. Target durable machine-role data: `%PROGRAMDATA%\ThreadsOperations\Controller\` with an explicit ACL granting the single stable Windows runtime user and expected OS administrators only. Child PostgreSQL runs under **the dedicated non-elevated Windows user**, not LocalSystem; verify this supported PostgreSQL privilege model in DX-03. No writable executable under data root and no durable state inside the release bundle.

Illustrative stable layout:

```text
Controller/
  config/                 non-secret role/endpoint/version
  identity/               versioned DPAPI-protected CA/private material
  postgres-data/          private PostgreSQL data directory
  logs/                   fixed-bound redacted operational logs
  runtime/                process locks and crash metadata, no secrets
```

Worker remains under its existing `%LOCALAPPDATA%\ThreadsOperations\worker|profiles|journal|logs` layout. A separate Console user-profile store contains non-secret recent endpoint/trust metadata; never copy Controller private material.

PostgreSQL binds `127.0.0.1` private persisted app-managed port; no LAN 5432. Controller API binds a persisted dedicated TLS LAN port; fail if unavailable rather than silently changing to a new Worker endpoint. Firewall rule and elevation model must be implemented and reviewed in DX-12 (installer/elevated phase) without asking UI to run permanently as administrator.

## 5. Startup and shutdown state machine

```text
UNPROVISIONED -> PREFLIGHT -> DB_STARTING -> DB_READY
              -> MIGRATING -> OWNER_READY -> HTTP_STARTING
              -> HTTP_READY -> SCHEDULER_STARTING -> RUNNING

RUNNING --X--> RUNNING+HIDDEN
RUNNING --explicit Quit--> STOPPING -> STOPPED -> desktop exits
failure -> DEGRADED/FAILED with bounded restart/manual intervention
```

Provisioning/role config has a versioned atomic file write. Lock identity/data root before launching children; fail duplicate launch. Schema unknown/newer than binary -> **stop and show diagnostics**, never auto downgrade/reinitialize. DB unavailable -> failed readiness, bounded restart only if safe; never delete cluster as self-healing. Browser Worker uses device-authenticated drain/quiescence before Quit; if timeout, report and require explicit user choice and documented interruption/recovery consequence.

Owner/operator session is unrelated to the tray node: UI logout or session expiry cannot stop Controller/Worker runtime. Reopening on an unattended PC should lock the business UI according to DX-05 security policy.

## 6. Crash, power and reboot boundaries

| Event | v1 expected behavior |
|---|---|
| X/window close | Hide to tray; runtime continues |
| Sign-in after boot | Tauri autostart -> configured runtime startup |
| Lock Windows | Existing process remains alive subject to OS sleep/power policy |
| Explicit Quit | Confirmation and ordered graceful node stop |
| Worker job in progress on Quit | Durable DRAINING/quiescence; no unchecked hard-kill |
| Unexpected Desktop death | Test real Windows process-tree/Job Object behavior; abrupt PostgreSQL termination is not graceful, so prove WAL recovery; Worker never reports completed drain on crash |
| Windows logout | Windows terminates interactive session; node stops. **Unsupported continuity** |
| Reboot before any sign-in | Node **not running** until designated Windows user signs in |
| Disk/DPAPI identity lost | No guaranteed Controller portable recovery in v1; clearly disclose |
| Windows Update reboot | Autostart only after sign-in; no Windows auto-login solution |

Use real Windows Job Objects/process locks where proven; test PostgreSQL graceful shutdown and recovery instead of presuming closing a parent tree is WAL-safe. When desktop crashes and PostgreSQL cannot stop cleanly, WAL recovery must be exercised; never recreate cluster. Worker OS-forced shutdown remains the existing durable lease-expiry/reconciliation scenario, not a graceful drain claim.

## 7. Packaging and upgrade cutover

- First DX-03 spike compares shared Python `onedir` multi-mode helper to alternative shared-runtime packaging; record binaries/manifest/license, **installer-elevation vs stable runtime user ACL/DPAPI handoff**, WSS TLS termination candidates and clean-machine proof. Bundle matching Playwright Chromium for Worker without copying browser profiles into release assets.
- Keep runtime binaries versioned and immutable. Desktop lifecycle/role/data schema do not depend on app release directory. No automatic downloader/updater.
- Upgrade manually **for disposable/internal test data only in v1**: validate source version and schema, stop/drain, preserve state where possible, replace binaries, migrate forward, restart, inspect health. Schema rollback is not assumed. Do not claim customer-data upgrade safety without a portable backup/restore solution. Product backup/restore is deferred: destructive migration and production-data upgrades require explicit future safety gate.
- Installer removal must not delete durable Controller DB or Worker browser profiles implicitly. Require separate explicit decommission/data-deletion user action and privacy warning.
- Generate pinned manifest, SHA256, third-party license notices, final signing/publisher plan and secret scan. Unsigned test artifact is internal only.

## 8. Operational diagnostics — no secret-bearing dumps

Controller shows database liveness/ready, HTTP/scheduler health, configured LAN endpoint, Workspace ID/display name, runtime versions, up/down bounded status and redacted correlation IDs. Worker shows presence, capacity, assigned account count, local browser session states and interventions; Console shows Controller availability/trust and operator identity. Never expose raw DB URL/password, root private key, bearer, enrollment code after display expiry, browser cookie, DOM, unredacted URL or personal account data in diagnostics.

## 9. Operational rehearsal and signoff

Use [Acceptance Matrix](ACCEPTANCE_MATRIX.md) as the canonical case list. Each run captures: image/build/version, Desktop and sidecar SHA256, Python/Rust/TS lock hashes, Windows version, Windows account context (non-secret), Controller root fingerprints (public), endpoint, test identity synthetic labels, test timestamps, exact PR commit head, outcomes with failure logs redacted. Store evidence in repo only when sanitized and approved. External real Threads/Meta activity must obey existing live-validation gate #3; synthetic browser fixture/login on test accounts does not prove production API capability. Notify Product Owner of no-backup/no-before-login limitations at each pilot signoff.
