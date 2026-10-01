from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import ssl
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from pydantic import SecretStr

from threads_platform.application.ports.threads import ThreadsTransportError
from threads_platform.infrastructure.threads_api.client import HttpThreadsAPI

_TOKEN = "SYNTHETIC_THREADS_BEARER_TOKEN"
_MEDIA_RESPONSE = b'{"id":"media-doc-example","text":"Synthetic TLS response"}'


def _write_certificate(
    directory: Path,
    *,
    common_name: str,
    subject_alt_name: x509.GeneralName,
) -> Path:
    private_key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([subject_alt_name]), critical=False)
        .sign(private_key, hashes.SHA256())
    )
    certificate_path = directory / "server-ca.pem"
    key_path = directory / "server-key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return certificate_path


@asynccontextmanager
async def _https_server(
    directory: Path,
    *,
    common_name: str,
    subject_alt_name: x509.GeneralName,
) -> AsyncGenerator[tuple[str, Path]]:
    certificate_path = _write_certificate(
        directory,
        common_name=common_name,
        subject_alt_name=subject_alt_name,
    )
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(certificate_path, directory / "server-key.pem")

    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                + f"Content-Length: {len(_MEDIA_RESPONSE)}\r\n".encode()
                + b"Content-Type: application/json\r\nConnection: close\r\n\r\n"
                + _MEDIA_RESPONSE
            )
            await writer.drain()
        except ConnectionError, asyncio.IncompleteReadError:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    server = await asyncio.start_server(
        respond,
        host="127.0.0.1",
        port=0,
        ssl=server_context,
    )
    port = server.sockets[0].getsockname()[1] if server.sockets else 0
    try:
        yield f"https://127.0.0.1:{port}/", certificate_path
    finally:
        server.close()
        await server.wait_closed()


def _clear_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    ):
        monkeypatch.delenv(variable, raising=False)


def _api_client(base_url: str, *, trust_env: bool) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        base_url=base_url,
        timeout=httpx2.Timeout(2.0),
        follow_redirects=False,
        verify=True,
        trust_env=trust_env,
    )


@pytest.mark.asyncio
async def test_threads_api_uses_verified_tls_and_ssl_cert_file_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_proxy_environment(monkeypatch)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    async with _https_server(
        tmp_path,
        common_name="127.0.0.1",
        subject_alt_name=x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ) as (base_url, certificate_path):
        async with _api_client(base_url, trust_env=True) as client:
            context = httpx2.create_ssl_context(verify=True, trust_env=True)
            assert type(context).__module__.startswith("truststore.")
            assert context.verify_mode == ssl.CERT_REQUIRED
            assert context.check_hostname
            with pytest.raises(ThreadsTransportError) as rejected:
                await HttpThreadsAPI(client).get_media(SecretStr(_TOKEN), "media-doc-example")

        assert rejected.value.__context__ is None
        assert _TOKEN not in repr(rejected.value)

        monkeypatch.setenv("SSL_CERT_FILE", str(certificate_path))
        async with _api_client(base_url, trust_env=True) as client:
            media = await HttpThreadsAPI(client).get_media(SecretStr(_TOKEN), "media-doc-example")

    assert media.media_id == "media-doc-example"
    assert media.text == "Synthetic TLS response"


@pytest.mark.asyncio
async def test_threads_api_rejects_hostname_mismatch_with_a_trusted_ca(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_proxy_environment(monkeypatch)
    async with _https_server(
        tmp_path,
        common_name="localhost",
        subject_alt_name=x509.DNSName("localhost"),
    ) as (base_url, certificate_path):
        monkeypatch.setenv("SSL_CERT_FILE", str(certificate_path))
        monkeypatch.delenv("SSL_CERT_DIR", raising=False)
        async with _api_client(base_url, trust_env=True) as client:
            with pytest.raises(ThreadsTransportError) as rejected:
                await HttpThreadsAPI(client).get_media(SecretStr(_TOKEN), "media-doc-example")

    assert rejected.value.__context__ is None
    assert _TOKEN not in repr(rejected.value)


@pytest.mark.asyncio
async def test_threads_api_uses_ssl_cert_dir_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_proxy_environment(monkeypatch)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    async with _https_server(
        tmp_path,
        common_name="threads-test-ca",
        subject_alt_name=x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ) as (base_url, certificate_path):
        certificate = x509.load_pem_x509_certificate(certificate_path.read_bytes())
        ca_directory = tmp_path / "ca-directory"
        ca_directory.mkdir()
        # OpenSSL's hashed CA directory uses the first four SHA-1 bytes of its
        # canonical X.509 subject name, written in reverse byte order.
        canonical_subject = certificate.subject.public_bytes()[2:]
        subject_hash = hashlib.sha1(canonical_subject).digest()[:4][::-1].hex()
        (ca_directory / f"{subject_hash}.0").write_bytes(certificate_path.read_bytes())
        monkeypatch.setenv("SSL_CERT_DIR", str(ca_directory))

        async with _api_client(base_url, trust_env=True) as client:
            media = await HttpThreadsAPI(client).get_media(SecretStr(_TOKEN), "media-doc-example")

    assert media.media_id == "media-doc-example"
    assert media.text == "Synthetic TLS response"


@pytest.mark.asyncio
async def test_trust_env_controls_https_proxy_without_exposing_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_proxy_environment(monkeypatch)
    async with _https_server(
        tmp_path,
        common_name="127.0.0.1",
        subject_alt_name=x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ) as (base_url, certificate_path):
        proxy_requests: list[str] = []

        async def proxy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                request_line = await reader.readline()
                proxy_requests.append(request_line.decode("latin-1").strip())
                while await reader.readline() not in (b"\r\n", b""):
                    pass
                writer.write(
                    b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                await writer.drain()
            except ConnectionError, asyncio.IncompleteReadError:
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except ConnectionError:
                    pass

        proxy_server = await asyncio.start_server(proxy, host="127.0.0.1", port=0)
        proxy_port = proxy_server.sockets[0].getsockname()[1] if proxy_server.sockets else 0
        proxy_url = f"http://127.0.0.1:{proxy_port}"
        proxy_target = base_url.removeprefix("https://").rstrip("/")
        monkeypatch.setenv("HTTPS_PROXY", proxy_url)
        monkeypatch.setenv("https_proxy", proxy_url)
        monkeypatch.setenv("NO_PROXY", "")
        monkeypatch.setenv("no_proxy", "")
        monkeypatch.setenv("SSL_CERT_FILE", str(certificate_path))
        monkeypatch.delenv("SSL_CERT_DIR", raising=False)
        try:
            async with _api_client(base_url, trust_env=True) as client:
                with pytest.raises(ThreadsTransportError) as rejected:
                    await HttpThreadsAPI(client).get_media(SecretStr(_TOKEN), "media-doc-example")

            assert rejected.value.__context__ is None
            assert _TOKEN not in repr(rejected.value)
            assert proxy_requests == [f"CONNECT {proxy_target} HTTP/1.1"]

            verification_context = ssl.create_default_context(cafile=certificate_path)
            async with httpx2.AsyncClient(
                base_url=base_url,
                timeout=httpx2.Timeout(2.0),
                follow_redirects=False,
                verify=verification_context,
                trust_env=False,
            ) as direct_client:
                media = await HttpThreadsAPI(direct_client).get_media(
                    SecretStr(_TOKEN), "media-doc-example"
                )

            assert media.media_id == "media-doc-example"
            assert len(proxy_requests) == 1
        finally:
            proxy_server.close()
            await proxy_server.wait_closed()
