# AGENTS.md

This file is the repository-wide constitution and router for coding agents. Detailed knowledge belongs in `docs/` and specialized project skills.

## Project

`pumni/thread` is a Distributed Hybrid Threads Operations Tool:
- Centralized Control Plane + PostgreSQL source of truth;
- Official Threads API preferred where it satisfies the capability;
- Distributed Windows-first Worker Agents for approved local/browser capabilities;
- Human intervention as a valid, fail-closed execution outcome.

## Authority by information type

- **Task authorization:** Current user instruction, authorized GitHub issue, and coordinator comments define *what to do now*. Static handoff/history documents never grant authorization.
- **Implementation truth:** Current code and passing tests on the checkout branch define *how the system behaves now*.
- **Accepted contracts:** Accepted ADRs (`docs/adr/`) and versioned protocol specifications (`docs/protocols/`) define durable architecture.
- **Subtree rules:** The nearest nested `AGENTS.md` (e.g. `apps/desktop/AGENTS.md`) governs localized component rules.

## Starting a task

1. Identify scope from the authorized GitHub issue / coordinator comment or direct user request.
2. Read this root `AGENTS.md`, and the nearest nested `AGENTS.md` if working within a subtree.
3. Inspect current code and tests in the target area before designing abstractions.
4. Load matching project skills (`.agents/skills/`) and canonical documentation just in time only when the task requires them.
5. Do not preload the full documentation set by default.

## Non-negotiable invariants

- PostgreSQL is authoritative business state; memory, WebSockets, and worker journals are not.
- `Command` is business intent; `WorkerJob` is remote execution. Do not collapse them.
- WorkerJob has independent lease/fencing/checkpoint semantics; stale-owner updates fail closed.
- WebSocket is notification/presence, never the durable queue or source of truth.
- Browser accounts use persistent account -> worker/profile affinity; no automatic profile migration; explicit reassignment requires authorized Controller flow and fresh human login per ADR-0004.
- Each account has its own execution mode: `API_ONLY`, `BROWSER_ONLY`, `HYBRID`, or `MANUAL`.
- Workers do not invent business actions; Control Plane/Scheduler creates them.
- API-first means preferred executor where suitable, not API-only architecture; `HYBRID` fallback is bounded by capability policy and never inferred automatically from arbitrary API failures.
- Browser automation is allowed only in the approved Worker/browser boundary and only from C3 onward.
- Login/session challenges require human intervention; do not bypass them.
- No anti-detect, fingerprint spoofing, or human-emulation-for-evasion subsystem.
- Domain code must not import FastAPI, httpx, SQLAlchemy, WebSocket/browser libraries, Windows APIs, or Meta DTOs.
- External side effects require explicit idempotency/recovery behavior; timeout is not proof of failure.
- Keep secrets, tokens, private keys, proxy credentials, and Authorization headers out of Git/logs/fixtures.

## Change discipline

- Implement only the explicitly authorized issue/batch; stop at the designated checkpoint.
- Prefer the smallest coherent change that satisfies the contract.
- Reuse existing ports, services, and patterns before adding abstractions.
- Do not add Redis, a broker, microservices, or new auth architecture without an approved ADR.
- Schema changes require Alembic migrations and integration coverage.
- If an accepted contract changes, update the relevant ADR/source-of-truth document in the same change.

## Progressive disclosure & skills

Load specialized skills under `.agents/skills/` only when triggered:
- `database-migration`: PostgreSQL schemas, Alembic revisions, SQLAlchemy models.
- `worker-protocol`: Worker WebSocket framing, protocol v1/v2, 256-bit enrollment, lease/fencing tokens.
- `browser-capability`: Fail-closed Threads browser capabilities, Playwright DOM contracts, staged mutations.
- `threads-api-contract`: Official Meta Threads Graph API client, OAuth credentials, API test fixtures.
- `ci-failure-triage`: Hosted CI failure triage, runner artifacts, root-cause classification, repeated-signature stop rule.
- `desktop-acceptance`: Windows Desktop local preflight, packaging smoke tests, native supervisor acceptance evidence.

## Validation & hosted CI

Run narrow deterministic checks during implementation. When required by the issue or checkpoint, verify the standard gate:

```bash
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run alembic check
uv run pytest
```

Local PASS is preflight only, never hosted acceptance. Hosted CI failure is a hard stop: do not push speculative fixes or rerun workflows hoping timing changes. If a hosted run fails, stop, load the `ci-failure-triage` skill, and follow `docs/CI_AGENT_WORKFLOW.md`.

## Stop instead of improvising

Stop the affected work and report when:
- Requirements conflict with accepted ADRs or source-of-truth documents;
- A safe recovery or idempotency path cannot be established;
- Production credentials or live external data are required;
- Browser automation would be needed before C3;
- Implementation would require evasion or bypass behavior;
- External Threads API or UI behavior materially contradicts the accepted contract;
- Quality gates can pass only by weakening correctness, typing, tests, or security.

## Delivery

- Roadmap entries and milestone lists are not authorization.
- Keep issue -> commit -> test evidence traceable.
- Do not self-merge unless the user or coordinator explicitly authorizes merge.
