from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import ssl
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx2
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from threads_platform.application.ports.worker_agent import WorkerControlClientError
from threads_platform.workers.control_client import HttpWorkerControlClient
from threads_platform.workers.key_store import WorkerDeviceIdentity

_WORKER_TOKEN = uuid4().hex
_ENROLLMENT_CODE = uuid4().hex
_CHALLENGE_ID = UUID("3d8f6cd0-8c3f-4b5e-8fa5-d6d887f32458")


def _write_server_certificate(
    directory: Path,
    *,
    subject_alt_name: x509.GeneralName,
    expired: bool = False,
) -> tuple[Path, Path, Path]:
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "worker-control-test-ca")])
    now = datetime.now(UTC)
    ca_certificate = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
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
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    server_key = ec.generate_private_key(ec.SECP256R1())
    server_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "worker-control-test")])
    server_certificate = (
        x509.CertificateBuilder()
        .subject_name(server_name)
        .issuer_name(ca_certificate.subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=2 if expired else 1))
        .not_valid_after(now - timedelta(days=1) if expired else now + timedelta(days=30))
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
        .add_extension(x509.SubjectAlternativeName([subject_alt_name]), critical=False)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_path = directory / "worker-control-test-ca.pem"
    certificate_path = directory / "worker-control-test-server.pem"
    key_path = directory / "worker-control-test-key.pem"
    ca_path.write_bytes(ca_certificate.public_bytes(serialization.Encoding.PEM))
    certificate_path.write_bytes(
        server_certificate.public_bytes(serialization.Encoding.PEM)
        + ca_certificate.public_bytes(serialization.Encoding.PEM)
    )
    key_path.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return ca_path, certificate_path, key_path


