---
name: worker-protocol
description: Implement or modify Worker WebSocket framing, protocol v1/v2 schemas, worker enrollment, and lease/fencing tokens. Not for browser automation.
---

# Worker Protocol Skill

## Purpose
Guide modifications and extensions to the distributed Worker wire protocol and lifecycle framing, preserving backward compatibility, fencing semantics, and transport boundaries.

## Trigger Conditions
Use this skill when a task involves:
- Defining or updating WebSocket frame schemas in `src/threads_platform/application/worker_protocol.py`.
- Modifying Worker WebSocket connection handling or dispatch in `src/threads_platform/transport/http/workers.py`.
- Updating worker enrollment, device challenge, or token authentication in `src/threads_platform/infrastructure/security/worker_auth.py`.
- Altering WorkerJob lease renewal, fencing token exchange, or checkpoint messaging.
- Implementing or changing worker lifecycle states (`REGISTERED`, `ONLINE`, `DRAINING`, `OFFLINE`, `UPGRADE_REQUIRED`).

## Non-Trigger Conditions
Do NOT use this skill for:
- Browser automation locators, DOM contracts, or Playwright adapters (use `browser-capability`).
- Scheduler activity generation or Command dispatching logic in the Control Plane core.
- React UI components displaying worker status in Desktop (use `apps/desktop/AGENTS.md`).

## Canonical References
Inspect these sources before modifying protocol contracts:
- `docs/protocols/WORKER_PROTOCOL_V1.md` (and documented additive v2 extensions).
- `docs/adr/0003-distributed-hybrid-execution.md`.
- `docs/adr/0004-persistent-worker-affinity-and-worker-jobs.md`.
- `src/threads_platform/application/worker_protocol.py`.
- `src/threads_platform/transport/http/workers.py`.
- Tests: `tests/unit/test_worker_jobs.py`, `tests/integration/test_worker_transport.py`, `tests/integration/test_worker_draining.py`.

## Invariants & Design Rules
1. **Command != WorkerJob:** `Command` is business intent in PostgreSQL; `WorkerJob` is the remote execution assignment. Never conflate them on the wire.
2. **WebSocket is presence/notification only:** WebSocket frames signal state changes; durable queuing, claim transactions, and lease recovery rely on PostgreSQL and HTTPS endpoints.
3. **Fencing & Leases:** All WorkerJob state updates and completions require a valid fencing token and active lease. Stale lease attempts must fail closed.
4. **Additive Protocol Evolution:** New protocol capabilities must negotiate version cleanly. Unknown message types or fields from newer/older peers must fail closed without crashing the server.
5. **High-Entropy Credentials:** Enrollment uses 256-bit single-use tokens; never replace high-entropy credentials with short unauthenticated tokens without an approved security review.

## Step-by-Step Procedure
1. **Inspect active wire contracts:** Review message dataclasses and schemas in `src/threads_platform/application/worker_protocol.py`.
2. **Design additive schema changes:** Ensure new fields provide safe defaults or version gating for backwards compatibility.
3. **Update handlers:** Modify WebSocket frame routing in `src/threads_platform/transport/http/workers.py`.
4. **Enforce authorization & fencing:** Verify caller identity and check fencing tokens before committing state transitions.
5. **Verify locally:**
   ```bash
   uv run pytest tests/unit/test_worker_jobs.py tests/integration/test_worker_transport.py
   ```
