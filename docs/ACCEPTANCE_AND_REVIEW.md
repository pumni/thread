# Acceptance and Review Protocol

The reviewer/coordinator is responsible for deciding whether a Codex implementation satisfies the issue and project architecture.

## 1. Acceptance order

Review in this order:

1. issue scope
2. behavior
3. architecture
4. data correctness
5. failure/recovery semantics
6. security
7. tests
8. observability
9. maintainability
10. documentation

A PR that fails an earlier gate should normally be returned before polishing later concerns.

## 2. Scope gate

Confirm:

- every acceptance criterion is addressed;
- no required deliverable is omitted;
- unrelated refactors are absent;
- out-of-scope features have not been smuggled in.

## 3. Architecture gate

Reject or request change when:

- domain imports infrastructure;
- transport contains business logic;
- Meta response DTOs leak into domain;
- global state controls business lifecycle;
- a god service owns unrelated capabilities;
- a new major dependency lacks justification;
- browser automation appears without approved ADR.

## 4. Data gate

For schema changes verify:

- Alembic migration exists;
- migration is reversible where practical;
- uniqueness constraints enforce idempotency/deduplication;
- external IDs have appropriate types;
- timestamps are UTC;
- indexes support expected queries;
- no credential appears in ordinary business tables/loggable models.

## 5. Reliability gate

For external side effects verify:

- duplicate delivery is safe;
- timeout does not imply assumed failure;
- retry classification is explicit;
- retry has bounded attempts/deadline;
- partial success can be reconciled;
- crash after remote success cannot cause blind duplicate execution.

## 6. Security gate

Reject immediately for:

- committed access token/password/secret;
- token in logs;
- Authorization header in test snapshot;
- plaintext production credential storage;
- disabled TLS verification without an approved development-only rationale;
- overly broad OAuth scope without reason.

## 7. Test gate

Required standard commands:

~~~
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
~~~

Additional tests depend on issue type.

### Publishing

- success
- duplicate command
- timeout
- server error
- invalid input
- auth failure
- partial/recovery state

### Conversation sync

- pagination
- nested parent mapping
- repeated sync
- cursor resume
- deleted/missing remote object behavior where applicable

### Scheduler

- restart durability
- duplicate claim protection
- overdue/expired job behavior
- retry timing

### OAuth

- token refresh success
- refresh failure
- reauth required
- concurrent refresh protection
- redaction

## 8. Observability gate

Important operations should expose enough context to diagnose failures without secrets.

Expected context where relevant:

- command_id
- correlation_id
- account_id
- operation
- attempt
- duration

## 9. Review severity

### BLOCKER

Security leak, data corruption, duplicate side effects, broken migration, architectural violation that will spread.

### MAJOR

Incorrect behavior, missing failure handling, incomplete tests, bad API abstraction.

### MINOR

Naming, maintainability, small observability/documentation gap.

### NIT

Style preference with no meaningful correctness/maintenance impact.

## 10. Acceptance output

Each reviewed PR should receive a concise verdict containing:

- Verdict
- Acceptance criteria status
- Blocking findings
- Non-blocking findings
- Verification evidence
- Follow-up issue(s), if any

No PR is considered done solely because CI is green.

## 11. Regression responsibility

When a bug is discovered after merge:

1. create a focused regression issue;
2. write a failing regression test first where feasible;
3. implement the fix;
4. evaluate whether the original acceptance checklist needs strengthening.
