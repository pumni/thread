# Project State Handoff — 2026-09-29

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

### Canonical C5-01 state after #27 is closed

PR #38 is accepted and merged. PR #37's C5-01 contract/evidence-gate foundation
is accepted and merged. PR #39's `threads.browser.feed.browse` v1 and PR #40's
`threads.browser.thread.open` v1 checkpoints are accepted and merged. Both are
DONE for their checkpoints and available only through the reviewed, bounded,
account-affine WorkerJob path, with explicit worker opt-in required. PR #41's
`threads.browser.profile.open` v1 is accepted and merged, DONE for its
checkpoint, and available only through the same bounded account-affine path
with explicit worker opt-in. All three read capabilities are DONE for their
checkpoints. PR #43's `threads.browser.media.local_upload` v1 is accepted and
merged at `59f3d2cf5589b4bdb052b404d58babb0dda7a3e4`, DONE for its checkpoint,
and available only through the reviewed, bounded, account-affine WorkerJob
path with explicit worker opt-in. Issue #27 is CLOSED / COMPLETED; all four
C5-01 capability acceptance criteria are satisfied. Issue #45 established the
C5-02/1 activity foundation, and issue #47 authorizes C5-02/2 materialization
and durable priority propagation only. Parent #28 remains open.

The coordinator accepted production UI evidence for
`threads.browser.thread.open` on 2026-09-28, and PR #40 accepted and merged its
v1 implementation from `main@dafd04a40aa8dcc3456ce7e0e49d341e69047435`. Its target is one
bounded relative `/@<username>/post/<id>` path. After normalizing one trailing
slash, the browser pathname must equal that path exactly. Recognition requires
the exact target permalink href, an exact matching `/@<username>` author href,
and non-empty text from `span[dir="auto"]` within the same ancestor association
at a bound of eight. Duplicate exact target anchors are valid only when every
anchor resolves to that same root association. Competing roots, reply
cross-association, ambiguous evidence, and evidence beyond the bound fail
closed. Redirects, off-origin navigation, pathname mismatch, or a loaded target
without the reviewed exact anchor create durable `REMOTE_STATE_UNCERTAIN`
intervention. No login or challenge selectors are inferred.

Thread open is DONE for this checkpoint and uses the Capability Router and
account-affine WorkerJob path, with explicit worker opt-in. The implementation
does not use canonical metadata, `main`, `article`, generated CSS classes, or a
localized Back label. Its result contains only the versioned recognized target
reference. Feed browse remains DONE and available through the same bounded
WorkerJob path with worker opt-in.

The accepted profile.open evidence consists of two authenticated public
profile observations dated 2026-09-28. Recognition requires exact normalized
`/@<username>` pathname equality, exactly one non-empty `<h1>`, and the nearest
bounded `<div>` ancestor of that heading containing at least one exact
target-profile href and no semantic post permalink. Duplicate exact profile
hrefs inside that association are allowed. No exact target href anywhere after
the target loads uses durable `REMOTE_STATE_UNCERTAIN`; exact hrefs only outside
the `<div>` association, missing/duplicate `<h1>`, post contamination,
ambiguity, and over-bound association fail closed. The implementation does not
use canonical metadata, `main`, `article`, generated CSS classes, localized
labels, or generic profile links outside that association.

`threads.browser.media.local_upload` v1 is an operator-assisted image staging
mutation for jpg/jpeg/png/webp files. The operator must open the composer; the
Worker does not click Create. Before selection, the Worker proves exactly one
dialog, one textbox, and one file input associated with that dialog. It arms
the network observer before selecting the worker-local file and reports
success only after the correlated approved-origin upload POST returns HTTP 200
and a blob preview remains in the same composer. Uncertain outcomes after
selection require `AMBIGUOUS_OUTCOME`; the file is never selected again.
WorkerJobs are non-preemptible and use `RECONCILIATION_REQUIRED`. Video fails
closed. The capability does not publish or submit, and Remove is not a
rollback. Worker opt-in defaults off. Synthetic fixtures test the reviewed
contracts but are not production evidence. Issue #45's C5-02/1 durable
foundation is accepted; issue #47 authorizes existing due occurrence
materialization into Commands and durable priority propagation. It does not
authorize recurrence generation, a background runner, cancellation, or
preemption. LIKE/FOLLOW remain VERIFY. No browser mutation is authorized beyond
this bounded staging capability, and publish/submit remains outside scope.