def _response(status: int, payload: dict[str, object] | None = None) -> bytes:
    if payload is None:
        body = b""
    else:
        body = json.dumps(payload).encode("utf-8")
    reason = {200: "OK", 204: "No Content", 401: "Unauthorized", 404: "Not Found"}[status]
    return (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Content-Type: application/json\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii") + body


@asynccontextmanager
async def _control_plane_server(
    directory: Path,
    *,
    subject_alt_name: x509.GeneralName,
    expired: bool = False,
) -> AsyncGenerator[tuple[str, Path, list[tuple[str, str, dict[str, str], bytes]]]]:
    ca_path, certificate_path, key_path = _write_server_certificate(
        directory,
        subject_alt_name=subject_alt_name,
        expired=expired,
    )
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(certificate_path, key_path)
    received: list[tuple[str, str, dict[str, str], bytes]] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            method, path, _ = request_line.decode("ascii").strip().split(" ", 2)
            headers: dict[str, str] = {}
            while line := await reader.readline():
                if line == b"\r\n":
                    break
                name, value = line.decode("latin-1").split(":", 1)
                headers[name.casefold()] = value.strip()
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            received.append((method, path, headers, body))

            if path == "/v1/workers/auth/challenges":
                writer.write(
                    _response(
                        200,
                        {"challenge_id": str(_CHALLENGE_ID), "nonce": "synthetic-nonce"},
                    )
                )
            elif path == "/v1/workers/auth/sessions":
                writer.write(
                    _response(
                        200,
                        {
                            "access_token": _WORKER_TOKEN,
                            "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
                        },
                    )
                )
            elif path == "/v1/workers/jobs/claim":
                if headers.get("authorization") != f"Bearer {_WORKER_TOKEN}":
                    writer.write(_response(401, {"detail": {"code": "WORKER_UNAUTHORIZED"}}))
                else:
                    writer.write(_response(204))
            else:
                writer.write(_response(404, {"detail": {"code": "NOT_FOUND"}}))
            await writer.drain()
        except ConnectionError, asyncio.IncompleteReadError, ValueError:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError, ssl.SSLError:
                pass

    server = await asyncio.start_server(
        handle,
        host="127.0.0.1",
        port=0,
        ssl=server_context,
    )
    port = server.sockets[0].getsockname()[1] if server.sockets else 0
    try:
        yield f"https://127.0.0.1:{port}", ca_path, received
    finally:
        server.close()
        await server.wait_closed()


@asynccontextmanager
async def _http_connect_proxy(
    *, reject_connect: bool = False
) -> AsyncGenerator[tuple[str, list[tuple[str, dict[str, str]]]]]:
    requests: list[tuple[str, dict[str, str]]] = []

    async def relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while data := await reader.read(65_536):
                writer.write(data)
                await writer.drain()
        except ConnectionError, OSError:
            pass

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream_writer: asyncio.StreamWriter | None = None
        relay_tasks: list[asyncio.Task[None]] = []
        try:
            request_line = (await reader.readline()).decode("latin-1").strip()
            headers: dict[str, str] = {}
            while line := await reader.readline():
                if line == b"\r\n":
                    break
                name, value = line.decode("latin-1").split(":", 1)
                headers[name.casefold()] = value.strip()
            requests.append((request_line, headers))
            if reject_connect:
                writer.write(
                    b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
                await writer.drain()
                return

            method, authority, _ = request_line.split(" ", 2)
            if method != "CONNECT":
                writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            hostname, port_text = authority.rsplit(":", 1)
            upstream_reader, upstream_writer = await asyncio.open_connection(
                hostname,
                int(port_text),
            )
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            relay_tasks = [
                asyncio.create_task(relay(reader, upstream_writer)),
                asyncio.create_task(relay(upstream_reader, writer)),
            ]
            await asyncio.wait(relay_tasks, return_when=asyncio.FIRST_COMPLETED)
        except ConnectionError, asyncio.IncompleteReadError, OSError, ValueError:
            pass
        finally:
            for task in relay_tasks:
                task.cancel()
            if relay_tasks:
                await asyncio.gather(*relay_tasks, return_exceptions=True)
            if upstream_writer is not None:
                upstream_writer.close()
                try:
                    await upstream_writer.wait_closed()
                except ConnectionError, ssl.SSLError:
                    pass
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError, ssl.SSLError:
                pass

    server = await asyncio.start_server(handle, host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1] if server.sockets else 0
    try:
        yield f"http://127.0.0.1:{port}", requests
    finally:
        server.close()
        await server.wait_closed()


def _clear_http_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    ):
        monkeypatch.delenv(variable, raising=False)


async def _authenticate_and_claim(base_url: str) -> None:
    client = HttpWorkerControlClient(base_url, timeout_seconds=2.0)
    try:
        await client.authenticate(
            uuid4(),
            WorkerDeviceIdentity.generate(),
            enrollment_pending=False,
            enrollment_code=None,
            display_name="HTTPX2 contract test worker",
            hostname="HTTPX2-TEST",
            max_concurrent_jobs=1,
            max_browser_sessions=1,
        )
        assert await client.claim_next() is None
    finally:
        await client.aclose()


async def _authenticate(client: HttpWorkerControlClient) -> None:
    await client.authenticate(
        uuid4(),
        WorkerDeviceIdentity.generate(),
        enrollment_pending=False,
        enrollment_code=None,
        display_name="HTTPX2 contract test worker",
        hostname="HTTPX2-TEST",
        max_concurrent_jobs=1,
        max_browser_sessions=1,
    )


def _assert_sanitized_worker_error(error: WorkerControlClientError, *secrets: str) -> None:
    assert error.__cause__ is None
    assert error.__context__ is None
    assert not hasattr(error, "request")
    assert all(secret not in str(error) and secret not in repr(error) for secret in secrets)


@pytest.mark.asyncio
@pytest.mark.parametrize("ca_override", ["SSL_CERT_FILE", "SSL_CERT_DIR"])
async def test_control_client_uses_verified_tls_and_trusted_ca_overrides(
    ca_override: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_http_environment(monkeypatch)
    context = httpx2.create_ssl_context(verify=True, trust_env=True)
    assert type(context).__module__.startswith("truststore.")
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname

    san = x509.IPAddress(ipaddress.ip_address("127.0.0.1"))
    async with _control_plane_server(tmp_path, subject_alt_name=san) as (
        base_url,
        certificate_path,
        received,
    ):
        client = HttpWorkerControlClient(base_url, timeout_seconds=2.0)
        try:
            with pytest.raises(WorkerControlClientError) as rejected:
                await _authenticate(client)
        finally:
            await client.aclose()
        assert rejected.value.code == "CONTROL_PLANE_UNAVAILABLE"
        _assert_sanitized_worker_error(rejected.value, _WORKER_TOKEN, _ENROLLMENT_CODE)

        if ca_override == "SSL_CERT_FILE":
            monkeypatch.setenv(ca_override, str(certificate_path))
        else:
            certificate = x509.load_pem_x509_certificate(certificate_path.read_bytes())
            ca_directory = tmp_path / "ca-directory"
            ca_directory.mkdir()
            canonical_subject = certificate.subject.public_bytes()[2:]
            subject_hash = hashlib.sha1(canonical_subject).digest()[:4][::-1].hex()
            (ca_directory / f"{subject_hash}.0").write_bytes(certificate_path.read_bytes())
            monkeypatch.setenv(ca_override, str(ca_directory))

        await _authenticate_and_claim(base_url)

    claim = next(item for item in received if item[1] == "/v1/workers/jobs/claim")
    assert claim[0] == "POST"
    assert claim[2]["authorization"] == f"Bearer {_WORKER_TOKEN}"
    assert _WORKER_TOKEN not in claim[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("subject_alt_name", "expired"),
    [
        (x509.DNSName("control-plane.invalid"), False),
        (x509.IPAddress(ipaddress.ip_address("127.0.0.1")), True),
    ],
    ids=["hostname-mismatch", "expired-certificate"],
)
async def test_control_client_rejects_invalid_certificate_even_when_ca_is_trusted(
    subject_alt_name: x509.GeneralName,
    expired: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_http_environment(monkeypatch)
    async with _control_plane_server(
        tmp_path,
        subject_alt_name=subject_alt_name,
        expired=expired,
    ) as (base_url, certificate_path, _):
        monkeypatch.setenv("SSL_CERT_FILE", str(certificate_path))
        client = HttpWorkerControlClient(base_url, timeout_seconds=2.0)
        try:
            with pytest.raises(WorkerControlClientError) as rejected:
                await _authenticate(client)
        finally:
            await client.aclose()

    assert rejected.value.code == "CONTROL_PLANE_UNAVAILABLE"
    _assert_sanitized_worker_error(rejected.value, _WORKER_TOKEN, _ENROLLMENT_CODE)


@pytest.mark.asyncio
@pytest.mark.parametrize("no_proxy", ["", "127.0.0.1"], ids=["use-connect", "bypass-proxy"])
async def test_control_client_honors_https_proxy_connect_and_no_proxy(
    no_proxy: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_http_environment(monkeypatch)
    san = x509.IPAddress(ipaddress.ip_address("127.0.0.1"))
    async with (
        _control_plane_server(tmp_path, subject_alt_name=san) as (
            base_url,
            certificate_path,
            _,
        ),
        _http_connect_proxy() as (proxy_url, proxy_requests),
    ):
        monkeypatch.setenv("SSL_CERT_FILE", str(certificate_path))
        monkeypatch.setenv("HTTPS_PROXY", proxy_url)
        monkeypatch.setenv("https_proxy", proxy_url)
        monkeypatch.setenv("NO_PROXY", no_proxy)
        monkeypatch.setenv("no_proxy", no_proxy)
        await _authenticate_and_claim(base_url)

    if no_proxy:
        assert proxy_requests == []
    else:
        target = base_url.removeprefix("https://")
        assert [request[0] for request in proxy_requests] == [f"CONNECT {target} HTTP/1.1"] * 3


@pytest.mark.asyncio
async def test_proxy_credentials_and_auth_request_do_not_escape_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_http_environment(monkeypatch)
    username = uuid4().hex
    password = uuid4().hex
    async with _http_connect_proxy(reject_connect=True) as (proxy_url, proxy_requests):
        proxy_scheme, proxy_endpoint = proxy_url.split("://", maxsplit=1)
        credentialed_proxy_url = f"{proxy_scheme}://{username}:{password}@{proxy_endpoint}"
        monkeypatch.setenv("HTTPS_PROXY", credentialed_proxy_url)
        monkeypatch.setenv("https_proxy", credentialed_proxy_url)
        client = HttpWorkerControlClient("https://control-plane.invalid", timeout_seconds=2.0)
        try:
            with pytest.raises(WorkerControlClientError) as rejected:
                await _authenticate(client)
        finally:
            await client.aclose()

    assert rejected.value.code == "CONTROL_PLANE_UNAVAILABLE"
    _assert_sanitized_worker_error(
        rejected.value,
        username,
        password,
        _WORKER_TOKEN,
        _ENROLLMENT_CODE,
    )
    assert len(proxy_requests) == 1
    assert proxy_requests[0][1]["proxy-authorization"].startswith("Basic ")


@pytest.mark.asyncio
async def test_enrollment_code_is_not_retained_when_transport_fails() -> None:
    enrollment_requests: list[httpx2.Request] = []

    async def handle(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/v1/workers/auth/challenges":
            return httpx2.Response(
                404,
                json={"detail": {"code": "WORKER_NOT_AUTHENTICATABLE"}},
            )
        enrollment_requests.append(request)
        raise httpx2.ConnectError("SYNTHETIC_ENROLLMENT_TRANSPORT_FAILURE", request=request)

    client = HttpWorkerControlClient(
        "https://control-plane.invalid",
        transport=httpx2.MockTransport(handle),
    )
    try:
        with pytest.raises(WorkerControlClientError) as rejected:
            await client.authenticate(
                uuid4(),
                WorkerDeviceIdentity.generate(),
                enrollment_pending=True,
                enrollment_code=_ENROLLMENT_CODE,
                display_name="HTTPX2 enrollment test worker",
                hostname="HTTPX2-ENROLL",
                max_concurrent_jobs=1,
                max_browser_sessions=1,
            )
    finally:
        await client.aclose()

    assert rejected.value.code == "CONTROL_PLANE_UNAVAILABLE"
    _assert_sanitized_worker_error(
        rejected.value,
        _ENROLLMENT_CODE,
        "SYNTHETIC_ENROLLMENT_TRANSPORT_FAILURE",
    )
    assert len(enrollment_requests) == 1
    assert _ENROLLMENT_CODE.encode() in enrollment_requests[0].content


@pytest.mark.asyncio
async def test_transport_timeout_does_not_replay_or_retain_authorized_request() -> None:
    requests: list[httpx2.Request] = []

    async def handle(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/v1/workers/auth/challenges":
            return httpx2.Response(
                200,
                json={"challenge_id": str(_CHALLENGE_ID), "nonce": "synthetic-nonce"},
            )
        if request.url.path == "/v1/workers/auth/sessions":
            return httpx2.Response(
                200,
                json={
                    "access_token": _WORKER_TOKEN,
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
                },
            )
        requests.append(request)
        raise httpx2.ReadTimeout(
            f"SYNTHETIC_TIMEOUT {_WORKER_TOKEN} {request.headers['Authorization']}",
            request=request,
        )

    client = HttpWorkerControlClient(
        "https://control-plane.invalid",
        transport=httpx2.MockTransport(handle),
    )
    try:
        await _authenticate(client)
        with pytest.raises(WorkerControlClientError) as rejected:
            await client.checkpoint_job(uuid4(), uuid4(), {"phase": "SYNTHETIC"})
    finally:
        await client.aclose()

    assert rejected.value.code == "CONTROL_PLANE_UNAVAILABLE"
    _assert_sanitized_worker_error(rejected.value, _WORKER_TOKEN, "SYNTHETIC_TIMEOUT")
    assert len(requests) == 1
    assert requests[0].headers["Authorization"] == f"Bearer {_WORKER_TOKEN}"


@pytest.mark.asyncio
async def test_request_cancellation_is_not_translated_or_retried() -> None:
    cancelled_requests: list[httpx2.Request] = []

    async def handle(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/v1/workers/auth/challenges":
            return httpx2.Response(
                200,
                json={"challenge_id": str(_CHALLENGE_ID), "nonce": "synthetic-nonce"},
            )
        if request.url.path == "/v1/workers/auth/sessions":
            return httpx2.Response(
                200,
                json={
                    "access_token": _WORKER_TOKEN,
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
                },
            )
        cancelled_requests.append(request)
        raise asyncio.CancelledError

    client = HttpWorkerControlClient(
        "https://control-plane.invalid",
        transport=httpx2.MockTransport(handle),
    )
    try:
        await _authenticate(client)
        with pytest.raises(asyncio.CancelledError):
            await client.heartbeat(0)
    finally:
        await client.aclose()

    assert len(cancelled_requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "error_code"),
    [
        (401, "WORKER_UNAUTHORIZED"),
        (429, "WORKER_RATE_LIMITED"),
        (503, "CONTROL_PLANE_UNAVAILABLE"),
    ],
)
async def test_worker_http_errors_keep_bounded_codes_without_retries(
    status_code: int,
    error_code: str,
) -> None:
    failed_requests: list[httpx2.Request] = []

    async def handle(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/v1/workers/auth/challenges":
            return httpx2.Response(
                200,
                json={"challenge_id": str(_CHALLENGE_ID), "nonce": "synthetic-nonce"},
            )
        if request.url.path == "/v1/workers/auth/sessions":
            return httpx2.Response(
                200,
                json={
                    "access_token": _WORKER_TOKEN,
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
                },
            )
        failed_requests.append(request)
        headers = {"Retry-After": "5"} if status_code == 429 else {}
        return httpx2.Response(
            status_code,
            json={"detail": {"code": error_code}},
            headers=headers,
        )

    client = HttpWorkerControlClient(
        "https://control-plane.invalid",
        transport=httpx2.MockTransport(handle),
    )
    try:
        await _authenticate(client)
        with pytest.raises(WorkerControlClientError) as rejected:
            await client.heartbeat(0)
    finally:
        await client.aclose()

    assert rejected.value.code == error_code
    assert rejected.value.status_code == status_code
    assert rejected.value.__cause__ is None
    assert rejected.value.__context__ is None
    assert len(failed_requests) == 1


@pytest.mark.asyncio
async def test_invalid_response_body_is_not_retained_as_exception_context() -> None:
    async def handle(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/v1/workers/auth/challenges":
            return httpx2.Response(
                200,
                json={"challenge_id": str(_CHALLENGE_ID), "nonce": "synthetic-nonce"},
            )
        if request.url.path == "/v1/workers/auth/sessions":
            return httpx2.Response(
                200,
                json={
                    "access_token": _WORKER_TOKEN,
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
                },
            )
        return httpx2.Response(200, content=f"{_WORKER_TOKEN} is not JSON".encode())

    client = HttpWorkerControlClient(
        "https://control-plane.invalid",
        transport=httpx2.MockTransport(handle),
    )
    try:
        await _authenticate(client)
        with pytest.raises(WorkerControlClientError) as rejected:
            await client.heartbeat(0)
    finally:
        await client.aclose()

    assert rejected.value.code == "WORKER_PROTOCOL_INVALID_RESPONSE"
    _assert_sanitized_worker_error(rejected.value, _WORKER_TOKEN)
