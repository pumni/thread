---
name: Codex implementation task
about: Bounded task for an authorized project checkpoint
title: "[C?-??] "
labels: ""
assignees: ""
---

## Context

## Objective

## Context / source of truth

- Root `AGENTS.md`
- `docs/PROJECT_STATE_HANDOFF.md`
- Relevant task row in `docs/CONTEXT_MAP.md`
- Applicable ADR/protocol/design source:

## In scope

-

## Out of scope

-

## Invariants / technical constraints

-

## Deliverables

-

## Acceptance criteria

- [ ]
- [ ] Failure/recovery tests added where relevant.
- [ ] No secrets introduced.
- [ ] Architecture docs updated if the accepted contract changes.

## Verification

~~~
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run alembic check
uv run pytest
~~~

Additional DB/distributed/browser tests:

## Dependencies

## Stop conditions

## Notes for reviewer