Issue #45 authorized C5-02 Checkpoint 1 for the durable AccountActivityPlan,
versioned template, ScheduledActivity occurrence, and priority foundation.
Issue #47 separately authorizes C5-02 Checkpoint 2 for due occurrence to
Command materialization and Command-to-WorkerJob priority propagation. It
excludes recurrence generation, a background runner, cancellation, and
preemption. Later C5-02 execution/preemption, C6 scheduling/operations, and
production release work still require their own authorization.

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
- fresh Worker presence gates new claims; it does not revoke or reclaim a running
  WorkerJob, whose PostgreSQL lease/fencing token remains authoritative;
- PostgreSQL is authoritative;
- WebSocket is notification/presence only;
- durable worker mutations use authenticated HTTPS;
- only a current, unexpired WorkerJob lease with matching worker and fencing token
  can checkpoint/finalize; stale presence alone does not revoke that lease, while
  expired, mismatched, or reclaimed tokens cannot mutate the job;
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

Final reported C4 evidence: 142 local tests passed, C4 PostgreSQL/migration suite 10 passed, migration 0009 downgrade/re-upgrade passed, crash rollback/replay and monotonic canonical enrichment regressions passed. The earlier migration check did not seed persisted C4 data before downgrading.

Migration `20260926_0009` has a destructive downgrade to `20260925_0008`: it drops
all C4 discovery, provenance, cursor, and lead tables and their rows. It also
deletes replies rooted in discovered Threads (clearing parent links among those
rows first), while retaining replies rooted in published posts. Re-upgrading
recreates empty C4 tables and cannot restore the deleted data. The seeded
downgrade regression in `tests/integration/test_c4_migration.py` covers this
existing migration behavior. This documents migration 0009 specifically; it does
not change the global stop condition for destructive migration assumptions.

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
4. Keep #27 CLOSED / COMPLETED. Feed browse, thread open, profile.open, and
   image-only media.local_upload are DONE for their checkpoints. Each available
   capability uses the bounded account-affine WorkerJob path with explicit
   worker opt-in.
5. Keep LIKE/FOLLOW in VERIFY unless a separate product decision explicitly retains them.
6. Keep issue #47 limited to C5-02/2: materialize existing due occurrences and
   propagate trusted priority through the existing Command Runtime. Recurrence,
   background runners, cancellation, and preemption still need separate
   authorization.

Do not guess production Threads selectors from synthetic fixtures. Any production UI contract must be based on reviewed observed UI evidence and must fail closed when the contract does not match.

## 10. Coordinator acceptance vocabulary

- ACCEPTED
- ACCEPTED WITH FOLLOW-UP
- CHANGES REQUIRED
- BLOCKED BY PRODUCT/API DECISION

CI green is necessary but not sufficient.

## 11. C5-01 post-merge status

PR #38 is merged at `f7cacfa9674d83592d68f0501da096e55250dcde`, PR #37's
C5-01 contract/evidence-gate foundation is accepted, PR #39's
`threads.browser.feed.browse` v1 and PR #40's `threads.browser.thread.open` v1
are accepted and merged. PR #41's `threads.browser.profile.open` v1 and
PR #43's `threads.browser.media.local_upload` v1 are also accepted and merged
at `59f3d2cf5589b4bdb052b404d58babb0dda7a3e4`.
All three read capabilities and the image-only media staging capability are
DONE for their checkpoints and available only through reviewed bounded, account-affine
WorkerJob paths with explicit worker opt-in. Profile recognition uses exact
normalized pathname equality, one non-empty `<h1>`, and the nearest bounded
`<div>` ancestor containing exact target-profile href(s) and no post permalink;
duplicate exact profile hrefs inside that association are valid. No exact
target href anywhere after load uses `REMOTE_STATE_UNCERTAIN`; exact hrefs only
outside the `<div>` association, malformed, contaminated, ambiguous, or
over-bound associations fail closed. Media local upload accepts only
worker-local image refs for jpg/jpeg/png/webp, requires an already-open
operator composer and the correlated successful upload response plus preview,
and reports uncertain post-selection outcomes as `AMBIGUOUS_OUTCOME`. It is
non-preemptible, uses reconciliation-required retry safety, and never publishes,
submits, or removes the staged image. Worker opt-in defaults off. Issue #27 is
CLOSED / COMPLETED after all C5-01 acceptance criteria were satisfied.
Synthetic fixtures are not production evidence. Issue #45 established the
C5-02/1 durable foundation; issue #47 authorizes only C5-02/2 materialization
and durable priority propagation. LIKE/FOLLOW remain VERIFY. No additional
mutation or publish/submit scope is authorized.
