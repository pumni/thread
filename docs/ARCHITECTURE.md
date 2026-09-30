# Target Architecture v2 — Distributed Hybrid Threads Tool

## 1. Architectural style

Use a modular monolith for the Control Plane plus distributed Worker Agents.

The architectural objective is not microservices. It is **clear execution boundaries**:
- centralized durable business state;
- local or remote executors;
- stable application ports;
- explicit capability routing.

## 2. Dependency direction

~~~text
transport -> application -> domain

infrastructure implements application/domain ports
worker adapters implement worker/application ports
~~~

Domain must not import:
- FastAPI;
- httpx;
- SQLAlchemy;
- WebSocket libraries;
- browser automation libraries;
- Meta DTOs;
- Windows APIs;
- filesystem/profile paths.

## 3. Runtime topology

### Control Plane

Responsibilities:
- command ingress;
- business validation;
- PostgreSQL source of truth;
- command runtime;
- scheduler;
- worker registry;
- capability router;
- account execution policy;
- WorkerJob dispatch;
- outbox/result delivery;
- observability.

### Worker Agent

Responsibilities:
- authenticate as a device;
- register presence/capabilities;
- manage local capacity;
- claim assigned WorkerJobs;
- execute local adapters;
- maintain job lease;
- persist allowed recovery journal metadata;
- report checkpoints/results/intervention states;
- manage browser sessions in C3+.

A Worker Agent does not own authoritative business state.

## 4. Core domain/application modules

### accounts
- Threads identity
- execution_mode
- API authorization state
- browser/session state summary
- worker assignment
- network-profile reference
- operational status

### commands
- business command
- idempotency
- command attempts
- deadlines
- business result

### worker_fleet
- WorkerNode
- WorkerCapability
- AccountWorkerAssignment
- WorkerJob
- WorkerJobAttempt
- WorkerIntervention
- protocol compatibility
- worker state

### capabilities
- capability identifier/version
- execution requirements
- preferred/fallback executor
- route decision
- operation class
- live verification flags

### publishing
- publish state machine
- container/recovery anchors
- post persistence

### conversations
- reply hierarchy
- sync/cursor state
- moderation

### discovery
- campaigns
- search queries
- discovered Threads/authors
- enrichment
- lead candidates

C4 discovery is API-first and command-driven. A campaign owns durable search queries
and runs for keyword/tag search, public-profile lookup and posts, mentions, and
conversation enrichment. Canonical Threads and authors are deduplicated by stable
remote IDs. Each observation is retained as source evidence, even when the same
entity appears in later runs or through another source. Profile and post fields
that are absent from API responses remain marked as needing enrichment.

Each run commits the fetched page, its source evidence, cursor history, and updated
run counters/status in one PostgreSQL transaction. API requests happen outside that
transaction; bounded page execution and an explicit resume command continue from the
last committed cursor. Repeated cursors fail closed. Cursor behavior is implemented
against Meta's documented contract and remains subject to issue #3 live validation.
LeadCandidate records are account-scoped, link to retained evidence, and use an
audited, explicit status lifecycle. Discovery does not invoke a browser, scheduler,
or worker runtime.

### activities
- AccountActivityPlan
- scheduled explicit activities
- priority/preemption policy

## 5. Account execution mode

Each account has one mode:

- API_ONLY
- BROWSER_ONLY
- HYBRID
- MANUAL

Execution mode is a policy input, not a transport detail.

HYBRID does not mean automatic fallback for every failure. Fallback is allowed only if:
- capability policy allows it;
- executor has sufficient evidence/authorization;
- switching executor does not violate recovery/idempotency semantics.

### Deterministic Capability Router

After command validation, the application Capability Router evaluates a versioned
business capability policy and account execution mode before selecting an executor.
The policy describes the business operation, its preferred executor, any explicitly
safe fallback, and the corresponding WorkerCapability name/version. WorkerCapability
advertisements are evidence about a particular worker; they do not define business
policy.

Current mode semantics are:
- API_ONLY uses an available API handler and waits if it is unavailable;
- BROWSER_ONLY queues work for the active assigned worker and never selects the API;
- HYBRID follows the capability's preference and permits fallback only before the
  first execution attempt when the policy explicitly allows it;
- MANUAL enters WAITING_INTERVENTION.

Once an executor has attempted a command, routing will not switch executors. An
existing WorkerJob remains authoritative for remote work, and reconciliation-required
work waits for intervention. API execution reuses its existing handler. Remote routing
persists the decision, Command transition, and WorkerJob through WorkerJobService in
one PostgreSQL transaction. WebSocket notifications remain advisory.

Exclusive account operations share one PostgreSQL account-execution lease across API
and WorkerJob execution. Its monotonically increasing generation fences stale owners;
claims and renewals hold the lease briefly, and checkpoints/finalization verify it.
Read operations do not take this exclusive lease. Account assignment changes lock the
account row so a worker claim cannot race a reassignment.

## 6. Persistent browser affinity

Browser identity is persistent:

~~~text
Account -> AccountWorkerAssignment -> WorkerNode -> BrowserProfile
~~~

Rules:
- browser jobs for an account route only to its active assigned worker;
- another worker cannot opportunistically claim the job;
- profile migration is not automatic;
- manual reassignment may be supported later through an explicit administrative workflow;
- API execution may be available independently from browser affinity.

