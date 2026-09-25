# Codex Execution Guide v2

## 1. First rule for a fresh Codex session

Before coding, read in order:

1. README.md
2. docs/PROJECT_STATE_HANDOFF.md
3. docs/MASTER_PLAN.md
4. docs/ARCHITECTURE.md
5. docs/FEATURE_PARITY_MATRIX.md
6. docs/WORK_BREAKDOWN.md
7. docs/CODEX_EXECUTION_GUIDE.md
8. docs/ACCEPTANCE_AND_REVIEW.md
9. applicable docs/adr/*
10. applicable docs/protocols/*
11. every authorized GitHub issue and coordinator comment

Do not use the deleted legacy Facebook report as implementation architecture.

## 2. Authorization boundary

The roadmap is not authorization.

Implement only the issue/batch explicitly handed off by the coordinator.

Do not start the next C-stage because earlier work appears complete.

## 3. Batch discipline

For an authorized batch:
- use the requested branch;
- implement in dependency order;
- keep issue-level commits/commit groups;
- run verification at boundaries;
- continue without intermediate review only inside the authorized batch;
- stop at the designated checkpoint PR.

Do not self-merge.

## 4. Architecture rules

Always preserve:
- transport -> application -> domain;
- infrastructure/worker adapters implement ports;
- PostgreSQL source of truth;
- Command = business intent;
- WorkerJob = remote execution assignment;
- leases/fencing for distributed ownership;
- typed bounded failures/retries;
- no secrets in logs/Git.

Domain must not import FastAPI, httpx, SQLAlchemy, WebSocket libs, browser libs, Windows APIs, or Meta DTOs.

## 5. Distributed-worker rules

For C1+:
- WebSocket is notification/presence only;
- durable state mutation uses the agreed Worker protocol;
- wrong worker/account assignment must fail;
- stale lease must fail;
- worker OFFLINE/DRAINING/UPGRADE_REQUIRED cannot claim;
- hostname is not worker identity;
- no automatic profile migration;
- strict-online behavior applies;
- worker local journal is recovery metadata only.

## 6. Browser rules

Browser implementation is not allowed before C3.

When authorized:
- browser is an infrastructure/worker adapter;
- every action corresponds to an explicit capability;
- UI contract mismatch fails closed;
- no random selector fallback;
- no anti-detect/fingerprint spoofing;
- no automated challenge/2FA bypass;
- no plaintext account-password model by default;
- mutation requires checkpoint/recovery design;
- lease loss prevents new irreversible actions.

## 7. External Threads API rules

- verify current official Meta docs/changelog before changing external contract code;
- documentation fixtures are labeled documentation-contract;
- live fixtures require scrubbed live evidence;
- issue #3 remains the live production gate;
- do not invent undocumented response behavior.

## 8. Dependencies

A new runtime dependency needs written justification in PR.

Requires coordinator decision:
- Redis/message broker;
- microservices;
- primary DB change;
- new browser execution architecture;
- automatic profile migration;
- OAuth/security-model change;
- fleet-wide secret scheme;
- breaking CRM/Worker protocol change.

## 9. Standard verification

~~~bash
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run alembic check
uv run pytest
~~~

DB changes also require migration/integration verification.
Distributed changes require concurrent-claim, stale-lease, reconnect/restart, wrong-worker/affinity and protocol-version tests.
Browser changes require adapter-contract, UI-mismatch, session/intervention and safe-boundary/lease-loss tests.

## 10. PR format

Include Summary, Issue/commit map, Architecture, Data/migrations, Protocol, Failure/recovery, Verification, Security, External evidence, Risks/follow-ups.

## 11. Stop conditions

Stop the affected work and report when:
- requirements conflict with ADR/source-of-truth;
- safe recovery cannot be proven;
- production credential/data is required;
- browser is required before C3;
- platform UI/API behavior materially differs from contract;
- implementation would need evasion/bypass behavior;
- quality gates cannot pass without weakening standards.

Partial completion of the authorized batch is preferable to architecture improvisation.