from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import httpx2
import pytest
import uvicorn
import websockets
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from starlette.types import ASGIApp

from threads_platform import controller_tls_admin as tls_admin
from threads_platform.app import create_app
from threads_platform.application.readiness import (
    ReadinessSnapshot,
    WorkerFleetReadinessCounts,
    readiness_snapshot_for_workers,
)
from threads_platform.application.worker_control import (
    AuthenticatedWorkerSession,
    WorkerControlService,
    WorkerPresence,
)
from threads_platform.config.settings import Settings
from threads_platform.domain.operators import OperatorPrincipal, OperatorRole
from threads_platform.domain.workers import WorkerStatus
from threads_platform.infrastructure.security.operator_auth import (
    IssuedOperatorSession,
    OperatorAuthService,
)
from threads_platform.workers.control_client import HttpWorkerControlClient


class _OperatorAuth:
    def __init__(self) -> None:
        self.operator = OperatorPrincipal(
            user_id=uuid4(),
            username="synthetic-owner",
            role=OperatorRole.OWNER,
            enabled=True,
            must_change_password=False,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

    async def login(self, username: str, password: str) -> IssuedOperatorSession | None:
        if username != "synthetic-owner" or password != "synthetic-passphrase":
            return None
        return IssuedOperatorSession(
            "synthetic-operator-bearer", self.operator.expires_at, self.operator
        )

    async def authenticate(self, token: str) -> OperatorPrincipal | None:
        return self.operator if token == "synthetic-operator-bearer" else None

    async def logout(self, _token: str | None) -> None:
        return None


class _WorkerControl:
    def __init__(self) -> None:
        self.worker_id = uuid4()
        self.expires_at = datetime.now(UTC) + timedelta(minutes=5)

    async def authenticate_session(self, token: str) -> AuthenticatedWorkerSession | None:
        if token != "synthetic-worker-session":
            return None
        return AuthenticatedWorkerSession(self.worker_id, self.expires_at)

    async def authenticate(self, token: str) -> UUID | None:
        return self.worker_id if token == "synthetic-worker-session" else None

    def session_time_remaining(self, session: AuthenticatedWorkerSession) -> float:
        return max((session.expires_at - datetime.now(UTC)).total_seconds(), 0.0)

    async def hello(self, *_args: object, **_kwargs: object) -> WorkerPresence:
        now = datetime.now(UTC)
        return WorkerPresence(
            self.worker_id, WorkerStatus.ONLINE, now, now + timedelta(seconds=90), True
        )

    async def heartbeat(self, *_args: object, **_kwargs: object) -> WorkerPresence:
        return await self.hello()


class _Readiness:
    async def snapshot(self) -> ReadinessSnapshot:
        return readiness_snapshot_for_workers(WorkerFleetReadinessCounts())


class _Server:
    def __init__(
        self,
        task: asyncio.Task[None],
        server: uvicorn.Server,
        listener: socket.socket,
        root_cert: Path,
        port: int,
    ) -> None:
        self.task = task
        self.server = server
        self.listener = listener
        self.root_cert = root_cert
        self.port = port

    async def stop(self) -> None:
        self.server.should_exit = True
        await asyncio.wait_for(self.task, timeout=5)


async def _start_server(
    directory: Path,
    app: ASGIApp,
    *,
    address: str = "127.0.0.1",
    leaf_profile: str = "valid",
) -> _Server:
    admin = directory / "admin"
    serving = directory / "serving"
    tls_admin.provision(admin, serving, address)
    if leaf_profile != "valid":
        _replace_leaf_profile(admin, serving, leaf_profile)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = int(listener.getsockname()[1])
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        ssl_certfile=str(serving / "leaf-fullchain.pem"),
        ssl_keyfile=str(serving / "leaf-key.pem"),
        proxy_headers=False,
        access_log=False,
        log_config=None,
        log_level="critical",
        lifespan="off",
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[listener]))
    for _ in range(500):
        if server.started:
            return _Server(task, server, listener, admin / "root-cert.pem", port)
        if task.done():
            await task
            raise RuntimeError("synthetic Uvicorn server exited before startup")
        await asyncio.sleep(0.01)
    server.should_exit = True
    await asyncio.wait_for(task, timeout=5)
    raise TimeoutError("synthetic Uvicorn server startup timed out")


