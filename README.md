# Distributed Hybrid Threads Operations Tool

A greenfield Python platform for durable multi-account Threads operations across a centralized Control Plane and multiple Worker machines.

## Current state

Completed:
- Batch A — Python/PostgreSQL/command foundation.
- TP-004A — durable command leases/checkpoints/recovery.
- Batch B — official-documentation-based Threads publishing, replies, conversation sync and moderation core.

Current authorized implementation checkpoint:
- **C6-02/9 — bounded Control Plane and scheduler metrics (#89)**.

Not production-ready:
- issue #3 remains open as the production/release gate;
- browser capabilities remain bounded by their accepted capability contracts;
- scheduler availability does not imply production activation;
- #62 remains a separate CRM result transport dependency;
- distributed tracing is deferred; #89 adds bounded Prometheus-compatible metrics;
- the Windows x64 Worker bundle is an unsigned internal/test artifact, with no
  Windows service, signing, or auto-update channel. The current headed browser
  Worker runs under a dedicated logged-in Windows user through Task Scheduler;
  planned updates require durable DRAINING to OFFLINE first.

## Product direction

This project is a **distributed hybrid tool**, not an API-only SaaS and not a literal port of the legacy Facebook Selenium project.

Core principles:
- PostgreSQL is authoritative.
- Business intent is a durable Command.
- Remote machine execution is a separate durable WorkerJob.
- Official Threads API is preferred where it satisfies the capability.
- Browser execution is an isolated Worker adapter for approved capability gaps/local-session workflows.
- Each account has its own execution mode: API_ONLY, BROWSER_ONLY, HYBRID or MANUAL.
- Browser accounts use persistent account -> worker/profile affinity.
- Worker offline => safe API fallback when policy permits, otherwise wait; no automatic profile migration.
- Background/account-activity work is centrally scheduled.
- Initial browser login/challenges are human-assisted.
- Browser/UI mismatch fails closed.
- No anti-detect/fingerprint-evasion objective.
- Python >=3.14,<3.15 managed with uv.

## Agent context and source of truth

Coding agents should start with `AGENTS.md`, then `docs/PROJECT_STATE_HANDOFF.md`, the authorized issue, and `docs/CONTEXT_MAP.md`. The context map points to the smallest relevant subset of detailed docs for the task.

Detailed durable knowledge lives in `docs/`:
- `MASTER_PLAN.md` — product decisions and C1-C6 roadmap.
- `ARCHITECTURE.md` — target runtime/data/execution architecture.
- `FEATURE_PARITY_MATRIX.md` — capability/executor matrix.
- `WORK_BREAKDOWN.md` — exact issue order/checkpoints.
- `protocols/` — versioned protocols.
- `adr/` — accepted architectural decisions.
- `ACCEPTANCE_AND_REVIEW.md` — reviewer gates.

Do not load every document by default. The legacy Facebook report is not part of the repository source of truth.

## Required quality gate

~~~bash
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run alembic check
uv run pytest
~~~

## Run the Control Plane scheduler

Configure `THREADS_PLATFORM_DATABASE_URL` and run this separate process:

~~~powershell
uv run python -m threads_platform.scheduler
~~~

The poll interval is wakeup latency only; PostgreSQL
`worker_nodes.presence_expires_at`, recurrence cursors,
`ScheduledActivity.due_at`, Command state and WorkerJob timing remain
authoritative. Each tick expires stale Worker presence, generates due
fixed-interval occurrences, dispatches conversation sync schedules, materializes
`ScheduledActivity` rows, drains Commands, recovers WorkerJobs, then pumps due
outbox deliveries sequentially.
Presence expiry affects eligibility for new work only; it does not revoke a
running lease, cancel work or satisfy preemption. Recurrence supports `NONE`
and UTC anchored `FIXED_INTERVAL` only; details and pause/disable semantics are in
`docs/ARCHITECTURE.md`. Optional environment settings configure the poll
interval and independent per-tick bounds:

- `THREADS_PLATFORM_SCHEDULER_POLL_INTERVAL_SECONDS` (default 15; positive, maximum 3,600)
- `THREADS_PLATFORM_SCHEDULER_PRESENCE_EXPIRY_BATCH_LIMIT` (default 50; 1–100)
- `THREADS_PLATFORM_SCHEDULER_ACTIVITY_GENERATION_BATCH_LIMIT` (default 50; 1–100)
- `THREADS_PLATFORM_SCHEDULER_CONVERSATION_SYNC_BATCH_LIMIT` (default 50; 1–100)
- `THREADS_PLATFORM_SCHEDULER_ACTIVITY_BATCH_LIMIT` (default 50; 1–100)
- `THREADS_PLATFORM_SCHEDULER_COMMAND_BATCH_LIMIT` (default 50; 1–100)
- `THREADS_PLATFORM_SCHEDULER_RECOVERY_BATCH_LIMIT` (default 50; 1–100)
- `THREADS_PLATFORM_SCHEDULER_OUTBOX_DELIVERY_BATCH_LIMIT` (default 50; 1–100)

Outbox delivery uses PostgreSQL IntegrationDelivery lease/retry state as its
authority. A delivery ID is attempted at most once per local tick; multiple
schedulers can split due deliveries through PostgreSQL claim locking. Delivery
is at-least-once at the network boundary because a remote success can precede
local lease finalization. FastAPI does not run an outbox delivery loop. The
standalone scheduler currently has no production `CRMResultSink`, reports
`CRM_RESULT_SINK_UNAVAILABLE`, and skips only this stage until #62 is resolved.

WorkerJob recovery and Worker presence expiry require this explicit scheduler
process; FastAPI performs neither wakeup in its lifespan. FastAPI and scheduler
share the production Threads API provider composition. Token execution remains
opt-in through `THREADS_PLATFORM_THREADS_TOKEN_PROVIDER_MODE=environment`; its
default is `disabled`. The mode uses PostgreSQL credential metadata and strict
versioned `env://THREADS_PLATFORM_THREADS_TOKEN_...` references. See the
[Threads credential operations guide](docs/THREADS_CREDENTIAL_OPERATIONS.md)
for metadata administration and rotation.

Phase A is accepted from the scrubbed
[`#68 evidence packet`](docs/evidence/threads-live-phase-a-2026-09-30.json).
Phase B is `PARTIAL_LIVE_EVIDENCE / NOT_READY` from the reviewed scrubbed
[`#74 evidence packet`](docs/evidence/threads-live-phase-b-partial-2026-09-30.json);
`full_tp002_ready=false`. Issue #3 remains OPEN, and no production release
readiness is claimed. The separate CRM result transport dependency #62 remains unresolved;
the scheduler still reports `CRM_RESULT_SINK_UNAVAILABLE` for that stage.

The Control Plane exposes `/health` for process liveness and `/ready` for
bounded PostgreSQL and persisted Worker-fleet readiness. Database failure makes
`/ready` return 503; fleet degradation is diagnostic and returns HTTP 200.
Structured logs carry bounded lifecycle correlation fields and centrally redact
known credential patterns. See the
[observability runbook](docs/OBSERVABILITY_RUNBOOK.md) for fields, limitations,
readiness semantics, metric names, labels, and process ownership. `GET /metrics`
uses the HTTP listener; the scheduler has a separate private metrics listener.
Metrics do not change `/ready` or durable state. Distributed tracing remains
deferred.

## Linux/Docker Control Plane smoke (#87)

The internal/test Docker image runs as a non-root user. PostgreSQL is a separate
durable service; the FastAPI/Uvicorn HTTP process and standalone scheduler run in
separate containers against the same database. A one-shot migration job runs
before either application process. Windows Workers remain external. Build and
rehearse restart/recovery with:

```powershell
uv run --locked python scripts/control_plane_compose_smoke.py
```

The smoke verifies image contents, liveness/readiness, HTTP and scheduler
metrics, HTTP and scheduler recreation over the same PostgreSQL volume,
scheduler rediscovery of persisted Worker presence, and fail-closed readiness
during a database outage. It confirms the scheduler metrics port is not
published. It removes its temporary volume on completion. See the
[Control Plane deployment and recovery runbook](docs/CONTROL_PLANE_DEPLOYMENT_RUNBOOK.md)
for process commands, scrape boundaries, manual teardown, and recovery limits.
The image is not published or signed; metrics remain process local, tracing and
#62 transport behavior are not included, and no production release claim is
made. #3 remains open and Phase B remains
`PARTIAL_LIVE_EVIDENCE / NOT_READY`.

The scheduler dispatches only already-configured `threads.sync_conversation`
schedules. It coalesces missed wall-clock intervals into one Command; the
conversation handler's durable `SyncState` cursor catches up remote data.
PAUSED schedules keep their due slot without dispatching, and DISABLED
schedules are terminal. Discovery and mentions recurrence remain deferred.

## TP-002 live evidence preparation

The #65 tooling checkpoint provides an offline packet validator and opaque-value fingerprint helper. Validate a scrubbed packet with:

```powershell
uv run python -m threads_platform.tools.threads_live_evidence validate docs/examples/threads-live-evidence-v1.template.json
```

The checked-in file is explicitly `TEMPLATE_ONLY_NOT_LIVE_EVIDENCE`; successful validation proves only that it matches the schema. The tool makes no network calls and accepts no credentials. See the [human live validation runbook](docs/THREADS_LIVE_VALIDATION_RUNBOOK.md) for a later coordinator-authorized #3 session.

## Delivery workflow

Architecture/issue -> authorized batch -> implementation branch -> Codex -> quality gate -> checkpoint PR -> coordinator acceptance -> merge.

Codex must not start the next checkpoint batch without explicit coordinator authorization.

## External documentation rule

Before changing a Threads API contract, verify current official Meta Threads developer documentation/changelog. Repository documentation may describe implementation intent and previously reviewed contracts, but external API behavior can change.
