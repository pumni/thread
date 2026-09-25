# Master Delivery Plan

## 1. Objective

Build a new Threads Operations Platform that preserves the useful business outcomes of the legacy Facebook system while using Threads-native concepts and official APIs wherever available.

The target is not behavioral cloning of Facebook UI automation. The target is business capability parity plus Threads-native capabilities, implemented with a maintainable production architecture.

## 2. Delivery principles

- Greenfield implementation.
- API-first integration.
- Typed domain models.
- Explicit dependency boundaries.
- Durable state in PostgreSQL.
- External commands are idempotent.
- Network delivery uses inbox/outbox semantics.
- Async workers use bounded concurrency.
- Secrets are never committed.
- Every phase has a measurable Definition of Done.
- Browser automation is excluded unless a documented capability gap justifies a separate adapter.

## 3. Target stack

- Python 3.14
- uv for Python/runtime/dependency management
- FastAPI for HTTP endpoints
- Pydantic v2 and pydantic-settings
- httpx AsyncClient
- SQLAlchemy 2
- PostgreSQL
- Alembic
- asyncio / TaskGroup
- pytest
- pytest-asyncio
- respx for HTTP client tests
- Ruff
- Pyright
- structured logging
- OpenTelemetry-compatible tracing
- Prometheus-compatible metrics
- Docker/Linux for deployment

Redis is intentionally deferred until a concrete distributed coordination or cache requirement exists.

## 4. Target repository layout

~~~
src/threads_platform/
  bootstrap/
  config/
  domain/
    accounts/
    publishing/
    conversations/
    discovery/
    analytics/
    commands/
  application/
    commands/
    queries/
    services/
    policies/
  infrastructure/
    threads_api/
    crm/
    persistence/
    security/
    messaging/
  transport/
    http/
    websocket/
  workers/
  observability/

tests/
  unit/
  integration/
  contract/
  e2e/

migrations/
docs/
  architecture/
  adr/
  protocols/
  operations/
~~~

Dependency direction:

transport -> application -> domain

Infrastructure implements ports/interfaces used by application/domain. Domain code must not import FastAPI, HTTPX, SQLAlchemy, WebSocket libraries, or Meta-specific response models.

## 5. Phase 0 — Security and capability baseline

### Deliverables

- Remove active credentials from tracked documentation.
- Rotate any credential that may have been exposed in the legacy public report.
- Create .env.example with placeholders only when implementation starts.
- Verify current official Threads API capabilities required by the parity matrix.
- Record capability gaps as explicit decisions, not assumptions.
- Confirm Meta app ownership, OAuth redirect strategy and required permissions.
- Confirm target CRM protocol requirements.

### Exit gate

No active secret is present in the working tree. Required official API capabilities have evidence links or are marked GAP / VERIFY.

## 6. Phase 1 — Project foundation

### Deliverables

- uv project initialized.
- Python constraint set to >=3.14,<3.15.
- src layout.
- pyproject.toml and uv.lock.
- Ruff, Pyright, pytest configuration.
- Settings model with environment validation.
- Structured logging foundation.
- FastAPI health endpoint.
- CI workflow for sync, lint, type-check and tests.
- Docker development/runtime skeleton.

### Mandatory local commands

~~~
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
~~~

### Exit gate

A fresh clone can execute all required commands without undocumented manual steps.

## 7. Phase 2 — Threads API integration spike

Purpose: validate external contracts before building the complete application.

### Deliverables

Using a dedicated development Threads account:

- OAuth authorization flow.
- Exchange authorization code for token.
- Long-lived token handling and refresh.
- Retrieve own Threads identity/profile.
- Publish a text post.
- Publish an image post.
- Publish a video post if the environment is available.
- Retrieve published media.
- Reply to a post/reply.
- Retrieve replies/conversation.
- Query current publishing limits where supported.
- Verify required scopes and error shapes.

Spike code may be temporary but findings must be converted into contract tests and ADR updates.

### Exit gate

We have real request/response evidence for the minimum API workflows and know the exact token lifecycle.

## 8. Phase 3 — Domain and persistence foundation

### Domain models

- ThreadsAccount
- OAuthCredential
- Command
- CommandAttempt
- ThreadPost
- ThreadReply
- Schedule
- SyncState
- SyncRun
- InsightSnapshot
- OutboxEvent
- IntegrationDelivery

### Persistence

PostgreSQL becomes the sole durable source of truth.

Required unique constraints:

- commands.command_id
- posts(account_id, threads_post_id)
- replies(account_id, threads_reply_id)
- outbox event id

### Required behavior

- Migrations are Alembic-based.
- Repository interfaces live outside infrastructure implementation details.
- Command lifecycle is durable.
- Outbox insert occurs in the same database transaction as business state change.

