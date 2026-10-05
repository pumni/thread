from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import ssl
import sys
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path
from threading import Event, Lock
from typing import Any, cast
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from threads_platform.application.worker_control import WorkerControlService  # noqa: E402
from threads_platform.application.worker_jobs import WorkerJobService  # noqa: E402
from threads_platform.domain.accounts import AccountExecutionMode, ThreadsAccount  # noqa: E402
from threads_platform.domain.workers import (  # noqa: E402
    AccountWorkerAssignment,
    BrowserProfile,
    BrowserSessionState,
)
from threads_platform.infrastructure.persistence.database import (  # noqa: E402
    create_database_engine,
    create_session_factory,
)
from threads_platform.infrastructure.persistence.uow import (  # noqa: E402
    SQLAlchemyUnitOfWorkFactory,
)
from threads_platform.infrastructure.security.operator_auth import OperatorAuthService  # noqa: E402
from threads_platform.infrastructure.worker_agent.identity import (  # noqa: E402
    WorkerIdentityFileStore,
)
from threads_platform.infrastructure.worker_agent.local_state import (  # noqa: E402
    LocalDataRoot,
    LocalProfileDirectoryResolver,
    WorkerLocalStateStore,
)
from threads_platform.infrastructure.worker_agent.windows_keys import (  # noqa: E402
    DPAPIWorkerKeyStore,
)
from threads_platform.workers.profile_open import (  # noqa: E402
    PROFILE_OPEN_CAPABILITY_NAME,
    PROFILE_OPEN_CAPABILITY_VERSION,
)


def _database_url() -> str:
    value = os.environ.get("THREADS_PLATFORM_DATABASE_URL")
    if not value or "@" in value.partition("://")[2].partition("/")[0]:
        raise RuntimeError("worker_desktop_fixture_database_configuration_required")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_private_key(path: Path, key: ec.EllipticCurvePrivateKey) -> None:
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )


