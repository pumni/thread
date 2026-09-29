# Distributed Hybrid Threads Operations Tool

A greenfield Python platform for durable multi-account Threads operations across a centralized Control Plane and multiple Worker machines.

## Current state

Completed:
- Batch A — Python/PostgreSQL/command foundation.
- TP-004A — durable command leases/checkpoints/recovery.
- Batch B — official-documentation-based Threads publishing, replies, conversation sync and moderation core.

Current authorized implementation checkpoint:
- **C6-01/4 — Scheduler-owned bounded Worker presence expiry (#60)**.

Not production-ready:
- issue #3 live Meta OAuth/API validation remains open;
- browser capabilities remain bounded by their accepted capability contracts;
- scheduler availability does not imply production activation.

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
process; FastAPI performs neither wakeup in its lifespan. The runtime
composition is shared, but this repository has no concrete
production `ThreadsAccessTokenProvider` available to the standalone process, so
its LOCAL_API handler registry is unavailable and the process reports that
condition at startup. Coordinator decision is required before standalone
scheduler LOCAL_API execution can be enabled.

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
