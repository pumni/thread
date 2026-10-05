# Project State & Handoff Router

> [!IMPORTANT]
> **NOT TASK AUTHORIZATION**
> This file is a durable compatibility pointer and index. It does **not** grant task authorization and may lag active development.
> - **Live task authorization:** Always consult the authorized GitHub issue / user prompt and coordinator comments.
> - **Implementation truth:** Current code and passing tests on the checkout branch define how the system behaves now.
> - **Accepted contracts:** Accepted ADRs (`docs/adr/`) and versioned protocol specifications (`docs/protocols/`) define durable architecture.
> - **Constitutional invariants:** See root [`AGENTS.md`](../AGENTS.md) for non-negotiable rules.
> - **Task-specific documentation:** Use [`docs/CONTEXT_MAP.md`](CONTEXT_MAP.md) to load only the relevant documentation and specialized skills for your task.

## Repository & System Overview

- **Repository:** `pumni/thread` (Distributed Hybrid Threads Operations Tool)
- **Architecture:** Centralized Control Plane + PostgreSQL authoritative business state, distributed Windows-first Worker Agents for approved browser capabilities, official Meta Threads Graph API client, and operator intervention for challenges.

## Information Architecture & Routing

| Need | Authority / Resource | Notes |
|---|---|---|
| **Current Task / Scope** | Authorized GitHub issue / coordinator instruction | Sole source of live task authority. |
| **Constitutional Rules** | [`AGENTS.md`](../AGENTS.md) | Universal invariants, change discipline, stop rules. |
| **Desktop Subsystem Rules** | [`apps/desktop/AGENTS.md`](../apps/desktop/AGENTS.md) | Nearest nested instruction for Windows Desktop app. |
| **Desktop Fresh Session** | [`docs/desktop/SESSION_HANDOFF.md`](desktop/SESSION_HANDOFF.md) | Desktop delivery roadmap, security and acceptance gates. |
| **Task Documentation & Skills** | [`docs/CONTEXT_MAP.md`](CONTEXT_MAP.md) | Capability/workflow documentation router & skills. |
| **Durable Architectural Decisions** | [`docs/adr/`](adr/) | Canonical ADRs (0001–0007). |
| **Worker Protocol Specifications** | [`docs/protocols/`](protocols/) | Additive protocol specs (e.g. `WORKER_PROTOCOL_V1.md`). |
| **Historical Milestone Archive** | [`docs/handoffs/PROJECT_STATE_HISTORY_2026-10.md`](handoffs/PROJECT_STATE_HISTORY_2026-10.md) | Detailed chronology for Batches A/B, TP-004A, C1–C6. |
| **Release & Master Planning** | [`docs/MASTER_PLAN.md`](MASTER_PLAN.md) | Product vision and milestone roadmap. |

## Historical Archive

The detailed narrative tracking milestones prior to 2026-10 (Batches A/B, TP-004A, C1–C6, and DX-01 planning) has been archived in:
- [`docs/handoffs/PROJECT_STATE_HISTORY_2026-10.md`](handoffs/PROJECT_STATE_HISTORY_2026-10.md)

Routine tasks should not load historical archives or past session handoffs. Rely on the authorized GitHub issue, current tests, and capability documentation via `docs/CONTEXT_MAP.md`.
