# Master Delivery Plan v2 — Distributed Hybrid Threads Tool

## 1. Product objective

Build a distributed Threads operations tool that preserves the useful business outcomes of the legacy Facebook automation system while replacing its fragile execution model with durable orchestration, typed protocols, PostgreSQL state, and explicit execution adapters.

The product is **not**:
- a centralized Threads API SaaS only;
- a literal port of the legacy Selenium bot;
- an anti-detect or verification-bypass system.

The product **is**:
- a durable Control Plane;
- multiple Windows-first Worker Agents;
- per-account execution modes;
- Official Threads API adapters where supported;
- isolated Browser Worker adapters for capabilities that require user-authorized web UI interaction;
- human-assisted intervention when session/login/challenge handling cannot be safely automated;
- one capability model shared across API, browser, and human execution.

## 2. Product decisions locked on 2026-09-25

These are architectural/product invariants unless superseded by a new ADR.

1. Browser accounts use **persistent account -> worker/profile affinity**.
2. Every account has its own execution mode:
   - API_ONLY
   - BROWSER_ONLY
   - HYBRID
   - MANUAL
3. If the assigned worker is offline:
   - fall back to API only when account policy and capability allow it;
   - otherwise wait for the assigned worker;
   - do not automatically migrate browser profiles between machines.
4. Background/account-activity work is created by the Control Plane/Scheduler. Workers do not invent random business actions.
5. One Worker Agent process may manage multiple browser profiles, with configurable max concurrency.
6. Initial login is operator-assisted; persistent profile/session is reused afterward.
7. Session expiry/challenge becomes an intervention state, not credential stuffing or automatic bypass.
8. Network/proxy configuration is account-scoped through a NetworkProfile.
9. Browser automation fails closed when its UI contract is no longer recognized.
10. Product/UI may use the familiar term “account nurturing”, but domain code models explicit AccountActivityPlan and capability-specific jobs.
11. Discovery scope is broad: public posts, public profiles, profile posts, mentions, conversations, and lead-oriented enrichment.
12. CRM moves to the new typed protocol. The core does not preserve the legacy raw message format.
13. Worker behavior is strict-online:
    - no new jobs while the Control Plane is unavailable;
    - an active job may continue only to a safe boundary;
    - no new irreversible side effect without a valid durable lease/checkpoint.
14. Browser Worker support is Windows-first. Control Plane remains Linux/Docker capable.
15. PostgreSQL remains the authoritative source of truth.
16. Redis/message broker remains deferred until a measured need exists.

## 3. Current implementation status

### Completed

- Batch A — Foundation
  - Python 3.14 + uv
  - FastAPI foundation
  - PostgreSQL / SQLAlchemy / Alembic
  - typed command protocol
  - idempotency
  - inbox/outbox
  - durable command attempts
- TP-004A
  - short claim transactions
  - durable command execution leases
  - heartbeat
  - checkpoint persistence
  - stale-worker fencing
  - bounded unexpected-error retries
- Batch B — Threads Core
  - documentation-contract Threads API adapter
  - text/image/video/carousel publishing
  - quote-post support from documented contract
  - crash-safe container checkpointing
  - conservative ambiguous publish recovery
  - reply-to-post / reply-to-reply
  - conversation synchronization
  - cursor compare-and-set
  - reply moderation contracts
  - quota contract support

### Open gates

- TP-000 / issue #1:
  - owner confirmation of historical credential status remains open;
  - this does not block engineering.
- TP-002 / issue #3:
  - live Threads OAuth/API verification remains open;
  - this blocks production/release activation, not documentation-contract implementation.

## 4. External Threads API baseline

As of 2026-09-25, Meta's official Threads API workspace documents capabilities including:
- OAuth authorization, long-lived token exchange and refresh;
- profile retrieval;
- public profile lookup;
- public profile posts;
- text/image/video/carousel publishing;
- quote-post and repost surfaces;
- replies and flattened conversation retrieval;
- reply creation and reply management;
- keyword/topic-tag search;
- mentions;
- insights;
- publishing quota retrieval.

