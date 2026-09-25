# Codex Execution Guide

This document defines how implementation work is handed to Codex running on the developer machine.

## 1. Unit of work

Codex receives ONE GitHub issue at a time unless the issue explicitly declares a dependency bundle.

Do not ask Codex to "implement the entire project".

Each issue must contain:

- Context
- Objective
- In scope
- Out of scope
- Technical constraints
- Deliverables
- Acceptance criteria
- Verification commands
- Dependencies
- Relevant docs/ADR links

## 2. Branch convention

Use:

feature/<issue-number>-short-name
fix/<issue-number>-short-name
chore/<issue-number>-short-name

One issue should normally produce one PR.

## 3. Before coding

Codex must:

1. read README.md;
2. read MASTER_PLAN.md;
3. read ARCHITECTURE.md;
4. read the relevant ADRs;
5. read the complete issue;
6. inspect current code before proposing new abstractions;
7. verify any external Threads API behavior required by the issue against current official documentation.

If an issue conflicts with an ADR, stop and report the conflict in the PR rather than silently rewriting architecture.

## 4. Implementation rules

- Prefer the smallest design that satisfies current requirements and architecture boundaries.
- No speculative framework.
- No new runtime dependency without explaining why it is necessary.
- No secret or real access token in tests/fixtures.
- No network calls in unit tests.
- Do not weaken typing/test gates to make CI pass.
- Do not skip migration for schema changes.
- Avoid broad formatting/refactor unrelated to the issue.
- Preserve backward compatibility of an established internal protocol unless the issue explicitly changes it.

## 5. Test requirements

At minimum, Codex runs:

~~~
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
~~~

For DB changes, run relevant integration tests/migrations.

For external Threads API integrations, unit tests must mock HTTP behavior and an integration test/manual evidence must be documented separately when real credentials are required.

## 6. Failure-path requirement

Any issue that performs an external side effect must test failure behavior.

Examples:

- timeout
- duplicate command
- retryable server error
- authorization failure
- validation error
- partial success
- restart/recovery state

Happy-path-only work is not accepted for publishing, CRM delivery, token handling or scheduling.

## 7. PR description expected from Codex

PR must include:

### Summary

What changed and why.

### Scope

Files/modules changed.

### Design

Important choices and how they respect existing ADRs.

### Verification

Exact commands run and their outcomes.

### Failure cases tested

List them.

### External assumptions

Current API documentation/capability assumptions.

### Risks / follow-ups

Anything intentionally deferred.

## 8. Handoff to acceptance reviewer

Once Codex opens a PR, do not self-merge.

The reviewer/coordinator will evaluate:

- scope compliance
- architecture boundaries
- data model/migrations
- error semantics
- idempotency/recovery
- tests
- security
- observability
- documentation
- regressions

Review outcomes:

- ACCEPTED
- ACCEPTED WITH FOLLOW-UP
- CHANGES REQUIRED
- BLOCKED BY PRODUCT/API DECISION

## 9. What Codex must not decide alone

Requires explicit architectural/product decision:

- adding browser automation
- switching primary database
- adding Redis/message broker
- splitting into microservices
- changing command protocol semantics
- removing idempotency guarantees
- changing OAuth/security model
- introducing account credential/password storage
- bypassing official API due to convenience

Create/propose an ADR instead.
