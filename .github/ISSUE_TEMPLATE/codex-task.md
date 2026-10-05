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

- Root `AGENTS.md` (and nearest nested `AGENTS.md` if applicable)
- Relevant task route in `docs/CONTEXT_MAP.md`
- Matching specialized skill in `.agents/skills/` (if triggered)
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