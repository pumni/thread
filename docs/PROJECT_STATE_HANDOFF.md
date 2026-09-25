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
- C3-01 — Windows Worker Agent, profile/session and NetworkProfile foundation.

The next authorized implementation milestone is **C3-02 — Browser engine ADR and fail-closed browser adapter foundation (#26)**.

Do not start the Threads UI capability pack, C4 browser enrichment, C5 or C6 until the coordinator explicitly authorizes the corresponding checkpoint.

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

At the C3-01 acceptance checkpoint, Python 3.14 quality gate and Secret scan were green on accepted head `644aedc05b3305bf2e2b05373c863498db5c7873`.

Final reported C3-01 local gate: Ruff/format/Pyright/Alembic green, migration downgrade/re-upgrade through revision 0008 green, 108 tests passed, and the native Windows DPAPI persistence/reload test passed locally.

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
2. Confirm `main` includes C3-01 merge commit `6b9984fecf7167ce02389be9bd935983f8ab7f1d`.
3. Inspect issue #26, ADR-0005, Worker Protocol v1/v2, the C3-01 worker/session contracts, and the current WorkerJob lease/recovery code.
4. Authorize only C3-02: browser-engine ADR plus isolated fail-closed browser lifecycle/navigation/contract adapter.
5. Reuse C3-01 profile/session/network foundations and C1 WorkerJob fencing rather than creating a parallel execution model.
6. Stop after #26 for coordinator review before any production Threads UI capability pack.

Do not implement production Threads selectors/actions, like/follow, publishing through the UI, anti-detect/fingerprint behavior, or automated challenge bypass in C3-02.

## 10. Coordinator acceptance vocabulary

- ACCEPTED
- ACCEPTED WITH FOLLOW-UP
- CHANGES REQUIRED
- BLOCKED BY PRODUCT/API DECISION

CI green is necessary but not sufficient.