Repository implementation must continue to verify current Meta developer documentation/changelog before changing a capability contract.

Official Postman collection is useful evidence but explicitly warns it may lag the latest developer changelog.

## 5. Target topology

~~~text
CRM / Operator UI
       |
       v
+-------------------------------+
|         CONTROL PLANE         |
| Command Runtime               |
| PostgreSQL                    |
| Scheduler                     |
| Worker Registry               |
| Capability Router             |
| Account Registry              |
| Outbox                        |
+---------------+---------------+
                |
      WSS notifications +
      HTTPS durable protocol
                |
      +---------+---------+
      |                   |
      v                   v
 Windows Worker S01   Windows Worker S08
      |                   |
 profiles A,B          profiles C,D,E
      |                   |
 API/Browser           API/Browser
 execution             execution
~~~

Official API execution may remain inside the Control Plane/application runtime where appropriate. Browser execution is delegated to an assigned WorkerJob.

## 6. Execution model

### Command

Represents the business intent.

Examples:
- threads.publish_image
- threads.create_reply
- threads.discovery.search
- threads.activity.execute

### WorkerJob

Represents remote execution delegated to one Worker Agent.

A Command and a WorkerJob are not the same lifecycle.

Remote browser execution must use:
- durable WorkerJob state;
- its own lease/fencing token;
- worker affinity;
- checkpoints;
- attempts;
- bounded retry;
- intervention state.

## 7. Future delivery roadmap

### C1 — Distributed Worker Foundation

Purpose: prove safe multi-machine execution before any browser automation is introduced.

Deliverables:
- WorkerNode registry;
- worker presence/heartbeat;
- worker protocol versioning;
- one-time enrollment + device identity;
- AccountWorkerAssignment;
- BrowserProfile metadata;
- NetworkProfile metadata;
- WorkerJob / WorkerJobAttempt;
- remote execution lease and fencing;
- checkpoint / reconnect / recovery;
- intervention state;
- test worker;
- WSS notification + HTTPS durable control protocol.

Exit gate:
- wrong worker cannot claim an account-affine job;
- concurrent workers cannot own one job;
- stale lease cannot checkpoint/finalize;
- disconnect/reconnect does not lose authoritative state;
- DRAINING/OFFLINE/UPGRADE_REQUIRED workers receive no new jobs;
- Control Plane restart does not lose queued/running recovery state.

### C2 — Capability Router and Account Execution Policy

Deliverables:
- capability registry with name + version;
- executor availability model;
- per-account execution mode;
- API/BROWSER/HUMAN routing policy;
- persistent account affinity enforcement;
- WAITING_EXECUTION / WAITING_INTERVENTION semantics;
- API fallback policy when assigned worker is offline;
- per-account operation classes and mutation coordination.

Exit gate:
- the same business Command can route to API or remote worker without transport/business duplication;
- routing is deterministic and testable;
- unsupported capabilities fail explicitly instead of silently falling back.

### C3 — Windows Browser Worker Foundation

Purpose: establish browser/session infrastructure without implementing the whole feature set.

Deliverables:
- Windows Worker Agent runtime;
- logical profile_ref -> local profile path resolution;
- local Worker identity/state directories;
- session manager;
- operator-assisted initial login;
- authenticated/session-expired/challenge states;
- configurable max browser sessions;
- local recovery journal interface;
- browser engine ADR/selection;
- fail-closed browser contract abstraction;
- account-scoped NetworkProfile integration.

Exit gate:
- multiple profiles can be managed by one agent;
- only assigned account/profile can execute a job;
- session/challenge transitions are durable and visible to Control Plane;
- UI contract mismatch fails safely;
- no plaintext account password/2FA is required by the core.

### C4 — Discovery and Leads

Deliverables:
- keyword/topic discovery;
- public profile lookup;
- public profile posts;
- mentions;
- conversation enrichment;
- deduplication;
- DiscoveryCampaign;
- DiscoveredThread / DiscoveredAuthor;
- LeadCandidate and enrichment state;
- CRM result integration;
- browser enrichment only where API data is insufficient and an approved capability exists.

