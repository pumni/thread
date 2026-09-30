# Work Breakdown v2 — Distributed Hybrid Roadmap

## 1. Current project position

Completed:
- Security implementation PR #13 (owner confirmation remains in issue #1).
- Batch A PR #15.
- TP-004A / partial TP-002 PR #18.
- Batch B PR #20.
- C1 PR #32 — issues #21, #22, #23 accepted and merged.
- C2 PR #33 — issue #24 accepted and merged.
- C3-01 PR #34 — issue #25 accepted and merged.
- C3-02 PR #35 — issue #26 accepted and merged.
- C4 PR #36 — issue #8 accepted and merged.

### Canonical C5-01 state after #27 is closed

PR #38 is accepted and merged. PR #37's C5-01 contract/evidence-gate foundation
is accepted and merged. PR #39's `threads.browser.feed.browse` v1 and PR #40's
`threads.browser.thread.open` v1 checkpoints are accepted and merged. Both are
DONE for their checkpoints and available only through reviewed, bounded,
account-affine WorkerJob paths with explicit worker opt-in. PR #41's
`threads.browser.profile.open` v1 is accepted and merged, DONE for its
checkpoint, and available only through the same bounded account-affine
WorkerJob path with explicit worker opt-in. PR #43's
`threads.browser.media.local_upload` v1 is accepted and merged, DONE for its
checkpoint, and available through the bounded account-affine WorkerJob path
with explicit worker opt-in. It stages only worker-local jpg/jpeg/png/webp
files through an operator-opened composer; it does not publish, submit, or
remove staged content. Issue #27 is **CLOSED / COMPLETED** after all C5-01
acceptance criteria were satisfied. Synthetic fixtures are not production
evidence. C5-02/#28 is CLOSED / COMPLETED after #45, #47, #49 and #51.
LIKE/FOLLOW remain VERIFY. No additional browser mutation or publish/submit
scope is authorized.

Issue #3 remains a production/release gate.

## 2. Delivery rule

Only the batch explicitly authorized by the coordinator may be implemented.

A future roadmap entry is not automatic permission for Codex to start it.

Each batch:
1. starts from current main;
2. uses one batch branch unless coordinator says otherwise;
3. keeps issue-level commits or clearly traceable commit groups;
4. runs quality gates at issue boundaries and final checkpoint;
5. stops on architecture/security/reliability stop conditions;
6. opens one checkpoint PR;
7. does not self-merge.

## 3. Roadmap

| Checkpoint | Issues | Purpose | Gate |
|---|---|---|---|
| C1 | #21, #22, #23 | Worker registry/auth/protocol/WorkerJob reliability | Review before any browser runtime |
| C2 | #24 | Capability Router + per-account execution policy | Review before executor expansion |
| C3 | #25, #26 | Windows Worker Agent + browser adapter foundation | Review before Threads UI capability pack |
| C4 | #8 | API-first Discovery + public-profile enrichment + Leads | Browser enrichment only after C3 |
| C5 | #27, #28 | Approved browser capabilities + AccountActivityPlan | Review before broad activity scheduling |
| C6 | #53, #9, #10 | Scheduler kernel + fleet operations/deployment | Operational readiness review |
| D | #11 | E2E production certification | Requires issue #3 complete |

## 4. C1 — Distributed Worker Foundation

Status: **ACCEPTED / MERGED** in PR #32, merge commit `8cbcd9e6c4fad73579148a826f3754e92e41e4f3`.

### Order

1. #21 — Worker registry, account affinity and persistence.
2. #22 — enrollment/device auth/presence/protocol.
3. #23 — WorkerJob leases/checkpoints/intervention/reconnect.

Recommended branch: batch/c1-distributed-worker-foundation

### C1 must prove

