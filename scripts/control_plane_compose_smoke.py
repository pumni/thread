from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

import websockets
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from threads_platform import controller_tls_admin
from threads_platform.workers.control_client import HttpWorkerControlClient
from threads_platform.workers.key_store import WorkerDeviceIdentity

_ROOT_CERT_PATH: Path | None = None
_TLS_ADMIN_DIR = "/var/lib/threads/controller-tls-admin"
_TLS_SERVING_DIR = "/var/lib/threads/controller-tls-serving"
_SENSITIVE_VALUES: list[str] = []


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
    environment = os.environ.copy()
    environment.setdefault("THREADS_PLATFORM_TLS_LAN_ADDRESS", "127.0.0.1")
    result = subprocess.run(
        ["docker", "compose", *arguments],
        capture_output=True,
        check=False,
        text=True,
        input=input_text,
        timeout=timeout,
        env=environment,
    )
    if result.returncode != 0:
        label = arguments[0] if arguments else "compose"
        raise SmokeFailure(f"Docker Compose {label} step failed (exit {result.returncode})")
    return result.stdout.strip()


def _tls_context() -> ssl.SSLContext:
    if _ROOT_CERT_PATH is None:
        raise SmokeFailure("synthetic Controller root was not prepared")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    context.load_verify_locations(cafile=str(_ROOT_CERT_PATH))
    return context


def _prepare_tls_state(root_directory: Path) -> str:
    global _ROOT_CERT_PATH
    _compose("--profile", "tls-admin", "up", "tls-admin", timeout=90)
    cli_fingerprint = _compose(
        "run",
        "--rm",
        "--no-deps",
        "tls-admin",
        "python",
        "-m",
        "threads_platform.controller_tls_admin",
        "fingerprint",
        "--admin-dir",
        _TLS_ADMIN_DIR,
    )
    root_path = root_directory / "root-cert.pem"
    _compose(
        "cp",
        f"tls-admin:{_TLS_ADMIN_DIR}/root-cert.pem",
        str(root_path),
        timeout=30,
    )
    root = x509.load_pem_x509_certificate(root_path.read_bytes())
    expected = "SHA256:" + hashlib.sha256(root.public_bytes(serialization.Encoding.DER)).hexdigest()
    if cli_fingerprint != expected:
        raise SmokeFailure("local TLS admin CLI fingerprint did not match the provisioned root DER")
    _ROOT_CERT_PATH = root_path
    return expected


def _assert_tls_prepare_completed() -> None:
    container_id = _compose("ps", "-a", "-q", "tls-prepare")
    if not container_id:
        raise SmokeFailure("normal HTTP startup did not create a TLS prepare container")
    result = _compose("inspect", "--format", "{{.State.Status}}:{{.State.ExitCode}}", container_id)
    if result != "exited:0":
        raise SmokeFailure("TLS preparation did not complete successfully before HTTP")


def _serving_leaf_summary() -> dict[str, object]:
    script = (
        "import hashlib,json,sys; from cryptography import x509; "
        "from cryptography.hazmat.primitives import serialization; "
        "from pathlib import Path; "
        "p=Path('/var/lib/threads/controller-tls-serving/leaf-fullchain.pem'); "
        "c=x509.load_pem_x509_certificates(p.read_bytes()); "
        "print(json.dumps({'leaf':hashlib.sha256(c[0].public_bytes(serialization.Encoding.DER)).hexdigest(),"
        "'root':'SHA256:'+hashlib.sha256(c[1].public_bytes(serialization.Encoding.DER)).hexdigest(),"
        "'not_after':c[0].not_valid_after_utc.isoformat()}))"
    )
    output = _compose("run", "--rm", "--no-deps", "tls-admin", "python", "-c", script)
    try:
        summary = json.loads(output)
    except json.JSONDecodeError as error:
        raise SmokeFailure("TLS admin could not report serving certificate state") from error
    if not isinstance(summary, dict):
        raise SmokeFailure("TLS admin returned invalid serving certificate state")
    return summary


