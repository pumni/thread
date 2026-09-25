# Context Map for Coding Agents

Purpose: load the **smallest sufficient context** for the current task.

Do not treat this as another mandatory reading list. Start with root `AGENTS.md`, current handoff, and the authorized issue; then follow the row that matches the task.

## Always

Read:
- `/AGENTS.md`
- `docs/PROJECT_STATE_HANDOFF.md`
- authorized GitHub issue/batch + coordinator comments
- current code/tests in the area being changed

Use README only when you need a human-facing project overview.

## C1 — Distributed Worker Foundation (#21, #22, #23)

Primary:
- `docs/ARCHITECTURE.md` — worker/runtime sections
- `docs/protocols/WORKER_PROTOCOL_V1.md`
- `docs/adr/0003-distributed-hybrid-execution.md`
- `docs/adr/0004-persistent-worker-affinity-and-worker-jobs.md`

Review/verification:
- `docs/ACCEPTANCE_AND_REVIEW.md` — WorkerJob/auth/data gates
- `docs/handoffs/C1_CODEX_BRIEF.md`

Usually unnecessary for C1:
- Threads API capability spike
- browser-engine ADR work
- discovery/lead details

## C2 — Capability Router (#24)

Primary:
- `docs/ARCHITECTURE.md` — capabilities/account execution mode
- `docs/FEATURE_PARITY_MATRIX.md`
- ADR-0003 and ADR-0004

Read Worker Protocol only where routing creates/coordinates WorkerJobs.

## C3 — Windows Worker / Browser Foundation (#25, #26)

Primary:
- `docs/ARCHITECTURE.md` — worker/browser/session/network sections
- `docs/protocols/WORKER_PROTOCOL_V1.md`
- `docs/adr/0003-distributed-hybrid-execution.md`
- `docs/adr/0004-persistent-worker-affinity-and-worker-jobs.md`
- `docs/adr/0005-browser-capability-boundary.md`

Also read C2 routing contracts in code before implementing executor integration.

## C4 — Discovery / Leads (#8)

Primary:
- `docs/FEATURE_PARITY_MATRIX.md` — discovery rows
- `docs/THREADS_API_CAPABILITY_SPIKE.md`
- discovery sections in `docs/ARCHITECTURE.md`

Read browser ADRs only if the authorized scope includes browser enrichment.

## C5 — Browser Capabilities / AccountActivityPlan (#27, #28)

Primary:
- `docs/adr/0005-browser-capability-boundary.md`
- browser/activity sections in `docs/ARCHITECTURE.md`
- relevant rows in `docs/FEATURE_PARITY_MATRIX.md`

Also inspect the C2/C3 implementation contracts; do not recreate routing/session abstractions.

## C6 — Scheduler / Operations (#9, #10)

Primary:
- scheduling/operations sections in `docs/MASTER_PLAN.md` and `docs/ARCHITECTURE.md`
- `docs/ACCEPTANCE_AND_REVIEW.md`
- Worker Protocol where fleet lifecycle/update behavior is involved

## TP-002 live Meta validation / external API changes (#3)

Primary:
- `docs/THREADS_API_CAPABILITY_SPIKE.md`
- current official Meta Threads developer docs/changelog
- relevant API adapter/contract tests

Only update `FEATURE_PARITY_MATRIX.md` when verified evidence changes a capability status/contract.

## Release certification (#11)

This is intentionally broad. Read:
- project handoff
- master plan
- architecture
- capability matrix
- work breakdown
- acceptance protocol
- relevant ADRs/protocols
- open release/security/API gates

Release is one of the few tasks where broad context is appropriate.

## Documentation-only changes

Read only the document being edited plus its direct source-of-truth dependencies.

Do not copy the same rule into multiple docs merely to make it more visible. Prefer one canonical definition plus links.

## When uncertain

Search the repository for the relevant type, protocol, invariant, test, or ADR before adding context.

If two sources disagree, use the source-of-truth order documented in `docs/MASTER_PLAN.md`; stop for coordinator decision when the conflict is architectural.