Exit gate:
- repeated discovery runs deduplicate;
- pagination/resume is durable;
- lead-oriented normalized output is queryable and traceable to source discovery.

### C5 — Browser Capabilities and Account Activity Plans

Deliverables are capability-based, not one random “warm account” loop.

Candidate browser capabilities:
- browse feed;
- open thread;
- open profile;
- local-file publishing where required;
- UI-only actions explicitly approved after capability review;
- like/follow only if retained as product requirements and implemented without evasion logic.

AccountActivityPlan:
- created centrally;
- scheduled centrally;
- low-priority;
- preemptible at safe boundaries;
- decomposed into explicit jobs.

Explicitly excluded:
- anti-detect/fingerprint spoofing;
- randomized behavior intended to bypass bot detection;
- automated checkpoint/challenge bypass;
- uncontrolled random engagement.

### C6 — Scheduler, Operations and Production Hardening

Deliverables:
- durable scheduler;
- worker/job priority and cancellation;
- draining/update workflow;
- observability;
- structured logs;
- metrics/tracing;
- remote diagnostics;
- Windows worker packaging/update procedure;
- Linux/Docker Control Plane deployment;
- health/readiness;
- security hardening;
- staged rollout runbook.

### D — Live Validation and Release Certification

Must include:
- TP-002 live Meta validation;
- worker/browser end-to-end tests;
- production credential provider;
- release security review;
- parity/capability matrix review;
- recovery tests across Control Plane + Worker;
- staged rollout.

No production activation while TP-002 remains incomplete.

## 8. Capability execution vocabulary

Every capability must use one of:

- NATIVE_API
- HYBRID
- BROWSER_ASSISTED
- HUMAN_ASSISTED
- UNSUPPORTED
- N/A

And separately record:
- implementation_status;
- preferred_executor;
- fallback_executor;
- live_verified;
- account/session requirements;
- priority.

## 9. Browser automation boundary

Browser automation is now an approved future infrastructure direction for documented capability gaps and local browser/session workflows.

It is still forbidden from:
- entering the domain layer;
- becoming the source of truth;
- introducing DOM-specific concepts into business models;
- bypassing authentication/challenges/platform controls;
- implementing anti-detect/fingerprint evasion as a project requirement.

Every browser capability must:
- have an explicit business outcome;
- be represented as a capability;
- run through WorkerJob;
- use account affinity;
- use bounded retry/checkpoints;
- fail closed on UI-contract mismatch.

## 10. Technology baseline

Control Plane:
- Python >=3.14,<3.15
- uv
- FastAPI
- Pydantic v2
- SQLAlchemy 2
- Alembic
- PostgreSQL
- httpx
- asyncio
- structured logging
- OpenTelemetry-compatible tracing
- Prometheus-compatible metrics
- Linux/Docker capable

Worker:
- same Python project/package where practical;
- Windows-first;
- asyncio;
- persistent secure device identity;
- browser engine added only in C3 after ADR;
- local recovery journal may use SQLite only for worker recovery metadata, never as authoritative business storage.

## 11. Quality gates

Standard:

~~~bash
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run alembic check
uv run pytest
~~~

Distributed-worker changes additionally require:
- PostgreSQL integration tests;
- lease/fencing concurrency tests;
- reconnect/restart tests;
- protocol/version tests;
- wrong-worker/account-affinity tests;
- security/redaction tests.

Browser changes additionally require:
- adapter contract tests;
- fail-closed UI mismatch test;
- session lifecycle tests;
- no real credentials in fixtures;
- safe-boundary/recovery tests for external side effects.

## 12. Source-of-truth order

1. README.md
2. docs/PROJECT_STATE_HANDOFF.md
3. docs/MASTER_PLAN.md
4. docs/ARCHITECTURE.md
5. docs/FEATURE_PARITY_MATRIX.md
6. docs/WORK_BREAKDOWN.md
7. docs/protocols/WORKER_PROTOCOL_V1.md
8. docs/adr/
9. active GitHub issues

If an issue conflicts with an ADR or a higher source-of-truth document, stop and resolve the conflict before implementation.
