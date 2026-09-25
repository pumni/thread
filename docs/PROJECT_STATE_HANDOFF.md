# Project State Handoff — 2026-09-25

After the repository root `AGENTS.md`, this is the first state document a new coordinator or Codex session should read.

## 1. Repository

Repository: pumni/thread

Project: Distributed Hybrid Threads Operations Tool.

The project is greenfield. The deleted legacy Facebook report is not an architectural blueprint.

## 2. Current milestone

Engineering completed:
- Batch A — Foundation;
- TP-004A — durable external-side-effect execution hardening;
- Batch B — Threads API publishing/conversation core;
- C1 — Distributed Worker Foundation;
- C2 — Capability Router and per-account execution policy;
- C3 — Windows Worker Agent + fail-closed browser adapter foundation;
- C4 — API-first Discovery, public-profile enrichment and Leads pipeline.

The next authorized implementation milestone is **C5-01 — Browser capability pack v1 (#27)**.

Do not start C5-02 AccountActivityPlan/preemption, C6 scheduling/operations, or production release work until the coordinator explicitly authorizes the corresponding checkpoint.

## 3. Important merged checkpoints

Planning/architecture baseline:
- PR #12 — initial architecture blueprint.

Security:
- PR #13 — secret scanning/remediation implementation.

Delivery process:
- PR #14 — batched checkpoints.

Batch A:
- PR #15 — Python/DB/command foundation.

TP-004A + partial TP-002:
- PR #18 — durable command leases/checkpoints.

Coordination:
- PR #19 — live Meta verification moved to production/release gate.

Batch B:
- PR #20 — Threads publishing + conversation sync.

Distributed-hybrid rebaseline:
- PR #29 — v2 architecture, Worker Protocol v1, C1-C6 roadmap and fresh-session handoffs.
- Merge commit: cb675e5109fb04d91ceaf7b81f83f34d5673a849.

C1 — Distributed Worker Foundation:
- PR #32 — Worker registry/affinity, enrollment/device auth/presence/protocol v1, durable WorkerJob leases/fencing/intervention/recovery.
- Accepted head: ff2f0d731a254f25a4058dba4281f9f589cfca4c.
- Merge commit: 8cbcd9e6c4fad73579148a826f3754e92e41e4f3.
- Issues #21, #22 and #23 closed completed after coordinator acceptance.

C2 — Capability Router:
- PR #33 — deterministic capability policy/router, durable route-decision history, API/WorkerJob executor integration and PostgreSQL account-execution fencing.
- Accepted head: c98b702099b8a2948dc4197de68af720a953a8c2.
- Merge commit: 29895a987d12b7325671fb8ca7c30272f865cd73.
- Issue #24 closed completed after coordinator acceptance.

C3-01 — Windows Worker Agent foundation:
- PR #34 — Windows process/runtime, DPAPI device identity, managed local data root, logical profile ownership, session/capacity recovery, durable session reporting and protocol v2 capacity extension.
- Accepted head: 644aedc05b3305bf2e2b05373c863498db5c7873.
- Merge commit: 6b9984fecf7167ce02389be9bd935983f8ab7f1d.
- Issue #25 closed completed after coordinator acceptance.

C3-02 — Browser adapter foundation:
- PR #35 — ADR-0006 selecting Playwright/Chromium, isolated browser infrastructure, versioned synthetic UI contract, typed browser failures, WorkerJob-fenced mutation boundary and restart reconciliation.
- Accepted head: 1fb1055d4f1680b8d7ac4bbb073e2863b6bde348.
- Merge commit: 0021517b55b1e7bcddc9f3fd9d2099feb3e0b6ab.
- Issue #26 closed completed after coordinator acceptance.

C4 — API-first Discovery and Leads:
- PR #36 — typed discovery commands, keyword/tag search, public profile/profile-post/mentions/conversation enrichment, durable cursor resume, canonical dedupe/provenance and audited LeadCandidate lifecycle.
- Accepted head: 9119bbcd754abd969c99ff7388b7ab221857dca1.
- Merge commit: 3a9e77b04ec1d68dcd4a285f9e0e9767cddf67a9.
- Issue #8 closed completed after coordinator acceptance.
- Issue #3 remains open for live Meta verification; C4 repository fixtures are documentation-contract evidence only.

## 4. Current open gates

### Issue #1 — historical credential status

Engineering remediation is merged.

Owner still needs to confirm, without sharing the value, whether the old credential-shaped value was:
- non-secret test data; or
- if real, rotated/revoked.

This is not an engineering blocker.

### Issue #3 — live Meta Threads API validation

This remains OPEN and is a production/release gate.

Still requires a dedicated development app/account to verify:
- effective OAuth scopes;
- token exchange/refresh lifecycle;
- real quota behavior;
- representative error bodies/status/headers;
- media processing;
- reply/conversation behavior;
- moderation permissions;
- reconciliation options for ambiguous published containers;
- whether pagination cursors are suitable for durable polling semantics.

No production activation should occur while #3 is incomplete.

## 5. Product model locked by owner

- persistent account -> worker/profile affinity;
- per-account mode: API_ONLY / BROWSER_ONLY / HYBRID / MANUAL;
- no automatic profile migration;
- if worker offline: API fallback only when policy allows, otherwise wait;
- background/account-activity jobs are centrally scheduled;
- one Worker Agent manages multiple profiles with configurable capacity;
- initial browser login is manual/operator-assisted;
- session challenges require intervention;
- NetworkProfile/proxy is account-scoped;
- browser UI mismatch fails closed;
- UI may call it “account nurturing”; domain models explicit AccountActivityPlan/jobs;
- discovery scope includes posts + profiles + leads + conversations;
- CRM adopts the new protocol; no legacy-protocol compatibility in the core;
- worker is strict-online;
- browser worker is Windows-first;
- Control Plane remains Linux/Docker capable.

## 6. Architecture decisions

Read:
- docs/MASTER_PLAN.md
- docs/ARCHITECTURE.md
- docs/FEATURE_PARITY_MATRIX.md
- docs/WORK_BREAKDOWN.md
- docs/protocols/WORKER_PROTOCOL_V1.md
- docs/adr/0003-distributed-hybrid-execution.md
- docs/adr/0004-persistent-worker-affinity-and-worker-jobs.md
- docs/adr/0005-browser-capability-boundary.md

Critical invariants:
- Command != WorkerJob;
- WorkerJob has its own lease/fencing token;
- PostgreSQL is authoritative;
- WebSocket is notification/presence only;
- durable worker mutations use authenticated HTTPS;
- stale worker cannot checkpoint/finalize;
- browser account jobs respect persistent affinity;
- workers do not invent business actions;
- browser automation does not enter domain;
- no anti-detect/fingerprint-evasion objective.

## 7. Current code quality baseline

Expected commands:

~~~bash
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run alembic check
uv run pytest
~~~

At the C4 acceptance checkpoint, Python 3.14 quality gate and Secret scan were green on accepted head `9119bbcd754abd969c99ff7388b7ab221857dca1`.

Final reported C4 evidence: 142 local tests passed, C4 PostgreSQL/migration suite 10 passed, migration 0009 downgrade/re-upgrade passed, crash rollback/replay and monotonic canonical enrichment regressions passed.

## 8. Threads API baseline

Official Meta Threads workspace checked 2026-09-25 currently shows:
- OAuth/token exchange/refresh;
- publishing;
- replies/conversation;
- reply management;
- profile/public-profile retrieval;
- public profile posts;
- keyword/tag search;
- mentions;
- insights;
- publishing quota.

Treat repository fixtures as documentation-contract fixtures unless explicitly marked as scrubbed live evidence.

## 9. Next action for a new coordinator

1. Read root `AGENTS.md` and this handoff.
2. Confirm `main` includes C4 merge commit `3a9e77b04ec1d68dcd4a285f9e0e9767cddf67a9`.
3. Inspect issue #27, ADR-0005, ADR-0006, C2 capability routing, C1 WorkerJob lease/recovery, and C3 browser/session abstractions.
4. Authorize only C5-01: explicit browser capability contracts and the first reviewed capability pack.
5. Keep LIKE/FOLLOW in VERIFY unless a separate product decision explicitly retains them.
6. Stop after #27 for coordinator review before #28 AccountActivityPlan/preemption.

Do not guess production Threads selectors from synthetic fixtures. Any production UI contract must be based on reviewed observed UI evidence and must fail closed when the contract does not match.

## 10. Coordinator acceptance vocabulary

- ACCEPTED
- ACCEPTED WITH FOLLOW-UP
- CHANGES REQUIRED
- BLOCKED BY PRODUCT/API DECISION

CI green is necessary but not sufficient.