def _install_expiring_leaf_fixture() -> None:
    script = r"""
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
admin = Path('/var/lib/threads/controller-tls-admin')
serving = Path('/var/lib/threads/controller-tls-serving')
root = x509.load_pem_x509_certificate((admin / 'root-cert.pem').read_bytes())
root_key = serialization.load_pem_private_key((admin / 'root-key.pem').read_bytes(), password=None)
key = ec.generate_private_key(ec.SECP256R1())
now = datetime.now(UTC)
leaf = (
    x509.CertificateBuilder()
    .subject_name(
        x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Smoke Controller')])
    )
    .issuer_name(root.subject)
    .public_key(key.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before(now - timedelta(minutes=5))
    .not_valid_after(now + timedelta(days=20))
    .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
    .add_extension(
        x509.KeyUsage(
            digital_signature=True,
            content_commitment=False,
            key_encipherment=False,
            data_encipherment=False,
            key_agreement=False,
            key_cert_sign=False,
            crl_sign=False,
            encipher_only=False,
            decipher_only=False,
        ),
        critical=True,
    )
    .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
    .add_extension(
        x509.SubjectAlternativeName([
            x509.IPAddress(__import__('ipaddress').IPv4Address(sys.argv[1])),
            x509.IPAddress(__import__('ipaddress').IPv4Address('127.0.0.1')),
        ]),
        critical=False,
    )
    .sign(root_key, hashes.SHA256())
)
cert = leaf.public_bytes(serialization.Encoding.PEM)
(serving / 'leaf-key.pem').write_bytes(key.private_bytes(serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
(serving / 'leaf-cert.pem').write_bytes(cert)
(serving / 'leaf-fullchain.pem').write_bytes(cert + root.public_bytes(serialization.Encoding.PEM))
"""
    address = os.environ.get("THREADS_PLATFORM_TLS_LAN_ADDRESS", "127.0.0.1")
    _compose("run", "--rm", "--no-deps", "tls-admin", "python", "-c", script, address)


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
    image_config = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{json .Config.Cmd}}", image],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    command = image_config.stdout
    if image_config.returncode != 0 or any(
        required not in command
        for required in (
            "--ssl-certfile",
            "--ssl-keyfile",
            "--no-proxy-headers",
            "--no-access-log",
        )
    ):
        raise SmokeFailure("HTTP image does not require direct Uvicorn HTTPS")
    if "root-key.pem" in command or "controller-tls-admin" in command:
        raise SmokeFailure("HTTP image command exposes the root signing-key path")


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


def _get_json(
    path: str,
    *,
    bearer: str | None = None,
) -> tuple[int | None, dict[str, object] | None]:
    port = os.environ["THREADS_PLATFORM_SMOKE_HTTP_PORT"]
    headers = {"Authorization": f"Bearer {bearer}"} if bearer is not None else {}
    request = Request(f"https://127.0.0.1:{port}{path}", headers=headers)
    try:
        with urlopen(request, context=_tls_context(), timeout=3) as response:
            return response.status, _json_object(response.read())
    except HTTPError as error:
        return error.code, _json_object(error.read())
    except TimeoutError, URLError, OSError, json.JSONDecodeError:
        return None, None


