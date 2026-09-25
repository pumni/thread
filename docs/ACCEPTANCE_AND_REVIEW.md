# Acceptance and Review Protocol v2

## 1. Review order

1. authorized scope
2. behavior
3. architecture
4. data/protocol correctness
5. ownership/concurrency
6. failure/recovery
7. security
8. tests
9. observability
10. maintainability/docs

Earlier failures normally block acceptance.

## 2. Architecture gate

Reject/request changes when:
- domain imports infrastructure/browser/Windows dependencies;
- transport contains business logic;
- Command and WorkerJob are conflated;
- WebSocket becomes source of truth;
- worker owns authoritative business state;
- profile affinity can be bypassed;
- new broker/DB/microservice architecture appears without ADR;
- browser code appears before/without approved scope.

## 3. Data gate

For schema changes verify:
- Alembic migration;
- reversible where practical;
- UTC timestamps;
- unique/FK/check constraints enforce invariants;
- claim/routing indexes;
- external IDs typed appropriately;
- secrets excluded from ordinary business/loggable rows.

## 4. Command/API side-effect reliability

Verify duplicate safety, timeout ambiguity, bounded retry, crash recovery, stale command-lease fencing, and atomic business-result-outbox finalization.

## 5. WorkerJob reliability gate

Required where WorkerJob changes:
- concurrent claim -> one winner;
- wrong worker rejected;
- account affinity enforced;
- OFFLINE/DRAINING/UPGRADE_REQUIRED cannot claim;
- short claim transaction;
- lease renewal;
- stale checkpoint rejected;
- stale completion rejected;
- expired lease reclaim gets new token;
- WebSocket-loss recovery;
- worker reconnect;
- Control Plane restart;
- intervention/requeue;
- bounded attempts/deadline.

Any stale-owner overwrite is BLOCKER.

## 6. Worker authentication/security gate

Reject for:
- fleet-wide shared static password as final design;
- private worker key in Git/DB/log;
- replayable enrollment/challenge;
- non-expiring enrollment credential;
- worker token in logs;
- disabled TLS verification;
- secret-bearing diagnostic payload.

## 7. Browser gate

When browser work is authorized:
- browser library stays outside domain/application business types;
- capability is explicit/versioned;
- account/session requirement explicit;
- UI mismatch fails closed;
- challenge/login produces intervention;
- no anti-detect/fingerprint spoofing;
- no random human-emulation requirement;
- no click-unknown fallback;
- lease loss prevents new irreversible mutation;
- ambiguous outcome has reconciliation/operator path.

## 8. Account/session gate

Verify persistent affinity, logical profile_ref, no auto migration, execution mode, explicit session state, no plaintext-password default model, and NetworkProfile redaction.

## 9. Discovery gate

Verify API-first behavior, browser enrichment through Capability Router/WorkerJob only, pagination/resume, dedupe, source traceability and bounded lead data.

## 10. Scheduler/activity gate

Verify Control Plane-created activities, durable schedule, deterministic priority, cooperative safe-boundary preemption and restart recovery.

## 11. Standard quality gate

~~~bash
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run alembic check
uv run pytest
~~~

CI + Secret scan must be green for merge unless reviewer explicitly documents an infrastructure-only exception.

## 12. Severity

BLOCKER: secret leak, data corruption, duplicate external side effect, stale-owner write, broken migration, account-affinity violation, architecture/security boundary break.

MAJOR: incorrect state transition, missing recovery, incomplete concurrency tests, unsafe fallback, protocol mismatch not handled.

MINOR: limited observability/docs/maintainability issue.

NIT: style preference only.

## 13. Verdict

- ACCEPTED
- ACCEPTED WITH FOLLOW-UP
- CHANGES REQUIRED
- BLOCKED BY PRODUCT/API DECISION

Every review states criteria status, blocking findings, non-blocking findings, verification evidence and follow-ups. CI green alone is not acceptance.