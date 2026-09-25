# ADR-0001: API-first modular monolith

Status: Accepted for bootstrap; **amended by ADR-0003 and ADR-0005**

## Context

The project began by intentionally avoiding a direct port of the legacy Facebook browser bot. Official Threads APIs cover many core workflows and were the safest foundation for a greenfield codebase.

The product requirements were later clarified: the system must operate as a distributed tool across multiple machines with persistent browser profiles for capabilities that are not adequately covered by official APIs.

## Decision

The following parts of ADR-0001 remain accepted:
- modular monolith Control Plane;
- official API preferred where it satisfies the capability;
- explicit ports/adapters;
- browser libraries excluded from domain/application business models;
- capability verification before implementation.

The following original restriction is superseded:
- browser automation is no longer treated as merely hypothetical.

ADR-0003 authorizes distributed hybrid execution.
ADR-0005 defines the browser capability boundary.

Browser execution is therefore allowed only as an isolated Worker/infrastructure adapter through the approved WorkerJob/Capability Router architecture.

## Consequences

API-first now means **preferred executor**, not **API-only product architecture**.

The project still rejects literal legacy-bot cloning, browser logic in domain, anti-detect/fingerprint-evasion objectives and undocumented bypass behavior.