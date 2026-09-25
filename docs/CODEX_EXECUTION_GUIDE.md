# Codex Execution Guide

This document defines how implementation work is handed to Codex running on the developer machine.

## 1. Unit of work

The default unit of implementation is a GitHub issue. The reviewer/coordinator may authorize a **dependency batch** containing multiple issues so Codex can continue through safe, linear work without waiting for review after every issue.

Do not interpret a batch as permission to implement the entire project.

Every issue still retains its own:
- Context
- Objective
- In scope
- Out of scope
- Technical constraints
- Deliverables
- Acceptance criteria
- Verification requirements
- Dependencies

When a batch is authorized, Codex must preserve traceability between each issue and its commits/tests.

## 2. Branch convention

Single-issue work:

- feature/<issue-number>-short-name
- fix/<issue-number>-short-name
- chore/<issue-number>-short-name

Authorized batch work:

- batch/<batch-name>

For a batch PR, keep each issue as a clearly identifiable commit or commit group and map them in the PR description.

Do not self-merge.

## 3. Before coding

Codex must:

1. read README.md;
2. read docs/MASTER_PLAN.md;
3. read docs/ARCHITECTURE.md;
4. read docs/WORK_BREAKDOWN.md;
5. read applicable ADRs;
6. read every issue authorized for the current batch;
7. inspect current code before proposing new abstractions;
8. verify current official Threads API behavior for any external integration work.

If implementation conflicts with an ADR or a stop condition in WORK_BREAKDOWN.md, stop and report the conflict instead of silently rewriting architecture.

## 4. Continuing inside a batch

Codex does not need reviewer approval between issue boundaries when:
- the next issue is explicitly part of the authorized batch;
- its declared dependencies have been implemented in the same branch;
- tests/quality gates are green;
- no stop condition has occurred.

After completing one issue inside the batch:

1. run relevant verification;
2. commit with a message referencing that issue;
3. record any important decision/failure case in the batch PR notes;
4. continue to the next authorized issue.

Do not expand beyond the authorized batch.

## 5. Implementation rules

- Prefer the smallest design that satisfies current requirements and architecture boundaries.
- No speculative framework.
- No new runtime dependency without explaining why it is necessary.
- No secret or real access token in tests/fixtures.
- No network calls in unit tests.
- Do not weaken typing/test gates to make CI pass.
- Do not skip migrations for schema changes.
- Avoid broad formatting/refactors unrelated to the authorized issues.
- Preserve established protocol compatibility unless an issue explicitly changes it.
- Keep domain logic independent of FastAPI, httpx, SQLAlchemy and Meta DTOs.
- Side-effecting commands require explicit idempotency/recovery design.

## 6. Test requirements

At minimum, after project bootstrap Codex runs:

~~~
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
~~~

For DB changes, run relevant integration tests and migration checks.

For external Threads API integrations:
- unit/contract tests mock HTTP behavior;
- real-account verification is recorded separately;
- real credentials must never enter Git, fixtures, logs or PR text.

For a batch, run the complete gate before presenting the checkpoint PR even if individual issue boundaries also ran narrower tests.

## 7. Failure-path requirement

Any issue that performs an external or durable side effect must test failure behavior.

Examples:
- timeout;
- duplicate command;
- retryable server error;
- authorization failure;
- validation error;
- partial success;
- restart/recovery state;
- concurrent claim/refresh behavior where relevant.

Happy-path-only work is not accepted for publishing, CRM delivery, token handling, migrations or scheduling.

## 8. PR description expected from Codex

For a batch PR include:

### Summary

What the batch establishes.

### Issue/commit map

For every issue:
- issue number;
- commit SHA(s);
- files/modules;
- acceptance criteria status.

### Architecture

Important decisions and applicable ADRs.

### Verification

Exact commands and outcomes.

### Migrations

Revision/order and how they were tested.

### Failure cases tested

List by issue.

### External assumptions

Current API documentation/capability evidence where applicable.

### Risks / follow-ups

Anything intentionally deferred.

## 9. Handoff to acceptance reviewer

At the designated checkpoint, stop and do not start the next batch.

The reviewer/coordinator evaluates:
- scope compliance;
- architecture boundaries;
- data model/migrations;
- error semantics;
- idempotency/recovery;
- tests;
- security;
- observability;
- documentation;
- regressions;
- cross-issue integration.

Review outcomes:
- ACCEPTED
- ACCEPTED WITH FOLLOW-UP
- CHANGES REQUIRED
- BLOCKED BY PRODUCT/API DECISION

## 10. What Codex must not decide alone

Requires explicit architectural/product decision:
- adding browser automation;
- switching primary database;
- adding Redis/message broker;
- splitting into microservices;
- changing established command protocol semantics outside issue scope;
- removing idempotency guarantees;
- changing OAuth/security model;
- introducing account credential/password storage;
- bypassing official API due to convenience.

Propose an ADR or stop at the checkpoint instead.