def _post_json(
    path: str,
    payload: dict[str, object],
    *,
    bearer: str | None = None,
) -> tuple[int | None, dict[str, object] | None, object | None]:
    port = os.environ["THREADS_PLATFORM_SMOKE_HTTP_PORT"]
    headers = {"Content-Type": "application/json"}
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    request = Request(
        f"https://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, context=_tls_context(), timeout=5) as response:
            return response.status, _json_object(response.read()), response.headers
    except HTTPError as error:
        return error.code, _json_object(error.read()), error.headers
    except TimeoutError, URLError, OSError, json.JSONDecodeError:
        return None, None, None


def _create_smoke_operator(owner_token: str, username: str, role: str) -> tuple[str, str]:
    status, body, headers = _post_json(
        "/v1/operator/users",
        {"username": username, "role": role},
        bearer=owner_token,
    )
    if (
        status != 201
        or body is None
        or not isinstance(body.get("temporary_password"), str)
        or not hasattr(headers, "get")
        or headers.get("Cache-Control") != "no-store"
    ):
        raise SmokeFailure("Operator user creation response was not one-time/no-store")
    temporary_password = cast(str, body["temporary_password"])
    _SENSITIVE_VALUES.append(temporary_password)
    login_status, login, _ = _post_json(
        "/v1/operator/login",
        {"username": username, "password": temporary_password},
    )
    if login_status != 200 or login is None or not isinstance(login.get("access_token"), str):
        raise SmokeFailure("temporary Operator password did not authenticate")
    token = cast(str, login["access_token"])
    _SENSITIVE_VALUES.append(token)
    password_status, _, _ = _post_json(
        "/v1/operator/me/password",
        {"new_password": f"{username} smoke passphrase 2026"},
        bearer=token,
    )
    if password_status != 200:
        raise SmokeFailure("temporary Operator password could not be changed")
    _SENSITIVE_VALUES.append(f"{username} smoke passphrase 2026")
    return token, f"{username} smoke passphrase 2026"


def _operator_bootstrap_api_parity() -> str:
    bootstrap_password = "synthetic local owner passphrase"
    _SENSITIVE_VALUES.append(bootstrap_password)
    output = _compose(
        "exec",
        "-T",
        "http",
        "python",
        "-m",
        "threads_platform.operator_bootstrap",
        "--username",
        "smoke-owner",
        input_text=f"{bootstrap_password}\n",
        timeout=60,
    )
    if "First Owner created: smoke-owner" not in output:
        raise SmokeFailure("local stdin bootstrap did not create the first Workspace Owner")

    status, login, headers = _post_json(
        "/v1/operator/login",
        {"username": "smoke-owner", "password": bootstrap_password},
    )
    if (
        status != 200
        or login is None
        or not isinstance(login.get("access_token"), str)
        or not hasattr(headers, "get")
        or headers.get("Cache-Control") != "no-store"
    ):
        raise SmokeFailure("Linux/Docker first Owner could not log in through the Operator API")
    owner_token = cast(str, login["access_token"])
    _SENSITIVE_VALUES.append(owner_token)
    owner_status, owner = _get_json("/v1/operator/me", bearer=owner_token)
    if owner_status != 200 or owner is None or owner.get("role") != "OWNER":
        raise SmokeFailure("Operator /me role did not match the Linux/Docker first Owner")

    role_tokens = {"OWNER": owner_token}
    for role in ("ADMIN", "OPERATOR", "VIEWER"):
        role_tokens[role], _ = _create_smoke_operator(owner_token, f"smoke-{role.lower()}", role)

    for role, token in role_tokens.items():
        me_status, me = _get_json("/v1/operator/me", bearer=token)
        users_status, _ = _get_json("/v1/operator/users", bearer=token)
        expected_users_status = 200 if role in {"OWNER", "ADMIN"} else 403
        enrollment_status, _, _ = _post_json(
            "/v1/workers/enrollments",
            {"created_by": "forged-smoke-actor"},
            bearer=token,
        )
        expected_enrollment_status = 200 if role in {"OWNER", "ADMIN"} else 403
        command_status, _, _ = _post_json("/v1/operator/commands", {}, bearer=token)
        expected_command_status = 403 if role == "VIEWER" else 422
        if (
            me_status != 200
            or me is None
            or me.get("role") != role
            or users_status != expected_users_status
            or enrollment_status != expected_enrollment_status
            or command_status != expected_command_status
        ):
            raise SmokeFailure(f"Linux/Docker Operator role parity failed for {role}")

    if _post_json("/v1/operator/bootstrap-owner", {}, bearer=owner_token)[0] != 404:
        raise SmokeFailure("first Owner bootstrap unexpectedly has a network endpoint")
    repeat = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "http",
            "python",
            "-m",
            "threads_platform.operator_bootstrap",
            "--username",
            "another-owner",
        ],
        capture_output=True,
        check=False,
        text=True,
        input="another synthetic passphrase\n",
        timeout=60,
    )
    if repeat.returncode == 0 or "OPERATOR_BOOTSTRAP_ALREADY_COMPLETED" not in repeat.stderr:
        raise SmokeFailure("repeat Owner bootstrap was not explicitly rejected")
    return owner_token


