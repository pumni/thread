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
`presence_expires_at`; `expire_presence()` changes an expired ONLINE or DEGRADED
worker to OFFLINE. A claim requires status ONLINE, a supported protocol/capability
schema, and `presence_expires_at > now` (as well as assignment and account policy).
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
- receives no new jobs;
- active jobs finish/cancel at safe boundaries;
- worker becomes safe to update/shutdown.

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
all four C5-01 capability checkpoints were accepted. Issue #45 established the
C5-02/1 activity foundation; issue #47 authorizes C5-02/2 materialization and
durable priority propagation only, without a background runner or preemption.
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
this checkpoint.

No global stop flag.

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