## 7. Command vs WorkerJob

### Command

Represents business intent and owns business-level idempotency.

### WorkerJob

Represents one durable remote execution assignment.

A Command may:
- execute locally through an API adapter;
- produce one WorkerJob;
- wait for intervention;
- later be rerouted only under an explicit safe policy.

WorkerJob must not replace Command.

## 8. WorkerJob lifecycle

Base states:

- QUEUED
- RUNNING
- WAITING_INTERVENTION
- SUCCEEDED
- FAILED_RETRYABLE
- FAILED_FINAL
- CANCELLED
- EXPIRED

Attempt history belongs in WorkerJobAttempt.

Required WorkerJob fields include:
- id
- command_id
- account_id
- capability_name
- capability_version
- worker_id
- status
- priority
- preemptible
- scheduled_at
- deadline_at
- lease_token
- lease_expires_at
- checkpoint
- result
- error_code
- created_at
- updated_at
- completed_at

## 9. Remote lease/fencing semantics

WorkerJob has a lease separate from the existing local Command execution lease.

Claim:
- performed in a short PostgreSQL transaction;
- worker must be eligible, online and assigned;
- claim writes a unique lease token and expiry.

Checkpoint/heartbeat/finalization:
- require matching lease token;
- require an unexpired eligible lease;
- stale worker cannot update state.

Reclaim:
- expired RUNNING jobs may be reclaimed according to retry/recovery policy;
- old lease token becomes invalid permanently.

## 10. Worker presence vs Job lease

Presence controls whether a worker can receive new work. The default presence TTL
is 90 seconds. `hello` and `heartbeat` update `last_heartbeat_at` and
`presence_expires_at`; bounded scheduler maintenance changes an expired ONLINE
or DEGRADED worker to OFFLINE. PostgreSQL `presence_expires_at` is authoritative;
the scheduler poll interval controls wakeup latency only. A claim requires
status ONLINE, a supported protocol/capability schema, and
`presence_expires_at > now` (as well as assignment and account policy).
DEGRADED, DRAINING, OFFLINE, DISABLED, and UPGRADE_REQUIRED workers cannot claim.

Presence does not own a running WorkerJob. Its PostgreSQL lease is authoritative:
the job remains RUNNING with its current `lease_worker_id`, `lease_token`, and
`lease_expires_at` when presence expires or a WSS connection drops. Presence expiry
does not clear the token, cancel the job, or make it reclaimable. An authenticated
worker may renew, checkpoint, or finalize that job while the lease is unexpired,
the worker/token match, and any exclusive account-coordination fence is still
valid. Those operations check the WorkerJob and account fence; they do not require
the worker to have fresh presence.

Expired-job recovery is driven by `lease_expires_at <= now`, deadlines, attempt
bounds, and retry-safety policy, not by presence expiry or disconnect. Reclaim
issues a new fencing token, permanently invalidating the old token. A worker that
cannot reach authenticated HTTPS cannot renew; its job remains authoritative until
the lease expires and recovery processes it. Reconnect reconciliation reads the
durable WorkerJob state from PostgreSQL. A worker may therefore be OFFLINE for new
claims while still holding an unexpired lease for already claimed work.

## 11. Worker transport

### WebSocket

Used for low-latency signals only:
- worker hello/presence;
- heartbeat hints;
- capacity change;
- job.available;
- cancel request;
- drain;
- upgrade required;
- session/intervention notification.

WebSocket state is never business truth.

### HTTPS

Used for durable mutations:
- enroll/authenticate;
- claim job;
- renew job lease;
- persist checkpoint;
- complete job;
- fail job;
- request/resolve intervention;
- reconcile jobs.

A lost WebSocket notification must not lose work.

## 12. Worker authentication

Target model:
- one-time enrollment;
- worker generates a device keypair;
- Control Plane stores public identity;
- worker private material remains local and protected by OS facilities;
- reconnect uses challenge/signature to obtain a short-lived worker access token;
- WSS and HTTPS use the authenticated worker identity.

Do not deploy one shared static secret to all machines.

Exact cryptographic/storage implementation is a C1 issue-level design detail and must receive security review.

## 13. Worker protocol/versioning

Worker reports:
- agent_version;
- worker_protocol_version;
- capability_name + capability_version.

Protocol mismatch:
- worker may remain visible;
- worker becomes DEGRADED/UPGRADE_REQUIRED;
- it cannot claim incompatible jobs.

## 14. WorkerNode lifecycle

- REGISTERING
- ONLINE
- DEGRADED
- DRAINING
- OFFLINE
- DISABLED
- UPGRADE_REQUIRED

DRAINING:
- receives no new jobs after the row-locked status transition commits;
- already-running WorkerJobs continue under their existing leases and state
  transitions until their safe/terminal boundary;
- does not imply cancellation, preemption, lease revocation, or process kill;
- reaches OFFLINE through the authenticated worker completion handshake only
  after stored active browser session count and durable RUNNING lease count are
  both zero.