def _assert_tls_isolation() -> None:
    config = _json_object(
        _compose("--profile", "tls-admin", "config", "--format", "json").encode("utf-8")
    )
    services = config.get("services") if config is not None else None
    if not isinstance(services, dict):
        raise SmokeFailure("Compose TLS service configuration could not be inspected")
    http = services.get("http")
    scheduler = services.get("scheduler")
    tls_admin = services.get("tls-admin")
    tls_prepare = services.get("tls-prepare")
    if not all(isinstance(service, dict) for service in (http, scheduler, tls_admin, tls_prepare)):
        raise SmokeFailure("Compose TLS services are incomplete")
    if "tls-admin" not in cast(dict[str, object], tls_admin).get("profiles", []):
        raise SmokeFailure("TLS administration must require its explicit Compose profile")
    if cast(dict[str, object], tls_prepare).get("profiles"):
        raise SmokeFailure("normal TLS preparation must not require an admin profile")
    dependencies = cast(dict[str, object], http).get("depends_on", {})
    prepare_dependency = dependencies.get("tls-prepare") if isinstance(dependencies, dict) else None
    if (
        not isinstance(dependencies, dict)
        or "tls-admin" in dependencies
        or not isinstance(prepare_dependency, dict)
        or prepare_dependency.get("condition") != "service_completed_successfully"
    ):
        raise SmokeFailure("ordinary HTTP startup must wait for successful TLS preparation")
    http_volumes = cast(dict[str, object], http).get("volumes")
    scheduler_volumes = cast(dict[str, object], scheduler).get("volumes")
    admin_volumes = cast(dict[str, object], tls_admin).get("volumes")
    prepare_volumes = cast(dict[str, object], tls_prepare).get("volumes")
    if not isinstance(http_volumes, list) or not isinstance(admin_volumes, list):
        raise SmokeFailure("Compose TLS volume boundaries could not be inspected")
    if not isinstance(prepare_volumes, list):
        raise SmokeFailure("TLS preparation volume boundaries could not be inspected")
    if any("controller-tls-admin" in str(volume) for volume in http_volumes):
        raise SmokeFailure("HTTP service has access to root-admin TLS state")
    if any("controller-tls" in str(volume) for volume in scheduler_volumes or []):
        raise SmokeFailure("scheduler service has a TLS private-key volume")
    if not any("controller_tls_admin" in str(volume) for volume in admin_volumes):
        raise SmokeFailure("one-shot TLS admin service has no root-admin volume")
    if not any(
        isinstance(volume, dict)
        and volume.get("source") == "controller_tls_admin"
        and volume.get("read_only") is True
        for volume in prepare_volumes
    ) or not any(
        isinstance(volume, dict) and volume.get("source") == "controller_tls_serving"
        for volume in prepare_volumes
    ):
        raise SmokeFailure("TLS preparation must read root-admin state and write serving state")
    prepare_command = cast(dict[str, object], tls_prepare).get("command")
    if not isinstance(prepare_command, list) or "ensure" not in prepare_command:
        raise SmokeFailure("normal TLS preparation must execute the ensure operation")
    http_environment = cast(dict[str, object], http).get("environment")
    scheduler_environment = cast(dict[str, object], scheduler).get("environment")
    if not isinstance(http_environment, dict) or not isinstance(scheduler_environment, dict):
        raise SmokeFailure("Compose TLS service environments could not be inspected")
    for environment in (http_environment, scheduler_environment):
        for name, value in cast(dict[str, object], environment).items():
            normalized_name = str(name).upper()
            normalized_value = str(value).upper()
            if (
                any(
                    marker in normalized_name
                    for marker in ("ROOT_KEY", "ROOT_PRIVATE_KEY", "PRIVATE_KEY_BYTES")
                )
                or ("-----BEGIN " + "PRIVATE KEY" + "-----") in normalized_value
            ):
                raise SmokeFailure("Compose environment contains root private-key material")
    if (
        http_environment.get("THREADS_PLATFORM_TLS_CERTFILE")
        != f"{_TLS_SERVING_DIR}/leaf-fullchain.pem"
        or http_environment.get("THREADS_PLATFORM_TLS_KEYFILE")
        != f"{_TLS_SERVING_DIR}/leaf-key.pem"
    ):
        raise SmokeFailure("HTTP service TLS paths do not point to the serving leaf")
    if any("TLS_" in str(name) for name in scheduler_environment):
        raise SmokeFailure("scheduler environment contains TLS configuration")
    if any(
        "THREADS_PLATFORM_WORKER_TLS_REQUIRED"
        in cast(dict[str, object], service).get("environment", {})
        for service in (http, scheduler)
    ):
        raise SmokeFailure("Compose still exposes the Worker TLS bypass setting")

    _compose(
        "exec",
        "-T",
        "http",
        "python",
        "-c",
        "from pathlib import Path; assert not Path("
        "'/var/lib/threads/controller-tls-admin/root-key.pem').exists()",
    )
    _compose(
        "exec",
        "-T",
        "scheduler",
        "python",
        "-c",
        "from pathlib import Path; assert not Path("
        "'/var/lib/threads/controller-tls-serving/leaf-key.pem').exists(); "
        "assert not Path('/var/lib/threads/controller-tls-admin/root-key.pem').exists()",
    )
    _compose(
        "exec",
        "-T",
        "http",
        "python",
        "-c",
        "import pathlib; p=pathlib.Path('/proc/1/cmdline').read_bytes()+"
        "pathlib.Path('/proc/1/environ').read_bytes(); "
        "assert b'root-key.pem' not in p and b'controller-tls-admin' not in p",
    )


