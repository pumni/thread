---
name: database-migration
description: Modify PostgreSQL schemas, author or review Alembic migrations, and update SQLAlchemy persistence models. Not for query-only changes.
---

# Database Migration Skill

## Purpose
Guide the creation, review, and verification of PostgreSQL schema changes and Alembic migrations while preserving data safety, reversibility, and system invariants.

## Trigger Conditions
Use this skill when a task involves:
- Creating, modifying, or testing Alembic migrations in `migrations/versions/`.
- Modifying SQLAlchemy ORM models in `src/threads_platform/infrastructure/persistence/models.py`.
- Altering database tables, columns, indexes, foreign keys, or check constraints.
- Validating database schema synchronization (`alembic check`).

## Non-Trigger Conditions
Do NOT use this skill for:
- Pure domain entities in `src/threads_platform/domain/` with no schema alterations.
- Read-only database queries or repository method additions that do not alter the table structure.
- Local in-memory or file-based caching changes.

## Canonical References
Inspect these sources before authoring schema changes:
- `migrations/env.py` and existing revisions in `migrations/versions/`.
- `src/threads_platform/infrastructure/persistence/models.py`.
- `docs/ACCEPTANCE_AND_REVIEW.md` (Section 3: Data gate).
- `docs/adr/0002-python-uv-postgresql.md`.
- `scripts/windows_local_preflight.ps1` (disposable PostgreSQL test cluster).

## Invariants & Design Rules
1. **PostgreSQL is Authoritative:** Database constraints (unique, foreign key, check constraints) enforce business invariants at rest.
2. **Reversibility:** Every migration must implement a safe, functional `downgrade()` function where practically feasible.
3. **UTC Timestamps:** All timestamp columns must use timezone-aware UTC (`sa.DateTime(timezone=True)`).
4. **Index Discipline:** Add composite indexes for high-frequency queries, particularly WorkerJob claim queries, command leasing, and account status lookups.
5. **Secret Hygiene:** Never introduce columns that store plaintext tokens, passwords, private keys, or unredacted proxy credentials.

## Step-by-Step Procedure
1. **Inspect current schema:** Check existing migration files in `migrations/versions/` to determine the latest revision head and naming convention (`YYYYMMDD_NNNN_<slug>.py`).
2. **Update models:** Modify SQLAlchemy models in `src/threads_platform/infrastructure/persistence/models.py`.
3. **Generate/author revision:** Create the migration script in `migrations/versions/` with explicit `upgrade()` and `downgrade()` operations.
4. **Verify locally:**
   ```bash
   uv run alembic check
   uv run pytest tests/integration/test_*migration*.py
   ```
5. **Run preflight:** On Windows, execute `powershell -NoProfile -File scripts/windows_local_preflight.ps1` to test the migration against a real disposable PostgreSQL instance.