Admin drain/abort requests serialize with job claims on the Worker row.
Quiescence counts every `RUNNING` WorkerJob whose `lease_worker_id` matches,
including expired leases. Presence expiry remains scheduler-owned and does not
prove local process shutdown. Abort is recovery to OFFLINE, never an ONLINE
shortcut. The WorkerAgent closes managed sessions, reports STOPPED and zero
capacity, completes the handshake, and exits after OFFLINE confirmation; a
network failure resumes from durable DRAINING after reconnect. See
`docs/protocols/WORKER_PROTOCOL_V1.md` and `docs/WORKER_UPDATE_RUNBOOK.md`.

## 15. Strict-online semantics

When Control Plane connectivity is lost:
- do not claim new work;
- do not initiate a new irreversible external side effect without a valid durable lease/checkpoint;
- current work may continue only to a safe boundary;
- if outcome is already ambiguous, journal local evidence and reconcile after reconnect.

## 16. Local recovery journal

Worker may use a local journal for recovery metadata only.

Allowed examples:
- job_id
- lease token reference/identifier
- account_id
- profile_ref
- local execution phase
- timestamp
- non-secret remote/recovery identifiers where required

Not allowed:
- authoritative CRM/business records;
- plaintext account passwords;
- access tokens;
- full Threads data store.

PostgreSQL remains authoritative.

## 17. Browser profile and session model

Control Plane stores logical references, not Windows paths.

Example:
- profile_ref = profile-<account_uuid>

Worker resolves the logical ref under its own local data root.

The Windows Worker Agent defaults to `%LOCALAPPDATA%/ThreadsOperations` and keeps paths in
worker infrastructure. Profile directories are derived beneath that root from `worker_id` and
the logical `profile_ref`; durable profile ownership prevents one account from claiming
another account's local directory. C3-01 defines session lifecycle and status reporting only;
it does not launch a browser engine.

Target session states:
- UNINITIALIZED
- LOGIN_REQUIRED
- STARTING
- AUTHENTICATED
- BUSY
- SESSION_EXPIRED
- CHALLENGE_REQUIRED
- ERROR
- STOPPED

Login/challenge handling is human-assisted. No automatic challenge bypass.
Session state and its intervention-required flag are persisted by the Control Plane. The
worker keeps a bounded local report journal and retries durable HTTPS reports after reconnect.

## 18. NetworkProfile

Network configuration is account-scoped.

NetworkProfile may contain references to:
- direct connection;
- approved proxy endpoint;
- connectivity policy.

The architecture goal is routing/availability, not detection evasion.

Sensitive proxy credentials must be protected and never exposed in normal logs/business payloads.

## 19. Browser automation boundary

Browser support is an isolated infrastructure capability for:
- user-authorized UI workflows;
- local media workflows;
- capabilities not exposed through a suitable official API.

Browser code must:
- stay outside domain;
- not leak selectors/DOM models into application business types;
- run through WorkerJob;
- fail closed on contract mismatch;
- use bounded recovery;
- never become an anti-detect/fingerprint-evasion subsystem.

## 20. Browser capability safety rule

Each browser capability must declare:
- business outcome;
- required session state;
- mutation/read classification;
- recovery checkpoints;
- ambiguous outcome behavior;
- whether it is preemptible;
- UI contract version.

No generic random “human behavior” function is accepted.

C5-01 declares versioned feed browse, Thread open, profile open, and local media
staging contracts. PR #39 feed browse, PR #40 Thread open, PR #41 profile open,
and PR #43 image-only local media staging are accepted and merged and DONE for
their checkpoints. Each uses a bounded account-affine WorkerJob path and
requires explicit worker opt-in. Local media staging requires an already-open
operator composer, correlates the upload response with a preview in that same
composer, and uses reconciliation after uncertain file selection. It never
publishes or submits content. Synthetic contracts verify adapter behavior only
and are not production evidence. Local media references resolve under the
Worker Agent's managed `media` directory. Issue #27 is CLOSED / COMPLETED after
all four C5-01 capability checkpoints were accepted. C5-02/#28 is also CLOSED /
COMPLETED after #45, #47, #49 and #51. Issue #53 authorizes C6-01/1: a bounded
scheduler kernel for existing durable occurrences, using the services below.
See `docs/WORKER_BROWSER_CAPABILITY_PACK_V1.md` for the per-capability bounds
and schemas.

## 21. Concurrency

### Worker capacity
Worker config:
- max_browser_sessions
- current active sessions
- resource health

Protocol v2 reports aggregate browser-session capacity independently from generic WorkerJob
concurrency. Protocol v1 workers remain compatible and report no browser-session summary.

### Account coordination
At most one mutating browser job for one account at a time.

Control Plane must enforce account-level coordination durably.

C2 may define operation classes:
- READ
- MUTATION
- SESSION
- BACKGROUND

## 22. Activity/preemption

Account activity is centrally planned.

### C5-02/1 durable foundation (#45)

`AccountActivityPlan` is account-scoped and has `ACTIVE`, `PAUSED`, and
`DISABLED` states; paused plans can resume and disabled plans are terminal.
Activity templates are explicit configuration revisions that reject known
secret-bearing fields and credential-bearing URLs. The domain/application
limit is 16 KiB of compact UTF-8 JSON. PostgreSQL stores activity configuration
as `json` to preserve serialized number notation, with a 32 KiB database bound
for serializer whitespace; this is not a larger application configuration limit. A
`ScheduledActivity` is a durable occurrence that snapshots the
plan revision and status, template revision and configuration, and semantic
priority at creation. PostgreSQL enforces uniqueness by
`(template_id, template_revision, due_at)` and restricts deletion of referenced
plans and template history.