def _assert_secret_free_logs() -> None:
    logs = _compose("logs", "--no-color", timeout=60)
    if "-----BEGIN PRIVATE KEY-----" in logs or "-----BEGIN EC PRIVATE KEY-----" in logs:
        raise SmokeFailure("a private key appeared in Compose logs")
    if any(secret and secret in logs for secret in _SENSITIVE_VALUES):
        raise SmokeFailure("an Operator or Worker credential appeared in Compose logs")


async def _exercise_worker_https_wss(owner_token: str, scratch: Path) -> None:
    if _ROOT_CERT_PATH is None:
        raise SmokeFailure("verified private root is unavailable")
    enrollment_status, enrollment, _ = _post_json("/v1/workers/enrollments", {}, bearer=owner_token)
    if (
        enrollment_status != 200
        or enrollment is None
        or not isinstance(enrollment.get("enrollment_code"), str)
    ):
        raise SmokeFailure("synthetic Worker enrollment could not be created")
    _SENSITIVE_VALUES.append(cast(str, enrollment["enrollment_code"]))

    port = os.environ["THREADS_PLATFORM_SMOKE_HTTP_PORT"]
    worker_id = uuid4()
    client = HttpWorkerControlClient(
        f"https://127.0.0.1:{port}",
        private_root_certificate=_ROOT_CERT_PATH,
    )
    try:
        await client.authenticate(
            worker_id,
            WorkerDeviceIdentity.generate(),
            enrollment_pending=True,
            enrollment_code=cast(str, enrollment["enrollment_code"]),
            display_name="DX-06 synthetic Worker",
            hostname="dx06-smoke-worker",
            max_concurrent_jobs=1,
            max_browser_sessions=1,
        )
        await client.hello(
            worker_id,
            agent_version="dx06-smoke",
            capabilities=(),
            max_concurrent_jobs=1,
            max_browser_sessions=1,
            active_browser_sessions=0,
        )
        bearer = client._access_token
        if not bearer:
            raise SmokeFailure("Worker HTTPS authentication did not create a session")
        _SENSITIVE_VALUES.append(bearer)

        uri = f"wss://127.0.0.1:{port}/v1/workers/connect"
        context = _tls_context()
        async with websockets.connect(
            uri,
            ssl=context,
            additional_headers={"Authorization": f"Bearer {bearer}"},
            proxy=None,
            open_timeout=5,
        ) as websocket:
            await websocket.send(
                json.dumps(
                    {"type": "worker.heartbeat", "healthy": True, "active_browser_sessions": 0}
                )
            )
            message = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5))
            if message != {"type": "worker.heartbeat.accepted", "status": "ONLINE"}:
                raise SmokeFailure("verified WSS Worker heartbeat was not accepted")

        try:
            async with websockets.connect(
                uri.replace("wss://", "ws://", 1), proxy=None, open_timeout=3
            ):
                raise SmokeFailure("plaintext ws unexpectedly connected to the HTTPS listener")
        except OSError, TimeoutError, websockets.exceptions.WebSocketException:
            pass

        wrong_admin = scratch / "wrong-root-admin"
        wrong_serving = scratch / "wrong-root-serving"
        controller_tls_admin.provision(wrong_admin, wrong_serving, "127.0.0.1")
        wrong_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        wrong_context.verify_mode = ssl.CERT_REQUIRED
        wrong_context.check_hostname = True
        wrong_context.load_verify_locations(cafile=str(wrong_admin / "root-cert.pem"))
        try:
            async with websockets.connect(
                uri,
                ssl=wrong_context,
                additional_headers={"Authorization": f"Bearer {bearer}"},
                proxy=None,
                open_timeout=3,
            ):
                raise SmokeFailure("WSS accepted a connection under the wrong Controller root")
        except OSError, TimeoutError, websockets.exceptions.WebSocketException:
            pass
    finally:
        await client._client.aclose()


def _get_metrics() -> tuple[int | None, str | None, str | None]:
    port = os.environ["THREADS_PLATFORM_SMOKE_HTTP_PORT"]
    try:
        with urlopen(
            f"https://127.0.0.1:{port}/metrics", context=_tls_context(), timeout=3
        ) as response:
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


