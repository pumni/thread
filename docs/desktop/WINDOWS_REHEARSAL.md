# Windows Desktop v1 — Packaging, Operations and Rehearsal Runbook

**M1 implementation and acceptance runbook.** This remains an internal disposable test bundle, not an installed customer product or production runbook. Exact-head Windows evidence and separate coordinator acceptance are still required.

## 1. Deployment profiles

| Profile | Customer/user interaction | Processes | State owner |
|---|---|---|---|
| Windows Controller | Same single Desktop setup, Controller first-run | Tauri tray supervisor, bundled PostgreSQL, Python HTTP, standalone scheduler | One Workspace PostgreSQL + Controller DPAPI trust |
| Windows Worker | Same setup, Worker first-run | Tauri tray supervisor, existing Python Worker, headed Chromium | One user-scoped Worker identity/profiles/journal |
| Windows Console | Same setup, Console first-run | Tauri UI + secure HTTPS client only | Client-specific trusted endpoint, in-memory human session |
| Linux/Docker Controller | Existing developer/IT Compose/server workflow | PostgreSQL + FastAPI + scheduler | Same database/schema/Operator API/Worker protocol |

Current legacy Windows Worker Task Scheduler host remains supported until explicit DX-07 cutover. Do not run it and the Desktop supervisor against the same data root.

## 2. Prototype proof vs production-shaped first run

- **M1 / DX-04:** provisional local engineering runtime **on loopback only**, using a disposable database and the accepted `shared` PyInstaller onedir. Its `http` and `scheduler` modes are separate OS processes. M1 does **not** expose an unauthenticated LAN API, create a fake durable Owner, imply production auth, or require a final installer. It proves PostgreSQL -> migrations -> M1 bootstrap boundary (no Owner writes) -> HTTP -> scheduler -> X-to-tray -> Quit -> crash recovery.
- The UI states that runtime is unavailable before this Windows user signs in, Windows logout is unsupported, and portable backup/production durability are unavailable.
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

Target immutable binaries: `C:\Program Files\Threads Operations\...`. M1 uses the current user's Tauri app-local-data directory, `%LOCALAPPDATA%\com.pumni.threads-desktop\Controller\`, so the interactive user owns one ACL-scoped root and current-user DPAPI can protect its database credential. DX-04 records the runner privilege context and verifies current-user Full Control plus same-user DPAPI round-trip. A later installed deployment may use `%PROGRAMDATA%\ThreadsOperations\Controller\` only after the installer-to-runtime-user ACL handoff is proven; M1 does not claim that installer behavior. No writable executable is stored inside Controller data and no database directory is copied to a release artifact.

Illustrative stable layout:

```text
Controller/
  controller.json                 atomic non-secret ID and persisted ports
  database-credential.dpapi       current-user DPAPI protected credential
  postgresql/                     private PostgreSQL data directory
  .owner.lock                     exclusive current-process data-root lock
```

Worker remains under its existing `%LOCALAPPDATA%\ThreadsOperations\worker|profiles|journal|logs` layout. A separate Console user-profile store contains non-secret recent endpoint/trust metadata; never copy Controller private material.

PostgreSQL and M1 HTTP bind `127.0.0.1` on ports persisted in `controller.json`; a collision fails without changing either port. M1 HTTP is intentionally unauthenticated and stays loopback-only. DX-05 adds Owner bootstrap/RBAC; DX-06 selects the trusted HTTPS/WSS LAN endpoint. Firewall and elevation behavior remain outside M1.

## 5. Startup and shutdown state machine

```text
UNPROVISIONED -> PREFLIGHT/ROOT_LOCK -> DB_STARTING -> DB_READY
              -> MIGRATING -> M1_BOOTSTRAP_BOUNDARY (no Owner write)
              -> HTTP_STARTING -> HTTP_READY -> SCHEDULER_STARTING -> RUNNING

RUNNING --X--> RUNNING+HIDDEN
RUNNING --explicit Quit--> STOPPING -> STOPPED -> desktop exits
failure -> FAILED with a fixed redacted code; readiness waits use bounded backoff
```