Activity priorities are `LOW`, `NORMAL`, and `HIGH`, mapped to numeric
priorities `-100`, `0`, and `100`; existing WorkerJob priority `0` remains
compatible with `NORMAL`.

### C5-02/2 materialization (#47)

The Control Plane materializes only already-persisted, due occurrences into
deterministic Commands in one PostgreSQL transaction. Pending occurrences on
paused plans remain pending; disabled plans and unsupported activity types get
an auditable non-materializable outcome. Only feed browse, Thread open, and
profile open may materialize. CRM protocol v1 remains priority-less and its
Commands default to `0`; internally materialized Commands inherit the activity
priority, which `CommandRuntime` propagates to the existing WorkerJob queue.
WorkerJob claim ordering applies priority to queued work. This checkpoint adds
no occurrence generator, timer, cancellation, or running-job preemption.

Low-priority activities:
- preemptible=true

High-priority CRM mutations:
- preemptible=false

### C5-02/3 safe-boundary cancellation (#49)

The internal cancellation request targets only a running, preemptible,
materialized account-activity WorkerJob for feed browse, Thread open, or profile
open. PostgreSQL records the request against the exact current attempt and
generation. Requesting cancellation does not change the job, attempt, worker,
lease token, or lease expiry. Repeating a request for that attempt returns the
same pending request.

The Worker observes pending request metadata through authenticated HTTPS
reconcile, renew, or checkpoint snapshots. WSS is advisory. After an existing
capability checkpoint, the Worker stops browser work and submits the request id,
generation, and checkpoint phase to `POST /v1/workers/jobs/{job_id}/cancel`.
If a renew exposes cancellation before any browser action since the latest
checkpoint, the Worker acknowledges at that checkpoint and skips the action. If
an action is already in progress, it finishes only to the next existing safe
checkpoint before acknowledging.
Acknowledgement is fenced by worker, lease, request, generation, current attempt,
live lease, and the persisted checkpoint. The accepted phases are:

- feed browse: `BEFORE_NAVIGATION`, `FEED_READY`, `ITEM_BATCH`;
- Thread open: `BEFORE_NAVIGATION`, `THREAD_READY`;
- profile open: `BEFORE_NAVIGATION`, `BEFORE_PROFILE_INSPECTION`, `PROFILE_READY`.