def _create_tls_fixture(directory: Path) -> dict[str, str]:
    now = datetime.now(UTC)
    root_key = ec.generate_private_key(ec.SECP256R1())
    root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "DX07 ephemeral test root")])
    root = (
        x509.CertificateBuilder()
        .subject_name(root_name)
        .issuer_name(root_name)
        .public_key(root_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=2))
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
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(root_key.public_key()), False)
        .sign(root_key, hashes.SHA256())
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    leaf = (
        x509.CertificateBuilder()
        .subject_name(leaf_name)
        .issuer_name(root.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.DNSName("www.threads.com"),
                    x509.IPAddress(ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
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
            x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()), False
        )
        .sign(root_key, hashes.SHA256())
    )
    directory.mkdir(parents=True, exist_ok=True)
    root_der_path = directory / "fixture-root.der"
    root_pem_path = directory / "fixture-root.pem"
    cert_path = directory / "fixture-leaf.pem"
    key_path = directory / "fixture-leaf-key.pem"
    root_der_path.write_bytes(root.public_bytes(serialization.Encoding.DER))
    root_pem_path.write_bytes(root.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(
        leaf.public_bytes(serialization.Encoding.PEM)
        + root.public_bytes(serialization.Encoding.PEM)
    )
    _write_private_key(key_path, leaf_key)
    return {
        "root_der": str(root_der_path),
        "root_pem": str(root_pem_path),
        "leaf_certificate": str(cert_path),
        "leaf_private_key": str(key_path),
        "root_fingerprint_sha256": root.fingerprint(hashes.SHA256()).hex(),
    }


async def _seed(args: argparse.Namespace) -> dict[str, Any]:
    database_url = _database_url()
    fixture_root = args.fixture_root.resolve()
    data_root_path = args.data_root.resolve()
    if data_root_path == REPOSITORY_ROOT or REPOSITORY_ROOT in data_root_path.parents:
        raise RuntimeError("worker_desktop_fixture_data_root_invalid")
    command.upgrade(Config(str(REPOSITORY_ROOT / "alembic.ini")), "head")

    engine = create_database_engine(database_url)
    session_factory = create_session_factory(engine)
    unit_of_work_factory = SQLAlchemyUnitOfWorkFactory(session_factory)
    auth = OperatorAuthService(session_factory)
    password = os.environ.pop("THREADS_DX07_OPERATOR_PASSWORD", None)
    if not password:
        raise RuntimeError("worker_desktop_fixture_operator_password_required")
    await auth.bootstrap_first_owner(args.operator_username, password)
    password = None

    root = LocalDataRoot(data_root_path)
    root.prepare()
    identity_store = WorkerIdentityFileStore(root)
    worker_id = identity_store.load_or_create()
    device_identity = DPAPIWorkerKeyStore(root).load_or_create(worker_id)
    control = WorkerControlService(unit_of_work_factory)
    enrollment = await control.create_enrollment("dx07_synthetic_fixture")
    await control.enroll(
        enrollment.code,
        worker_id=worker_id,
        display_name="DX07 synthetic Worker",
        hostname="DX07-TEST-RUNNER",
        platform="windows",
        public_key=device_identity.public_key_bytes,
        max_concurrent_jobs=2,
        max_browser_sessions=1,
    )
    identity_store.mark_enrolled()
    enrollment = None

    account_id = uuid4()
    profile_ref = "dx07-synthetic-profile"
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.accounts.add(
            ThreadsAccount(
                threads_user_id=f"dx07-{account_id}",
                username="dx07synthetic",
                id=account_id,
                execution_mode=AccountExecutionMode.BROWSER_ONLY,
            )
        )
        await unit_of_work.browser_profiles.add(BrowserProfile(worker_id, profile_ref))
        await unit_of_work.assignments.add(
            AccountWorkerAssignment(account_id, worker_id, profile_ref)
        )

    state = WorkerLocalStateStore(root, worker_id)
    profile_resolver = LocalProfileDirectoryResolver(root, state)
    profile_directory = profile_resolver.resolve(worker_id, account_id, profile_ref)
    state.record_session_state(
        worker_id=worker_id,
        account_id=account_id,
        profile_ref=profile_ref,
        session_id=uuid4(),
        state=BrowserSessionState.AUTHENTICATED,
        updated_at=datetime.now(UTC),
    )
    sentinel_path = profile_directory / "dx07-profile-continuity.sentinel"
    sentinel_path.write_bytes(b"DX07 synthetic persistent browser profile sentinel v1\n")

    tls = _create_tls_fixture(fixture_root / "tls")
    control_plane_url = f"https://127.0.0.1:{args.control_port}"
    host_config_path = fixture_root / "worker-host-config.json"
    host_config = {
        "schema": "threads-worker-host-v1",
        "control_plane_url": control_plane_url,
        "data_root": str(data_root_path),
        "display_name": "DX07 synthetic Worker",
        "agent_version": "0.1.0",
        "max_concurrent_jobs": 2,
        "max_browser_sessions": 1,
        "profile_open_enabled": True,
    }
    host_config_path.write_text(json.dumps(host_config, sort_keys=True) + "\n", encoding="utf-8")
    result = {
        "worker_id": str(worker_id),
        "account_id": str(account_id),
        "operator_username": args.operator_username,
        "control_plane_url": control_plane_url,
        "control_port": args.control_port,
        "fixture_root": str(fixture_root),
        "data_root": str(data_root_path),
        "host_config_path": str(host_config_path),
        "profile_directory": str(profile_directory),
        "profile_sentinel_sha256": _sha256(sentinel_path),
        "identity_marker_sha256": _sha256(root.child("worker", "worker_id")),
        "protected_key_sha256": _sha256(root.child("worker", f"{worker_id}.device-key.dpapi")),
        "journal_path": str(root.journal_path),
        "tls": tls,
    }
    (fixture_root / "fixture.json").write_text(
        json.dumps(result, sort_keys=True) + "\n", encoding="utf-8"
    )
    await engine.dispose()
    return result


async def _queue_profile_job(args: argparse.Namespace) -> dict[str, str]:
    engine = create_database_engine(_database_url())
    try:
        job = await WorkerJobService(
            SQLAlchemyUnitOfWorkFactory(create_session_factory(engine))
        ).enqueue(
            PROFILE_OPEN_CAPABILITY_NAME,
            PROFILE_OPEN_CAPABILITY_VERSION,
            account_id=UUID(args.account_id),
            assigned_worker_id=UUID(args.worker_id),
            account_affinity_required=True,
            input_data={"profile_ref": "/@dx07synthetic"},
        )
        return {"worker_job_id": str(job.id), "status": job.status.value}
    finally:
        await engine.dispose()


def _seed_identity_only(args: argparse.Namespace) -> dict[str, str]:
    root = LocalDataRoot(args.data_root.resolve())
    root.prepare()
    identity_store = WorkerIdentityFileStore(root)
    worker_id = identity_store.load_or_create()
    DPAPIWorkerKeyStore(root).load_or_create(worker_id)
    identity_store.mark_enrolled()
    return {
        "worker_id": str(worker_id),
        "identity_marker_sha256": _sha256(root.child("worker", "worker_id")),
        "protected_key_sha256": _sha256(root.child("worker", f"{worker_id}.device-key.dpapi")),
    }


def _serve_api(args: argparse.Namespace) -> None:
    import uvicorn

    from threads_platform.app import app as control_plane_app

    counter_path = args.request_counts
    counter_path.parent.mkdir(parents=True, exist_ok=True)
    counts = {"drain_post_count": 0, "drain_status_get_count": 0}
    counter_lock = Lock()
    counter_path.write_text(json.dumps(counts, sort_keys=True) + "\n", encoding="utf-8")
    drain_route = re.compile(r"^/v1/workers/[0-9a-f-]{36}/drain$")

    class CountDrainRequests:
        async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
            if scope.get("type") == "http" and drain_route.fullmatch(str(scope.get("path", ""))):
                method = scope.get("method")
                key = (
                    "drain_post_count"
                    if method == "POST"
                    else "drain_status_get_count"
                    if method == "GET"
                    else None
                )
                if key is not None:
                    with counter_lock:
                        counts[key] += 1
                        temporary = counter_path.with_suffix(".tmp")
                        temporary.write_text(
                            json.dumps(counts, sort_keys=True) + "\n", encoding="utf-8"
                        )
                        temporary.replace(counter_path)
            await control_plane_app(scope, receive, send)

    config = uvicorn.Config(
        CountDrainRequests(),
        host=args.host,
        port=args.port,
        ssl_certfile=str(args.certificate),
        ssl_keyfile=str(args.private_key),
        access_log=False,
    )
    asyncio.run(uvicorn.Server(config).serve())


def _request_counts(args: argparse.Namespace) -> dict[str, int]:
    try:
        value: object = json.loads(args.counter_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("worker_desktop_fixture_request_counts_unavailable") from error
    if not isinstance(value, dict):
        raise RuntimeError("worker_desktop_fixture_request_counts_invalid")
    counts = cast(dict[str, Any], value)
    if set(counts) != {"drain_post_count", "drain_status_get_count"} or any(
        type(count) is not int or count < 0 for count in counts.values()
    ):
        raise RuntimeError("worker_desktop_fixture_request_counts_invalid")
    return cast(dict[str, int], counts)


async def _status(args: argparse.Namespace) -> dict[str, Any]:
    engine = create_database_engine(_database_url())
    try:
        factory = SQLAlchemyUnitOfWorkFactory(create_session_factory(engine))
        status = await WorkerControlService(factory).drain_status(UUID(args.worker_id))
        return {
            "worker_id": str(status.worker_id),
            "status": status.status.value,
            "active_browser_sessions": status.active_browser_sessions,
            "running_worker_jobs": status.running_worker_jobs,
            "quiescent": status.quiescent,
        }
    finally:
        await engine.dispose()


async def _watch_status(args: argparse.Namespace) -> None:
    engine = create_database_engine(_database_url())
    factory = SQLAlchemyUnitOfWorkFactory(create_session_factory(engine))
    output_path = args.output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    observations: list[dict[str, Any]] = []
    deadline = datetime.now(UTC) + timedelta(seconds=args.duration_seconds)
    try:
        while datetime.now(UTC) < deadline:
            try:
                status = await WorkerControlService(factory).drain_status(UUID(args.worker_id))
            except Exception:
                await asyncio.sleep(0.1)
                continue
            observation = {
                "status": status.status.value,
                "active_browser_sessions": status.active_browser_sessions,
                "running_worker_jobs": status.running_worker_jobs,
                "quiescent": status.quiescent,
            }
            if not observations or observations[-1] != observation:
                observations.append(observation)
                temporary = output_path.with_suffix(".tmp")
                temporary.write_text(
                    json.dumps(observations, sort_keys=True) + "\n", encoding="utf-8"
                )
                temporary.replace(output_path)
            await asyncio.sleep(0.1)
    finally:
        await engine.dispose()


async def _job_status(args: argparse.Namespace) -> dict[str, str]:
    from threads_platform.infrastructure.persistence.models import WorkerJobRecord

    engine = create_database_engine(_database_url())
    try:
        session_factory = create_session_factory(engine)
        async with session_factory() as session:
            row = await session.get(WorkerJobRecord, UUID(args.worker_job_id))
        if row is None:
            raise RuntimeError("worker_desktop_fixture_worker_job_missing")
        return {"worker_job_status": str(row.status)}
    finally:
        await engine.dispose()


async def _operator_session_count(args: argparse.Namespace) -> dict[str, int]:
    from sqlalchemy import func, select

    from threads_platform.infrastructure.persistence.models import (
        OperatorSessionRecord,
        OperatorUserRecord,
    )

    engine = create_database_engine(_database_url())
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(OperatorSessionRecord)
            .join(
                OperatorUserRecord,
                OperatorUserRecord.id == OperatorSessionRecord.operator_user_id,
            )
            .where(
                OperatorUserRecord.username == args.operator_username,
                OperatorSessionRecord.revoked_at.is_(None),
                OperatorSessionRecord.expires_at > datetime.now(UTC),
            )
        )
    await engine.dispose()
    return {"active_operator_sessions": int(count or 0)}


async def _audit_count(args: argparse.Namespace) -> dict[str, int]:
    from sqlalchemy import func, select

    from threads_platform.infrastructure.persistence.models import WorkspaceAuditEventRecord

    engine = create_database_engine(_database_url())
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(WorkspaceAuditEventRecord)
            .where(
                WorkspaceAuditEventRecord.event_type == "worker.drain_requested",
                WorkspaceAuditEventRecord.target_type == "worker",
                WorkspaceAuditEventRecord.target_id == args.worker_id,
            )
        )
    await engine.dispose()
    return {"drain_request_audit_count": int(count or 0)}


