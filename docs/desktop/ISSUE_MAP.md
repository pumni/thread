# GitHub issue map — Desktop v1

**Epic:** [#94](https://github.com/pumni/thread/issues/94) · **Planning draft:** [#93](https://github.com/pumni/thread/pull/93) · **Date:** 2026-10-01

This is a linkable execution index for the canonical [Delivery Plan](DELIVERY_PLAN.md). Begin a new conversation at [SESSION_HANDOFF.md](SESSION_HANDOFF.md), and read the [pre-implementation audit](PREIMPLEMENTATION_AUDIT.md). Child issues are planning scope unless separately authorized. Issue #100 / DX-06 is authorized by coordinator comment `5973437416`; its exact-base Draft checkpoint is the only active implementation scope. Do not start DX-07/08/09 from this authorization.

| ID | GitHub issue | Phase / checkpoint | Required dependencies |
|---|---|---|---|
| DX-01 | [#95 — ADR/contracts approval](https://github.com/pumni/thread/issues/95) | P0 / M1 gate | none |
| DX-02 | [#96 — Desktop scaffold/tray](https://github.com/pumni/thread/issues/96) | P1 / M1 | #95 |
| DX-03 | [#97 — native runtime packaging feasibility](https://github.com/pumni/thread/issues/97) | P1 / M1 hard gate | #95 |
| DX-04 | [#98 — Controller Windows supervisor vertical slice](https://github.com/pumni/thread/issues/98) | P1 / M1 | #96, #97 |
| DX-05 | [#99 — Operator RBAC/login/audit](https://github.com/pumni/thread/issues/99) | P2 / M2 | #95; integrate #98 |
| DX-06 | [#100 — Controller HTTPS/trust](https://github.com/pumni/thread/issues/100) | P2 / M2 | #97, #98, #99 |
| DX-07 | [#101 — Worker Desktop host/cutover](https://github.com/pumni/thread/issues/101) | P2 / M2 | #96, #97 |
| DX-08 | [#102 — Worker pairing/Console trust](https://github.com/pumni/thread/issues/102) | P2 / M2 | #99, #100, #101 |
| DX-09 | [#103 — Worker-centric account onboarding](https://github.com/pumni/thread/issues/103) | P3 / M3 | #99, #101, #102 |
| DX-10 | [#104 — account lifecycle/interventions](https://github.com/pumni/thread/issues/104) | P3 / M3 | #103 |
| DX-11 | [#105 — role-aware UI/read models](https://github.com/pumni/thread/issues/105) | P3 / M3-M4 | #99, #102, #103; integrate #104 |
| DX-12 | [#106 — one installer/scoped CI](https://github.com/pumni/thread/issues/106) | P4 / M4 | #98, #101, #102, #105 |
| DX-13 | [#107 — three-PC E2E/failure rehearsal](https://github.com/pumni/thread/issues/107) | P4 / M4 | applicable #98–#106 |
| DX-14 | [#108 — security/operations/pilot handoff](https://github.com/pumni/thread/issues/108) | P4 / M4 closure | #100, #102, #104, #107 |

## Audit-driven hard gates

- [#99 DX-05] Windows always isolates/disables legacy `worker_admin_token`; Linux/IT compatibility is explicit opt-in. Bootstrap the first Owner locally over a non-network channel before public Operator routes are available.
- [#102 DX-08] Do **not** replace existing 256-bit Worker enrollment credential with six-digit UI code. Optional short-code UX is a security-reviewed redemption after verified TLS; otherwise use securely conveyed high-entropy code.
- [#103 DX-09] Existing browser session requires Account/assignment. Build isolated pending local profile + reviewed login-state detector, atomic Controller registration and version-negotiated additive protocol.
- [#98 DX-04] M1 is loopback-only disposable prototype without final installer or real Owner/remote HTTPS. First usable LAN requires #99 + #100; final customer installer is #106.
- [#100 DX-06] Direct Uvicorn TLS is the sole terminator (`proxy_headers=False`); one HTTPS/WSS listener serves Operator, Worker, health, readiness, and metrics. Persist an explicit stable IPv4/port, exact two-IP SAN, DPAPI CurrentUser root/leaf keys, temporary leaf serving file, strict TLS-only first contact, endpoint-bound app-private trust, and Linux/Docker private TLS parity.
- [#97 DX-03/#98 DX-04] Windows installer admin context vs single non-elevated runtime user and DPAPI must be proven. No data-root/identity surprises.

- [#99 DX-05 / #101 DX-07] Deliberate Worker Quit/Restart requires OWNER/ADMIN/OPERATOR login; Controller stop/restart requires OWNER/ADMIN. No device self-drain endpoint or hidden admin bearer.
- [#100 DX-06 / #107 DX-13] Linux/Docker Controller uses the local Python TLS admin CLI, strict private-key modes, direct Uvicorn TLS, leaf-only HTTP volume, and no scheduler key. Verify the same secure Operator/Worker API without Windows DPAPI, Tauri, or CA-store installation.

## Current authorization checkpoint

Issue #100 was authorized by coordinator comment `5973437416` against base `14942ddebb6322db049d61db83cd335b055a93b6`, on branch `codex/dx06-controller-https-trust`. DX-06 is the only authorized implementation scope in that checkpoint. Push a Draft PR, wait only for Secret scan and PR Head Guard, report the exact head, and stop for coordinator review. Do not transition the PR to Ready, run heavy hosted workflows, merge, or start DX-07/08/09 from this authorization.

## Product/security decisions

- DX-05 / #99: role matrix, human session lock behavior and concurrency-safe last-Owner guard are frozen by the latest coordinator authorization comment.
- DX-03: exact Python shared runtime, Windows PostgreSQL bundle, pinned versions and redistributable license support.
- DX-06: the remote trust probe sends no HTTP application bytes or credentials; the user compares the candidate root DER fingerprint with the trusted local Controller display before explicit opaque-probe confirmation. No Worker pairing UX is part of DX-06. DX-08 retains the existing 256-bit enrollment token.
- DX-09: migration shape for nullable canonical remote identity; reviewed evidence level for browser session and API/browser identity binding.

## External issues deliberately **not** dependencies for the synthetic/Desktop controlled prototype

- [#3 Meta Threads live API gate](https://github.com/pumni/thread/issues/3)
- [#80 discovery permissions/review](https://github.com/pumni/thread/issues/80)
- [#62 production CRMResultSink](https://github.com/pumni/thread/issues/62)
- [#11 production end-to-end certification](https://github.com/pumni/thread/issues/11)
- [#1 owner legacy security confirmation](https://github.com/pumni/thread/issues/1)

These gates remain essential to **production/release activation** wherever relevant, and must never be inferred as complete from the Desktop demo.

## Historical planning PR review checklist

- [ ] ADR-0007 product/architecture decisions confirmed.
- [ ] DX-01…DX-14 issue scope/dependencies reviewed.
- [ ] M1-first/clean-machine critical path accepted.
- [x] DX-05 RBAC matrix and security policy frozen in #99 coordinator authorization.
- [ ] TLS and browser identity assumptions assigned explicit security stop gates.
- [ ] Deferred backup/portable restore risk acknowledged.
- [ ] GitHub PR #93 independently ACCEPTED after exact-head document/consistency review; **no auto-merge**.
