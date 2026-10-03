from __future__ import annotations

import ast
import hashlib
import ipaddress
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from _pytest.capture import CaptureFixture
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from threads_platform import controller_tls_admin as tls_admin


def _state(tmp_path: Path, address: str = "192.0.2.44") -> tuple[Path, Path]:
    admin = tmp_path / "controller-tls-admin"
    serving = tmp_path / "controller-tls-serving"
    tls_admin.provision(admin, serving, address)
    return admin, serving


def _simulate_posix_private_mode(
    monkeypatch: pytest.MonkeyPatch, target_path: Path, mode: int
) -> None:
    original_lstat = Path.lstat

    def lstat(path: Path) -> os.stat_result:
        result = original_lstat(path)
        if path == target_path:
            values = list(result)
            values[0] = stat.S_IFREG | mode
            return os.stat_result(values)
        return result

    monkeypatch.setattr(tls_admin.os, "name", "posix")
    monkeypatch.setattr(Path, "lstat", lstat)


def test_root_identity_persists_and_fingerprint_uses_actual_der(tmp_path: Path) -> None:
    admin, serving = _state(tmp_path)
    root_cert = x509.load_pem_x509_certificate((admin / "root-cert.pem").read_bytes())
    der = root_cert.public_bytes(serialization.Encoding.DER)
    expected = f"SHA256:{hashlib.sha256(der).hexdigest()}"

    first = tls_admin.fingerprint(admin)
    second = tls_admin.provision(admin, serving, "192.0.2.44")

    assert first == expected == second
    assert len(first) == 71
    assert first.startswith("SHA256:")
    assert first[7:] == first[7:].lower()


def test_leaf_is_signed_by_root_matches_key_and_has_exact_configured_sans(tmp_path: Path) -> None:
    admin, serving = _state(tmp_path)
    root = x509.load_pem_x509_certificate((admin / "root-cert.pem").read_bytes())
    leaf = x509.load_pem_x509_certificate((serving / "leaf-cert.pem").read_bytes())
    leaf_key = serialization.load_pem_private_key(
        (serving / "leaf-key.pem").read_bytes(), password=None
    )
    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    usages = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value

    leaf.verify_directly_issued_by(root)
    assert leaf.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    ) == leaf_key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    assert list(san) == [
        x509.IPAddress(ipaddress.IPv4Address("192.0.2.44")),
        x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
    ]
    assert list(usages) == [x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]
    assert (serving / "leaf-fullchain.pem").read_bytes() == (
        leaf.public_bytes(serialization.Encoding.PEM)
        + root.public_bytes(serialization.Encoding.PEM)
    )


def test_leaf_with_wrong_san_fails_validation(tmp_path: Path) -> None:
    admin, serving = _state(tmp_path)
    with pytest.raises(ValueError, match="controller_tls_identity_invalid"):
        tls_admin.provision(admin, serving, "198.51.100.9")


@pytest.mark.parametrize("not_before_offset,not_after_offset", [(-2, -1), (1, 2)])
def test_expired_and_not_yet_valid_leaf_fail(
    tmp_path: Path, not_before_offset: int, not_after_offset: int
) -> None:
    admin, serving = _state(tmp_path)
    root = x509.load_pem_x509_certificate((admin / "root-cert.pem").read_bytes())
    root_key = serialization.load_pem_private_key(
        (admin / "root-key.pem").read_bytes(), password=None
    )
    if not isinstance(root_key, ec.EllipticCurvePrivateKey):
        raise AssertionError("synthetic TLS fixture root is not ECDSA")
    addresses = [ipaddress.IPv4Address("192.0.2.44"), ipaddress.IPv4Address("127.0.0.1")]
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC)
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([]))
        .issuer_name(root.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now + timedelta(days=not_before_offset))
        .not_valid_after(now + timedelta(days=not_after_offset))
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
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(address) for address in addresses]),
            critical=False,
        )
        .sign(root_key, hashes.SHA256())
    )
    leaf_pem = leaf.public_bytes(serialization.Encoding.PEM)
    leaf_key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    (serving / "leaf-key.pem").write_bytes(leaf_key_pem)
    (serving / "leaf-cert.pem").write_bytes(leaf_pem)
    (serving / "leaf-fullchain.pem").write_bytes(
        leaf_pem + root.public_bytes(serialization.Encoding.PEM)
    )

    with pytest.raises(ValueError, match="controller_tls_identity_invalid"):
        tls_admin.provision(admin, serving, "192.0.2.44")


def test_wrong_root_and_persisted_root_substitution_fail_closed(tmp_path: Path) -> None:
    admin_a, serving_a = _state(tmp_path / "a")
    admin_b, _ = _state(tmp_path / "b")
    with pytest.raises(ValueError, match="controller_tls_identity_invalid"):
        tls_admin.provision(admin_b, serving_a, "192.0.2.44")

    original = (admin_a / "root-cert.pem").read_bytes()
    (admin_a / "root-cert.pem").write_bytes((admin_b / "root-cert.pem").read_bytes())
    with pytest.raises(ValueError, match="controller_tls_identity_invalid"):
        tls_admin.fingerprint(admin_a)
    (admin_a / "root-cert.pem").write_bytes(original)


