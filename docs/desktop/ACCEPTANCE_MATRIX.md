# Desktop v1 — Acceptance Matrix

**Rule:** evidence must map to an exact commit SHA, OS/build/runtime versions, test mode and artifacts. Green CI is a technical prerequisite, **not** product acceptance. Existing [acceptance protocol](../ACCEPTANCE_AND_REVIEW.md) continues to apply. A planned test is not a passing test.

## Global gates

| ID | Acceptance proof | Required evidence |
|---|---|---|
| G-01 | Python Control Plane remains authoritative; no React/Worker DB writes or Rust business retries | Code review of module boundaries and regression suite |
| G-02 | `Command != WorkerJob`; assignment, active lease/fencing and safe checkpoint preserved | Existing PostgreSQL concurrency/recovery tests green |
| G-03 | No approved browser capability widened, bypassed, or inferred from UI demo | Contract tests + reviewer scope diff |
| G-04 | Operator login does not leak bearer; Worker private keys/profile material stay local | IPC snapshot inspection, secret scan, negative redaction tests |
| G-05 | Linux/Docker Controller works against same schema/protocol/Operator API | Existing Compose smoke + new auth/schema smoke |
| G-06 | Every PR updated docs, records exact-head test evidence and gets separate coordinator acceptance | PR checklist and links |

## DX checkpoint evidence

| Package | Positive evidence | Adversarial/failure evidence | Exit condition |
|---|---|---|---|
| DX-01 | ADR/plan/contracts approved | Conflict against existing ADR/workflow audited | Product scope and risk owner recorded |
| DX-02 | On Windows X hides UI; tray node remains; reopening relocks protected UI; Windows session lock and inactivity timeout lock deterministically without stopping Controller/Worker helpers | Second invocation, Quit during startup, force-kill fake helper, incorrect persisted role, shared-PC lock, logout/relock, native WTS lock event, tray reopen | Fake supervisor and session lifecycle deterministic; Operator authentication/RBAC remains DX-05 |
| DX-03 | Exact-SHA GitHub-hosted Windows x64 runner builds and runs both candidates as a non-admin; host Python/uv/PostgreSQL are excluded from runtime resolution, and ambient Docker presence is recorded without becoming a runtime prerequisite | Missing redistributable, wrong architecture, host runtime resolution, bad ACL, migration drift, non-loopback listen, log leak, license unresolved | Reproducible `onedir`/alternate packaging decision; coordinator selects no layout before review |
| DX-04 | **Disposable loopback-only `shared` test bundle** initdb/migrate/HTTP/scheduler; HTTP and scheduler have distinct OS PIDs; close tray; ordered Quit; restart same data | No LAN listener or fake Owner before auth/TLS; corrupt/unowned/unwritable root, disk full, migration failure, DB and endpoint port conflict, DB crash, true Tauri parent crash/WAL recovery, duplicate supervisor, second `initdb` | **M1** restricted prototype accepted (not final installer) |
| DX-05 | Provision Owner on Windows **and Linux CLI**, login each role, create users, logout/revoke; operator-driven worker admin actions | Last Owner concurrent disable/demote, brute force, stale role after downgrade, **existing worker_admin_token enrollment/drain/intervention bypass**, forged created_by audit, Windows lock/unattended UI | Server-side auth/RBAC + legacy route reconciliation accepted |
| DX-06 | IP-SAN HTTPS + **WSS** verified; Controller local/Console API parity; fingerprint from trusted local Controller identity; explicit TLS termination selection | MITM peer, wrong SAN/root/expiry, fake fingerprint UI, **spoofed forwarded headers, Worker ASGI scheme/proxy mismatch, Operator HTTP bypass**, changed IP, plaintext downgrade, credentials before trust | LAN trust security signoff |
| DX-07 | Existing Worker package/current-user DPAPI/profile/journal continue under Desktop; headed browser visible | Old scheduled task double-start, wrong user, corrupted DPAPI identity, quit during active job, forced host shutdown | One Worker host + safe drain and rollback |
| DX-08 | Owner/Admin pairing, verified Worker, existing **256-bit enrollment credential** retained, authenticated job/presence and Console login; separately authorized optional short-code redemption | Replay/expiry, brute attempts, **reduced-entropy credential substitution**, wrong root/device, offline revoke, cross-Workspace profile exposure | **M2** secure three-mode LAN connectivity |
| DX-09 | Worker human login -> **separate pending-profile launch** -> reviewed login-state validation -> atomic registration -> same profile on restart; explicit new capability version | No preassigned Account, registration response lost after commit, retry and local bind crash, role revoked, wrong worker, stale intent, unknown UI, unverified browser identity/API duplicate, cookie leak | Browser-only Account first class, no premature AUTHENTICATED claim |
| DX-10 | Human same-profile re-login, disable/enable, safe reassign/quarantine, capability-specific API availability | Worker offline, race with WorkerJob claim/lease, old Worker stale report, unknown remote identity, profile deletion offline | **M3** account lifecycle acceptance |
| DX-11 | Controller and Console consistent summary; Worker UI functional; Viewer truly read-only | API 403 even if UI control forged; revoked session, Worker status stale, long account lists, keyboard/focus/accessibility | Usable role-aware product UI |
| DX-12 | One installer, pinned manifest/lockfiles, no user dependency prompts, data retained across upgrade | Uninstall vs data retention, shared root permission, wrong signature, corrupted archive, legacy task migration | Reproducible internal installer with release caveats |
| DX-13 | Three fresh Windows PCs/VMs execute end-to-end pairing/account lifecycle, collect evidence; exercise Linux/Docker bootstrap/API | Network partition, Controller crash, scheduler restart, Windows reboot-before-login, Worker offline/reconnect, TLS mismatch, migration failure, **legacy-token privilege bypass**, account-registration commit/bind crash | **M4** synthetic-data-only controlled pilot/demo acceptance |
| DX-14 | Signed-off threat-model/runbook and explicit deferred risks | Open blockers and external API/release gates misrepresented | Epic closed **only** for Desktop v1 accepted scope |