- stable worker identity independent of hostname;
- persistent account -> worker/profile affinity;
- worker protocol compatibility;
- secure enrollment/authentication;
- WSS presence/notification does not own business truth;
- durable HTTPS job mutations;
- wrong worker cannot claim;
- concurrent claim has one winner;
- stale lease cannot checkpoint/finalize;
- disconnect/reconnect and Control Plane restart preserve recoverable state;
- intervention is durable;
- DRAINING/OFFLINE/UPGRADE_REQUIRED workers get no new work.

### Explicit C1 non-goals

- Playwright/Selenium;
- Threads DOM selectors;
- feed browsing;
- local-media UI publish;
- like/follow;
- discovery;
- ActivityPlan;
- scheduler;
- worker auto-update implementation.

## 5. C2 — Capability Router

Issue #24.

Status: **ACCEPTED / MERGED** in PR #33, merge commit `29895a987d12b7325671fb8ca7c30272f865cd73`.

Must separate business capability from executor.

Required concepts:
- capability_name/version;
- API/BROWSER/HUMAN support;
- account mode;
- preferred/fallback executor;
- READ/MUTATION/SESSION/BACKGROUND coordination;
- WAITING_EXECUTION / WAITING_INTERVENTION;
- deterministic/auditable route decision.

No browser business action is implemented here.

## 6. C3 — Windows Browser Worker Foundation

Order:
1. #25 Worker Agent/profile/session/network foundation — **ACCEPTED / MERGED** in PR #34, merge commit `6b9984fecf7167ce02389be9bd935983f8ab7f1d`.
2. #26 browser engine ADR + fail-closed adapter — **ACCEPTED / MERGED** in PR #35, merge commit `0021517b55b1e7bcddc9f3fd9d2099feb3e0b6ab`.

C3 establishes infrastructure only and is complete.

Do not expand C3 into the full Threads UI feature set.

## 7. C4 — Discovery and Leads

Issue #8.

Status: **ACCEPTED / MERGED** in PR #36, merge commit `3a9e77b04ec1d68dcd4a285f9e0e9767cddf67a9`.

Scope:
- keyword/topic;
- public profiles/profile posts;
- mentions;
- conversation enrichment;
- normalized discovered Thread/author entities;
- LeadCandidate;
- dedupe/resume;
- API first.

Browser enrichment waits for C3 and must route through C2.

## 8. C5 — Browser Capabilities and Account Activity

Issues:
- PR #37 C5-01 contract/evidence-gate foundation — accepted and merged;
- #27 `threads.browser.feed.browse` v1 — PR #39 accepted and merged, DONE;
- #27 `threads.browser.thread.open` v1 — PR #40 accepted and merged, DONE,
  available only through the reviewed bounded account-affine WorkerJob path
  with explicit worker opt-in;
- PR #41 `threads.browser.profile.open` v1 is accepted and merged, DONE for its
  checkpoint, READ-only, and available through the bounded account-affine
  WorkerJob path with explicit opt-in;
- PR #43 `threads.browser.media.local_upload` v1 is accepted and merged, DONE
  for its checkpoint, image-only, operator-assisted, and available through the
  bounded account-affine WorkerJob path with explicit opt-in; it stages media
  only and never publishes or submits;
- #27 is CLOSED / COMPLETED;
- #45, #47, #49 and #51 completed C5-02: durable plans/occurrences, deterministic
  Command materialization and priority, safe-boundary cancellation, and trusted
  HIGH preemption with browser-profile gating. Parent #28 is CLOSED / COMPLETED.
- #53 authorizes C6-01/1: a bounded PostgreSQL scheduler kernel for already
  persisted due occurrences, ready Commands through `CommandRuntime`, and
  bounded existing WorkerJob recovery.
- #56 authorizes C6-01/2: deterministic fixed-interval occurrence generation
  from immutable AccountActivityTemplate revisions and PostgreSQL cursors.
- PostgreSQL due state is authoritative; the scheduler loop is wakeup-only.
  Generation is bounded separately from materialization, Command draining and
  WorkerJob recovery.