def test_same_root_leaf_renewal_preserves_fingerprint(tmp_path: Path) -> None:
    admin, serving = _state(tmp_path)
    fingerprint_before = tls_admin.fingerprint(admin)
    old_leaf = (serving / "leaf-cert.pem").read_bytes()

    fingerprint_after = tls_admin.renew(admin, serving, "192.0.2.44")

    assert fingerprint_after == fingerprint_before
    assert (serving / "leaf-cert.pem").read_bytes() != old_leaf
    tls_admin.provision(admin, serving, "192.0.2.44")


def test_expired_leaf_can_be_explicitly_renewed_under_the_same_root(tmp_path: Path) -> None:
    admin, serving = _state(tmp_path)
    fingerprint_before = tls_admin.fingerprint(admin)
    root_bytes = (admin / "root-cert.pem").read_bytes()
    root = x509.load_pem_x509_certificate(root_bytes)
    root_key = serialization.load_pem_private_key(
        (admin / "root-key.pem").read_bytes(), password=None
    )
    if not isinstance(root_key, ec.EllipticCurvePrivateKey):
        raise AssertionError("synthetic TLS fixture root is not ECDSA")
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC)
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([]))
        .issuer_name(root.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=2))
        .not_valid_after(now - timedelta(days=1))
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
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.IPAddress(ipaddress.IPv4Address("192.0.2.44")),
                    x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(root_key, hashes.SHA256())
    )
    expired_leaf = leaf.public_bytes(serialization.Encoding.PEM)
    (serving / "leaf-cert.pem").write_bytes(expired_leaf)
    (serving / "leaf-key.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    (serving / "leaf-fullchain.pem").write_bytes(
        expired_leaf + root.public_bytes(serialization.Encoding.PEM)
    )

    fingerprint_after = tls_admin.renew(admin, serving, "192.0.2.44")
    renewed = x509.load_pem_x509_certificate((serving / "leaf-cert.pem").read_bytes())

    assert fingerprint_after == fingerprint_before
    assert renewed.public_bytes(serialization.Encoding.DER) != leaf.public_bytes(
        serialization.Encoding.DER
    )
    assert renewed.not_valid_after_utc > datetime.now(UTC)


def test_corrupt_root_key_fails_without_replacing_root_identity(tmp_path: Path) -> None:
    admin, serving = _state(tmp_path)
    original_root = (admin / "root-cert.pem").read_bytes()
    (admin / "root-key.pem").write_bytes(b"corrupt synthetic test key")

    with pytest.raises(ValueError, match="controller_tls_identity_invalid"):
        tls_admin.provision(admin, serving, "192.0.2.44")
    assert (admin / "root-cert.pem").read_bytes() == original_root


def test_missing_tls_files_after_provisioning_fail_without_replacing_root(
    tmp_path: Path,
) -> None:
    admin, serving = _state(tmp_path)
    original_fingerprint = tls_admin.fingerprint(admin)
    for path in (
        admin / "root-cert.pem",
        admin / "root-key.pem",
        serving / "leaf-cert.pem",
        serving / "leaf-key.pem",
        serving / "leaf-fullchain.pem",
    ):
        path.unlink()

    with pytest.raises(ValueError, match="controller_tls_identity_invalid"):
        tls_admin.provision(admin, serving, "192.0.2.44")
    with pytest.raises(ValueError, match="controller_tls_identity_invalid"):
        tls_admin.fingerprint(admin)
    assert (admin / "identity-provisioned").read_text(encoding="ascii").strip() == (
        original_fingerprint
    )
    assert not (admin / "root-cert.pem").exists()


def test_linux_root_private_key_mode_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin, _ = _state(tmp_path)
    _simulate_posix_private_mode(monkeypatch, admin / "root-key.pem", 0o644)

    with pytest.raises(ValueError, match="controller_tls_identity_invalid"):
        tls_admin.fingerprint(admin)


def test_linux_leaf_private_key_mode_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin, serving = _state(tmp_path)
    _simulate_posix_private_mode(monkeypatch, serving / "leaf-key.pem", 0o640)

    with pytest.raises(ValueError, match="controller_tls_identity_invalid"):
        tls_admin.provision(admin, serving, "192.0.2.44")


def test_cli_fingerprint_matches_the_served_controller_root(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    admin, serving = _state(tmp_path)
    root = x509.load_pem_x509_certificate((admin / "root-cert.pem").read_bytes())
    served_chain = x509.load_pem_x509_certificates((serving / "leaf-fullchain.pem").read_bytes())
    expected = f"SHA256:{hashlib.sha256(root.public_bytes(serialization.Encoding.DER)).hexdigest()}"

    assert tls_admin.main(["fingerprint", "--admin-dir", str(admin)]) == 0
    assert capsys.readouterr().out.strip() == expected
    assert served_chain[1].public_bytes(serialization.Encoding.DER) == root.public_bytes(
        serialization.Encoding.DER
    )


def test_configured_endpoint_requires_ipv4(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="controller_tls_identity_invalid"):
        tls_admin.provision(tmp_path / "admin", tmp_path / "serving", "2001:db8::1")


def test_linux_tls_admin_has_no_tauri_or_windows_dpapi_imports() -> None:
    source = Path(tls_admin.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_roots = {
        alias.name.split(".", maxsplit=1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_roots.update(
        node.module.split(".", maxsplit=1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )

    assert "cryptography" in imported_roots
    assert "tauri" not in imported_roots
    assert "windows_crypto" not in imported_roots