Controller config has a versioned atomic write and contains no credential. The DPAPI credential is separate. The root lock is held before any child starts; a second owner fails. Migrations use the unchanged Alembic head and never roll back schema. `initdb` runs only for a root created by the current first-run call; an incomplete, corrupt or unowned existing root fails without reset. HTTP and scheduler share `threads-runtime.exe` but have independent process IDs and lifecycles. Quit stops scheduler, then HTTP, then asks bundled `pg_ctl` for PostgreSQL fast shutdown. PostgreSQL crash or parent crash leaves the cluster in place for WAL recovery.

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

DX-04 assigns each runtime child to a Windows Job Object with `KILL_ON_JOB_CLOSE` and holds an exclusive data-root file handle. A parent crash closes the job, terminates PostgreSQL/HTTP/scheduler, and releases the root lock. The acceptance smoke kills only the Tauri parent, verifies all three runtime processes exit, relaunches the same cluster, checks the PostgreSQL system identifier and a committed synthetic sentinel, and confirms exactly one scheduler. A PostgreSQL-only crash fails closed and stops its HTTP/scheduler dependents before same-data relaunch. No crash path runs `initdb` for an existing root.

## 7. Packaging and upgrade cutover

- DX-03 compared shared Python `onedir` multi-mode helper to split executables; `shared` is selected for M1 only. M1 records binaries/manifest/license and verifies current-user ACL/DPAPI under the logged-in Windows user. Installer-elevation handoff, TLS termination, signing and AV remain later gates. Bundle matching Playwright Chromium for Worker without copying browser profiles into release assets.
- Keep runtime binaries versioned and immutable. Desktop lifecycle/role/data schema do not depend on app release directory. No automatic downloader/updater.
- Upgrade manually **for disposable/internal test data only in v1**: validate source version and schema, stop/drain, preserve state where possible, replace binaries, migrate forward, restart, inspect health. Schema rollback is not assumed. Do not claim customer-data upgrade safety without a portable backup/restore solution. Product backup/restore is deferred: destructive migration and production-data upgrades require explicit future safety gate.
- Installer removal must not delete durable Controller DB or Worker browser profiles implicitly. Require separate explicit decommission/data-deletion user action and privacy warning.
- Generate pinned manifest, SHA256, third-party license notices, final signing/publisher plan and secret scan. Unsigned test artifact is internal only.

## 8. Operational diagnostics — no secret-bearing dumps

Controller shows database liveness/ready, HTTP/scheduler health, configured LAN endpoint, Workspace ID/display name, runtime versions, up/down bounded status and redacted correlation IDs. Worker shows presence, capacity, assigned account count, local browser session states and interventions; Console shows Controller availability/trust and operator identity. Never expose raw DB URL/password, root private key, bearer, enrollment code after display expiry, browser cookie, DOM, unredacted URL or personal account data in diagnostics.

## 9. Quit authorization and Linux Controller interoperability

Normal Quit on a provisioned busy Worker needs a reviewable initiation mechanism, **not** an unbounded global static Worker admin token. Current device auth only completes a drain requested by a privileged caller. DX-05/DX-07 must choose a bounded v1 policy: proposed normal UX prompts for suitably privileged Operator login when no human is authenticated (or security-review a self-only device drain endpoint). Test no human login, expired Operator session, Worker mid-job and Controller unreachable; if the user confirms forced exit or Windows logs off, report possible lease recovery rather than pretending DRAINING reached OFFLINE. For Controller Quit, freeze Owner/Admin operational permission in DX-05; OS user process termination remains outside an API guarantee.

Linux/Docker continues as an official IT/developer Controller profile: provide a separate OS-appropriate protected trust-key store and local administrator CLI fingerprint retrieval; configure an HTTPS/WSS public ingress that preserves Worker/Operator authorization and secure ASGI scheme semantics. Windows Worker and Console first-contact trust verification and login must interoperate with this profile. Linux packaging need not reuse the Tauri app or Windows DPAPI.

## 10. Operational rehearsal and signoff

Use [Acceptance Matrix](ACCEPTANCE_MATRIX.md) as the canonical case list. Each run captures: image/build/version, Desktop and sidecar SHA256, Python/Rust/TS lock hashes, Windows version, Windows account context (non-secret), Controller root fingerprints (public), endpoint, test identity synthetic labels, test timestamps, exact PR commit head, outcomes with failure logs redacted. Store evidence in repo only when sanitized and approved. External real Threads/Meta activity must obey existing live-validation gate #3; synthetic browser fixture/login on test accounts does not prove production API capability. Notify Product Owner of no-backup/no-before-login limitations at each pilot signoff.
