from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
from uuid import UUID, uuid4


class SmokeFailure(RuntimeError):
    pass


_STATE_HELPER = r"""
import asyncio
import sys
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID

from threads_platform.config.settings import Settings
from threads_platform.domain.workers import WorkerNode, WorkerStatus
from threads_platform.infrastructure.persistence.database import (
    create_database_engine,
    create_session_factory,
)
from threads_platform.infrastructure.persistence.uow import SQLAlchemyUnitOfWorkFactory


async def main() -> None:
    operation = sys.argv[1]
    worker_id = UUID(sys.argv[2])
    database_url = Settings().database_url
    if database_url is None:
        raise RuntimeError("smoke database is not configured")

    engine = create_database_engine(database_url)
    factory = SQLAlchemyUnitOfWorkFactory(create_session_factory(engine))
    try:
        if operation in {"seed-offline", "seed-expired"}:
            now = datetime.now(UTC)
            expired = operation == "seed-expired"
            worker = WorkerNode(
                worker_id=worker_id,
                display_name="Synthetic deployment smoke Worker",
                hostname=f"deployment-smoke-{worker_id}",
                platform="linux-smoke",
                agent_version="smoke",
                protocol_version=1,
                capabilities_schema_version=1,
                status=WorkerStatus.ONLINE if expired else WorkerStatus.OFFLINE,
                last_heartbeat_at=now - timedelta(minutes=2) if expired else None,
                presence_expires_at=now - timedelta(minutes=1) if expired else None,
                created_at=now,
                updated_at=now,
            )
            async with factory() as unit_of_work:
                if await unit_of_work.workers.get(worker_id) is not None:
                    raise RuntimeError("smoke fixture already exists")
                await unit_of_work.workers.add(worker)
            return

        deadline = time.monotonic() + 75
        while True:
            async with factory() as unit_of_work:
                worker = await unit_of_work.workers.get(worker_id)
                events = await unit_of_work.worker_security.list_audit_events(worker_id)
            if worker is None:
                raise RuntimeError("persisted smoke fixture is missing")
            if operation == "verify-offline":
                if worker.status is not WorkerStatus.OFFLINE or events:
                    raise RuntimeError("persisted smoke fixture changed unexpectedly")
                return
            if operation == "wait-expired" and worker.status is WorkerStatus.OFFLINE:
                if sum(event.event_type == "worker.presence.expired" for event in events) != 1:
                    raise RuntimeError("scheduler did not persist one presence-expiry event")
                return
            if operation != "wait-expired":
                raise RuntimeError("unsupported deployment smoke operation")
            if time.monotonic() >= deadline:
                raise RuntimeError("scheduler did not recover persisted Worker status")
            await asyncio.sleep(1)
    finally:
        await engine.dispose()


asyncio.run(main())
"""


def _compose(*arguments: str, input_text: str | None = None, timeout: int = 180) -> str:
    result = subprocess.run(
        ["docker", "compose", *arguments],
        capture_output=True,
        check=False,
        text=True,
        input=input_text,
        timeout=timeout,
    )
    if result.returncode != 0:
        label = arguments[0] if arguments else "compose"
        raise SmokeFailure(f"Docker Compose {label} step failed (exit {result.returncode})")
    return result.stdout.strip()


def _check_runtime_image() -> None:
    image = os.environ["THREADS_PLATFORM_IMAGE"]
    check = """
import os
from pathlib import Path

root = Path('/app')
if os.geteuid() == 0:
    raise SystemExit('runtime image is root')
allowed = {'.venv', 'alembic.ini', 'migrations'}
if {path.name for path in root.iterdir()} != allowed:
    raise SystemExit('runtime image contains unexpected application files')
if Path.home().joinpath('.cache', 'ms-playwright').exists():
    raise SystemExit('runtime image contains Playwright browser binaries')
if Path('/ms-playwright').exists():
    raise SystemExit('runtime image contains Playwright browser binaries')
if Path.home().joinpath('ThreadsOperations').exists():
    raise SystemExit('runtime image contains Worker durable data')
"""
    result = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "python", image, "-c", check],
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise SmokeFailure("runtime image user/content check failed")


def _database_operation(operation: str, worker_id: UUID) -> None:
    _compose(
        "run",
        "--rm",
        "-T",
        "--no-deps",
        "--entrypoint",
        "python",
        "http",
        "-",
        operation,
        str(worker_id),
        input_text=_STATE_HELPER,
        timeout=100,
    )


