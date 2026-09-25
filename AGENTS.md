# AGENTS.md

This file is the repository-wide map for coding agents. Keep it short. Detailed knowledge belongs in `docs/`.

## Project

`pumni/thread` is a Distributed Hybrid Threads Operations Tool:
- centralized Control Plane + PostgreSQL source of truth;
- official Threads API where it is the best supported executor;
- distributed Windows-first Worker Agents for approved local/browser capabilities;
- human intervention as a valid execution outcome.

Do not infer current milestone/status from this file. Read `docs/PROJECT_STATE_HANDOFF.md`.

## Start every task

1. Read `docs/PROJECT_STATE_HANDOFF.md`.
2. Read the authorized GitHub issue/batch and coordinator comments.
3. Use `docs/CONTEXT_MAP.md` to load only the docs relevant to the task.
4. Inspect the current implementation/tests before designing new abstractions.
5. If external Threads behavior matters, verify current official Meta docs/changelog.

Do not preload every document in `docs/` unless the task genuinely spans the whole architecture.

## Non-negotiable invariants

- PostgreSQL is authoritative business state.
- `Command` is business intent; `WorkerJob` is remote execution. Do not collapse them.
- WorkerJob has independent lease/fencing/checkpoint semantics.
- WebSocket is notification/presence, never the durable queue or source of truth.
- Browser accounts use persistent account -> worker/profile affinity; no automatic profile migration.
- Each account has its own execution mode: `API_ONLY`, `BROWSER_ONLY`, `HYBRID`, or `MANUAL`.
- Workers do not invent business actions; Control Plane/Scheduler creates them.
- API-first means preferred executor where suitable, not API-only architecture.
- Browser automation is allowed only in the approved Worker/browser boundary and only from C3 onward.
- Login/session challenges require intervention; do not bypass them.
- No anti-detect, fingerprint spoofing, or human-emulation-for-evasion subsystem.
- Domain code must not import FastAPI, httpx, SQLAlchemy, WebSocket/browser libraries, Windows APIs, or Meta DTOs.

## Change discipline

- Implement only the explicitly authorized issue/batch.
- Prefer the smallest coherent change that satisfies the contract.
- Reuse existing ports/state machines/patterns before introducing new frameworks.
- Do not add Redis, a broker, microservices, automatic profile migration, or a new auth model without an approved architecture decision.
- Schema changes require Alembic migrations and integration coverage.
- External side effects require explicit idempotency/recovery behavior; timeout is not proof of failure.
- Keep secrets, tokens, private keys, proxy credentials, and Authorization headers out of Git/logs/fixtures.
- If an accepted contract changes, update the relevant source-of-truth document/ADR in the same change.

## Validation

Standard gate:

```bash
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run alembic check
uv run pytest
```

Also run task-specific migration, PostgreSQL, concurrency, protocol, recovery, or browser-contract tests described by the issue/context map.

## Stop instead of improvising

Stop the affected work and report when:
- source-of-truth docs/ADRs conflict with the requested implementation;
- a safe recovery/idempotency path cannot be established;
- production credentials/data are required;
- browser automation would be needed before C3;
- implementation would require evasion/bypass behavior;
- a new distributed-system dependency or breaking protocol/security change appears necessary;
- current official API/UI behavior materially contradicts the accepted contract;
- quality gates can pass only by weakening correctness, typing, tests, or security.

## Delivery

- Roadmap entries are not automatic authorization.
- Keep issue -> commit -> test evidence traceable.
- Do not self-merge unless the user/coordinator explicitly authorizes merge.
- Stop at the checkpoint named in the authorized batch.

## Context maintenance

- `AGENTS.md` is a map, not an encyclopedia.
- Put durable architecture/product knowledge in `docs/`, not here.
- Avoid generic advice the model already knows.
- Add nested `AGENTS.md` only when a subtree develops stable, genuinely local rules that cannot be expressed cleanly in normal docs.