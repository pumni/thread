# Desktop v1 — Fresh Session Handoff (start here)

**Updated:** 2026-10-04 for authorized DX-06 implementation. **Repository:** [pumni/thread](https://github.com/pumni/thread). **Planning PR:** [#93 — ACCEPTED/MERGED](https://github.com/pumni/thread/pull/93) at `ed90ce7cfc3d26c40a94d153b5ff17653c3e6e1a`. **Epic:** [#94](https://github.com/pumni/thread/issues/94), child issue map [#95–#108](ISSUE_MAP.md). Issue #100 coordinator authorization is comment `5973437416`; it authorizes DX-06 only from base `14942ddebb6322db049d61db83cd335b055a93b6`. The Draft exact-head checkpoint is not coordinator acceptance.

## 1. Read order; never start from old conversation reconstruction

1. Read `AGENTS.md` and the **current live** `docs/PROJECT_STATE_HANDOFF.md` on latest main (the historical handoff may be outdated; follow its dated Desktop pointer).
2. Verify latest main SHA and PR #93 acceptance/merge. Read this handoff and [PREIMPLEMENTATION_AUDIT.md](PREIMPLEMENTATION_AUDIT.md) from latest main; inspect live issue comments and work only on an explicitly authorized issue.
3. Read [ADR-0007](../adr/0007-windows-first-single-app-desktop.md), [DELIVERY_PLAN.md](DELIVERY_PLAN.md), [SECURITY_AND_PROTOCOLS.md](SECURITY_AND_PROTOCOLS.md), [ACCEPTANCE_MATRIX.md](ACCEPTANCE_MATRIX.md), [WINDOWS_REHEARSAL.md](WINDOWS_REHEARSAL.md) and [ISSUE_MAP.md](ISSUE_MAP.md) **at the same ref**.
4. Fetch live epic #94, scope/acceptance and comments on **DX-01 issue #95**. Inspect current implementation in scope. For a later task, read only its expressly authorized DX issue and linked focused docs rather than all Desktop material every time.
5. Check exact-head docs/CI/review state; report PASS/BLOCKER by AC. **Do not self-merge** on CI success; obtain separate coordinator ACCEPTED and recheck unchanged SHA before any merge.

## 2. Current work status as of this handoff

- DX-02 through DX-05 are implemented on the authorized dependency chain; issues #97/#98/#99 were accepted and closed before #100 authorization.
- DX-06 #100 is the only authorized implementation scope for this checkpoint. Work proceeds from exact base `14942ddebb6322db049d61db83cd335b055a93b6` on `codex/dx06-controller-https-trust`, then Draft PR, Secret scan + PR Head Guard, and STOP for exact-head coordinator review.
- Do not mark Ready, merge, rebase, run heavy hosted workflows, or start DX-07, DX-08, DX-09, or later issues from this checkpoint.
- The one-app Tauri/Rust/React runtime and Python/PostgreSQL Controller implementation exist. A green local or Draft check is technical evidence, not coordinator acceptance.
- Existing #3 Meta live gate, #80 discovery permission gate, #62 CRMResultSink dependency, #11 final release certification, and #1 owner historical security follow-up remain separate unless live GitHub says otherwise.

## 3. Product choices **confirmed** by owner — do not ask again

- One customer-visible Threads Desktop app and one installer; Tauri 2 + Rust supervisor + React/TypeScript. Internal PostgreSQL/Python/Chromium helper processes are allowed. No customer-visible Controller service executable in MVP.
- First-run Controller, Worker or client-only Console. Controller/Worker role effectively fixed until explicit decommission/reset; Console may reconnect freely. Combined same-PC Controller/Worker is dev/demo deferred.
- X/close hides window and leaves node running in tray. Explicit Quit confirms and orderly stops node. Autostarts when its **single dedicated Windows runtime user signs in**. Windows logout or reboot-before-login stops v1; no service-host mode or Windows auto-login workaround now.
- Windows-first customer deployment but existing Linux/Docker Controller stays supported, same Python schema/API/protocol. One Controller = one Workspace, multiple Operator users/Workers.
- Stable LAN IP (static/DHCP reservation), private trusted HTTPS, no LAN discovery in v1. Worker re-pair only via explicit drain/decommission/revoke; old Workspace Chromium profiles quarantined, not automatically reused.
- **DX-06 topology:** Uvicorn itself is the sole TLS terminator; direct HTTPS/WSS shares one listener with Operator, Worker, health, readiness, and metrics. Bind `0.0.0.0:<persisted port>`, publish `https://<explicit LAN IPv4>:<port>`, use `https://127.0.0.1:<port>` locally, and disable proxy headers. Leaf SAN is exactly configured LAN IPv4 + `127.0.0.1`. No reverse proxy, discovery, plaintext health listener, or Windows CA-store installation.
- Windows root/leaf keys are DPAPI CurrentUser protected; the root fingerprint hashes actual DER. First contact is TLS-only, two-stage verified, memory-pending for five minutes, then confirmed by opaque probe ID and persisted as application-private root DER + canonical endpoint/fingerprint. Credentials remain unavailable until out-of-band comparison and explicit confirmation.
- Linux/Docker uses the Python TLS admin CLI and protected root-admin/leaf-serving volumes. HTTP receives leaf materials only; scheduler receives no TLS key. Linux/domain does not import Tauri or Windows DPAPI.
- Human authentication mandatory on Controller PC, Worker PC and remote Console. Four fixed roles OWNER/ADMIN/OPERATOR/VIEWER. The #99 coordinator authorization freezes the matrix: all roles may view workspace/fleet/account summaries and read-only diagnostics; only OWNER may create/change OWNER or ADMIN and recovery-sensitive settings; OWNER/ADMIN may manage OPERATOR/VIEWER and provision/revoke/re-pair Workers, manage Accounts and attach APIs; OWNER/ADMIN/OPERATOR may drain assigned Workers, acknowledge interventions, handle browser re-login/challenges and submit approved Commands within scope. Worker-local OWNER/ADMIN login then Add Account and human Threads browser login; Controller owns Account UUID, assignment, policy/audit.
- **Deferred and NOT in pilot:** portable backup/recovery and Owner Recovery Key, verified recovery email (Gmail or company email), automatic updates, Internet/relay, Windows Service before login, advanced SSO/HA/discovery/automatic browser profile migration.

## 4. Critical implementation discoveries from audit — mandatory issue stop gates

- **Legacy RBAC bypass risk (DX-05 #99):** `src/threads_platform/transport/http/workers.py` currently accepts a static `worker_admin_token` on enrollment creation, drain/abort and intervention resolve, with caller-supplied `created_by`/`resolved_by`. New Operator RBAC must inventory and retire/isolate these on Windows customer profile; server derives actor. CRM ingress/device auth remain separate. Do not claim all local/remote human mutations are RBAC-protected while old route still works unrestricted.
- **256-bit Worker enrollment (DX-08 #102):** `WorkerControlService.create_enrollment` uses `secrets.token_urlsafe(32)` and SHA-256 at rest, 10-minute single use. **Do not replace it with the UI's illustrative six-digit code.** For MVP use a safely conveyed full token after independently verified TLS, or an explicitly security-reviewed rate-limited short-code redemption that yields the unchanged strong credential.
- **No pre-account browser open (DX-09 #103):** current `WorkerSessionService.account_context` and `LocalBrowserSessionManager.open` require Account + active assignment. Design isolated pending provisioning profile + reviewed human-login-state detection; no fake Threads ID, auto-scrape assumptions or WorkerJob before registration. Controller atomically creates Account + profile ref + assignment + consumed intent + audit. Recover lost response/Worker crash by idempotency. Additive Worker protocol capability must version-negotiate with v2.
- **Pre-account network route (DX-09 #103):** existing `NetworkProfile` is account-scoped while the pending browser profile has no Account; current packaged Worker lacks a production proxy-secret provider. Decide and surface DIRECT-only onboarding vs reviewed pre-account proxy support **before browser launch**; do not silently ignore an expected proxy.
- **M1 scope error corrected (DX-04 #98):** M1 is **disposable, loopback-only** packaging/runtime/tray/crash proof without real Owner, remote API or final installer. DX-05 adds real Owner/operator bootstrap (Windows **and Linux CLI**), DX-06 adds verified public HTTPS/WSS, DX-12 packages final one-installer artifact. No unprotected LAN endpoint or fake persisted Owner.
- **DX-06 #100 frozen contract:** `TransportSecurityMiddleware` enforces HTTPS for `/v1/operator/**` and `/v1/workers/**`, WSS for Worker WebSocket, and rejects forwarded-proto spoofing. Uvicorn is the direct terminator (`proxy_headers=False`). Windows private root/leaf custody, two-stage TLS-only probe, strict endpoint-bound DPAPI trust, and Linux/Docker private TLS parity are specified in [SECURITY_AND_PROTOCOLS.md](SECURITY_AND_PROTOCOLS.md). This checkpoint must test actual HTTPS and WSS sockets.
- **Operational/data safety:** legacy Worker scheduled task can double-start with Desktop (#101); current-user DPAPI requires same runtime user and ACL/elevation handoff (#97/#98); parent kill-on-close is **abrupt** PostgreSQL stop and needs WAL test (#98); if Worker cannot reach Controller on Quit, never assert completed drain (#101); no customer production/irreplaceable data until future backup/recovery (#106–#108).
- **Tray Quit without human Operator login (DX-05/DX-07):** deliberate Worker Quit/Restart requires OWNER/ADMIN/OPERATOR login; Controller stop/restart requires OWNER/ADMIN. No device self-drain route or hidden admin bearer. UI session lock leaves the node running; Windows forced shutdown is not a graceful drain.
- **Linux/Docker secure Controller parity (DX-05/DX-06):** current-user Windows DPAPI/private-CA wizard cannot run on Linux. Specify protected Linux trust-key custody, HTTPS/WSS ingress and independently verifiable CLI fingerprint with the **same** Operator API and Worker protocol.

Read [audit findings A-01…A-18](PREIMPLEMENTATION_AUDIT.md) before changing any roadmap design.

## 5. DX-06 checkpoint boundary

Complete the authorized DX-06 implementation locally, commit it, verify `origin/main` still equals `14942ddebb6322db049d61db83cd335b055a93b6`, push the Draft PR, and wait only for Secret scan + PR Head Guard. Report exact-head evidence and stop. A hosted failure is a hard stop; do not rerun the same SHA or push a speculative fix. Coordinator exact-head review determines any correction or next transition. For later work, reread live GitHub issue status and authorization; this handoff never authorizes a subsequent DX issue.

## 6. Coordinator quality/merge policy

For every code PR: authorization -> AC-by-AC review -> relevant unit/integration/negative/Windows evidence on **exact head SHA** -> technical PASS only after no blockers -> independent coordinator ACCEPTED -> re-fetch unchanged head + CI -> expected-SHA merge -> verify main -> close/update issue and dependent parent. A historical PASS without separate ACCEPTED is not merge authorization.

For docs-only PR #93: link exact reviewed SHA, verify paths/issue labels/bodies/ADR conflicts, record unresolved policy gates as **gated later** not “implemented”, keep it draft until owner/coordinator accepts and the merge step is authorized.

## 7. Useful anchors

- [Planning PR #93](https://github.com/pumni/thread/pull/93)
- [Epic #94](https://github.com/pumni/thread/issues/94)
- [DX-01 #95](https://github.com/pumni/thread/issues/95), [DX-02 #96](https://github.com/pumni/thread/issues/96), [DX-03 #97](https://github.com/pumni/thread/issues/97), [DX-04 #98](https://github.com/pumni/thread/issues/98)
- [Detailed issue map](ISSUE_MAP.md), [Audit](PREIMPLEMENTATION_AUDIT.md), [Acceptance matrix](ACCEPTANCE_MATRIX.md)
- Existing `docs/PROJECT_STATE_HANDOFF.md`, `docs/WORKER_AGENT_WINDOWS.md`, `docs/WORKER_UPDATE_RUNBOOK.md`, `docs/protocols/WORKER_PROTOCOL_V1.md` (filename **v1**, current Worker supports protocol **v2** additive extension)
