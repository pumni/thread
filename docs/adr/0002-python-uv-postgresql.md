# ADR-0002: Python 3.14, uv and PostgreSQL

Status: Accepted for project bootstrap

## Context

The project requires a reproducible Python toolchain, async networking, typed configuration, durable commands and transactional reliability.

The legacy JSON-file persistence model does not provide transactional safety, uniqueness constraints or robust concurrent access.

## Decision

Use:

- CPython >=3.14,<3.15;
- uv for Python and project/dependency management;
- pyproject.toml + committed uv.lock;
- PostgreSQL as the primary durable database;
- SQLAlchemy 2 and Alembic for persistence/migrations.

Redis is not a default dependency and must be justified by a concrete requirement.

## Consequences

- local and CI dependency resolution is reproducible;
- relational constraints can enforce deduplication/idempotency;
- inbox/outbox can share transactions with business state;
- initial development requires PostgreSQL availability for integration tests.