Browser capabilities must be explicit and independently reviewable.

No random warm-account loop.

LIKE/FOLLOW remain VERIFY unless explicitly retained at implementation review.

## 9. C6 — Scheduler and Operations

Issues:
- #53 C6-01/1 PostgreSQL scheduler kernel for existing due work;
- #56 C6-01/2 deterministic AccountActivityPlan recurrence generation;
- #58 C6-01/3 durable periodic `threads.sync_conversation` scheduling;
- #60 C6-01/4 scheduler-owned bounded Worker presence expiry;
- #9 later durable scheduler/fleet orchestration checkpoints;
- #10 observability/security/Windows packaging/deployment.

C6 turns explicit jobs/plans into durable operations and establishes deploy/update/recovery procedures.
The #53 scheduler owns due materialization, ready Command draining, and
WorkerJob recovery; #60 adds bounded Worker presence expiry; #63 adds bounded
outbox delivery as the final tick stage. #56 adds
deterministic recurrence for already-defined
`AccountActivityTemplate` revisions. PostgreSQL due state and activity-specific
recurrence cursors are authoritative; the loop only wakes bounded ticks and
re-discovers state after restart. Fixed intervals use UTC anchors and integer
seconds, with no cron or timezone grammar. `ACTIVE` and `PAUSED` generate
occurrences; paused rows remain pending for resume. `DISABLED` generates
nothing. Only the latest template revision generates, while old occurrence
snapshots remain immutable. Presence expiry, generation, materialization,
Command, recovery and outbox delivery limits are independently bounded to 1–100
and support concurrent scheduler
instances through PostgreSQL locking. Presence expiry uses authoritative
`worker_nodes.presence_expires_at`, ordered bounded PostgreSQL row locking, and
changes only new-work eligibility; FastAPI owns neither presence expiry nor
WorkerJob recovery in its lifespan. FastAPI and scheduler share one production
Threads API provider composition. C6-02/1a (#70) uses the existing non-secret
`oauth_credentials` metadata and strict, versioned environment references. It
does not store token values in PostgreSQL or refresh them at runtime. Runtime
use remains opt-in and #3 stays the production/release gate.
Issue #58 adds a narrow conversation-sync schedule and immutable dispatch
audit, not a generic scheduled-command framework. ACTIVE schedules dispatch
one deterministic Command for the oldest due slot and coalesce overdue slots;
PAUSED schedules retain the due time, and DISABLED schedules are terminal.
The dispatch audit, Command, and schedule cursor update share one transaction.
Each scheduler process has an independent 1–100 conversation-sync batch limit.
The Command continues through CommandRuntime and the Capability Router, where
the existing handler uses SyncState for remote cursor catch-up.

Outbox delivery uses a separate 1–100 per-tick limit and PostgreSQL
IntegrationDelivery claims/leases. A delivery is attempted at most once per
local tick; remote network delivery remains at-least-once. Retry, deadline,
lease-reclaim and stale-token fencing semantics remain unchanged. FastAPI owns
no delivery loop. The standalone scheduler reports `CRM_RESULT_SINK_UNAVAILABLE`
and skips only this stage until the separately authorized production transport
dependency #62 is resolved. No migration or generic event bus is added.

C6-02/3 (#72) adds scoped correlation to bounded Command, WorkerJob, and
scheduler lifecycle logs; recursive redaction before JSON rendering; and
aggregate PostgreSQL/Worker readiness at `/ready`. `/health` remains process
liveness. Database failure returns HTTP 503, while fleet degradation remains
diagnostic HTTP 200. Readiness does not own presence expiry. This checkpoint
does not add metrics or tracing. Issue #3 remains the production/release gate,
and #62 remains a separate CRM transport dependency. See
`docs/OBSERVABILITY_RUNBOOK.md`.

## 10. D — Release

Issue #11.

Issue #3 must be complete before production/release certification.

## 11. Open gates outside normal engineering sequence

### #1 security owner follow-up

Does not block engineering.
Must be resolved/accepted before final release security sign-off.

### #3 live Meta validation

Does not block safe documentation-contract implementation.
Does block production activation.

Issue #65 prepared the offline packet/runbook/validator. Accepted Phase A live
evidence is recorded in
`docs/evidence/threads-live-phase-a-2026-09-30.json`; #55 and #70 are closed.
Phase B is `NOT_RUN`, so discovery/mentions scheduling remains blocked. Keep #3
OPEN as the production/release gate, and keep #62 as a separate CRM transport
dependency. No production release claim follows from #70.

## 12. Stop conditions

Codex stops and reports instead of improvising when:
- an ADR must be contradicted;
- Command and WorkerJob would need to be collapsed;
- Redis/broker/new distributed system is required;
- plaintext account/worker secrets appear necessary;
- automatic profile migration appears necessary;
- browser automation is needed before C3;
- anti-detect/fingerprint-evasion behavior appears in scope;
- a side effect cannot be reconciled safely;
- quality gates would need weakening;
- destructive migration assumptions are required;
- current official API behavior materially contradicts the documented capability contract.

## 13. Parallelism

C1 is intentionally sequential because each issue establishes a dependency for the next.

After C2:
- API-only portions of C4 may proceed independently from browser infrastructure;
- browser enrichment waits for C3;
- C5 waits for C3;
- C6 scheduling may be designed alongside late C5 only when shared contracts are stable.

Coordinator controls parallel authorization.

## 14. Checkpoint evidence

Every checkpoint PR must report:
- branch/HEAD;
- issue -> commit map;
- migrations;
- data/state-machine changes;
- protocol changes;
- exact quality-gate results;
- CI/secret-scan status;
- concurrency/restart/recovery tests;
- security-sensitive design;
- known limitations;
- deferred gates;
- ADR/docs changed.

## 15. TP-002 evidence harness (#65)

The #65 checkpoint added an offline strict evidence packet validator,
recursive secret checks, stdin-only opaque fingerprinting, a template-only
matrix, and a human runbook. It did not call Meta or fabricate live
observations. The #68 Phase A packet is accepted; #55 and #70 are closed. Phase
B is `NOT_RUN`, discovery and mentions scheduling remain gated, and #3 remains
open as the production/release gate. Issue #72 is authorized for observability
and readiness only; metrics/tracing and #62 remain out of scope.

## C6-02/5 — Durable Worker draining (#78)

Issue #78 adds row-locked admin drain/status/abort operations and the
authenticated worker quiescence completion handshake. DRAINING blocks new
claims while preserving already-running WorkerJob leases and their normal
terminal operations. Readiness does not own presence expiry. The WorkerAgent
closes managed sessions and resumes finalization after reconnect; no process
kill, task cancellation, updater, migration, or package installer is included.
The service-manager-neutral update and rollback procedure is in
`docs/WORKER_UPDATE_RUNBOOK.md`. Issue #3 remains OPEN, #62 remains separate,
and no production release claim follows from this checkpoint.

## C6-02/6 — Reproducible Windows Worker package (#81)

The Windows x64 Worker has a `threads-worker` console entry point and a pinned
PyInstaller onedir build. The locked Playwright 1.63.0 runtime bundles only its
matching Chromium. `--version` is metadata-only; `--package-check` validates
temporary identity/DPAPI/journal state and bundled Chromium on `about:blank`
without Control Plane or external navigation. An exact-head Windows workflow
creates a safe manifest, normalized ZIP, SHA-256 digest, and unsigned
internal/test artifact. Worker identity, private key, profiles, journal and
media remain in the external data root. There is no service installation,
signing, downloader, or self-update. #78 DRAINING semantics are unchanged; #3
remains OPEN and #62 remains separate.