### Exit gate

Crash/restart tests prove state can be recovered without duplicate durable records.

## 9. Phase 4 — Command runtime and CRM protocol v1

### Envelope

Every inbound command must include:

- protocol_version
- command_id
- correlation_id
- command_type
- account_id
- created_at
- deadline where applicable
- payload

### Command lifecycle

RECEIVED -> VALIDATED -> PROCESSING -> SUCCEEDED

Alternate terminal/intermediate states:

- REJECTED
- EXPIRED
- FAILED_RETRYABLE
- FAILED_FINAL

### Reliability

- Duplicate command_id returns prior outcome and never repeats the side effect.
- Inbox pattern handles at-least-once inbound delivery.
- Outbox pattern handles reliable outbound delivery.
- Transport can be HTTP or WebSocket without changing handlers.

### Exit gate

A simulated reconnect/resend cannot create a duplicate publish/reply.

## 10. Phase 5 — Publishing

### Scope

- text
- image
- video
- carousel
- quote post where supported
- repost where supported
- reply controls and supported Threads-native metadata
- media/container status
- permalink/media retrieval
- publishing quota awareness

### State machine

RECEIVED
-> VALIDATED
-> CONTAINER_CREATED when applicable
-> READY
-> PUBLISHED
-> PERSISTED
-> RESULT_QUEUED
-> COMPLETED

Persist recovery anchors such as container_id immediately after obtaining them.

### Required failure tests

- crash after container creation
- crash after remote publish but before CRM result
- Meta timeout
- Meta 5xx
- rate limit
- invalid media
- expired/invalid token
- duplicate command

### Exit gate

The same command never produces two posts even across process crashes and retries.

## 11. Phase 6 — Conversations and moderation

### Scope

- sync top-level replies
- sync nested conversation
- create reply
- reply to reply
- hide/unhide where supported
- approve/ignore pending replies where supported
- reply controls
- pagination/cursors
- incremental sync

### Data rule

Replies are stored relationally using parent_reply_id/root_post_id. Do not store a giant mutable nested JSON document as the source of truth.

### Exit gate

Repeated sync is deterministic and produces no duplicate replies. Parent-child mapping is stable.

## 12. Phase 7 — Discovery, mentions and analytics

### Discovery

Where officially supported:

- keyword search
- topic/tag search
- public profile/media retrieval
- account mentions

Every discovery feature must record its API constraints and pagination semantics.

### Analytics

Insight data is stored as snapshots, not overwritten counters:

- entity
- metric
- value
- captured_at

### Exit gate

Sync can resume after interruption and analytics history remains queryable over time.

## 13. Phase 8 — Scheduling and workers

No while-true + sleep business scheduler.

Durable job fields:

- job_id
- job_type
- account_id
- scheduled_at
- status
- attempts
- next_retry_at
- deadline

Worker responsibilities:

- scheduled publishing
- token refresh
- conversation sync
- insights sync
- discovery jobs
- outbox delivery

### Exit gate

Restarting the process does not lose or double-run a due job.

## 14. Phase 9 — Observability and production hardening

### Logs

Every important log should carry:

- request_id
- command_id
- correlation_id
- account_id
- operation
- attempt
- duration

Tokens/secrets/Authorization headers must be redacted.

### Metrics

At minimum:

- commands_received_total
- commands_failed_total
- command_duration_seconds
- threads_api_requests_total
- threads_api_errors_total
- threads_api_latency_seconds
- posts_published_total
- replies_created_total
- outbox_pending_total
- accounts_reauth_required
- quota_remaining where measurable

### Deployment

Start with:

- one API service
- one worker service
- PostgreSQL
- Linux/Docker

Scale replicas only after account-level coordination semantics are proven.

## 15. Rollout sequence

1 development account
-> 3 accounts
-> 10 accounts
-> wider rollout

At each stage verify:

- duplicate rate = 0 for idempotent commands
- queue backlog behavior
- token refresh reliability
- rate limit handling
- DB connection pressure
- external API latency/error distribution

## 16. Definition of Done for any implementation issue

An issue is not complete until:

- acceptance criteria pass
- tests are added or updated
- lint/type-check pass
- migrations are included when schema changes
- logs do not contain secrets
- external API assumptions are cited in PR notes
- failure/retry behavior is tested where applicable
- docs/ADR updated for architectural changes
- no unrelated refactor is bundled
- PR includes exact verification commands and results

## 17. Explicit non-goals for the initial architecture

- Selenium/Playwright as a core dependency.
- Chrome profile farms.
- storing usernames/passwords for login automation.
- anti-detect behavior.
- JSON files as databases.
- per-account cloned Python entrypoints.
- premature microservices.
- Redis without a demonstrated need.

Any future exception requires an ADR.