def _replace_leaf_profile(admin: Path, serving: Path, profile: str) -> None:
    root = x509.load_pem_x509_certificate((admin / "root-cert.pem").read_bytes())
    root_key = serialization.load_pem_private_key((admin / "root-key.pem").read_bytes(), None)
    key = serialization.load_pem_private_key((serving / "leaf-key.pem").read_bytes(), None)
    if not isinstance(root_key, ec.EllipticCurvePrivateKey) or not isinstance(
        key, ec.EllipticCurvePrivateKey
    ):
        raise AssertionError("synthetic TLS fixture is not ECDSA")
    now = datetime.now(UTC)
    if profile == "expired":
        not_before, not_after = now - timedelta(days=3), now - timedelta(days=1)
    elif profile == "not_yet_valid":
        not_before, not_after = now + timedelta(days=1), now + timedelta(days=3)
    elif profile == "wrong_san":
        not_before, not_after = now - timedelta(minutes=5), now + timedelta(days=90)
    else:
        raise AssertionError("unknown synthetic leaf profile")
    alt_names = (
        [x509.IPAddress(ipaddress.IPv4Address("198.51.100.99"))]
        if profile == "wrong_san"
        else [x509.IPAddress(ipaddress.IPv4Address("127.0.0.1"))]
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic Controller")]))
        .issuer_name(root.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
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
        .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
        .sign(root_key, hashes.SHA256())
    )
    leaf_pem = certificate.public_bytes(serialization.Encoding.PEM)
    (serving / "leaf-cert.pem").write_bytes(leaf_pem)
    (serving / "leaf-fullchain.pem").write_bytes(
        leaf_pem + root.public_bytes(serialization.Encoding.PEM)
    )


def _http_client(client: HttpWorkerControlClient) -> httpx2.AsyncClient:
    return client._client  # pyright: ignore[reportPrivateUsage]


def _private_client_context(root_cert: Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    context.load_verify_locations(cafile=str(root_cert))
    return context


@pytest.mark.asyncio
async def test_uvicorn_direct_tls_serves_health_ready_operator_and_authenticated_wss(
    tmp_path: Path,
) -> None:
    worker = _WorkerControl()
    app = create_app(
        Settings(database_url=None),
        operator_auth_service=cast(OperatorAuthService, _OperatorAuth()),
        worker_control_service=cast(WorkerControlService, worker),
        readiness_probe=_Readiness(),
    )
    server = await _start_server(tmp_path, app)
    try:
        context = _private_client_context(server.root_cert)
        async with httpx2.AsyncClient(
            base_url=f"https://127.0.0.1:{server.port}",
            verify=context,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            assert (await client.get("/health")).status_code == 200
            assert (await client.get("/ready")).status_code == 200
            login = await client.post(
                "/v1/operator/login",
                json={"username": "synthetic-owner", "password": "synthetic-passphrase"},
            )
        assert login.status_code == 200
        assert login.json()["operator"]["role"] == "OWNER"
        async with websockets.connect(
            f"wss://127.0.0.1:{server.port}/v1/workers/connect",
            ssl=context,
            additional_headers={"Authorization": "Bearer synthetic-worker-session"},
            proxy=None,
            open_timeout=3,
        ):
            assert server.server.config.proxy_headers is False
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_worker_private_root_http_client_uses_only_supplied_root(tmp_path: Path) -> None:
    app = create_app(Settings(database_url=None))
    server = await _start_server(tmp_path / "server", app)
    try:
        correct = HttpWorkerControlClient(
            f"https://127.0.0.1:{server.port}",
            private_root_certificate=server.root_cert,
        )
        root_der = x509.load_pem_x509_certificate(server.root_cert.read_bytes()).public_bytes(
            serialization.Encoding.DER
        )
        correct_der = HttpWorkerControlClient(
            f"https://127.0.0.1:{server.port}",
            private_root_certificate=root_der,
        )
        wrong_admin = tmp_path / "wrong-admin"
        wrong_serving = tmp_path / "wrong-serving"
        tls_admin.provision(wrong_admin, wrong_serving, "127.0.0.1")
        wrong = HttpWorkerControlClient(
            f"https://127.0.0.1:{server.port}",
            private_root_certificate=wrong_admin / "root-cert.pem",
        )
        response = await _http_client(correct).get("/health")
        assert response.status_code == 200
        assert (await _http_client(correct_der).get("/health")).status_code == 200
        with pytest.raises(httpx2.ConnectError):
            await _http_client(wrong).get("/health")
        await correct.aclose()
        await correct_der.aclose()
        await wrong.aclose()
    finally:
        await server.stop()


def test_worker_private_root_rejects_expired_trust_anchor() -> None:
    now = datetime.now(UTC)
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "expired private root")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=2))
        .not_valid_after(now - timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )

    with pytest.raises(ValueError, match="controller_trust_store_invalid"):
        HttpWorkerControlClient(
            "https://127.0.0.1:8443",
            private_root_certificate=certificate.public_bytes(serialization.Encoding.DER),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["wrong_san", "expired", "not_yet_valid"])
async def test_worker_private_root_rejects_wrong_san_and_invalidity(
    tmp_path: Path, profile: str
) -> None:
    app = create_app(Settings(database_url=None))
    server = await _start_server(tmp_path, app, leaf_profile=profile)
    try:
        client = HttpWorkerControlClient(
            f"https://127.0.0.1:{server.port}",
            private_root_certificate=server.root_cert,
        )
        with pytest.raises(httpx2.ConnectError):
            await _http_client(client).get("/health")
        await client.aclose()
    finally:
        await server.stop()
