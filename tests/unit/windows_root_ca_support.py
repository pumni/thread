from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

_CERTIFICATE_FILE = "synthetic-worker-control-root.cer"
_PRIVATE_KEY_FILE = "synthetic-worker-control-root-key.pem"
_ROOT_SNAPSHOT_FILE = "root-stores-before.json"
_FIXTURE_MARKER_FILE = "fixture.json"
_FIXTURE_FILES = {
    _CERTIFICATE_FILE,
    _PRIVATE_KEY_FILE,
    _ROOT_SNAPSHOT_FILE,
    _FIXTURE_MARKER_FILE,
}
_FIXTURE_PREFIX = "thread-currentuser-root-ca-"


def _validate_fixture_directory(directory: Path) -> None:
    resolved_directory = directory.resolve()
    if resolved_directory.parent != Path(
        tempfile.gettempdir()
    ).resolve() or not resolved_directory.name.startswith(_FIXTURE_PREFIX):
        raise ValueError("the validation fixture is not in its expected temporary directory")


@dataclass(frozen=True, slots=True)
class TestCertificateAuthority:
    private_key: ec.EllipticCurvePrivateKey
    certificate: x509.Certificate


def create_test_certificate_authority() -> TestCertificateAuthority:
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
    return TestCertificateAuthority(ca_key, ca_certificate)


def save_test_certificate_authority(
    authority: TestCertificateAuthority,
    directory: Path,
) -> None:
    (directory / _CERTIFICATE_FILE).write_bytes(
        authority.certificate.public_bytes(serialization.Encoding.DER)
    )
    (directory / _PRIVATE_KEY_FILE).write_bytes(
        authority.private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )


def load_test_certificate_authority(directory: Path) -> TestCertificateAuthority:
    certificate = x509.load_der_x509_certificate((directory / _CERTIFICATE_FILE).read_bytes())
    private_key = serialization.load_pem_private_key(
        (directory / _PRIVATE_KEY_FILE).read_bytes(),
        password=None,
    )
    if not isinstance(private_key, ec.EllipticCurvePrivateKey):
        raise ValueError("the validation fixture CA key is not an EC private key")
    certificate_public_key = certificate.public_key()
    if not isinstance(certificate_public_key, ec.EllipticCurvePublicKey):
        raise ValueError("the validation fixture CA certificate is not an EC certificate")
    if private_key.public_key().public_numbers() != certificate_public_key.public_numbers():
        raise ValueError("the validation fixture CA key does not match its certificate")
    return TestCertificateAuthority(private_key, certificate)


def load_root_store_snapshot(directory: Path) -> tuple[set[str], set[str]]:
    snapshot_value: object = json.loads((directory / _ROOT_SNAPSHOT_FILE).read_text("utf-8"))
    if not isinstance(snapshot_value, dict):
        raise ValueError("the validation fixture root-store snapshot is invalid")
    snapshot = cast(dict[str, object], snapshot_value)
    current_user = snapshot.get("current_user")
    local_machine = snapshot.get("local_machine")
    if not isinstance(current_user, list) or not isinstance(local_machine, list):
        raise ValueError("the validation fixture root-store snapshot is incomplete")
    user_values = cast(list[object], current_user)
    machine_values = cast(list[object], local_machine)
    if any(not isinstance(value, str) for value in user_values + machine_values):
        raise ValueError("the validation fixture root-store snapshot has invalid values")
    return set(cast(list[str], user_values)), set(cast(list[str], machine_values))


def validate_test_certificate_authority_fixture(directory: Path) -> None:
    _validate_fixture_directory(directory)
    if {path.name for path in directory.iterdir()} != _FIXTURE_FILES:
        raise ValueError("the validation fixture directory has missing or unexpected files")
    marker_value: object = json.loads((directory / _FIXTURE_MARKER_FILE).read_text("utf-8"))
    if not isinstance(marker_value, dict):
        raise ValueError("the validation fixture marker is invalid")
    marker = cast(dict[str, object], marker_value)
    if (
        marker.get("format") != 1
        or marker.get("purpose") != "Windows CurrentUser root CA validation"
    ):
        raise ValueError("the validation fixture marker does not match this test")


def write_root_store_snapshot(
    directory: Path,
    current_user: set[str],
    local_machine: set[str],
) -> None:
    (directory / _ROOT_SNAPSHOT_FILE).write_text(
        json.dumps(
            {
                "current_user": sorted(current_user),
                "local_machine": sorted(local_machine),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def write_fixture_marker(directory: Path) -> None:
    (directory / _FIXTURE_MARKER_FILE).write_text(
        json.dumps({"format": 1, "purpose": "Windows CurrentUser root CA validation"}),
        encoding="utf-8",
    )


def cleanup_test_certificate_authority_fixture(directory: Path) -> None:
    _validate_fixture_directory(directory)
    if any(path.name not in _FIXTURE_FILES for path in directory.iterdir()):
        raise ValueError("the validation fixture directory contains unexpected files")
    for filename in _FIXTURE_FILES:
        (directory / filename).unlink(missing_ok=True)
    directory.rmdir()
