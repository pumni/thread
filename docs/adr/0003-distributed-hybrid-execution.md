# ADR-0003: Distributed Hybrid Execution

- Status: Accepted
- Date: 2026-09-25

## Context

The product must operate as a tool across multiple machines and accounts. Official Threads API support is valuable and should be preferred where it provides the required business outcome, but not every desired account/session/UI capability is exposed through a suitable public API.

The initial architecture treated browser automation as an exceptional future adapter. Product requirements now explicitly include multi-machine persistent browser profiles and user-authorized UI workflows.

## Decision

Adopt a distributed hybrid execution architecture:
- centralized Control Plane;
- PostgreSQL source of truth;
- official API adapters;
- remote Worker Agents;
- isolated browser adapters;
- human-assisted intervention as a first-class execution outcome;
- a future Capability Router chooses an executor according to account policy and capability support.

Browser automation remains outside domain/application business models.

## Consequences

Positive:
- preserves durable command/idempotency architecture;
- supports API and browser without duplicating business intent;
- allows capabilities to migrate from browser to API later;
- supports multi-machine execution cleanly.

Costs:
- requires WorkerJob protocol, worker authentication, presence, recovery and deployment;
- introduces browser UI-contract maintenance in later phases.

## Explicit exclusions

This ADR does not approve:
- anti-detect/fingerprint spoofing;
- platform-control bypass;
- challenge/2FA bypass;
- random engagement loops;
- browser code in domain;
- browser implementation before C3.
