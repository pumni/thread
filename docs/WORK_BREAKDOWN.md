# Work Breakdown and Codex Handoff Order

This file is the operational bridge between the architecture documents and GitHub issues.

## Handoff rule

Give Codex one issue at a time unless the reviewer explicitly authorizes a dependency bundle.

For every task:

1. Codex reads the issue and referenced architecture documents.
2. Codex creates an implementation branch.
3. Codex implements only the declared scope.
4. Codex runs all required verification commands.
5. Codex opens a PR using the repository template.
6. Reviewer/coordinator performs acceptance using docs/ACCEPTANCE_AND_REVIEW.md.
7. Only accepted work is merged.
8. Follow-up work is opened as a new issue rather than silently expanding the current PR.

## Recommended execution order

| Order | Issue | Purpose | Blocking dependency |
|---:|---|---|---|
| 0 | #1 TP-000 | Security remediation | None |
| 1 | #2 TP-001 | Python 3.14 + uv bootstrap | TP-000 risk addressed |
| 2 | #3 TP-002 | Real Threads API/OAuth spike | TP-001 |
| 3 | #4 TP-003 | Domain + PostgreSQL foundation | TP-001 |
| 4 | #5 TP-004 | Command runtime + inbox/outbox + CRM protocol | TP-003 |
| 5 | #6 TP-005 | Publishing pipeline | TP-002 + TP-003 + TP-004 |
| 6 | #7 TP-006 | Replies/conversation/moderation | TP-002 + TP-003 + TP-004 |
| 7 | #8 TP-007 | Discovery/mentions/insights | TP-002 + TP-003 |
| 8 | #9 TP-008 | Durable scheduler/workers | TP-003 + TP-004 |
| 9 | #10 TP-009 | Observability/security/deployment hardening | Functional core available |
| 10 | #11 TP-010 | End-to-end release certification | MVP issues complete |

## Parallelism allowed

After TP-003 and TP-004 establish stable contracts, some work may run in parallel:

- TP-005 publishing
- TP-006 conversations
- TP-007 discovery/analytics

However, do not parallelize changes that modify the same command envelope, account model or migration contract without reviewer coordination.

## Review checkpoints

### Checkpoint A — Foundation

Issues #1-#4.

Reviewer validates:

- no active secret in repository;
- uv workflow reproducible;
- real Threads API constraints captured;
- domain does not depend on infrastructure;
- migrations are sound.

### Checkpoint B — Reliability backbone

Issue #5.

Reviewer validates:

- command idempotency;
- durable lifecycle;
- inbox/outbox;
- retry/deadline behavior;
- CRM disconnect/reconnect behavior.

Do not proceed to production side effects until this checkpoint passes.

### Checkpoint C — Core product parity

Issues #6-#8.

Reviewer validates:

- publishing is crash-safe;
- replies/conversation sync is deduplicated;
- Threads-native capabilities replace legacy scraping where available;
- unsupported product features remain explicit gaps rather than hidden browser workarounds.

### Checkpoint D — Operations

Issues #9-#10.

Reviewer validates:

- durable scheduling;
- bounded concurrency;
- token refresh;
- metrics/tracing/log redaction;
- deployment/recovery procedure.

### Checkpoint E — Release

Issue #11.

Reviewer compares implementation against docs/FEATURE_PARITY_MATRIX.md and issues a release verdict.

## Codex stop conditions

Codex should stop and surface the decision instead of improvising when:

- official Threads API does not provide a required capability;
- a new browser automation dependency appears necessary;
- an existing ADR must be contradicted;
- a schema/protocol change would break already merged modules;
- real credentials or production data are needed to proceed;
- external API behavior differs materially from the parity matrix.

These are coordinator decisions, not implementation details.

## What the reviewer will request from Codex after each PR

- exact commit/PR scope;
- test output;
- changed schema/migrations;
- external API assumptions;
- failure cases exercised;
- known limitations;
- security-sensitive handling;
- any proposed follow-up issues.

This keeps implementation and acceptance auditable throughout the project.
