# Threads Operations Platform

Greenfield Python platform for managing and automating supported Meta Threads workflows.

## Status

Architecture and delivery planning. No production implementation should begin until the planning pull request is reviewed and merged.

## Product direction

This project is a Threads-first platform; it does not port the legacy Facebook browser bot.

Core principles:

- Threads-first, not Facebook-first.
- Official Threads API first.
- Modular monolith before microservices.
- Async I/O, durable commands, idempotency, inbox/outbox.
- PostgreSQL as source of truth.
- Python 3.14 managed with uv.
- No browser automation in the core architecture.
- Capability gaps must be documented before any browser-based fallback is considered.
- Every Codex task must have explicit acceptance criteria and test evidence.

## Source of truth

1. docs/MASTER_PLAN.md — delivery plan and phase gates.
2. docs/FEATURE_PARITY_MATRIX.md — mapping from legacy Facebook requirements to Threads.
3. docs/ARCHITECTURE.md — target codebase and runtime architecture.
4. docs/CODEX_EXECUTION_GUIDE.md — rules for handing implementation tasks to Codex.
5. docs/ACCEPTANCE_AND_REVIEW.md — acceptance process used by the reviewer/coordinator.
6. docs/adr/ — architectural decisions.

## Workflow

Issue -> implementation branch -> Codex implementation -> local verification -> pull request -> acceptance review -> merge.

No issue should mix unrelated phases or large cross-cutting refactors.

## External documentation rule

Before implementing a Threads API capability, verify it against Meta's current Threads API documentation and changelog. The API evolves; repository documentation records intent, not a permanent promise that every external capability remains unchanged.