One PostgreSQL transaction records the acknowledgement, marks the attempt,
WorkerJob, and linked Command `CANCELLED`, clears the old lease, and writes the
Command result outbox event. Completion, failure, intervention, deadline
recovery, and reclaim lock the WorkerJob row and supersede a pending request if
they end its target attempt first. Lease expiry never acknowledges cancellation;
reclaim starts a new attempt and any new request uses the next generation. A
stale token cannot mutate the job. No priority policy requests cancellation in
this checkpoint; C5-02/4 (#51) adds the narrowly scoped HIGH-priority policy below.

No global stop flag.

### C5-02/4 HIGH safe preemption and profile gate (#51)

Only trusted Control Plane routing of a `HIGH` Command may create a HIGH
preemptor WorkerJob, with `Command.priority == WorkerJob.priority == 100`.
CRM protocol v1 remains caller-priority-less. When that WorkerJob is created,
the same PostgreSQL transaction records a `WorkerJobPreemption` relationship
for each strictly lower-priority, RUNNING, account-affine browser job on the
same account. A valid materialized preemptible READ activity also reuses the
#49 cancellation request primitive in that transaction. Other running browser
jobs, including `media.local_upload` and generic/unlinked jobs, get no cancel
request but remain profile blockers.

HIGH stays QUEUED until its relationships reach `SATISFIED` and no browser job
is still using the account profile. Every account-affine browser candidate,
regardless of priority, locks the account row and checks both the running-job
set and unresolved preemption relationships before its job row is claimed.
The database claim is revalidated under row lock. Different accounts remain
independent, and nonbrowser WorkerJobs do not use this profile gate.

An explicit current-lease attempt end (safe cancellation acknowledgement,
completion, failure, or intervention request) satisfies waiting preemptions.
An ended preemptor supersedes its own waiting relationships; a shared victim
cancel request remains pending while another HIGH preemptor still waits. Lease
expiry, recovery, and reclaim never satisfy preemption. Expired victims keep
HIGH blocked; reclaim creates a new attempt and a new #49 cancellation
generation before HIGH can claim. Equal-priority HIGH jobs do not cancel one
another and retain the existing deterministic queue tie order. This checkpoint
does not add NORMAL-over-LOW preemption, fairness, or process/lease-revocation
cancellation.

### C6-01 PostgreSQL scheduler and recurrence (#53, #56, #58, #60, #63)

The scheduler runs one bounded tick in this order: expire stale Worker presence,
generate due recurrence occurrences, dispatch due conversation sync schedules,
materialize due `ScheduledActivity` rows, drain ready Commands through
`CommandRuntime` and the Capability Router, then invoke bounded
`WorkerJobService` recovery, and finally pump due outbox deliveries. It never
constructs WorkerJobs or executes browser work directly. PostgreSQL
`presence_expires_at`, occurrence, schedule, Command, WorkerJob, and
IntegrationDelivery rows remain authoritative. The process loop is a wakeup
mechanism only; every tick rediscovers work from PostgreSQL after restart.

The explicit process runs as `python -m threads_platform.scheduler`; it is not
hidden in FastAPI request handling. Ticks run sequentially within a process.
Separate processes may run concurrently: expired Worker selection, recurrence
cursor, due-occurrence, conversation schedule, and ready-Command selection use
PostgreSQL row locks and `FOR UPDATE SKIP LOCKED`, while existing uniqueness,
Command routing, WorkerJob recovery and lease fencing remain authoritative.
Presence expiry affects eligibility for new work only. It does not revoke a
WorkerJob lease, cancel a job, acknowledge preemption, release account/browser
coordination, terminate a browser process, or change an Attempt outcome. The
FastAPI lifespan performs neither Worker presence expiry nor WorkerJob
recovery; both wakeups require the explicit scheduler process. FastAPI
and the scheduler use one process composition helper for the UnitOfWork,
WorkerJobService, CapabilityRouter, Threads API gateway, token provider and
handler registry. Production Threads API execution is opt-in through
`THREADS_PLATFORM_THREADS_TOKEN_PROVIDER_MODE=environment` (default
`disabled`). Environment mode requires PostgreSQL and resolves only versioned
`env://THREADS_PLATFORM_THREADS_TOKEN_[A-Z0-9_]+` references. The existing
`oauth_credentials` table stores reference, token type, scopes, expiry and
status; it has no plaintext access-token or refresh-token column. The provider
requires an active, unexpired Bearer credential with `threads_basic`; it does
not refresh tokens. Operators bind, rotate and revoke metadata through the
metadata-only administration CLI. See
`docs/THREADS_CREDENTIAL_OPERATIONS.md` for deployment and rotation order.

Accepted Phase A live evidence is recorded at
`docs/evidence/threads-live-phase-a-2026-09-30.json`. Phase B is `NOT_RUN`, and
#3 remains OPEN as a production/release gate. This composition does not claim
production release readiness. The CRM result transport remains a separate #62
dependency; scheduler LOCAL_API availability does not change its
`CRM_RESULT_SINK_UNAVAILABLE` report. Presence expiry has an independent
per-tick limit of 1–100 Workers, default 50.

The outbox pump is a sequential final tick stage with its own batch limit of
1–100 deliveries (default 50). PostgreSQL `IntegrationDelivery` due/retry state
and fenced leases determine claims. Each delivery ID is attempted at most once
per local tick; multiple scheduler processes can claim distinct due deliveries
through `FOR UPDATE SKIP LOCKED`. Unexpired PROCESSING, DELIVERED, and FAILED_FINAL
rows do not consume due selection. Existing retry/deadline rules and stale
lease fencing remain unchanged. Delivery is at-least-once at the network
boundary: a remote success can precede local DELIVERED finalization and be
repeated after lease expiry. The stable event identity remains available for
remote idempotency. FastAPI has no outbox delivery loop. The standalone
scheduler has no production `CRMResultSink`; it reports
`CRM_RESULT_SINK_UNAVAILABLE` and skips only delivery until dependency #62
defines the transport. No migration is required for this checkpoint.
Recurrence is defined by the immutable `AccountActivityTemplate` revision.
`NONE` has no anchor or interval. `FIXED_INTERVAL` has a UTC anchor and an
integer interval from 900 through 2,592,000 seconds; its slots are exactly
`anchor_at + n * interval_seconds` for `n >= 0`. It is allowed only for feed
browse, thread open, and profile open. There is no jitter, cron, timezone/DST
grammar, random timing, or catch-up policy beyond generating every due slot.

Migration `20260929_0015` backfills existing revisions as `NONE` and adds the
activity-specific `account_activity_recurrence_states` cursor. A cursor binds
to one exact template revision and stores only the next/last due timestamps,
generated count, and audit timestamps. Cursor advancement and the immutable
`ScheduledActivity.from_plan_template` snapshot insert commit in one
transaction. A unique occurrence identity and PostgreSQL cursor row locking
prevent duplicate slots across generator instances. The cursor is initialized
to the revision anchor. A bounded call advances at most its generation limit;
later calls continue at the next exact interval slot after restart.

Only the latest template revision generates. Appending a revision leaves old
occurrences unchanged, stops the old cursor from generating, and starts the
new revision from its own anchor. `ACTIVE` and `PAUSED` plans both generate
durable occurrences. The existing materializer leaves paused occurrences
pending, so resume makes those same rows eligible. `DISABLED` plans generate
nothing; existing pending rows retain existing `PLAN_DISABLED` handling.
Downgrade from 0015 is allowed with only `NONE` revisions and no cursor rows;
it refuses to discard recurrence configuration or cursor history.

Periodic conversation sync is limited to the existing
`threads.sync_conversation` READ Command. A `ConversationSyncSchedule` binds
one account, local root post ID, sync kind, UTC anchor, and fixed interval from
900 through 2,592,000 seconds. There is no cron, timezone/DST grammar, jitter,
or randomization. Its next due time and control state are PostgreSQL-owned;
the immutable `ConversationSyncDispatch` audit row records each exact due
slot, schedule revision, and deterministic Command ID. The due slot is the
identity of a dispatch. If the scheduler was unavailable across several
intervals, it creates one Command for the oldest due slot and advances the
next due time to the first interval strictly after `now`; the existing
`SyncState` cursor performs remote data catch-up.

Only ACTIVE schedules dispatch. PAUSED schedules retain their due time and
create no Command; resume can dispatch one coalesced due slot. DISABLED is
terminal, and a partial unique index allows a replacement schedule while
retaining disabled history. Before dispatch, the service checks the latest
dispatch's Command under schedule-row serialization. A non-terminal Command
keeps the schedule due and blocks overlap. A missing account-owned local root
post likewise creates no Command, dispatch, or cursor movement and is logged
with a bounded error code. Command creation, dispatch audit insertion, and
schedule cursor advancement commit in one transaction. IDs derive from
schedule ID plus exact due timestamp; the Command payload contains only the
root post ID and `conversation` or `replies`, with priority 0 and no invented
deadline. The independent
`THREADS_PLATFORM_SCHEDULER_CONVERSATION_SYNC_BATCH_LIMIT` defaults to 50 and
allows 1–100 schedules per tick.

The Command still passes through `CommandRuntime` and the Capability Router
to the LOCAL_API conversation handler. The handler reads and advances the
existing `SyncState` cursor, so coalescing dispatch history never collapses
remote cursor catch-up. #70 provides a shared, opt-in environment-backed token
provider for FastAPI and the standalone process; #3 remains the production
gate. When provider mode is disabled, its Command remains durable and the
outstanding-command gate prevents an overlapping dispatch.
Discovery and mentions recurrence remain deferred because #3 has not
established the required live time-window/cursor semantics.

Generation, conversation dispatch, materialization, Command, and recovery
limits are each 1–100, with a default of 50. The generation setting is
`THREADS_PLATFORM_SCHEDULER_ACTIVITY_GENERATION_BATCH_LIMIT`; it is separate
from the materialization limit. The poll interval defaults to 15 seconds,
must be positive, and is bounded to 3,600 seconds. Worker offline state leaves
account-affine browser work in the durable WorkerJob queue; normal worker
eligibility and claim behavior resumes it later. Recovery preserves existing
retry timing, reconciliation-required intervention, stale-lease fencing, and
the rule that lease expiry alone does not satisfy safe-boundary preemption.

## 23. Persistence

PostgreSQL remains authoritative.

Expected future tables:
- worker_nodes
- worker_capabilities
- account_worker_assignments
- browser_profiles
- network_profiles
- worker_jobs
- worker_job_attempts
- worker_job_cancel_requests
- worker_job_preemptions
- worker_interventions
- worker_account_sessions
- discovery_campaigns
- discovered_threads
- discovered_authors
- lead_candidates
- activity_plans

Use:
- UTC timestamps;
- durable uniqueness constraints;
- explicit indexes for claim/routing queries;
- JSONB only for bounded execution metadata/checkpoints, not mutable business aggregate trees.

## 24. Existing API execution

The current TP-004A/Batch-B runtime remains valid.

Official API adapter is not replaced by the worker design.

The C2 Capability Router selects local API execution, a durable WorkerJob, a human
intervention state, or explicit unsupported status. C3 supplies the browser runtime;
the router does not launch a browser.

## 25. Reliability invariants

- command_id remains the business idempotency key;
- WorkerJob has its own durable identity and lease;
- timeout never means remote failure;
- stale lease cannot finalize;
- WebSocket loss cannot lose work;
- worker crash cannot erase authoritative state;
- profile affinity is enforced server-side;
- retries are bounded;
- ambiguous side effects reconcile instead of blind replay.

## 26. Security invariants

Reject:
- plaintext account credentials committed/stored in normal business tables;
- shared static worker secret deployed to every machine;
- tokens in logs;
- disabled TLS without explicit dev-only rationale;
- browser challenge bypass;
- anti-detect/fingerprint spoofing requirements;
- selector fallback that clicks unknown UI.

## 27. Architectural fitness checks

Review rejects:
- Command and WorkerJob collapsed into one state model;
- worker considered source of truth;
- business payload stored only on WebSocket;
- other worker claiming an account-affine browser job;
- domain importing browser/Windows/SQLAlchemy/httpx;
- raw DOM selectors in application commands;
- automatic profile migration;
- random background action loops;
- unbounded local tasks;
- new broker/cache without evidence and ADR.

## 28. Structured observability and readiness (#72)

Command, WorkerJob, and scheduler lifecycle logs use bounded correlation fields
and pass through the central recursive redaction processor before JSON output.
Correlation context is scoped to each asynchronous operation. Logs must never
contain business payload/result/checkpoint data, credentials, lease tokens,
raw request/response bodies, or unsafe exception arguments. Redaction protects
known sensitive keys and token-like patterns; it cannot reliably identify an
arbitrary high-entropy secret under a neutral field, so callers must not log
untrusted or secret-bearing content. See `docs/OBSERVABILITY_RUNBOOK.md`.

`GET /health` is process liveness and does not access PostgreSQL. `GET /ready`
uses a bounded PostgreSQL probe and aggregate persisted `worker_nodes.status`
counts. A database failure returns `NOT_READY` / `DOWN` with HTTP 503. An empty
fleet is `READY` / `EMPTY`, all-online is `READY` / `HEALTHY`, and any
non-disabled non-online worker makes the fleet `DEGRADED` while keeping HTTP
200. Readiness does not expire presence or mutate state; scheduler-owned
presence expiry remains authoritative. This checkpoint adds no metrics,
tracing, schema, or audit table. Issue #3 remains the production/release gate;
the #62 CRM result transport remains separate.

## 29. Windows Worker package and interactive host boundary

The Windows x64 package is a PyInstaller `onedir` application built from the
locked Python dependencies with the matching Playwright 1.63.0 Chromium only.
The executable and its `BUILD-MANIFEST.json` live in an immutable release
directory. Durable Worker identity, DPAPI-protected private key, profiles,
SQLite journal, media staging, and recovery data remain under
`%LOCALAPPDATA%\ThreadsOperations` or the explicitly configured
`THREADS_WORKER_DATA_ROOT`; package staging never copies that root.

`--version` is metadata-only. `--package-check` uses an isolated temporary data
root, tests identity persistence and DPAPI protect/unprotect, initializes the
local journal, and opens bundled Chromium on `about:blank`. It does not call the
Control Plane, Threads/Meta, or external URLs. The workflow creates a safe
manifest, stable ZIP serialization, and a SHA-256 digest. The manifest's
required build-clock timestamp means separate CI builds can have different
archive hashes. The artifact is unsigned and internal/test only; a hash is not
publisher authentication. No service registration, signing, downloader, or
self-update is included.

The current browser Worker stays headed and runs under a dedicated logged-in
Windows user through the built-in Task Scheduler API. Its stable task uses that
user's `Interactive` token and `Limited` run level, starts at the same user's
logon, ignores duplicate instances, has unlimited duration, and sets
`AllowHardTerminate=false`. It stores no Windows password. Windows Services run
outside the interactive desktop; using a service principal would also change the
current-user DPAPI context that protects the Worker device key. A service host
is therefore not an authorized deployment model for this headed Worker. It can
be reconsidered only after a separately validated headless/browser-host design.

The executable accepts a strict, non-secret `threads-worker-host-v1` JSON file
through `--host-config`. It allows only bounded existing deployment settings;
host values take precedence over their matching ordinary environment settings,
while omitted fields fall back to environment configuration. Enrollment remains
environment-only and is never part of the host config or scheduled task.
First enrollment and task registration happen interactively under the same
dedicated Windows user. Current-user DPAPI identity must remain with that same
principal; do not copy the identity to another user.

Planned release changes require durable #78 DRAINING, quiescence, and Worker
completion to OFFLINE before switching the task action. Task registration,
update, and uninstall preserve the host config and data root; old releases remain
available for rollback. A Task Scheduler task cannot promise graceful completion
after abrupt logoff, OS shutdown, crash, or user termination; durable lease
recovery and Worker presence expiry remain authoritative. Issue #3 remains open,
#62 remains separate, and the unsigned package is not a production release claim.

## 30. Linux/Docker Control Plane deployment boundary (#87)

The Control Plane deployment uses one immutable Linux image for three separate
roles: a one-shot Alembic migration job, the FastAPI/Uvicorn HTTP process, and
the standalone scheduler. HTTP and scheduler are independent containers sharing
the same PostgreSQL database; scheduler loops stay outside FastAPI lifespan.
The database is a separate durable service in Compose and remains external to
the application image. Windows browser Workers stay on their interactive
Windows hosts and are never containerized here.

The runtime image is built from locked Python 3.14 dependencies, runs as a
non-root user, and copies only the installed shared Python project plus
Alembic configuration/revisions. It does not contain source-control metadata,
environment files, tests, local databases, Worker durable state, browser
profiles, Windows onedir artifacts, or Playwright browser binaries. The
Playwright Python dependency remains in the shared lock; image build does not
install a browser. There are no secret build arguments, embedded database
credentials, privileged containers, Docker socket mounts, host networking, or
database process inside the application image.

Migration is explicit and one-shot before application startup; replicas do not
run migrations from FastAPI or scheduler startup. `/health` remains process
liveness and `/ready` remains the #72 bounded PostgreSQL/fleet probe. A database
outage produces `/ready` `NOT_READY` / `DOWN` / HTTP 503 without making memory
authoritative. Scheduler restarts rediscover durable rows and leases from
PostgreSQL. The Compose restart smoke uses existing Worker persistence and
scheduler presence-expiry behavior; it adds no schema or business API.

Compose is an offline internal/test topology with a disposable named PostgreSQL
volume and trust authentication on its private network. It contains no real
Threads or CRM credentials. Do not use this local authentication setup as a
networked production database. The image is not pushed or signed. At the #87
checkpoint, metrics and tracing were still deferred; #89 adds bounded metrics
as described in section 31. #91 separately adds bounded opt-in tracing as
described in section 32. #62 remains a
separate CRM dependency; #3 remains open as the production/release gate. See
`docs/CONTROL_PLANE_DEPLOYMENT_RUNBOOK.md` for exact process, smoke, restart,
and recovery commands. #78 DRAINING and #84 Windows Worker lifecycle semantics
are unchanged.

## 31. Bounded operational metrics (#89)

The HTTP process exposes `GET /metrics` on its existing listener. It reads
aggregate `worker_nodes.status` and `worker_jobs.status` counts from a short-lived
read-only PostgreSQL session. The `threads_platform_workers{status}` gauge
includes all `WorkerStatus` values, including `DISABLED`; the
`threads_platform_worker_jobs{status}` gauge includes all `WorkerJobStatus`
values. If either aggregate fails or times out, HTTP emits
`threads_platform_database_up 0`, omits both persisted count families, and
returns bounded Prometheus text. `/ready` is unchanged and remains the separate
orchestrator readiness contract.

The `threads_platform_command_execution_duration_seconds` histogram measures
monotonic duration around existing `CommandRuntime` processing methods
(`process` and selected `process_next`). Each process owns its local histogram
and exposes it at its own metrics endpoint.

The scheduler owns a separate `CollectorRegistry` and private listener. Its
bounded metrics are `threads_platform_scheduler_ticks_total{outcome}` with
`success|error`, `threads_platform_scheduler_tick_duration_seconds`,
`threads_platform_scheduler_worker_presences_expired_total`,
`threads_platform_scheduler_worker_job_lease_reclaims_total`, and
`threads_platform_scheduler_stage_failures_total{stage}`. Stage labels use only
the existing fixed tick-stage vocabulary. Presence and reclaim counters consume
the actual `SchedulerTickResult` values, including the partial result on a
failed tick; this adds no recovery behavior.

All project metric names use `threads_platform_`. Labels are bounded to Worker
status, WorkerJob status, tick outcome, or fixed stage. No business or machine
identity, command/capability value, URL, payload, exception, DSN, credential, or
request header enters the registry. No browser-session metric is added: the
persisted `active_browser_sessions` value is a last Worker report that can stay
nonzero after scheduler-owned presence expiry. Defining live freshness would
require changing or duplicating that ownership contract; no Worker telemetry
protocol is added.

Scheduler metrics are disabled by default and bind to loopback by default. The
listener accepts only `127.0.0.1` or `0.0.0.0` and ports 1–65535. Compose enables
`0.0.0.0:9101` inside the private network without publishing that port to the
host. PostgreSQL gauges are re-read after HTTP restart. Histograms and scheduler
counters are process local, may reset at restart, and are never persisted.
Metrics do not define business state or readiness. Tracing was deferred at the
#89 checkpoint; #91 adds optional tracing in section 32. #3 remains open and
#62 remains separate. See
`docs/OBSERVABILITY_RUNBOOK.md` for the scrape and reset contract.

## 32. Bounded OpenTelemetry tracing (#91)

Tracing is disabled by default with `THREADS_PLATFORM_TRACING_ENABLED=false`.
When enabled, the HTTP Control Plane and standalone scheduler each construct
their own `TracerProvider`, fixed `service.name` resource, OTLP/HTTP exporter,
and process shutdown lifecycle. The global OpenTelemetry provider is not
replaced. No collector is included in Compose, and the existing process and
metrics-listener boundaries do not change. Exporter endpoint and authentication
are environment-only through `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` or
`OTEL_EXPORTER_OTLP_ENDPOINT`, and the corresponding standard `*_HEADERS`
settings. Configured endpoints must be HTTP(S), include a hostname, and contain
no user information, query, or fragment. Values are never logged. Compose pins
tracing to disabled and publishes no OTLP port.

The HTTP process emits `http.server.request` for application routes, with
`process.role`, a bounded HTTP method, a FastAPI route template (or
`unmatched`), and response status. `/health`, `/ready`, and `/metrics` are
excluded. The command runtime adds `command.receive`, `command.process`, and
`command.process_next` spans; the last is created only after a ready Command is
selected. Command spans add only the bounded `process.role` and terminal
`command.status` vocabulary.

The scheduler emits `scheduler.tick` and child spans named
`scheduler.stage.<stage>` for its existing fixed stages. The optional
`outbox_delivery` child exists only when an outbox worker is configured. Stage
attributes use only `process.role` and the fixed `scheduler.stage` vocabulary;
tick outcome is `success` when the tick returns. Failures set OTel status to
ERROR and the constant `error.type=exception`; exception messages, stack
traces, and events are not exported. Stage order, retry/backoff, partial
results, and scheduler semantics are unchanged.

Inbound HTTP accepts W3C `traceparent` for remote parent context. Vendor
`tracestate` and baggage are not propagated. Child spans stay in the process
context. Worker HTTP/WebSocket contracts do not carry trace context. No trace
IDs are written to PostgreSQL, Commands, WorkerJobs, or Worker records.
Structured logs add lowercase fixed-width `trace_id` and `span_id` only while
an active recording span is current; those fields are diagnostic correlation,
not durable keys.

The exporter uses a 64-span bounded queue, batches at most 64 spans, limits one
OTLP/HTTP request to 1 MiB with a two-second request timeout, and attempts a
bounded flush on normal process shutdown. Export/setup/shutdown failures are
fail-open and logged with only fixed phase/service information. No SQLAlchemy
or outbound Threads/Meta HTTP auto-instrumentation is installed; no SQL, URLs,
headers, payloads, IDs, or exception messages are span data. #89 metrics and
`/ready` semantics are unchanged. OpenTelemetry collection, dashboards, and
alerts are not deployed. #3 remains OPEN, #62 remains separate, and this
checkpoint makes no production release claim.