def _run_smoke(scratch: Path) -> None:
    port = os.environ["THREADS_PLATFORM_SMOKE_HTTP_PORT"]
    if not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise SmokeFailure("THREADS_PLATFORM_SMOKE_HTTP_PORT must be a valid TCP port")

    _compose("build", timeout=900)
    _check_runtime_image()
    print("PASS image build, non-root identity, and runtime-content boundary")
    fingerprint = _prepare_tls_state(scratch)
    print(f"PASS TLS admin provisioning and local CLI fingerprint {fingerprint}")

    _compose("up", "--detach", "--wait", "postgres", timeout=180)
    _compose("up", "--force-recreate", "migrate", timeout=240)
    _compose("up", "--detach", "http", "scheduler", timeout=180)
    _assert_tls_prepare_completed()
    _assert_tls_isolation()
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
    owner_token = _operator_bootstrap_api_parity()
    print("PASS local stdin first-Owner bootstrap and Linux/Docker Operator API parity")
    asyncio.run(_exercise_worker_https_wss(owner_token, scratch))
    print("PASS private-root Worker HTTPS, verified WSS, ws rejection, and wrong-root rejection")

    initial_tls = _serving_leaf_summary()
    _install_expiring_leaf_fixture()
    expiring_tls = _serving_leaf_summary()
    if initial_tls.get("root") != expiring_tls.get("root"):
        raise SmokeFailure("expiring leaf fixture changed the Controller root")
    _compose("rm", "--stop", "--force", "tls-prepare")
    _compose("up", "--detach", "--force-recreate", "http", timeout=180)
    _assert_tls_prepare_completed()
    renewed_tls = _serving_leaf_summary()
    if renewed_tls.get("root") != initial_tls.get("root") or renewed_tls.get(
        "leaf"
    ) == expiring_tls.get("leaf"):
        raise SmokeFailure("startup ensure did not renew the leaf under the same Controller root")
    renewed_expiry = renewed_tls.get("not_after")
    if not isinstance(renewed_expiry, str):
        raise SmokeFailure("renewed leaf expiry is unavailable")
    if datetime.fromisoformat(renewed_expiry) - datetime.now(UTC) <= timedelta(days=80):
        raise SmokeFailure("startup ensure did not issue a 90-day leaf")
    _expect_live()
    _expect_database_ready()
    print("PASS normal TLS prepare renewed an expiring leaf under the same root before HTTP")

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
    _compose("rm", "--stop", "--force", "tls-prepare")
    _compose("up", "--detach", "--force-recreate", "http", "scheduler")
    _assert_tls_prepare_completed()
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
    _assert_secret_free_logs()
    print("PASS TLS key and credential log inspection")


def main() -> int:
    project_name = os.environ.get("THREADS_PLATFORM_SMOKE_PROJECT_NAME") or (
        f"threads-cp-smoke-{uuid4().hex[:10]}"
    )
    os.environ["COMPOSE_PROJECT_NAME"] = project_name
    os.environ.setdefault(
        "THREADS_PLATFORM_IMAGE", f"threads-control-plane:smoke-{uuid4().hex[:10]}"
    )
    os.environ.setdefault("THREADS_PLATFORM_SCHEDULER_POLL_INTERVAL_SECONDS", "0.25")
    os.environ["THREADS_PLATFORM_TLS_LAN_ADDRESS"] = "127.0.0.1"
    if "THREADS_PLATFORM_SMOKE_HTTP_PORT" not in os.environ:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            os.environ["THREADS_PLATFORM_SMOKE_HTTP_PORT"] = str(listener.getsockname()[1])

    failure: str | None = None
    scratch = tempfile.TemporaryDirectory(prefix="threads-dx06-compose-")
    try:
        _run_smoke(Path(scratch.name))
    except SmokeFailure as error:
        failure = str(error)
    except (OSError, subprocess.SubprocessError) as error:
        failure = f"smoke subprocess failed ({type(error).__name__})"
    try:
        _compose("down", "--volumes", "--remove-orphans", timeout=180)
    except SmokeFailure, OSError, subprocess.SubprocessError:
        if failure is None:
            failure = "Docker Compose cleanup failed"
    scratch.cleanup()
    global _ROOT_CERT_PATH
    _ROOT_CERT_PATH = None

    if failure is not None:
        print(f"Control Plane Compose smoke failed: {failure}", file=sys.stderr)
        return 1
    print("Control Plane Compose restart/recovery smoke passed; temporary volume removed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