def _json_object(payload: bytes) -> dict[str, object] | None:
    try:
        decoded: object = json.loads(payload)
    except json.JSONDecodeError, UnicodeDecodeError:
        return None
    if not isinstance(decoded, dict):
        return None
    result: dict[str, object] = {}
    for key, value in cast(dict[object, object], decoded).items():
        if not isinstance(key, str):
            return None
        result[key] = value
    return result


def _get_json(path: str) -> tuple[int | None, dict[str, object] | None]:
    port = os.environ["THREADS_PLATFORM_SMOKE_HTTP_PORT"]
    try:
        with urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as response:
            return response.status, _json_object(response.read())
    except HTTPError as error:
        return error.code, _json_object(error.read())
    except TimeoutError, URLError, OSError, json.JSONDecodeError:
        return None, None


def _get_metrics() -> tuple[int | None, str | None, str | None]:
    port = os.environ["THREADS_PLATFORM_SMOKE_HTTP_PORT"]
    try:
        with urlopen(f"http://127.0.0.1:{port}/metrics", timeout=3) as response:
            return (
                response.status,
                response.read().decode("utf-8"),
                response.headers.get("Content-Type"),
            )
    except HTTPError as error:
        return (
            error.code,
            error.read().decode("utf-8", "replace"),
            error.headers.get("Content-Type"),
        )
    except TimeoutError, URLError, OSError, UnicodeDecodeError:
        return None, None, None


