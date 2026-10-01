# Project State Handoff — 2026-09-30

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
C5-01 capability acceptance criteria are satisfied. C5-02/#28 is CLOSED /
COMPLETED after #45, #47, #49 and #51. Issue #53 authorizes C6-01/1, the bounded
PostgreSQL scheduler kernel for existing due occurrences. C6-01/2 (#56) adds
deterministic AccountActivity recurrence, C6-01/3 (#58) adds durable
conversation sync scheduling, and C6-01/4 (#60) moves bounded Worker presence
expiry into the explicit scheduler. Parent #9 remains open.

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
contracts but are not production evidence. C5-02/#28 completed the durable
activity foundation, occurrence materialization, trusted priority propagation,
safe-boundary cancellation and HIGH preemption/profile gate. C6-01/1 (#53)
added the bounded scheduler process for due materialization, ready Command
draining and WorkerJob recovery. C6-01/4 (#60) moves bounded Worker presence
expiry to that scheduler. PostgreSQL `worker_nodes.presence_expires_at` remains
authoritative and polling is wakeup latency only. FastAPI performs neither
presence expiry nor WorkerJob recovery in its lifespan. The app and scheduler
share the production Threads API provider composition. C6-02/1a (#70) adds the
opt-in environment-backed provider on the existing metadata-only
`oauth_credentials` table; token values are not stored in PostgreSQL, and
runtime refresh is disabled. Operator-managed versioned rotation is documented
in `docs/THREADS_CREDENTIAL_OPERATIONS.md`. Phase A is accepted; Phase B is
`NOT_RUN`, #3 remains OPEN, and no production release readiness is claimed.
Parent #9 remains open.

C6-01/2 (#56) implements only `NONE` and deterministic `FIXED_INTERVAL`
recurrence on immutable `AccountActivityTemplate` revisions. PostgreSQL
cursor rows and occurrence rows are authoritative; the scheduler loop remains
wakeup-only. Fixed slots use UTC anchor plus integer interval arithmetic
(900–2,592,000 seconds), with no cron, jitter, or timezone grammar. ACTIVE and
PAUSED plans generate durable occurrences; PAUSED rows remain PENDING for
resume. DISABLED plans stop generation. A newer template revision supersedes
only ungenerated older slots; existing occurrences remain immutable.

C6-01/3 (#58) adds durable fixed-interval schedules for the existing
`threads.sync_conversation` READ Command only. Each schedule has one UTC anchor,
interval, status/control revision, and due cursor; each dispatch has an
immutable audit row linked to its deterministic Command. One coalesced Command
represents the oldest due slot after an outage, and the existing SyncState
cursor performs remote catch-up. PAUSED schedules retain their due time;
DISABLED is terminal. A non-terminal prior Command prevents overlap. Command,
dispatch audit, and cursor advancement commit together. Scheduler order is
generation, conversation dispatch, materialization, Command draining, and
WorkerJob recovery, each with an independent bounded limit. Discovery and
mentions recurrence remain deferred pending #3 time-window/cursor semantics.
Production LOCAL_API execution uses the shared opt-in provider from #70 when
environment mode is enabled; #3 remains a production/release gate. This
checkpoint does not close parent #9.

C6-01/5 (#63) adds bounded, sequential outbox delivery as the final scheduler
tick stage. PostgreSQL IntegrationDelivery due/retry state and fenced leases
remain authoritative, with an independent 1–100 delivery limit. Each delivery
ID is attempted at most once per local tick. Delivery is at-least-once at the
network boundary; lease reclaim can repeat a remote call whose local DELIVERED
commit was interrupted. FastAPI does not pump outbox deliveries. The standalone
scheduler reports `CRM_RESULT_SINK_UNAVAILABLE` and skips only this stage
until the production transport dependency #62 is resolved. No migration is
required.
LIKE/FOLLOW remain VERIFY. No browser mutation is authorized beyond this
bounded staging capability, and publish/submit remains outside scope.

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

Accepted Phase A evidence is recorded in
`docs/evidence/threads-live-phase-a-2026-09-30.json`. It verifies code and
long-lived token exchange, refresh with reset expiry, debugger validity and
effective scopes, and own-profile behavior. Phase B is `NOT_RUN`.

Remaining live verification includes:
- real quota behavior;
- representative error bodies/status/headers;
- text/image/video publishing and media retrieval;
- media processing;
- reply/conversation behavior;
- moderation permissions;
- public discovery/profile/mentions behavior and required scopes;
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
6. Track the exact #53 PR head and its PostgreSQL concurrency, restart,
   worker-offline and recovery evidence. Do not close parent #9; later C6
   checkpoints still require separate authorization.

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
Synthetic fixtures are not production evidence. C5-02/#28 is CLOSED /
COMPLETED after #45, #47, #49 and #51. C6-01/1/#53 added the bounded
scheduler kernel for durable due work; C6-01/2/#56 added deterministic
AccountActivityPlan recurrence; C6-01/3/#58 adds durable periodic conversation
sync scheduling. C6-01/4 (#60) adds bounded, PostgreSQL-locked Worker presence
expiry as the first scheduler tick stage. Presence controls new-work eligibility
only and does not affect running WorkerJob leases, attempts, or preemption.
The explicit scheduler process owns both presence expiry and bounded WorkerJob
recovery; FastAPI lifespan performs neither. Polling is wakeup latency only.
The standalone scheduler uses the same opt-in provider composition as FastAPI.
LIKE/FOLLOW remain VERIFY. No additional mutation or publish/submit scope is
authorized.

## 12. TP-002 evidence harness checkpoint #65

Issue #65 prepared only offline evidence tooling: a strict scrubbed
`threads-live-evidence-v1` packet, a template classified
`TEMPLATE_ONLY_NOT_LIVE_EVIDENCE`, recursive secret validation, opaque-value
fingerprinting, and a human runbook. No live API validation happened in the
#65 PR and the harness makes no network calls to Meta.

The #68 Phase A packet is accepted and authorized #70. Phase B is `NOT_RUN`,
so discovery/mentions scheduler policy under #9 remains blocked. Issue #3 stays
OPEN as a production/release gate. The separate #62 CRM transport dependency
also remains open.

## 13. Phase A scrubbed live evidence checkpoint #68

Issue #68's accepted scrubbed Phase A evidence is recorded in
`docs/evidence/threads-live-phase-a-2026-09-30.json`. It establishes
authorization/code exchange, long-lived exchange, refresh with reset expiry,
debugger validity/effective scopes (including `threads_read_replies`), and own
profile behavior. #70 implements the metadata-only, environment-backed
provider with operator-managed versioned rotation and no automatic refresh.
No plaintext token is stored in PostgreSQL. Phase B is `NOT_RUN`; #3 remains
OPEN and no production release readiness is claimed. Parent #55 and #70 are
closed; #62 remains a separate CRM transport dependency.

## 14. C6-02/3 observability and readiness (#72)

Issue #72 is authorized from `main@3e642bb84363d73094cdcde3cd26297b165b6da3`.
It adds bounded Command/WorkerJob/scheduler lifecycle logs, scoped correlation
context, recursive structured-log redaction, and `/ready`. `/health` remains
process liveness and does not require PostgreSQL. `/ready` reports only
aggregate persisted Worker states: DB failure is `NOT_READY` / `DOWN` / HTTP
503, while fleet degradation is informational `DEGRADED` / HTTP 200. Readiness
does not expire Worker presence or mutate PostgreSQL. No migration, metrics,
distributed tracing, deployment work, DRAINING workflow, or #62 transport work
is authorized in this checkpoint. #3 remains OPEN and no production release
readiness is claimed. See `docs/OBSERVABILITY_RUNBOOK.md` for the log fields,
redaction limitations, and endpoint contract.

## 15. C6-02/4 sensitive representations and worker response cache controls (#75)

Issue #75 adds defense-in-depth `repr()` hiding for Command and WorkerJob
documents/leases and worker authentication material. Pydantic representations
hide credential fields while existing JSON/wire fields remain unchanged.
Enrollment, challenge, session-token, and assigned account-context responses
carry `Cache-Control: no-store`. PostgreSQL continues to store digests and
credential references, not raw enrollment codes or session access tokens. No
migration or auth redesign was introduced.

This does not change session revocation behavior and does not implement
metrics/tracing. Issue #3 remains OPEN as the production/release gate; #62
remains separate. See `docs/OBSERVABILITY_RUNBOOK.md` for the precise repr and
cache-control contract.

## 16. TP-002 Phase B partial evidence checkpoint #74

Issue #74 records reviewed scrubbed partial Phase B live evidence
(`docs/evidence/threads-live-phase-b-partial-2026-09-30.json`). Phase A evidence
remains accepted with `token_provider_ready_evidence=true`. Phase B session
completed with status `PARTIAL_LIVE_EVIDENCE / NOT_READY`: canonical discovery
endpoints (`/keyword_search`, `/profile_lookup`, `/profile_posts`,
`/me/mentions`) failed with HTTP 500 (`Proxy-Status: http_request_error`) or
remained blocked on the dedicated test app; canonical B05/B06 (`replies`,
`conversation`) were verified (HTTP 200); collection pagination semantics
(`after` parameter, `paging.cursors.after`, terminal page, repeated/cross-run
stability, and `owner.id`) were corroborated on `/me/threads` outside the
canonical B07–B13 endpoints. `polling_cursor_ready_evidence=false` and
discovery/mentions recurring scheduling policy under #9 remains blocked.
Opportunistic Phase C observations (`/me/threads` text container publish C01,
published media retrieval C04, and publishing quota C08) were recorded as PASS
but do not equal Phase C acceptance. Issue #3 remains OPEN;
`full_tp002_ready=false`.

## 17. C6-02/5 durable Worker draining (#78)

Issue #78 adds durable row-locked DRAINING and abort transitions, bounded admin
drain status, and a worker-authenticated quiescence completion handshake.
Existing RUNNING WorkerJobs continue normally and block quiescence even after
lease expiry. The WorkerAgent closes managed sessions, reports STOPPED and zero
capacity, completes the handshake, and resumes finalization after reconnect.
Abort returns only to OFFLINE and does not claim quiescence. No migration,
process kill, updater, or installer was added. See
`docs/protocols/WORKER_PROTOCOL_V1.md` and `docs/WORKER_UPDATE_RUNBOOK.md`.
Issue #3 remains OPEN and #62 remains separate; this checkpoint is not a
production release claim.

## 18. C6-02/6 Windows Worker package (#81)

Issue #81 authorizes a reproducible-on-CI Windows x64 PyInstaller onedir package
with a console entry point, locked Playwright 1.63.0 and matching Chromium. The
package is an unsigned internal/test artifact with a safe build manifest,
normalized ZIP serialization, SHA-256 digest, and a packaged-runtime smoke
check. Durable Worker data stays outside the release directory. No service
registration, signing, downloader, or self-update is included. Phase A remains
accepted; Phase B remains `PARTIAL_LIVE_EVIDENCE / NOT_READY` with
`full_tp002_ready=false`. Issue #3 remains OPEN and #62 remains separate; no
production release readiness is claimed.

## 19. C6-02/7 interactive Windows Worker host (#84)

Issue #84 is the sole authorized C6-02/7 checkpoint. Earlier #83 LocalService
Windows Service hosting was superseded before merge and is not the deployment
model for this headed browser Worker. The Worker stays headed and runs under a
dedicated logged-in Windows user via Task Scheduler `Interactive` / `Limited`,
with no saved password, `IgnoreNew`, unlimited duration, and
`AllowHardTerminate=false`. Its current-user DPAPI identity remains bound to
that same Windows user. Strict non-secret host config overrides only matching
ordinary environment settings; enrollment remains process-environment-only.
Install/update/rollback are tied to durable #78 DRAINING -> quiescent -> OFFLINE;
abrupt session or OS loss is not drain completion. No migration, service,
downloader, or self-updater is included. Phase A remains accepted; Phase B is
`PARTIAL_LIVE_EVIDENCE / NOT_READY`; issue #3 remains OPEN, #62 remains separate,
and no production release claim is made.

## 20. C6-02/8 Linux/Docker Control Plane deployment (#87)

Issue #87 fixed the Control Plane process topology before metrics/tracing: a
locked Python 3.14 non-root image supports an explicit one-shot migration job,
FastAPI/Uvicorn HTTP container, and standalone scheduler container. HTTP and
scheduler share PostgreSQL; PostgreSQL is separate durable state, and Windows
Workers remain external. The internal/test Compose smoke uses a named PostgreSQL
volume and proves liveness/readiness, HTTP and scheduler recreation, persisted
Worker presence rediscovery, and fail-closed `/ready` during a database outage.
It does not run Meta, CRM, or Worker calls. #87 added no new schema migration,
production secret, metrics, tracing, Windows Worker container, or production
release claim. Metrics are added in #89; at that checkpoint tracing remained
deferred. #78
DRAINING and #84 interactive Worker behavior are unchanged. Phase
B remains `PARTIAL_LIVE_EVIDENCE / NOT_READY`, #3 remains OPEN, and #62 remains a
separate dependency. See `docs/CONTROL_PLANE_DEPLOYMENT_RUNBOOK.md`.

## 21. C6-02/9 bounded operational metrics (#89)

Issue #89 adds bounded Prometheus-compatible metrics to the existing topology:
the HTTP process serves `/metrics` alongside `/health` and `/ready`, while the
standalone scheduler owns a separate process-local registry/listener on the
private Compose network. HTTP Worker and WorkerJob status gauges and the
database-up diagnostic are refreshed from read-only PostgreSQL aggregates.
CommandRuntime durations use monotonic timing. Scheduler tick, stage-failure,
presence-expiry, and WorkerJob-reclaim metrics use fixed vocabularies and actual
tick results. No IDs, hosts, raw command/capability values, payloads, errors, or
credentials become metric labels or values.

Compose smoke verifies both metrics surfaces, the unpublished scheduler port,
database-derived gauge rediscovery after HTTP restart, and reset of
scheduler-local counters after scheduler recreation. Counters/histograms are
not persisted. No migration, Worker telemetry change, collector, dashboard, or
tracing is included. #3 remains OPEN, #62 remains separate, and no production
release claim follows from this checkpoint. See
`docs/OBSERVABILITY_RUNBOOK.md` and
`docs/CONTROL_PLANE_DEPLOYMENT_RUNBOOK.md`.

## 22. C6-02/10 bounded OpenTelemetry tracing (#91)

Issue #91 authorizes optional tracing for the existing HTTP Control Plane and
standalone scheduler processes. Each process owns a separate provider, fixed
service identity, bounded OTLP/HTTP exporter, and hard-bounded process shutdown.
Tracing is disabled by default, and committed Compose keeps it disabled with
no collector or OTLP port. Inbound `traceparent` establishes HTTP request
context; `tracestate`, baggage, Worker protocol propagation, and persisted
trace context are excluded. HTTP, Command, scheduler tick, and existing fixed
scheduler-stage spans use only bounded role, route/method/status, Command
status, scheduler outcome/stage, and generic error-type attributes. Trace/span
IDs are added to structured logs only during an active recording span.
Exporter SDK and HTTP transport diagnostics are reduced to a fixed log event;
shutdown waits at most three seconds and disables the SDK's synchronous atexit
shutdown hook.

No SQLAlchemy/database or outbound Threads/Meta HTTP auto-instrumentation is
included. Exporter failure is fail-open; `/health`, `/ready`, #89 metrics,
scheduler durability, and Worker protocol behavior remain unchanged. No schema
migration, collector, dashboard, or production release claim is included. The
#91 checkpoint is authorized but remains subject to coordinator review and
acceptance. Issue #3 remains OPEN and #62 remains separate. See
`docs/OBSERVABILITY_RUNBOOK.md` and
`docs/CONTROL_PLANE_DEPLOYMENT_RUNBOOK.md`.
