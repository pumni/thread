# Windows Desktop v1 — Packaging, Operations and Rehearsal Runbook

**Windows Desktop implementation and rehearsal runbook.** This remains an internal disposable test bundle, not an installed customer product or production runbook. DX-06 uses the authorized exact-head HTTPS/trust contract; exact-head evidence and separate coordinator acceptance remain required.

## 1. Deployment profiles

| Profile | Customer/user interaction | Processes | State owner |
|---|---|---|---|
| Windows Controller | Same single Desktop setup, Controller first-run | Tauri tray supervisor, bundled PostgreSQL, Python HTTP, standalone scheduler | One Workspace PostgreSQL + Controller DPAPI trust |
| Windows Worker | Same setup, Worker first-run | Tauri tray supervisor, existing Python Worker, headed Chromium | One user-scoped Worker identity/profiles/journal |
| Windows Console | Same setup, Console first-run | Tauri UI + secure HTTPS client only | Client-specific trusted endpoint, in-memory human session |
| Linux/Docker Controller | Existing developer/IT Compose/server workflow | PostgreSQL + FastAPI + scheduler | Same database/schema/Operator API/Worker protocol |

Current legacy Windows Worker Task Scheduler host remains supported until explicit DX-07 cutover. Do not run it and the Desktop supervisor against the same data root.

## 2. Prototype proof vs production-shaped first run

- **M1 / DX-04 + DX-05:** provisional local engineering runtime **on loopback only**, using a disposable database and the accepted `shared` PyInstaller onedir. Its `http` and `scheduler` modes are separate OS processes. DX-05 adds a real locally bootstrapped Owner and Operator-session authorization; this still does **not** expose an unauthenticated LAN API, imply verified remote TLS, or require a final installer. The runtime proves PostgreSQL -> migrations -> local Owner bootstrap -> HTTP -> scheduler -> X-to-tray -> authenticated Quit -> crash recovery.
- The UI states that runtime is unavailable before this Windows user signs in, Windows logout is unsupported, and portable backup/production durability are unavailable.
- **DX-06:** Controller uses direct Uvicorn TLS on a single HTTPS/WSS listener. The user configures one stable LAN IPv4 and HTTPS port; there is no address discovery, reverse proxy, forwarded-header trust, or plaintext health listener. Local Owner bootstrap remains native/stdin and no LAN Operator API starts before first Owner creation.
- **M4 / DX-12:** one final installable Windows setup artifact for Controller/Worker/Console; package signing/licensing and real Windows test evidence are separate release checks.

## 3. Installation vs first run

Installer installs branded Desktop and bundled dependencies under immutable release directories; it does not ask the customer to install PostgreSQL/Python/Docker or manage credentials/ports. The first-run wizard obtains role and provisioning details, performs preflight and initializes state. No backup, owner email setup or auto-update in MVP.

Controller first-run wizard:
1. Check OS/architecture, running dedicated interactive Windows user, supported data-root ACL, free disk and LAN addresses; warn if power plan allows sleep.
2. Configure the stable LAN IPv4 assigned to this PC and the HTTPS port explicitly (8443 is the first-run proposal). Provision the private TLS identity, then create the first OWNER locally after migrations through native stdin; there is no network Owner-setup endpoint and no listener before that Owner exists.
3. Show and verify planned immutable runtime version and data root. Explicitly disclose **data backup/recovery not yet available** and no runtime before Windows login. Accept only **disposable/synthetic pilot data** until a separately reviewed backup/recovery release gate exists.
4. Create provisioned Controller identity and private PostgreSQL cluster; store generated DB password and trust keys encrypted for the same current Windows account.
5. Migrate schema exactly once per eligible version; bootstrap first Owner locally; start Uvicorn HTTPS and scheduler behind bounded health gates; show the public HTTPS address and root DER fingerprint. Worker pairing UI is not part of DX-06.
6. Autostart Tauri at dedicated user's Windows sign-in (single instance), start configured node automatically and minimize to tray by user-selected preference (default on).

Worker wizard:
1. Run under one stable dedicated interactive Windows user; preserve existing Worker DPAPI root if migrating an installed Worker.
2. Enter Controller stable HTTPS endpoint; fetch bootstrap-only TLS identity, verify root fingerprint independently against Controller's local UI before sending pairing code.
3. Enter time-limited one-use code, enroll existing Worker UUID/key, report presence, then show Worker-local browser capabilities, login/intervention and diagnostics.
4. If old `ThreadsPlatformWorker` scheduled task exists, stop and drain safely, disable old start trigger and prove only the Desktop host owns it; provide rollback without moving current-user DPAPI keys.

Console wizard: enter Controller endpoint -> first-contact identity verification by independent trusted channel -> user/password -> role-aware Dashboard. No local business runtime.

## 4. Internal Controller root and permissions