### DX-04 evidence details

- Build and run only the accepted `shared` candidate for M1. `threads-runtime.exe http`, `scheduler`, and one-shot `migrate` are distinct process lifecycles. The `split` candidate remains a feasibility fallback and is not used by M1.
- Capture the CurrentUser LocalAppData root ACL, CurrentUser DPAPI round-trip, atomic non-secret `controller.json`, stable Controller ID/DB port/HTTP endpoint, and loopback-only listeners. No credential, raw process output or DB directory is uploaded.
- Windows smoke uses synthetic state to exercise X-to-tray, second launch convergence, ordered Quit, relaunch identity, PostgreSQL-only crash, Tauri parent termination with Job Object ownership/WAL recovery, failed migration, both persisted-port collisions, unowned root and corrupt cluster.
- Rust tests cover the zero/below-minimum free-space boundary and inject that preflight failure to verify a fresh root is not created and an existing root is untouched. The Windows smoke does not physically fill a volume, so post-preflight disk exhaustion is outside this M1 evidence.

## Planning-audit blocker regression gates

- **Legacy admin surface:** With Desktop profile enabled, old static `worker_admin_token` must not authorize enrollment, admin drain/abort or intervention resolution outside the explicit reviewed compatibility policy; `created_by`/`resolved_by` must be assigned by server-side Operator context. Existing Worker device/auth and CRM ingress paths must retain their intentionally separate principals.
- **Pairing entropy:** An untrusted LAN peer cannot receive the full enrollment token or Operator password before independent Controller root verification; existing 256-bit token semantics persist. Six-digit redemption, if implemented, must pass distributed wrong-code/rate-limit and strong-token exchange tests.
- **First-time browser login:** Test old `WorkerSessionService.account_context` still rejects unassigned Accounts; isolated local pending profile is allowed only in provisioning context and cannot run WorkerJobs. Unknown session verification stays non-ready. After server commit, crash before Worker local bind reconciles by idempotency and same profile_ref.
- **Browser route and evidence:** Before pending profile login, choose and explicitly display DIRECT or a reviewed proxy-backed route; a pending profile has no `account_id`, so existing `NetworkProfile` cannot silently supply proxy configuration. If a required route or production proxy secret provider is unavailable, block onboarding before credentials are entered. Synthetic fixtures alone are not a real Threads login proof; the accepted M3 live-browser check needs consented test-account evidence separately authorized from #3.
- **M1 security boundary:** No public LAN listener, first-Owner HTTP bootstrap or final-installer claim before DX-05/06/12. The disposable test proof has its own isolated state root.
- **Transport:** Enforce TLS on Operator + Worker public paths and WSS; run through actual Windows terminating process with ASGI scope header-negative tests, not just HTTP fixtures.
- **Operations:** Hard-kill Controller owner and check WAL restart; Worker unreachable on Quit cannot claim OFFLINE without authenticated Controller status.