async def _drain_audit_counts(args: argparse.Namespace) -> dict[str, int]:
    from sqlalchemy import func, select

    from threads_platform.infrastructure.persistence.models import (
        WorkerAuditEventRecord,
        WorkspaceAuditEventRecord,
    )

    engine = create_database_engine(_database_url())
    try:
        session_factory = create_session_factory(engine)
        async with session_factory() as session:
            request_count = await session.scalar(
                select(func.count())
                .select_from(WorkspaceAuditEventRecord)
                .where(
                    WorkspaceAuditEventRecord.event_type == "worker.drain_requested",
                    WorkspaceAuditEventRecord.target_type == "worker",
                    WorkspaceAuditEventRecord.target_id == args.worker_id,
                )
            )
            completion_count = await session.scalar(
                select(func.count())
                .select_from(WorkerAuditEventRecord)
                .where(
                    WorkerAuditEventRecord.event_type == "worker.drain.completed",
                    WorkerAuditEventRecord.worker_id == UUID(args.worker_id),
                )
            )
        return {
            "drain_request_audit_count": int(request_count or 0),
            "drain_completion_audit_count": int(completion_count or 0),
        }
    finally:
        await engine.dispose()


def _serve_page(args: argparse.Namespace) -> None:
    release_event = Event()
    if args.release_file is None:
        release_event.set()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.rstrip("/") != "/@dx07synthetic":
                self.send_error(404)
                return
            deadline = datetime.now(UTC) + timedelta(seconds=args.maximum_wait_seconds)
            while not release_event.is_set() and datetime.now(UTC) < deadline:
                if args.release_file is not None and args.release_file.exists():
                    release_event.set()
                    break
                release_event.wait(0.1)
            body = (
                b"<!doctype html><html><head><title>DX07 synthetic profile</title></head>"
                b"<body><main><div><a href='/@dx07synthetic'>Synthetic profile</a>"
                b"<h1>DX07 Synthetic Profile</h1></div></main></body></html>"
            )
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(args.certificate, args.private_key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    seed = commands.add_parser("seed")
    seed.add_argument("--fixture-root", type=Path, required=True)
    seed.add_argument("--data-root", type=Path, required=True)
    seed.add_argument("--control-port", type=int, required=True)
    seed.add_argument("--operator-username", required=True)
    identity_seed = commands.add_parser("seed-identity-only")
    identity_seed.add_argument("--data-root", type=Path, required=True)
    job = commands.add_parser("queue-profile-job")
    job.add_argument("--worker-id", required=True)
    job.add_argument("--account-id", required=True)
    status = commands.add_parser("status")
    status.add_argument("--worker-id", required=True)
    status_watch = commands.add_parser("watch-status")
    status_watch.add_argument("--worker-id", required=True)
    status_watch.add_argument("--output-path", type=Path, required=True)
    status_watch.add_argument("--duration-seconds", type=int, default=180)
    job_status = commands.add_parser("job-status")
    job_status.add_argument("--worker-job-id", required=True)
    operator_sessions = commands.add_parser("operator-session-count")
    operator_sessions.add_argument("--operator-username", required=True)
    audit = commands.add_parser("audit-count")
    audit.add_argument("--worker-id", required=True)
    page = commands.add_parser("serve-page")
    page.add_argument("--host", default="127.0.0.1")
    page.add_argument("--port", type=int, default=443)
    page.add_argument("--certificate", type=Path, required=True)
    page.add_argument("--private-key", type=Path, required=True)
    page.add_argument("--release-file", type=Path)
    page.add_argument("--maximum-wait-seconds", type=int, default=45)
    api = commands.add_parser("serve-api")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, required=True)
    api.add_argument("--certificate", type=Path, required=True)
    api.add_argument("--private-key", type=Path, required=True)
    api.add_argument("--request-counts", type=Path, required=True)
    request_counts = commands.add_parser("request-counts")
    request_counts.add_argument("--counter-file", type=Path, required=True)
    drain_audits = commands.add_parser("drain-audit-counts")
    drain_audits.add_argument("--worker-id", required=True)
    args = parser.parse_args()
    try:
        if args.command == "seed":
            result = asyncio.run(_seed(args))
        elif args.command == "seed-identity-only":
            result = _seed_identity_only(args)
        elif args.command == "queue-profile-job":
            result = asyncio.run(_queue_profile_job(args))
        elif args.command == "status":
            result = asyncio.run(_status(args))
        elif args.command == "watch-status":
            asyncio.run(_watch_status(args))
            return 0
        elif args.command == "job-status":
            result = asyncio.run(_job_status(args))
        elif args.command == "operator-session-count":
            result = asyncio.run(_operator_session_count(args))
        elif args.command == "audit-count":
            result = asyncio.run(_audit_count(args))
        elif args.command == "request-counts":
            result = _request_counts(args)
        elif args.command == "drain-audit-counts":
            result = asyncio.run(_drain_audit_counts(args))
        elif args.command == "serve-api":
            _serve_api(args)
            return 0
        else:
            _serve_page(args)
            return 0
    except Exception:
        print("worker_desktop_fixture_operation_failed", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
