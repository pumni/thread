# Desktop v1 — Fresh Session Handoff (start here)

**Prepared:** 2026-10-01 after detailed pre-implementation audit. **Repository:** [pumni/thread](https://github.com/pumni/thread). **Planning PR:** [#93 — DRAFT](https://github.com/pumni/thread/pull/93), source branch `plan/desktop-windows-v1-20261001`. **Epic:** [#94](https://github.com/pumni/thread/issues/94), child issue map [#95–#108](ISSUE_MAP.md). The referenced branch is **not main until PR #93 is independently accepted and merged**.

## 1. Read order; never start from old conversation reconstruction

1. Read `AGENTS.md` and the **current live** `docs/PROJECT_STATE_HANDOFF.md` on latest main (the historical handoff may be outdated; follow its dated Desktop pointer).
2. Fetch live PR #93 status, its **current head SHA**, the current main SHA and any review comments. Read this handoff and [PREIMPLEMENTATION_AUDIT.md](PREIMPLEMENTATION_AUDIT.md) from the PR head while it is unmerged; do not assume these files already exist on main.
3. Read [ADR-0007](../adr/0007-windows-first-single-app-desktop.md), [DELIVERY_PLAN.md](DELIVERY_PLAN.md), [SECURITY_AND_PROTOCOLS.md](SECURITY_AND_PROTOCOLS.md), [ACCEPTANCE_MATRIX.md](ACCEPTANCE_MATRIX.md), [WINDOWS_REHEARSAL.md](WINDOWS_REHEARSAL.md) and [ISSUE_MAP.md](ISSUE_MAP.md) **at the same ref**.
4. Fetch live epic #94, scope/acceptance and comments on **DX-01 issue #95**. Inspect current implementation in scope. For a later task, read only its expressly authorized DX issue and linked focused docs rather than all Desktop material every time.
5. Check exact-head docs/CI/review state; report PASS/BLOCKER by AC. **Do not self-merge** on CI success; obtain separate coordinator ACCEPTED and recheck unchanged SHA before any merge.

## 2. Current work status as of this handoff

- Backend main at the audit start: `73681a4b7a74826915ccea86eff918edf4497bd5`; **re-fetch** before any work because this SHA is an observation, not a permanent base.
- #93 is **planning-only DRAFT**; architecture, detailed 14-slice roadmap and GitHub issue network have been written to its branch. New audit corrections and this handoff are also on that branch, **not accepted into main yet**.
- All 14 DX issues #95–#108 were created under epic #94, each with scoped deliverables, prerequisites, acceptance criteria, failure tests and non-goals. **Issue existence is not implementation authorization.**
- No Tauri/React/Rust/Desktop runtime code currently exists on the reviewed baseline. No clean Windows Desktop test/installer has been run. Planning document completeness does not prove feasibility.
- Existing #3 Meta live gate, #80 reviewed discovery permission gate, #62 CRMResultSink dependency, #11 final release certification, #1 owner historical security follow-up remain separate/open unless **live GitHub** says otherwise.

## 3. Product choices **confirmed** by owner — do not ask again

- One customer-visible Threads Desktop app and one installer; Tauri 2 + Rust supervisor + React/TypeScript. Internal PostgreSQL/Python/Chromium helper processes are allowed. No customer-visible Controller service executable in MVP.
- First-run Controller, Worker or client-only Console. Controller/Worker role effectively fixed until explicit decommission/reset; Console may reconnect freely. Combined same-PC Controller/Worker is dev/demo deferred.
- X/close hides window and leaves node running in tray. Explicit Quit confirms and orderly stops node. Autostarts when its **single dedicated Windows runtime user signs in**. Windows logout or reboot-before-login stops v1; no service-host mode or Windows auto-login workaround now.
- Windows-first customer deployment but existing Linux/Docker Controller stays supported, same Python schema/API/protocol. One Controller = one Workspace, multiple Operator users/Workers.
- Stable LAN IP (static/DHCP reservation), private trusted HTTPS, no LAN discovery in v1. Worker re-pair only via explicit drain/decommission/revoke; old Workspace Chromium profiles quarantined, not automatically reused.
- Human authentication mandatory on Controller PC, Worker PC and remote Console. Four fixed roles OWNER/ADMIN/OPERATOR/VIEWER. Worker-local OWNER/ADMIN login then Add Account and human Threads browser login; Controller owns Account UUID, assignment, policy/audit. Operator may handle approved routine re-login/intervention per **proposed** policy, subject to DX-05 Owner signoff.
- **Deferred and NOT in pilot:** portable backup/recovery and Owner Recovery Key, verified recovery email (Gmail or company email), automatic updates, Internet/relay, Windows Service before login, advanced SSO/HA/discovery/automatic browser profile migration.

## 4. Critical implementation discoveries from audit — mandatory issue stop gates

- **Legacy RBAC bypass risk (DX-05 #99):** `src/threads_platform/transport/http/workers.py` currently accepts a static `worker_admin_token` on enrollment creation, drain/abort and intervention resolve, with caller-supplied `created_by`/`resolved_by`. New Operator RBAC must inventory and retire/isolate these on Windows customer profile; server derives actor. CRM ingress/device auth remain separate. Do not claim all local/remote human mutations are RBAC-protected while old route still works unrestricted.
- **256-bit Worker enrollment (DX-08 #102):** `WorkerControlService.create_enrollment` uses `secrets.token_urlsafe(32)` and SHA-256 at rest, 10-minute single use. **Do not replace it with the UI's illustrative six-digit code.** For MVP use a safely conveyed full token after independently verified TLS, or an explicitly security-reviewed rate-limited short-code redemption that yields the unchanged strong credential.
- **No pre-account browser open (DX-09 #103):** current `WorkerSessionService.account_context` and `LocalBrowserSessionManager.open` require Account + active assignment. Design isolated pending provisioning profile + reviewed human-login-state detection; no fake Threads ID, auto-scrape assumptions or WorkerJob before registration. Controller atomically creates Account + profile ref + assignment + consumed intent + audit. Recover lost response/Worker crash by idempotency. Additive Worker protocol capability must version-negotiate with v2.
- **M1 scope error corrected (DX-04 #98):** M1 is **disposable, loopback-only** packaging/runtime/tray/crash proof without real Owner, remote API or final installer. DX-05 adds real Owner/operator bootstrap (Windows **and Linux CLI**), DX-06 adds verified public HTTPS/WSS, DX-12 packages final one-installer artifact. No unprotected LAN endpoint or fake persisted Owner.
- **TLS deployment gap (DX-06 #100):** existing `WorkerTransportTLSMiddleware` only checks `/v1/workers` ASGI scheme. Prove exact TLS termination, WSS upgrade, constrained forwarded headers and HTTPS enforcement on newly added public Operator endpoints; preserve isolated CA root private key.
- **Operational/data safety:** legacy Worker scheduled task can double-start with Desktop (#101); current-user DPAPI requires same runtime user and ACL/elevation handoff (#97/#98); parent kill-on-close is **abrupt** PostgreSQL stop and needs WAL test (#98); if Worker cannot reach Controller on Quit, never assert completed drain (#101); no customer production/irreplaceable data until future backup/recovery (#106–#108).

Read [audit findings A-01…A-16](PREIMPLEMENTATION_AUDIT.md) before changing any roadmap design.

## 5. First correct action in the next new session

**If #93 still draft/open and DX-01 #95 not separately accepted:**
1. Verify PR exact head and base, contents and issue links. Review updated planning docs against latest backend code and PR review comments.
2. Run a documentation-consistency audit; verify every linked DX issue/body was updated with its applicable audit blocker and dependency.
3. State any remaining BLOCKER/MAJOR and concrete fix. If evidence is sufficient, record exact-head technical **PASS**, then require explicit independent coordinator **ACCEPTED** of #93/#95. Do **not** merge without that acceptance and unchanged-head CI check.
4. Only after plan acceptance and explicit user/coordinator authorization, start **DX-02 #96** (Tauri foundation) and **DX-03 #97** (Windows Python/PostgreSQL bundle feasibility). Those are parallelizable if resourced. DX-03 is a hard feasibility stop; DX-04 #98 needs **both accepted**.
5. Next: DX-05 (#99) RBAC + Linux local bootstrap -> DX-06 (#100) TLS; DX-07 (#101) Worker Desktop host; DX-08 (#102) secure pairing; DX-09 (#103) account onboarding; DX-10/11; DX-12/13/14. See [issue dependency DAG](ISSUE_MAP.md).

**If #93 was already merged and accepted:** use live merged main/ADR and current issue bodies rather than this PR branch as authority; start only the specifically authorized DX issue, rechecking tests/CI and base SHA.

## 6. Coordinator quality/merge policy

For every code PR: authorization -> AC-by-AC review -> relevant unit/integration/negative/Windows evidence on **exact head SHA** -> technical PASS only after no blockers -> independent coordinator ACCEPTED -> re-fetch unchanged head + CI -> expected-SHA merge -> verify main -> close/update issue and dependent parent. A historical PASS without separate ACCEPTED is not merge authorization.

For docs-only PR #93: link exact reviewed SHA, verify paths/issue labels/bodies/ADR conflicts, record unresolved policy gates as **gated later** not “implemented”, keep it draft until owner/coordinator accepts and the merge step is authorized.

## 7. Useful anchors

- [Planning PR #93](https://github.com/pumni/thread/pull/93)
- [Epic #94](https://github.com/pumni/thread/issues/94)
- [DX-01 #95](https://github.com/pumni/thread/issues/95), [DX-02 #96](https://github.com/pumni/thread/issues/96), [DX-03 #97](https://github.com/pumni/thread/issues/97), [DX-04 #98](https://github.com/pumni/thread/issues/98)
- [Detailed issue map](ISSUE_MAP.md), [Audit](PREIMPLEMENTATION_AUDIT.md), [Acceptance matrix](ACCEPTANCE_MATRIX.md)
- Existing `docs/PROJECT_STATE_HANDOFF.md`, `docs/WORKER_AGENT_WINDOWS.md`, `docs/WORKER_UPDATE_RUNBOOK.md`, `docs/protocols/WORKER_PROTOCOL_V1.md` (filename **v1**, current Worker supports protocol **v2** additive extension)