Target immutable binaries: `C:\Program Files\Threads Operations\...`. M1 uses the current user's Tauri app-local-data directory, `%LOCALAPPDATA%\com.pumni.threads-desktop\Controller\`, so the interactive user owns one ACL-scoped root and current-user DPAPI can protect its database credential. DX-04 records the runner privilege context and verifies current-user Full Control plus same-user DPAPI round-trip. A later installed deployment may use `%PROGRAMDATA%\ThreadsOperations\Controller\` only after the installer-to-runtime-user ACL handoff is proven; M1 does not claim that installer behavior. No writable executable is stored inside Controller data and no database directory is copied to a release artifact.

Illustrative stable layout:

```text
Controller/
  controller.json                 versioned non-secret ID, database/HTTPS ports, LAN IPv4
  database-credential.dpapi       current-user DPAPI protected credential
  postgresql/                     private PostgreSQL data directory
  .owner.lock                     exclusive current-process data-root lock
  tls/
    root-cert.der
    root-key.dpapi                 DPAPI CurrentUser; never plaintext
    leaf-cert.der
    leaf-key.dpapi                 DPAPI CurrentUser
    leaf-fullchain.pem
    serving/leaf-key.pem           temporary Uvicorn material; removed on stop
```

Worker remains under its existing `%LOCALAPPDATA%\ThreadsOperations\worker|profiles|journal|logs` layout. A separate Console user-profile store contains non-secret recent endpoint/trust metadata; never copy Controller private material.

PostgreSQL binds `127.0.0.1`; Uvicorn binds `0.0.0.0:<persisted HTTPS port>`. The configured public origin is `https://<stable LAN IPv4>:<port>` and local origin is `https://127.0.0.1:<port>`. Leaf SAN contains exactly those two IPs. The persisted endpoint can be explicitly reconfigured: setup may correct it before an Owner exists; after Owner creation the native operation requires an authenticated OWNER or ADMIN session. It validates old state, preflights the new assigned IPv4 and available port, preserves the root DER/fingerprint, and issues a fresh leaf. Collision or unavailable-IP failure leaves the prior config and TLS state intact. A running transition stops scheduler and HTTP, keeps PostgreSQL, starts and verifies the new HTTPS listener, then starts scheduler. HTTPS and WSS, including `/health`, `/ready`, and `/metrics`, share Uvicorn's listener. Operator traffic uses Workspace sessions and server-side roles. Firewall and elevation behavior remain outside this internal rehearsal.

## 5. Startup and shutdown state machine

```text
UNPROVISIONED -> PREFLIGHT/ROOT_LOCK -> DB_STARTING -> DB_READY
              -> MIGRATING -> HTTPS_SETUP_REQUIRED (no listener)
              -> local Owner stdin path (no listener until Owner exists)
              -> HTTPS_STARTING -> verified /ready -> SCHEDULER_STARTING -> RUNNING

RUNNING --X--> RUNNING+HIDDEN
RUNNING --authenticated Quit--> STOPPING -> STOPPED -> desktop exits
failure -> FAILED with a fixed redacted code; readiness waits use bounded backoff
```

Controller config has a versioned atomic write and contains no credential. DX-04/05 configs migrate while preserving the persisted HTTPS port; missing LAN address remains unconfigured and is never guessed. The DPAPI database credential and root/leaf keys are separate protected files. The root lock is held before any child starts; a second owner fails. Migrations use the unchanged Alembic head and never roll back schema. `initdb` runs only for a root created by the current first-run call; an incomplete, corrupt or unowned existing root fails without reset. Uvicorn and scheduler share `threads-runtime.exe` but have independent process IDs and lifecycles. Uvicorn receives only the leaf fullchain and temporary leaf key, uses `proxy_headers=False`, and readiness uses verified HTTPS to loopback under the Controller root. Quit stops scheduler, then HTTP, removes and verifies deletion of the serving key, then asks bundled `pg_ctl` for PostgreSQL fast shutdown. PostgreSQL crash or parent crash leaves the cluster in place for WAL recovery; startup removes stale serving material before provisioning the current leaf.

Owner/operator session is unrelated to the tray node: UI logout or session expiry cannot stop Controller/Worker runtime. Reopening on an unattended PC should lock the business UI according to DX-05 security policy.

## 6. Crash, power and reboot boundaries

| Event | v1 expected behavior |
|---|---|
| X/window close | Hide to tray; runtime continues |
| Sign-in after boot | Tauri autostart -> configured runtime startup |
| Lock Windows | Existing process remains alive subject to OS sleep/power policy |
| Explicit Quit | Confirmation and ordered graceful node stop |
| Worker job in progress on Quit | DX-07 defines the Worker host/drain path; DX-05 adds no device self-drain endpoint |
| Unexpected Desktop death | Test real Windows process-tree/Job Object behavior; abrupt PostgreSQL termination is not graceful, so prove WAL recovery; Worker never reports completed drain on crash |
| Windows logout | Windows terminates interactive session; node stops. **Unsupported continuity** |
| Reboot before any sign-in | Node **not running** until designated Windows user signs in |
| Disk/DPAPI identity lost | No guaranteed Controller portable recovery in v1; clearly disclose |
| Windows Update reboot | Autostart only after sign-in; no Windows auto-login solution |

