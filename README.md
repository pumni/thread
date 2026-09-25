# Distributed Hybrid Threads Operations Tool

A greenfield Python platform for durable multi-account Threads operations across a centralized Control Plane and multiple Worker machines.

## Current state

Completed:
- Batch A — Python/PostgreSQL/command foundation.
- TP-004A — durable command leases/checkpoints/recovery.
- Batch B — official-documentation-based Threads publishing, replies, conversation sync and moderation core.

Next implementation milestone:
- **C1 — Distributed Worker Foundation (#21, #22, #23)**.

Not production-ready:
- issue #3 live Meta OAuth/API validation remains open;
- browser execution is planned but not implemented yet;
- no C1+ implementation should be assumed from architecture docs alone.

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

## Source of truth

Read in this order:

1. docs/PROJECT_STATE_HANDOFF.md — current state and next action for a new session.
2. docs/MASTER_PLAN.md — product decisions and C1-C6 roadmap.
3. docs/ARCHITECTURE.md — target runtime/data/execution architecture.
4. docs/FEATURE_PARITY_MATRIX.md — capability/executor matrix.
5. docs/WORK_BREAKDOWN.md — exact issue order/checkpoints.
6. docs/protocols/WORKER_PROTOCOL_V1.md — C1 worker protocol.
7. docs/CODEX_EXECUTION_GUIDE.md — implementation rules.
8. docs/ACCEPTANCE_AND_REVIEW.md — reviewer gates.
9. docs/adr/ — accepted architectural decisions.

The legacy Facebook report is not part of the repository source of truth.

## Required quality gate

~~~bash
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run alembic check
uv run pytest
~~~

## Delivery workflow

Architecture/issue -> authorized batch -> implementation branch -> Codex -> quality gate -> checkpoint PR -> coordinator acceptance -> merge.

Codex must not start the next checkpoint batch without explicit coordinator authorization.

## External documentation rule

Before changing a Threads API contract, verify current official Meta Threads developer documentation/changelog. Repository documentation may describe implementation intent and previously reviewed contracts, but external API behavior can change.