def _wait_http_metrics(timeout: int = 30) -> tuple[str, str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, body, content_type = _get_metrics()
        if status == 200 and body is not None and content_type is not None:
            if not content_type.startswith("text/plain"):
                raise SmokeFailure("HTTP metrics content type is invalid")
            return body, content_type
        time.sleep(1)
    raise SmokeFailure("HTTP /metrics did not become available")


def _metric_value(payload: str, sample: str) -> float | None:
    match = re.search(rf"^{re.escape(sample)} ([0-9]+(?:\.[0-9]+)?)$", payload, re.MULTILINE)
    return float(match.group(1)) if match is not None else None


def _scheduler_metrics_from_http_container() -> str:
    fetch = (
        "from urllib.request import urlopen; "
        "print(urlopen('http://scheduler:9101/metrics', timeout=3)"
        ".read().decode('utf-8'))"
    )
    return _compose("exec", "-T", "http", "python", "-c", fetch)


def _wait_scheduler_tick_count(minimum: int, timeout: int = 30) -> float:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            payload = _scheduler_metrics_from_http_container()
        except SmokeFailure:
            time.sleep(1)
            continue
        values = re.findall(
            r'^threads_platform_scheduler_ticks_total\{outcome="(?:success|error)"\} '
            r"([0-9]+(?:\.[0-9]+)?)$",
            payload,
            re.MULTILINE,
        )
        count = sum(float(value) for value in values)
        if count >= minimum:
            return count
        time.sleep(0.5)
    raise SmokeFailure("scheduler metrics did not report the expected tick count")


def _assert_scheduler_metrics_are_private() -> None:
    config = _json_object(_compose("config", "--format", "json").encode("utf-8"))
    services = config.get("services") if config is not None else None
    if not isinstance(services, dict):
        raise SmokeFailure("Compose service configuration could not be inspected")
    scheduler = services.get("scheduler")
    if not isinstance(scheduler, dict):
        raise SmokeFailure("scheduler service is missing from Compose configuration")
    ports = scheduler.get("ports")
    if ports not in (None, []):
        raise SmokeFailure("scheduler metrics must not publish a host port")


def _assert_tracing_is_disabled_and_private() -> None:
    config = _json_object(_compose("config", "--format", "json").encode("utf-8"))
    services = config.get("services") if config is not None else None
    if not isinstance(services, dict):
        raise SmokeFailure("Compose service configuration could not be inspected")
    for name in ("http", "scheduler"):
        service = services.get(name)
        environment = service.get("environment") if isinstance(service, dict) else None
        if not isinstance(environment, dict):
            raise SmokeFailure(f"{name} service environment could not be inspected")
        if environment.get("THREADS_PLATFORM_TRACING_ENABLED") not in ("false", False):
            raise SmokeFailure(f"{name} tracing must remain disabled in the Compose smoke")
    if any(
        any(token in name.casefold() for token in ("opentelemetry", "jaeger", "tempo", "zipkin"))
        for name in services
    ):
        raise SmokeFailure("Compose smoke must not add a tracing collector service")
    for name, service in services.items():
        ports = service.get("ports") if isinstance(service, dict) else None
        if not isinstance(ports, list):
            continue
        for port in ports:
            if isinstance(port, dict) and port.get("target") in (4317, 4318):
                raise SmokeFailure(f"{name} must not publish an OTLP port")


def _wait_for(
    path: str,
    expected: Callable[[int | None, dict[str, object] | None], bool],
    description: str,
    timeout: int = 75,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, body = _get_json(path)
        if expected(status, body) and body is not None:
            return body
        time.sleep(1)
    raise SmokeFailure(f"HTTP {path} did not reach {description}")


def _expect_live() -> None:
    body = _wait_for(
        "/health",
        lambda status, payload: status == 200 and payload == {"status": "ok"},
        "process liveness",
    )
    if body != {"status": "ok"}:
        raise SmokeFailure("unexpected liveness response")


def _assert_bounded_readiness(body: dict[str, object]) -> dict[str, int]:
    if set(body) != {"overall", "database", "fleet", "workers_available", "workers"}:
        raise SmokeFailure("readiness response has unexpected fields")
    if body.get("overall") not in {"READY", "DEGRADED", "NOT_READY"}:
        raise SmokeFailure("readiness overall state is invalid")
    if body.get("database") not in {"UP", "DOWN"}:
        raise SmokeFailure("readiness database state is invalid")
    if body.get("fleet") not in {"EMPTY", "HEALTHY", "DEGRADED"}:
        raise SmokeFailure("readiness fleet state is invalid")
    if type(body.get("workers_available")) is not bool:
        raise SmokeFailure("readiness Worker availability is invalid")
    workers = body.get("workers")
    expected_counts = {
        "total",
        "online",
        "degraded",
        "draining",
        "offline",
        "registering",
        "upgrade_required",
    }
    if not isinstance(workers, dict):
        raise SmokeFailure("readiness Worker counts are not bounded")
    counts_payload: dict[str, object] = {}
    for key, value in cast(dict[object, object], workers).items():
        if not isinstance(key, str):
            raise SmokeFailure("readiness Worker counts are not bounded")
        counts_payload[key] = value
    if set(counts_payload) != expected_counts:
        raise SmokeFailure("readiness Worker counts are not bounded")
    counts: dict[str, int] = {}
    for name in expected_counts:
        count = counts_payload.get(name)
        if type(count) is not int or count < 0:
            raise SmokeFailure("readiness Worker counts are invalid")
        counts[name] = count
    return counts


def _expect_database_ready() -> dict[str, object]:
    body = _wait_for(
        "/ready",
        lambda status, payload: (
            status == 200 and payload is not None and payload.get("database") == "UP"
        ),
        "PostgreSQL readiness",
    )
    _assert_bounded_readiness(body)
    return body


def _run_smoke() -> None:
    port = os.environ["THREADS_PLATFORM_SMOKE_HTTP_PORT"]
    if not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise SmokeFailure("THREADS_PLATFORM_SMOKE_HTTP_PORT must be a valid TCP port")

    _compose("build", timeout=900)
    _check_runtime_image()
    print("PASS image build, non-root identity, and runtime-content boundary")

    _compose("up", "--detach", "--wait", "postgres", timeout=180)
    _compose("up", "--force-recreate", "migrate", timeout=240)
    _compose("up", "--detach", "http", "scheduler", timeout=180)
    _expect_live()
    initial_metrics, _ = _wait_http_metrics()
    if _metric_value(initial_metrics, "threads_platform_database_up") != 1.0:
        raise SmokeFailure("HTTP metrics did not report PostgreSQL as available")
    ready = _expect_database_ready()
    if (
        ready.get("overall") != "READY"
        or ready.get("fleet") != "EMPTY"
        or _assert_bounded_readiness(ready)["total"] != 0
    ):
        raise SmokeFailure("initial readiness did not report an empty Worker fleet")

    http_id = _compose("ps", "-q", "http")
    scheduler_id = _compose("ps", "-q", "scheduler")
    postgres_id = _compose("ps", "-q", "postgres")
    if not http_id or not scheduler_id or http_id == scheduler_id or not postgres_id:
        raise SmokeFailure("HTTP, scheduler, and PostgreSQL process boundaries are invalid")
    _assert_scheduler_metrics_are_private()
    _assert_tracing_is_disabled_and_private()
    scheduler_ticks_before_restart = _wait_scheduler_tick_count(20)
    print("PASS HTTP and private scheduler metrics listeners")

    preserved_worker_id = uuid4()
    _database_operation("seed-offline", preserved_worker_id)
    _compose("stop", "http", "scheduler")

    recovered_worker_id = uuid4()
    _database_operation("seed-expired", recovered_worker_id)
    _compose("up", "--detach", "--force-recreate", "--no-deps", "http", "scheduler")
    _expect_live()
    _expect_database_ready()
    restarted_metrics, _ = _wait_http_metrics()
    if (
        _metric_value(restarted_metrics, "threads_platform_database_up") != 1.0
        or _metric_value(
            restarted_metrics,
            'threads_platform_workers{status="OFFLINE"}',
        )
        != 2.0
    ):
        raise SmokeFailure("HTTP metrics did not rediscover PostgreSQL state after restart")
    scheduler_ticks_after_restart = _wait_scheduler_tick_count(1)
    if scheduler_ticks_after_restart >= scheduler_ticks_before_restart:
        raise SmokeFailure("scheduler process-local metrics did not reset after restart")
    _database_operation("verify-offline", preserved_worker_id)
    _database_operation("wait-expired", recovered_worker_id)
    restarted_ready = _expect_database_ready()
    restarted_workers = _assert_bounded_readiness(restarted_ready)
    if (
        restarted_ready.get("overall") != "DEGRADED"
        or restarted_ready.get("fleet") != "DEGRADED"
        or restarted_workers["total"] != 2
        or restarted_workers["offline"] != 2
    ):
        raise SmokeFailure("restarted readiness did not reflect persisted Worker state")
    if _compose("ps", "-q", "postgres") != postgres_id:
        raise SmokeFailure("PostgreSQL container changed during HTTP/scheduler restart")
    print("PASS HTTP and scheduler restart with PostgreSQL state rediscovery")

    _compose("stop", "scheduler")
    _compose("stop", "postgres")
    metrics_status, unavailable_metrics, metrics_content_type = _get_metrics()
    if (
        metrics_status != 200
        or unavailable_metrics is None
        or metrics_content_type is None
        or not metrics_content_type.startswith("text/plain")
        or _metric_value(unavailable_metrics, "threads_platform_database_up") != 0.0
        or re.search(r"^threads_platform_workers\{", unavailable_metrics, re.MULTILINE)
        or re.search(r"^threads_platform_worker_jobs\{", unavailable_metrics, re.MULTILINE)
    ):
        raise SmokeFailure("database outage metrics response was not bounded")
    live = _get_json("/health")
    if live != (200, {"status": "ok"}):
        raise SmokeFailure("/health did not remain live during database outage")
    unavailable = _wait_for(
        "/ready",
        lambda status, payload: (
            status == 503
            and payload is not None
            and payload.get("overall") == "NOT_READY"
            and payload.get("database") == "DOWN"
        ),
        "fail-closed database readiness",
        timeout=20,
    )
    unavailable_workers = _assert_bounded_readiness(unavailable)
    if (
        unavailable.get("overall") != "NOT_READY"
        or unavailable.get("database") != "DOWN"
        or unavailable_workers["total"] != 0
    ):
        raise SmokeFailure("database outage readiness contract changed")

    _compose("up", "--detach", "--wait", "postgres", timeout=180)
    ready_after_outage = _expect_database_ready()
    if ready_after_outage.get("database") != "UP":
        raise SmokeFailure("readiness did not recover after PostgreSQL returned")
    _database_operation("verify-offline", preserved_worker_id)
    _database_operation("wait-expired", recovered_worker_id)
    _compose("up", "--detach", "scheduler")
    print("PASS /health liveness and /ready database outage recovery")


def main() -> int:
    project_name = os.environ.get("THREADS_PLATFORM_SMOKE_PROJECT_NAME") or (
        f"threads-cp-smoke-{uuid4().hex[:10]}"
    )
    os.environ["COMPOSE_PROJECT_NAME"] = project_name
    os.environ.setdefault(
        "THREADS_PLATFORM_IMAGE", f"threads-control-plane:smoke-{uuid4().hex[:10]}"
    )
    os.environ.setdefault("THREADS_PLATFORM_SCHEDULER_POLL_INTERVAL_SECONDS", "0.25")
    if "THREADS_PLATFORM_SMOKE_HTTP_PORT" not in os.environ:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            os.environ["THREADS_PLATFORM_SMOKE_HTTP_PORT"] = str(listener.getsockname()[1])

    failure: str | None = None
    try:
        _run_smoke()
    except SmokeFailure as error:
        failure = str(error)
    except (OSError, subprocess.SubprocessError) as error:
        failure = f"smoke subprocess failed ({type(error).__name__})"
    try:
        _compose("down", "--volumes", "--remove-orphans", timeout=180)
    except SmokeFailure, OSError, subprocess.SubprocessError:
        if failure is None:
            failure = "Docker Compose cleanup failed"

    if failure is not None:
        print(f"Control Plane Compose smoke failed: {failure}", file=sys.stderr)
        return 1
    print("Control Plane Compose restart/recovery smoke passed; temporary volume removed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
