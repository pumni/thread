# GitHub issue map — Desktop v1

**Epic:** [#94](https://github.com/pumni/thread/issues/94) · **Planning draft:** [#93](https://github.com/pumni/thread/pull/93) · **Date:** 2026-10-01

This is a linkable execution index for the canonical [Delivery Plan](DELIVERY_PLAN.md). Begin a new conversation at [SESSION_HANDOFF.md](SESSION_HANDOFF.md), and read the [pre-implementation audit](PREIMPLEMENTATION_AUDIT.md). All child issues are **open planning scope, not automatic authorization**. The ADR/plan must be reviewed and independently accepted before DX implementation work. The exact role-action policy is **proposed** until DX-05 product sign-off.

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

- [#99 DX-05] Close legacy `worker_admin_token` enrollment/drain/intervention bypass in new Windows profile; Windows and Linux secure Owner bootstrap before LAN auth.
- [#102 DX-08] Do **not** replace existing 256-bit Worker enrollment credential with six-digit UI code. Optional short-code UX is a security-reviewed redemption after verified TLS; otherwise use securely conveyed high-entropy code.
- [#103 DX-09] Existing browser session requires Account/assignment. Build isolated pending local profile + reviewed login-state detector, atomic Controller registration and version-negotiated additive protocol.
- [#98 DX-04] M1 is loopback-only disposable prototype without final installer or real Owner/remote HTTPS. First usable LAN requires #99 + #100; final customer installer is #106.
- [#100 DX-06] Real TLS/WSS terminator, trusted ASGI scheme, new Operator HTTPS enforcement, correct leaf/root key custody.
- [#97 DX-03/#98 DX-04] Windows installer admin context vs single non-elevated runtime user and DPAPI must be proven. No data-root/identity surprises.

- [#99 DX-05 / #101 DX-07] Worker device credentials cannot currently **initiate** drain. Deliberate Quit without a human session must prompt for an authorized Operator login (default proposal) or use a separately reviewed self-only device drain. Never retain a hidden global admin bearer.
- [#100 DX-06 / #107 DX-13] Linux/Docker Controller needs non-DPAPI protected TLS trust identity + local CLI fingerprint and must serve the same secure Operator/Worker API to Windows clients. Test interoperability before claiming deployment parity.

## Practical next authorized actions after planning review

1. Coordinator reviews and accepts #93 / DX-01 (#95); approved ADR/review exact SHA is recorded.
2. Authorize **DX-02 #96** scaffold and **DX-03 #97** packaging spike independently. They may run in parallel if resourced.
3. Treat DX-03 clean-Windows feasibility failure as a hard stop. Do **not** enlarge React scope to compensate for an unproven bundled Python/PostgreSQL distribution.
4. Only after #96 + #97 acceptance authorize **DX-04 #98**. Its verified one-installer/Controller-X-to-tray/quit/restart demonstration is M1.
5. Authorize downstream auth/trust/Worker/UX issues according to the dependency graph, not all at once.

## Product/security decisions still awaiting focused sign-off

- DX-05: exact OWNER/ADMIN/OPERATOR/VIEWER action matrix, human session idle-lock behavior, sole-Owner anti-lockout.
- DX-03: exact Python shared runtime, Windows PostgreSQL bundle, pinned versions and redistributable license support.
- DX-06/DX-08: reviewed first-contact root fingerprint display/verification and secure enrollment protocol; no password or pairing code before trust.
- DX-09: migration shape for nullable canonical remote identity; reviewed evidence level for browser session and API/browser identity binding.

## External issues deliberately **not** dependencies for the synthetic/Desktop controlled prototype

- [#3 Meta Threads live API gate](https://github.com/pumni/thread/issues/3)
- [#80 discovery permissions/review](https://github.com/pumni/thread/issues/80)
- [#62 production CRMResultSink](https://github.com/pumni/thread/issues/62)
- [#11 production end-to-end certification](https://github.com/pumni/thread/issues/11)
- [#1 owner legacy security confirmation](https://github.com/pumni/thread/issues/1)

These gates remain essential to **production/release activation** wherever relevant, and must never be inferred as complete from the Desktop demo.

## Planning PR review checklist

- [ ] ADR-0007 product/architecture decisions confirmed.
- [ ] DX-01…DX-14 issue scope/dependencies reviewed.
- [ ] M1-first/clean-machine critical path accepted.
- [ ] RBAC proposed matrix assigned to DX-05 explicit owner decision.
- [ ] TLS and browser identity assumptions assigned explicit security stop gates.
- [ ] Deferred backup/portable restore risk acknowledged.
- [ ] GitHub PR #93 independently ACCEPTED after exact-head document/consistency review; **no auto-merge**.
