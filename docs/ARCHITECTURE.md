# Target Architecture

## 1. Architectural style

Use a modular monolith with ports/adapters boundaries.

Why:

- current team/project size does not justify microservice overhead;
- business modules remain independently testable;
- infrastructure can change without rewriting domain logic;
- future extraction is possible if scale proves necessary.

## 2. Logical layers

### Domain

Pure business concepts and invariants.

Must not import:

- FastAPI
- httpx
- SQLAlchemy
- WebSocket libraries
- Meta API response DTOs
- environment/config objects

### Application

Use cases and orchestration:

- command handlers
- queries
- policies
- transaction boundaries
- account coordination

### Infrastructure

Adapters:

- Threads API
- PostgreSQL
- CRM
- token encryption
- messaging transport implementations

### Transport

Ingress/egress protocol adaptation:

- FastAPI
- WebSocket
- CLI/admin endpoints where required

Routes parse/authenticate/validate/dispatch only.

## 3. Core modules

### accounts

Responsibilities:

- Threads account identity
- authorization state
- scopes
- encrypted credential reference
- reauthorization status
- token lifecycle state

### publishing

Responsibilities:

- publish state machine
- container lifecycle
- media metadata
- quota policy
- recovery after partial external success

### conversations

Responsibilities:

- reply model
- reply hierarchy
- conversation sync
- moderation actions
- cursor state

### discovery

Responsibilities:

- keyword/topic discovery where officially supported
- mentions
- public profile/media retrieval where supported
- pagination

### analytics

Responsibilities:

- metrics/insights retrieval
- snapshot persistence
- time-series query model

### commands

Responsibilities:

- command schema
- idempotency
- deadlines
- lifecycle
- attempts
- errors

## 4. Command execution model

Inbound transport writes/deduplicates command -> application worker claims command -> handler performs use case -> business state and outbox are committed together -> outbox worker delivers result.

Never let WebSocket connection state be the source of truth for business processing.

## 5. Concurrency

Most work is I/O-bound and should use asyncio.

Use bounded concurrency. Do not create an unbounded task for every inbound event.

Per-account serialization is required for operations that may conflict:

- token refresh
- certain publish sequences
- cursor/sync updates

Initial single-process lock can use asyncio.Lock, but multi-worker deployment must rely on a durable/shared coordination mechanism such as PostgreSQL advisory locks or a lease table.

## 6. Persistence rules

PostgreSQL is authoritative.

Do not persist mutable business state in JSON files.

Suggested tables:

- threads_accounts
- oauth_credentials
- commands
- command_attempts
- posts
- replies
- schedules
- sync_states
- sync_runs
- insight_snapshots
- outbox_events
- integration_deliveries
- audit_events

Use UTC timestamps.

Store external IDs as strings unless official constraints justify another representation.

## 7. Idempotency

Side-effecting operations must have a stable idempotency key.

Primary mechanism: CRM command_id.

If a direct API use case does not originate from CRM, generate an operation_id before the first external side effect and persist it.

A retry must resume/reconcile; it must not blindly restart remote work.

## 8. Publishing recovery

Persist intermediate external references such as container_id.

On restart:

- inspect durable command state;
- query remote state when necessary;
- reconcile;
- continue from the last safe state.

Never assume a timeout means the remote operation failed.

## 9. Inbox/outbox

Inbox:

- deduplicate inbound commands;
- store receipt/lifecycle;
- enable retry after process crash.

Outbox:

- store result/event in same transaction as business data;
- deliver asynchronously to CRM;
- retry independently;
- record attempts/delivery status.

## 10. API client rules

Use one long-lived httpx.AsyncClient per process/application lifetime.

Centralize:

- base URL
- API version
- auth header generation
- timeout
- response decoding
- error mapping
- retry metadata
- request correlation
- secret redaction

Do not expose raw Meta response objects to the domain layer.

## 11. Error taxonomy

Define typed errors, at minimum:

- ThreadsRateLimited
- ThreadsUnavailable
- ThreadsAuthExpired
- ThreadsPermissionDenied
- ThreadsValidationError
- ThreadsNotFound
- CRMUnavailable
- DatabaseUnavailable
- DuplicateCommand
- CommandExpired
- InvalidCommand
- ReauthorizationRequired

Retry policy is based on error class, not a generic Exception catch.

## 12. Retry

For retryable network/service failures:

- exponential backoff
- jitter
- max attempts
- total deadline

Honor server-provided retry hints where available.

No infinite retries.

## 13. OAuth/token security

- encrypted at rest
- never logged
- never returned to normal clients
- never stored in Git
- redaction on exception/HTTP traces
- explicit status for reauthorization required

Separate account identity from credential material.

## 14. Observability

Every command/request should carry:

- request_id
- correlation_id
- command_id
- account_id

Logs are structured.

Trace external calls and DB operations.

Record audit events separately from operational logs for sensitive actions.

## 15. Browser automation boundary

Core dependency graph contains no Selenium or Playwright.

If future feature X has no official API:

1. document the business need;
2. prove the official API gap;
3. assess stability/security/platform constraints;
4. create ADR;
5. if approved, implement an isolated adapter/service.

Browser implementation must not change core domain models merely to expose DOM details.

## 16. Architecture fitness checks

PR review should reject:

- domain importing infrastructure;
- large god-service classes;
- route handlers containing business logic;
- direct SQL in transport layer;
- direct httpx calls in command handlers if a port exists;
- global mutable runtime state;
- hardcoded secrets/endpoints/quotas;
- catch-all retry loops;
- non-idempotent side effects without recovery design.
