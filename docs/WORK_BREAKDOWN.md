# Work Breakdown and Codex Handoff Order

This file is the operational bridge between architecture documents and GitHub implementation work.

## Delivery mode

The project uses **batched checkpoints** rather than mandatory review after every issue.

A batch is allowed only when:
- issue dependencies are linear and understood;
- no unresolved architectural decision blocks the next issue;
- Codex keeps commit boundaries clear by issue;
- acceptance criteria remain traceable to each issue;
- Codex stops immediately on any stop condition listed below.

The reviewer/coordinator may authorize either:
1. one batch branch/PR containing multiple issue-scoped commits; or
2. stacked PRs that Codex continues to build before the next acceptance checkpoint.

For the current project phase, prefer **one batch branch/PR with clear commits per issue** unless the diff becomes too large to review safely.

## Standard batch workflow

1. Read README.md, MASTER_PLAN.md, ARCHITECTURE.md, applicable ADRs, and every issue in the authorized batch.
2. Create the authorized batch branch.
3. Implement issues in dependency order.
4. Keep each issue as a logically separate commit or tightly grouped commit series.
5. Run the full quality gate after each issue boundary when possible.
6. If the next issue depends only on work already completed in the same batch and no stop condition is hit, continue without waiting for reviewer approval.
7. Open/update one PR for the batch and map each issue to its commits, files, tests, and acceptance criteria.
8. Stop at the designated checkpoint.
9. Reviewer/coordinator accepts, requests changes, or blocks the batch.
10. Only accepted work is merged.

## Current execution plan

| Batch | Issues | Purpose | Review checkpoint |
|---|---|---|---|
| Security | #1 TP-000 | Legacy credential remediation | Implementation merged; owner credential-status follow-up remains open |
| A — Foundation | #2 TP-001, #4 TP-003, #5 TP-004 | Python/uv foundation, domain + PostgreSQL, reliable command runtime | Review after all three are implemented |
| B — Threads Core | #3 TP-002, #6 TP-005, #7 TP-006 | Real Threads API validation, publishing, conversations/moderation | Review after core API workflows are implemented |
| C — Expansion & Operations | #8 TP-007, #9 TP-008, #10 TP-009 | Discovery/insights, durable workers, production hardening | Review after all three are implemented |
| D — Release | #11 TP-010 | End-to-end certification and parity review | Final release review |

## Batch A — Foundation

Execution order:

1. #2 TP-001 — Python 3.14 + uv bootstrap.
2. #4 TP-003 — domain model + PostgreSQL persistence.
3. #5 TP-004 — command runtime + idempotency + inbox/outbox + CRM protocol v1.

Codex may continue from #2 to #4 to #5 without waiting for intermediate review if:
- all required verification commands remain green;
- package/dependency boundaries match ARCHITECTURE.md;
- no major dependency or architecture change is needed;
- migrations remain coherent;
- no secret is introduced.

Checkpoint A reviewer validates:
- uv workflow reproducible from a fresh clone;
- domain does not depend on infrastructure;
- migrations apply from an empty database;
- command lifecycle is durable;
- command idempotency works;
- inbox/outbox transactional semantics are correct;
- CRM transport does not own business state;
- retry/deadline behavior is typed and bounded.

No production Threads side effects should be implemented before Batch A passes review.

## Batch B — Threads Core

Execution order:

1. #3 TP-002 — validate real Threads OAuth/API capabilities.
2. #6 TP-005 — publishing pipeline.
3. #7 TP-006 — replies/conversation/moderation.

Continuation rule:
- If the API spike confirms the expected contracts, Codex may continue directly into publishing and conversations.
- If official API behavior materially differs from FEATURE_PARITY_MATRIX.md, required permissions are unavailable, product UI capability is not exposed through the official API, or OAuth/token lifecycle assumptions are wrong, **stop after TP-002** and report the mismatch before implementing workarounds.

Checkpoint B reviewer validates:
- real API evidence and current permissions;
- token lifecycle;
- crash-safe publishing;
- duplicate-command protection;
- timeout/reconciliation behavior;
- deterministic conversation synchronization;
- no browser automation was introduced without an approved ADR.

## Batch C — Expansion & Operations

Execution order:

1. #8 TP-007 — discovery, mentions and insights.
2. #9 TP-008 — durable scheduler/background workers.
3. #10 TP-009 — observability, security hardening and deployment.

Codex may continue through the batch while all previously established contracts remain stable.

Checkpoint C reviewer validates:
- cursor/resume semantics;
- historical insight snapshots;
- durable job claiming and restart behavior;
- bounded concurrency;
- token refresh coordination;
- metrics/tracing/log redaction;
- deployment and recovery procedures.

## Batch D — Release

Issue #11 performs end-to-end certification.

No scope expansion is allowed here. Any discovered defect becomes a focused issue unless it blocks certification.

## Parallelism

Within a batch, prefer dependency-order implementation over uncontrolled parallel edits.

Parallel work is permitted only when modules do not modify the same:
- command envelope;
- account model;
- migration chain;
- shared protocol;
- configuration contract.

If Codex has multiple worktrees/agents available, it may parallelize independent test/docs/adapters after shared contracts are stable, but one integration owner must reconcile them before the checkpoint PR is presented.

## Codex stop conditions

Codex must stop and surface the decision instead of improvising when:
- official Threads API does not provide a required capability;
- a browser automation dependency appears necessary;
- an existing ADR must be contradicted;
- a new major runtime dependency is required beyond the approved stack;
- a schema/protocol change would invalidate work already completed in the batch;
- real production credentials/data are required to continue;
- external API behavior materially differs from the parity matrix;
- quality gates cannot be restored without weakening tests, typing, linting, migrations or security;
- a migration would require destructive production assumptions not already approved.

These are coordinator decisions, not implementation details.

## Evidence required at each checkpoint

Codex must report:
- batch branch and PR number;
- commit mapping by issue;
- files/modules changed by issue;
- migration revisions;
- exact verification commands and results;
- GitHub Actions state;
- external API evidence/assumptions where relevant;
- failure/recovery cases tested;
- known limitations;
- security-sensitive handling;
- any deferred follow-up issues.

This keeps development fast without sacrificing auditable acceptance.
