# Linux/Docker Control Plane deployment and recovery (#87)

This is a reproducible internal/test topology for the current Control Plane.
It does not certify a production deployment. PostgreSQL is the durable source
of truth; FastAPI/Uvicorn and the standalone scheduler are separate processes
using the same database. Windows browser Workers stay on their external Windows
hosts and are not included in this image or Compose project.

The image is built locally from the exact checkout using Python 3.14 and the
locked project dependencies. It runs as an unprivileged `app` user and contains
the installed shared Python project plus Alembic configuration/revisions needed
for the migration role. It does not copy Git metadata, environment files,
tests, local databases, Worker data, browser profiles, or Windows onedir
artifacts. The lock includes the Playwright Python package used by the shared
project; the Docker build does not run `playwright install` and contains no
Playwright browser binaries. No secret is passed as a build argument or stored
in the image. The image is internal/test only; it is not pushed or signed.

The committed Compose file is an offline smoke topology, not a secret store.
Its PostgreSQL service uses trust authentication on a private Compose network,
publishes no database port, and stores data in a named volume. Do not reuse that
authentication configuration for a networked deployment. Token provider mode
is disabled; no Threads, CRM, Meta, or Windows Worker call is made by the
smoke.

## Build and run

From the repository root:

```powershell
docker compose build
docker compose up -d --wait postgres
docker compose up --force-recreate --no-deps migrate
docker compose up -d http scheduler
```

The migration service runs `alembic upgrade head` once and exits. The HTTP and
scheduler services depend on its successful completion; neither process runs
migrations during startup. Run the migration job again explicitly for each
deployment with `--force-recreate` before starting/recreating application
processes.

The HTTP service runs the existing FastAPI application through Uvicorn. Its
container bind address defaults to `0.0.0.0` and port to `8000`; configure
`THREADS_PLATFORM_HTTP_HOST` and `THREADS_PLATFORM_CONTAINER_PORT` to change
them. Compose publishes the HTTP port on host loopback only, using
`THREADS_PLATFORM_SMOKE_HTTP_PORT` (default `8000`). `/health` is liveness;
`/ready` checks PostgreSQL and persisted Worker status. The Compose healthcheck
uses `/health`; it does not replace `/ready`.

The scheduler runs independently as:

```text
python -m threads_platform.scheduler
```

Both application roles receive the same `THREADS_PLATFORM_DATABASE_URL` from
the Compose service configuration. For a real deployment, provide database
credentials and any approved Threads token-reference environment through the
deployment's runtime secret/configuration facility. Never put their values in
the Dockerfile, image, or committed Compose file. Keep
`THREADS_PLATFORM_THREADS_TOKEN_PROVIDER_MODE=disabled` unless an operator has
explicitly configured the existing #70 metadata and versioned environment
secret references. This smoke does not enable token execution.

Inspect the two independently running application containers with:

```powershell
docker compose ps http scheduler
```

## Compose restart and recovery smoke

Run the repository-owned exact-head smoke from the project environment:

```powershell
uv sync --locked
uv run --locked python scripts/control_plane_compose_smoke.py
```

The smoke builds and inspects the image, starts PostgreSQL, runs the one-shot
migration, starts HTTP and scheduler separately, and checks `/health` and
`/ready`. It writes two synthetic Worker rows using the existing Worker
repository: one proves PostgreSQL state survives while HTTP/scheduler are
recreated; the other is inserted while both processes are stopped and has
expired presence. After restart, the scheduler must discover and expire that
persisted row exactly once. The smoke also stops PostgreSQL and checks that
`/health` remains live while `/ready` returns `NOT_READY` / `DOWN` / HTTP 503,
then verifies readiness and persisted rows after PostgreSQL returns. It uses no
Meta API, CRM, or external Worker. It removes its temporary Compose project and
PostgreSQL volume in a `finally` path; CI also has an unconditional cleanup
step.

For a manual non-smoke Compose deployment, the named PostgreSQL volume survives
HTTP and scheduler restarts and ordinary `docker compose down`. Do not add
`--volumes` when retaining that database. The smoke cleanup intentionally uses
`docker compose down --volumes --remove-orphans` because its database is
disposable.

## Recovery procedures

### HTTP Control Plane process restart

Keep PostgreSQL available and recreate only the HTTP container:

```powershell
docker compose up -d --force-recreate --no-deps http
```

Check `/health` for process liveness and `/ready` for database availability.
In-flight HTTP requests can be interrupted by a process replacement; use the
existing idempotency and durable Command recovery contracts. FastAPI does not
own scheduler loops or durable business state.

### Scheduler restart

Keep PostgreSQL available and recreate only the scheduler:

```powershell
docker compose up -d --force-recreate --no-deps scheduler
```

The scheduler reconstructs its work from PostgreSQL: poll timing is only a
wakeup interval. Due state, leases, Worker presence expiry, attempts, and
recovery decisions remain database-owned. Do not infer completion from a
scheduler process restart or create an in-memory substitute queue. The smoke
rehearses scheduler rediscovery through persisted expired Worker presence.

### PostgreSQL outage

During database failure, `/ready` returns bounded `NOT_READY` / `DOWN` state
with HTTP 503. `/health` can remain HTTP 200 because it reports only that the
HTTP process is alive. Do not claim that work progressed while PostgreSQL was
unavailable or treat process memory as authoritative. The scheduler retries
through its existing bounded error backoff and rediscovers durable work after
database recovery. Confirm `/ready` reports `database: UP` after recovery.

This checkpoint does not define database backup or restore procedures. Follow
the deployment's separately approved PostgreSQL backup/restore policy; do not
infer one from the Compose volume or restart smoke.

### Windows Worker disconnect and reconnect

Worker presence expiry and WorkerJob lease/recovery remain scheduler-owned and
PostgreSQL-backed. A disconnected Worker is not treated as proof that its
current job was canceled or completed. After Control Plane restart, the Worker
reconnects through its existing authentication and hello/reconciliation flow;
the server and its durable leases remain authoritative. No account/profile
affinity is moved automatically. Docker smoke does not launch or emulate a
Windows Worker.

Durable `DRAINING` semantics from #78 are unchanged: a draining Worker receives
no new claims, but current running work continues to its existing safe boundary;
quiescence still requires zero running jobs and browser sessions. The interactive
Windows installation/update/rollback procedure from #84 remains external to
Docker and still requires durable `DRAINING` to quiescent `OFFLINE` before
switching releases. Abrupt loss is not drain completion.

## Scope and release status

There is no schema migration, Windows Worker container, Kubernetes/deployment
template, registry publication, signing, metrics, or tracing in this
checkpoint. Metrics/tracing are deferred to C6-02/9 after this process topology
is accepted. #62 remains a separate CRM transport dependency. Phase B remains
`PARTIAL_LIVE_EVIDENCE / NOT_READY`, and #3 remains OPEN as the production/release
gate. This internal/test image and smoke do not make a production release claim.