DX-04 assigns each runtime child to a Windows Job Object with `KILL_ON_JOB_CLOSE` and holds an exclusive data-root file handle. A parent crash closes the job, terminates PostgreSQL/HTTP/scheduler, and releases the root lock. The acceptance smoke kills only the Tauri parent, verifies all three runtime processes exit, relaunches the same cluster, checks the PostgreSQL system identifier and a committed synthetic sentinel, and confirms exactly one scheduler. A PostgreSQL-only crash fails closed and stops its HTTP/scheduler dependents before same-data relaunch. No crash path runs `initdb` for an existing root.

## 7. Packaging and upgrade cutover

- DX-03 compared shared Python `onedir` multi-mode helper to split executables; `shared` is selected for M1 only. M1 records binaries/manifest/license and verifies current-user ACL/DPAPI under the logged-in Windows user. Installer-elevation handoff, TLS termination, signing and AV remain later gates. Bundle matching Playwright Chromium for Worker without copying browser profiles into release assets.
- Keep runtime binaries versioned and immutable. Desktop lifecycle/role/data schema do not depend on app release directory. No automatic downloader/updater.
- Upgrade manually **for disposable/internal test data only in v1**: validate source version and schema, stop/drain, preserve state where possible, replace binaries, migrate forward, restart, inspect health. Schema rollback is not assumed. Do not claim customer-data upgrade safety without a portable backup/restore solution. Product backup/restore is deferred: destructive migration and production-data upgrades require explicit future safety gate.
- Installer removal must not delete durable Controller DB or Worker browser profiles implicitly. Require separate explicit decommission/data-deletion user action and privacy warning.
- Generate pinned manifest, SHA256, third-party license notices, final signing/publisher plan and secret scan. Unsigned test artifact is internal only.

## 8. Operational diagnostics — no secret-bearing dumps

Controller shows database liveness/ready, HTTPS/scheduler health, configured LAN endpoint, root fingerprint and leaf expiry. Console shows Controller availability/trust and operator identity. Never expose raw DB URL/password, root private key, bearer, enrollment code after display expiry, browser cookie, DOM, unredacted URL or personal account data in diagnostics.

## 9. Quit authorization and Linux Controller interoperability

Deliberate Worker Quit/Restart requires current OWNER/ADMIN/OPERATOR authentication; VIEWER is denied. Controller stop/restart requires OWNER/ADMIN. Windows disables the legacy `worker_admin_token` bypass; Linux/IT compatibility is opt-in with `THREADS_PLATFORM_WORKER_ADMIN_AUTH_PROFILE=legacy_linux_it`. No device self-drain endpoint or hidden/global admin bearer is allowed. DX-07 defines the real Worker host/drain path; DX-05 authorizes the deliberate local lifecycle action only. OS process termination remains outside an API guarantee.

Linux/Docker first-Owner bootstrap uses the same local CLI/stdin contract and Operator API as Windows. Its Python TLS admin CLI provisions an ECDSA P-256 root and leaf, enforces private-key permissions, and prints the root DER fingerprint. Direct Uvicorn TLS uses only the leaf-serving volume; scheduler receives no TLS private key. Verified HTTPS and WSS clients trust the application-private root without installing it into an OS CA store. Linux packaging does not import Tauri or Windows DPAPI.

## 10. Operational rehearsal and signoff

Use [Acceptance Matrix](ACCEPTANCE_MATRIX.md) as the canonical case list. Each run captures: image/build/version, Desktop and sidecar SHA256, Python/Rust/TS lock hashes, Windows version, Windows account context (non-secret), Controller root fingerprints (public), endpoint, test identity synthetic labels, test timestamps, exact PR commit head, outcomes with failure logs redacted. Store evidence in repo only when sanitized and approved. External real Threads/Meta activity must obey existing live-validation gate #3; synthetic browser fixture/login on test accounts does not prove production API capability. Notify Product Owner of no-backup/no-before-login limitations at each pilot signoff.

## 11. Shared Desktop acceptance and fallback gates

The `PR Acceptance` and `Main Verification` orchestrators use the selected `shared` runtime. Rust/Tauri and shared package verification run in parallel; a later join consumes their exact-SHA artifacts and runs the non-admin Controller lifecycle smoke. Draft PR pushes run only Secret scan and PR Head Guard; the coordinator starts full exact-head acceptance by marking the reviewed PR Ready. The `split` fallback is a separate `Desktop Diagnostic` `workflow_dispatch` runtime-only feasibility check, requires an exact `source_sha`, and does not replace shared acceptance. Artifact names and workflow behavior are documented in the [Windows runtime packaging runbook](../../packaging/windows_desktop/README.md).
