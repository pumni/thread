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

The Control Plane listener uses direct Uvicorn TLS. There is no reverse proxy:
HTTPS and WSS, Operator and Worker routes, `/health`, `/ready`, and `/metrics`
share one listener. Configure an explicit stable IPv4 address and port; the
leaf certificate SAN contains exactly that address and `127.0.0.1`. A local
administrator provisions the private root and leaf before starting HTTP:

```powershell
uv run python -m threads_platform.controller_tls_admin provision `
  --admin-dir <controller-tls-admin> `
  --serving-dir <controller-tls-serving> `
  --lan-address <stable-ipv4>
uv run python -m threads_platform.controller_tls_admin fingerprint `
  --admin-dir <controller-tls-admin>
```

Use `renew` with the same directories and address to issue a same-root leaf.
Keep `root-key.pem` in the administrator-only directory; mount only
`leaf-fullchain.pem` and `leaf-key.pem` read-only into the ordinary HTTP
service. The scheduler receives no TLS key. Linux private-key files must be
owned by the service administrator and mode `0600`; the admin command fails
closed on broader modes. Compare the printed SHA-256 root DER fingerprint
out-of-band with remote clients before they confirm private application trust.
No product root is installed in an operating-system or browser CA store.

From the repository root:

```powershell
$env:THREADS_PLATFORM_TLS_LAN_ADDRESS = "<stable-ipv4>"
docker compose build
docker compose --profile tls-admin run --rm --no-deps tls-admin
docker compose up -d --wait postgres
docker compose up --force-recreate --no-deps migrate
docker compose up -d http scheduler
```

The `tls-admin` profile is an explicit one-shot provisioning operation; normal
`docker compose up` never runs it. Its `identity-provisioned` fingerprint marker
is retained with the root state, so missing or partial certificate state fails
closed instead of generating a replacement root. Run the explicit `renew`
command with the same private directories and address when rotating only the
leaf.

The migration service runs `alembic upgrade head` once and exits. The HTTP and
scheduler services depend on its successful completion; neither process runs
migrations during startup. Run the migration job again explicitly for each
deployment with `--force-recreate` before starting/recreating application
processes.

The HTTP service runs the existing FastAPI application through Uvicorn with
`--ssl-certfile`, `--ssl-keyfile`, and `--no-proxy-headers`. Missing TLS paths
fail startup; there is no plaintext production listener. Its container bind
address defaults to `0.0.0.0` and port to `8000`; configure
`THREADS_PLATFORM_HTTP_HOST` and `THREADS_PLATFORM_CONTAINER_PORT` to change
them. Compose publishes the selected stable IPv4 using
`THREADS_PLATFORM_TLS_LAN_ADDRESS` and host port
`THREADS_PLATFORM_SMOKE_HTTP_PORT` (default `8000`). `/health` is liveness;
`/ready` checks PostgreSQL and persisted Worker status. The Compose healthcheck
verifies HTTPS using the Controller root; it does not replace `/ready`.

The scheduler runs independently as:

```text
python -m threads_platform.scheduler
```

## Metrics scrape boundaries (#89)

The HTTP process serves Prometheus text at `/metrics` on the configured HTTPS
port. It reads Worker and WorkerJob counts from PostgreSQL for each scrape; when
PostgreSQL is unavailable it reports `threads_platform_database_up 0` and omits
the persisted count samples. `/ready` keeps its separate #72 status and HTTP
behavior.

Compose enables the scheduler's independent metrics listener at
`scheduler:9101/metrics` for containers on the private Compose network. The
scheduler binds inside its container, but Compose publishes no host port for
9101. The listener is disabled by default outside this topology; its bounded
host and port settings are `THREADS_PLATFORM_SCHEDULER_METRICS_HOST` and
`THREADS_PLATFORM_SCHEDULER_METRICS_PORT`.

## Tracing process ownership (#91)

