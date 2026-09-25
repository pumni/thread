# ADR-0001: API-first modular monolith

Status: Accepted for project bootstrap

## Context

The legacy Facebook system depended on browser automation and a large monolithic script. The new product targets Threads, which exposes official APIs for many core workflows.

Starting from browser automation would import unnecessary operational fragility into a greenfield system. Starting with microservices would introduce deployment and distributed-systems complexity before scale requires it.

## Decision

Build a modular monolith around the official Threads API.

Business modules remain isolated by explicit package boundaries and ports/adapters.

Browser automation is not a core dependency. A future browser adapter requires a separate ADR proving an official API capability gap and business necessity.

## Consequences

Positive:

- fewer UI-breakage risks;
- easier unit/contract testing;
- simpler deployment;
- explicit domain boundaries;
- future service extraction remains possible.

Tradeoffs:

- module boundaries require review discipline;
- some Threads product features may not be exposed in official API;
- capability verification is required continuously.