- **Quit authorization:** User logs out of Worker Desktop but Agent keeps executing device-authenticated jobs. Intentional tray Quit while no human is logged in must trigger approved Operator login for a Controller-authorized drain **or** pass a separately reviewed device-authenticated self-only drain implementation. Test Worker online/busy/Controller unreachable and no hidden static admin bearer; forced OS shutdown is not graceful.
- **Linux/Docker interoperability:** Configure Linux-specific protected trust-key storage and HTTPS/WSS ingress. Verify Windows Worker/Console independently compare actual Controller root fingerprint from local CLI, then connect through the **same** API and Worker protocol; no Windows DPAPI import in Linux/domain and no plaintext public Operator API.

## Windows/CI matrix

| Scope changed | Gate |
|---|---|
| React UI only | `bun install --frozen-lockfile`, lint, TS strict typecheck, React tests, Vite build, Tauri IPC snapshot tests |
| Rust supervisor/native auth | `cargo fmt --check`, clippy warnings-as-errors, Rust unit/integration, Windows lifecycle process tests |
| Python auth/accounts/protocol | `uv sync --locked`, `ruff check`, `ruff format --check`, `pyright`, `alembic check`, `pytest`; PostgreSQL migration/real-concurrency tests |
| Installer/sidecar/PostgreSQL | Windows x64 version-pinned packaging job, signed-ready artifacts, SHA256/manifest/third-party license audit, isolated GitHub-hosted Windows x64 smoke |
| Shared Operator/Worker protocol | Python + Rust/TS fixture parity and negative auth, version mismatch tests |
| Linux/Docker backend/schema | Existing Docker Control Plane smoke and scheduler crash/recovery; new Operator API reachability smoke |
| Any scope | Secret scan, dependency review where relevant, accepted issue and exact-head PR check |

CI path filters optimize routine scope, **but** shared protocol/schema and packaging changes must trigger appropriate cross-language/Windows/Linux integration. Browser/Windows E2E may be gated manual runner rehearsal until reproducible automation exists; state this explicitly rather than treating a skipped test as passed.

## Three-PC rehearsal script

1. Windows x64 Controller VM: fresh image, sign in dedicated user, install one Desktop, select Controller; verify no Python/PostgreSQL/Docker required; set stable address, provision Workspace and first OWNER, inspect HTTPS identity and DB loopback-only.
2. Window X -> hidden tray -> HTTP and scheduler stay alive; open Desktop again -> same PID/runtime and single Controller. Quit -> confirm impact and stop orderly; reopen -> same data and Controller identity.
3. Worker VM: install same setup, Worker mode, verify trusted Controller fingerprint against local Controller screen **before submitting code**, pair once, allow no duplicate scheduled-task/desktop host, create device identity and start existing headed Worker.
4. Console VM: install same setup, Console mode, verify root fingerprint out-of-band, login as each role, compare read model with local Controller view and test 403 server-side for disallowed mutations.
5. Worker VM: OWNER/ADMIN login, Add Account, human browser login, bounded session validation, atomic Controller registration and account/profile/Worker affinity persistence. Logout human; Agent continues. Force restart and verify no cookie/profile material left Worker.
6. Simulate expired browser session, human re-login on same profile, Worker network loss, Worker DRAINING and explicit re-pair requiring profile quarantine. Exercise Account disable/reassign and stale report fencing in staging.
7. Crash HTTP and scheduler separately; disconnect PostgreSQL; repeat process start from one data root; inspect WAL-safe recovery, bounded retries and fail-closed migration. Check Windows lock vs actual logout; never claim reboot-before-login operation.
8. Run Linux/Docker Controller compatibility route and source-of-truth regression tests; capture signed/dated, scrubbed evidence and outstanding production blockers.

## Pilot acceptance vs production certification

The **Desktop v1 pilot** may be accepted when M1–M4, DX-14 and all applicable gates pass on review. It must be labeled **controlled pilot/internal demonstration**, not general-availability/production durability. Production activation still independently requires #3 live Meta validation, #80 discovery permission gates if in scope, #62 CRM sink if included, #11 end-to-end certification and #1 owner's security follow-up as applicable. Backup/recovery deferral means a single Controller host failure may cause unrecoverable loss: do not onboard irreplaceable business data without an explicit owner risk decision and follow-on recovery implementation.