Tracing is disabled in committed Compose through
`THREADS_PLATFORM_TRACING_ENABLED=false`; neither process constructs an
exporter or opens an OTLP connection. To enable it in another deployment, set
`THREADS_PLATFORM_TRACING_ENABLED=true` independently for the HTTP and
scheduler process environments. Each creates its own bounded provider/export
lifecycle and uses an environment-configured OTLP/HTTP endpoint. Configure
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` or `OTEL_EXPORTER_OTLP_ENDPOINT`, with
authentication only in the corresponding standard OpenTelemetry header
environment variables. Do not put these values in image layers, committed
Compose, application settings files, command lines, or logs.

The generic endpoint receives `/v1/traces`; the trace-specific endpoint is used
as supplied. Configured endpoint user information, query strings, fragments,
unsupported schemes, missing hosts, and whitespace are rejected without
printing the value. The default endpoint is the standard local OTLP/HTTP
endpoint if tracing is enabled without an override. This repository does not
deploy a collector, publish an OTLP port, or instrument SQLAlchemy or outbound
Threads/Meta HTTP calls. HTTP and scheduler trace providers remain separate;
they do not alter `/health`, `/ready`, `/metrics`, scheduler durability, or the
Windows Worker protocol. Shutdown gives exporter flush and provider close a
three-second hard wait bound in a daemon cleanup thread; the SDK atexit shutdown
hook is disabled. SDK and HTTP transport diagnostic records are replaced with
a fixed event, including `urllib3` retry records that could contain endpoint
paths. See `docs/OBSERVABILITY_RUNBOOK.md` for span privacy, W3C propagation,
correlation, and failure semantics.

Scrape the two process owners separately:

```powershell
curl.exe --cacert <controller-tls-admin>\root-cert.pem https://<stable-ipv4>:8000/metrics
docker compose exec -T http python -c "from urllib.request import urlopen; print(urlopen('http://scheduler:9101/metrics').read().decode())"
```

The extended Compose smoke provisions isolated synthetic private TLS state,
compares the CLI fingerprint with the served root, checks verified HTTPS for
health/readiness/Operator and Worker routes, and checks verified WSS with a
synthetic authenticated Worker. It also proves the HTTP container cannot read
root signing state and the scheduler receives no TLS private key. The smoke
checks both metrics surfaces, verifies that scheduler port
9101 is not published, and checks PostgreSQL-derived gauges after HTTP
recreation. It also confirms that the scheduler's process-local tick counters
start over after scheduler recreation. HTTP gauges rediscover durable rows from
PostgreSQL; neither process restores counters from database state. Scraping
requires no Meta, CRM, or Windows Worker connection.

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

The smoke builds and inspects the image, creates isolated synthetic TLS state,
starts PostgreSQL, runs the one-shot migration, starts HTTP and scheduler
separately, and checks verified HTTPS `/health` and `/ready`. It writes two
synthetic Worker rows using the existing Worker
repository: one proves PostgreSQL state survives while HTTP/scheduler are
recreated; the other is inserted while both processes are stopped and has
expired presence. After restart, the scheduler must discover and expire that
persisted row exactly once. The smoke also stops PostgreSQL and checks that
`/health` remains live while `/ready` returns `NOT_READY` / `DOWN` / HTTP 503,
then verifies readiness and persisted rows after PostgreSQL returns. The
smoke exercises verified HTTPS Operator and Worker routes and authenticated
WSS, with a wrong-root failure check. It uses no Meta API, CRM, or external
Worker. It removes its temporary Compose project, TLS material, and PostgreSQL
volume in a `finally` path; CI also has an unconditional cleanup step.

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

Check `/health` and `/ready` with the configured Controller root as trust
anchor, for process liveness and database availability respectively.
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
template, registry publication, signing, or collector in this #87 checkpoint.
Bounded metrics are described in `docs/OBSERVABILITY_RUNBOOK.md`; #91 later adds
opt-in tracing without changing this topology. #62 remains a separate CRM transport dependency. Phase B remains
`PARTIAL_LIVE_EVIDENCE / NOT_READY`, and #3 remains OPEN as the production/release
gate. This internal/test image and smoke do not make a production release claim.
