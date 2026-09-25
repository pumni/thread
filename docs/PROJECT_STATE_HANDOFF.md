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
- C3 — Windows Worker Agent + fail-closed browser adapter foundation.

The next authorized implementation milestone is **C4 — Discovery, public-profile enrichment and Leads pipeline (#8)**.

C4 is API-first. Do not start C5 browser capability pack, AccountActivityPlan or C6 scheduling/operations until the coordinator explicitly authorizes the corresponding checkpoint.

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

At the C3-02 acceptance checkpoint, Python 3.14 quality gate and Secret scan were green on accepted head `1fb1055d4f1680b8d7ac4bbb073e2863b6bde348`.

Final reported C3-02 evidence: locked Playwright/Chromium install green in CI, 121 tests passed with 1 skipped and one upstream Starlette/httpx deprecation warning, targeted browser-adapter suite 14 passed, and no schema migration was required.

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
2. Confirm `main` includes C3-02 merge commit `0021517b55b1e7bcddc9f3fd9d2099feb3e0b6ab`.
3. Inspect issue #8, `docs/THREADS_API_CAPABILITY_SPIKE.md`, `docs/FEATURE_PARITY_MATRIX.md`, existing Threads API ports/adapters, conversation sync persistence and C2 capability routing.
4. Authorize C4 as API-first discovery: keyword/tag search, public profile/profile posts, mentions, conversation enrichment, dedupe/resume and LeadCandidate persistence.
5. Keep documentation-contract fixtures clearly labeled; issue #3 remains the required live Meta validation gate before production.
6. Stop after #8 for coordinator review before C5 browser capability work.

Do not add production browser enrichment actions in C4 unless separately approved through the capability model; do not implement uncontrolled scraping, DM automation or unrestricted personal-data collection.

## 10. Coordinator acceptance vocabulary

- ACCEPTED
- ACCEPTED WITH FOLLOW-UP
- CHANGES REQUIRED
- BLOCKED BY PRODUCT/API DECISION

CI green is necessary but not sufficient.
