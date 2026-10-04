from __future__ import annotations

import argparse
import ipaddress
import os
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

_ROOT_CERT = "root-cert.pem"
_ROOT_KEY = "root-key.pem"
_LEAF_CERT = "leaf-cert.pem"
_LEAF_KEY = "leaf-key.pem"
_FULLCHAIN = "leaf-fullchain.pem"
_IDENTITY_MARKER = "identity-provisioned"
_ERROR = "controller_tls_identity_invalid"


@dataclass(frozen=True, slots=True)
class _Root:
    certificate: x509.Certificate
    private_key: ec.EllipticCurvePrivateKey


def _invalid(error: ValueError) -> ValueError:
    if str(error) == _ERROR:
        return error
    return ValueError(_ERROR)


def _now() -> datetime:
    return datetime.now(UTC)


def _endpoint_addresses(value: str) -> tuple[ipaddress.IPv4Address, ipaddress.IPv4Address]:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise ValueError(_ERROR) from error
    if not isinstance(address, ipaddress.IPv4Address):
        raise ValueError(_ERROR)
    return address, ipaddress.IPv4Address("127.0.0.1")


def _check_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise ValueError(_ERROR)
    owner = _current_uid()
    if owner is not None and path.stat().st_uid != owner:
        raise ValueError(_ERROR)


def _current_uid() -> int | None:
    getuid = vars(os).get("getuid")
    if getuid is None:
        return None
    return getuid()


def _read_private_key(path: Path) -> ec.EllipticCurvePrivateKey:
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ValueError(_ERROR)
        if os.name == "posix" and stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ValueError(_ERROR)
        owner = _current_uid()
        if owner is not None and metadata.st_uid != owner:
            raise ValueError(_ERROR)
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except ValueError as error:
        raise _invalid(error) from error
    except Exception as error:
        raise ValueError(_ERROR) from error
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError(_ERROR)
    return key


def _write(path: Path, payload: bytes, *, private: bool) -> None:
    mode = 0o600 if private else 0o644
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    except Exception as error:
        raise ValueError(_ERROR) from error
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception as error:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise ValueError(_ERROR) from error


def _replace(path: Path, payload: bytes, *, private: bool) -> None:
    descriptor = -1
    staging: Path | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        staging = Path(raw_path)
        os.chmod(staging, 0o600 if private else 0o644)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staging, path)
        if os.name == "posix":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except Exception as error:
        if descriptor >= 0:
            os.close(descriptor)
        if staging is not None:
            staging.unlink(missing_ok=True)
        raise ValueError(_ERROR) from error


def _public_key_bytes(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def _load_root(admin_dir: Path) -> _Root:
    try:
        cert_path = admin_dir / _ROOT_CERT
        metadata = cert_path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ValueError(_ERROR)
        certificate = x509.load_pem_x509_certificate(cert_path.read_bytes())
        private_key = _read_private_key(admin_dir / _ROOT_KEY)
        constraints = certificate.extensions.get_extension_for_class(x509.BasicConstraints)
        usage = certificate.extensions.get_extension_for_class(x509.KeyUsage)
        public_key = certificate.public_key()
        signature_hash = certificate.signature_hash_algorithm
        if (
            not constraints.critical
            or not constraints.value.ca
            or constraints.value.path_length != 0
            or usage.value.digital_signature
            or usage.value.content_commitment
            or usage.value.key_encipherment
            or usage.value.data_encipherment
            or usage.value.key_agreement
            or not usage.value.key_cert_sign
            or not usage.value.crl_sign
            or not isinstance(public_key, ec.EllipticCurvePublicKey)
            or not isinstance(public_key.curve, ec.SECP256R1)
            or signature_hash is None
            or signature_hash.name != "sha256"
            or certificate.issuer != certificate.subject
            or _public_key_bytes(public_key) != _public_key_bytes(private_key.public_key())
        ):
            raise ValueError(_ERROR)
        certificate.verify_directly_issued_by(certificate)
        if certificate.not_valid_before_utc > _now() or certificate.not_valid_after_utc <= _now():
            raise ValueError(_ERROR)
        return _Root(certificate, private_key)
    except ValueError as error:
        raise _invalid(error) from error
    except Exception as error:
        raise ValueError(_ERROR) from error


def _fingerprint(certificate: x509.Certificate) -> str:
    import hashlib

    return (
        f"SHA256:{hashlib.sha256(certificate.public_bytes(serialization.Encoding.DER)).hexdigest()}"
    )


def _load_provisioned_root(admin_dir: Path) -> _Root:
    root = _load_root(admin_dir)
    marker = admin_dir / _IDENTITY_MARKER
    try:
        metadata = marker.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ValueError(_ERROR)
        expected = f"{_fingerprint(root.certificate)}\n".encode("ascii")
        if marker.read_bytes() != expected:
            raise ValueError(_ERROR)
    except ValueError as error:
        raise _invalid(error) from error
    except Exception as error:
        raise ValueError(_ERROR) from error
    return root


def _new_root() -> _Root:
    private_key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Threads Controller Root")])
    now = _now()
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=3650))
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
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(private_key.public_key()), False)
        .sign(private_key, hashes.SHA256())
    )
    return _Root(certificate, private_key)


def _new_leaf(
    root: _Root, addresses: tuple[ipaddress.IPv4Address, ...]
) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Threads Controller")])
    now = _now()
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(root.certificate.subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=90))
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
            x509.SubjectAlternativeName([x509.IPAddress(address) for address in addresses]),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(root.private_key.public_key()), False
        )
        .sign(root.private_key, hashes.SHA256())
    )
    return certificate, private_key


def _validate_leaf(
    root: _Root,
    certificate: x509.Certificate,
    private_key: ec.EllipticCurvePrivateKey,
    expected_addresses: tuple[ipaddress.IPv4Address, ...],
    *,
    check_validity: bool = True,
) -> None:
    try:
        constraints = certificate.extensions.get_extension_for_class(x509.BasicConstraints)
        usage = certificate.extensions.get_extension_for_class(x509.KeyUsage)
        eku = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
        san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        public_key = certificate.public_key()
        signature_hash = certificate.signature_hash_algorithm
        if (
            constraints.value.ca
            or not usage.value.digital_signature
            or usage.value.content_commitment
            or usage.value.key_encipherment
            or usage.value.data_encipherment
            or usage.value.key_agreement
            or usage.value.key_cert_sign
            or usage.value.crl_sign
            or list(eku.value) != [ExtendedKeyUsageOID.SERVER_AUTH]
            or list(san.value) != [x509.IPAddress(address) for address in expected_addresses]
            or len(san.value) != 2
            or certificate.issuer != root.certificate.subject
            or signature_hash is None
            or signature_hash.name != "sha256"
            or not isinstance(public_key, ec.EllipticCurvePublicKey)
            or not isinstance(public_key.curve, ec.SECP256R1)
            or _public_key_bytes(public_key) != _public_key_bytes(private_key.public_key())
        ):
            raise ValueError(_ERROR)
        certificate.verify_directly_issued_by(root.certificate)
        now = _now()
        if check_validity and (
            certificate.not_valid_before_utc > now or certificate.not_valid_after_utc <= now
        ):
            raise ValueError(_ERROR)
    except ValueError as error:
        raise _invalid(error) from error
    except Exception as error:
        raise ValueError(_ERROR) from error


def _load_leaf(
    serving_dir: Path,
    root: _Root,
    addresses: tuple[ipaddress.IPv4Address, ...],
    *,
    allow_expired: bool = False,
) -> x509.Certificate:
    try:
        path = serving_dir / _LEAF_CERT
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ValueError(_ERROR)
        certificate = x509.load_pem_x509_certificate(path.read_bytes())
        private_key = _read_private_key(serving_dir / _LEAF_KEY)
        _validate_leaf(
            root,
            certificate,
            private_key,
            addresses,
            check_validity=not allow_expired,
        )
        fullchain = (serving_dir / _FULLCHAIN).read_bytes()
        expected = certificate.public_bytes(
            serialization.Encoding.PEM
        ) + root.certificate.public_bytes(serialization.Encoding.PEM)
        if fullchain != expected:
            raise ValueError(_ERROR)
        return certificate
    except ValueError as error:
        raise _invalid(error) from error
    except Exception as error:
        raise ValueError(_ERROR) from error


def _store_leaf(
    serving_dir: Path,
    root: _Root,
    addresses: tuple[ipaddress.IPv4Address, ...],
) -> x509.Certificate:
    certificate, private_key = _new_leaf(root, addresses)
    cert_bytes = certificate.public_bytes(serialization.Encoding.PEM)
    key_bytes = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    _validate_leaf(root, certificate, private_key, addresses)
    _replace_leaf_bundle(
        serving_dir,
        root,
        addresses,
        cert_bytes,
        key_bytes,
        cert_bytes + root.certificate.public_bytes(serialization.Encoding.PEM),
    )
    return certificate


def _replace_leaf_bundle(
    serving_dir: Path,
    root: _Root,
    addresses: tuple[ipaddress.IPv4Address, ...],
    cert_bytes: bytes,
    key_bytes: bytes,
    fullchain_bytes: bytes,
) -> None:
    names = (_LEAF_CERT, _LEAF_KEY, _FULLCHAIN)
    current = [serving_dir / name for name in names]
    present = [path.exists() or path.is_symlink() for path in current]
    if any(present) and not all(present):
        raise ValueError(_ERROR)
    for path in current:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError(_ERROR)

    try:
        with tempfile.TemporaryDirectory(prefix=".tls-candidate-", dir=serving_dir) as raw_stage:
            stage = Path(raw_stage)
            candidate = stage / "candidate"
            candidate.mkdir(mode=0o700)
            _write(candidate / _LEAF_CERT, cert_bytes, private=False)
            _write(candidate / _LEAF_KEY, key_bytes, private=True)
            _write(candidate / _FULLCHAIN, fullchain_bytes, private=False)
            _load_leaf(candidate, root, addresses)

            old = (
                {name: path.read_bytes() for name, path in zip(names, current, strict=True)}
                if all(present)
                else None
            )
            try:
                for name in names:
                    os.replace(candidate / name, serving_dir / name)
                if os.name == "posix":
                    directory_fd = os.open(serving_dir, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
            except Exception as error:
                try:
                    if old is None:
                        for name in names:
                            (serving_dir / name).unlink(missing_ok=True)
                    else:
                        for name, payload in old.items():
                            _replace(
                                serving_dir / name,
                                payload,
                                private=name == _LEAF_KEY,
                            )
                except Exception as rollback_error:
                    raise ValueError(_ERROR) from rollback_error
                raise ValueError(_ERROR) from error
    except ValueError as error:
        raise _invalid(error) from error
    except Exception as error:
        raise ValueError(_ERROR) from error


def provision(admin_dir: Path, serving_dir: Path, lan_address: str) -> str:
    addresses = _endpoint_addresses(lan_address)
    _check_directory(admin_dir)
    _check_directory(serving_dir)
    files = [admin_dir / _ROOT_CERT, admin_dir / _ROOT_KEY]
    serving_files = [serving_dir / _LEAF_CERT, serving_dir / _LEAF_KEY, serving_dir / _FULLCHAIN]
    marker = admin_dir / _IDENTITY_MARKER
    if marker.exists() or marker.is_symlink():
        root = _load_provisioned_root(admin_dir)
        _load_leaf(serving_dir, root, addresses)
        return _fingerprint(root.certificate)
    if any(path.exists() or path.is_symlink() for path in files + serving_files):
        raise ValueError(_ERROR)

    root = _new_root()
    root_key = root.private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    _write(admin_dir / _ROOT_KEY, root_key, private=True)
    _write(
        admin_dir / _ROOT_CERT,
        root.certificate.public_bytes(serialization.Encoding.PEM),
        private=False,
    )
    _store_leaf(serving_dir, root, addresses)
    root_fingerprint = _fingerprint(root.certificate)
    _write(marker, f"{root_fingerprint}\n".encode("ascii"), private=False)
    return root_fingerprint


def renew(admin_dir: Path, serving_dir: Path, lan_address: str) -> str:
    addresses = _endpoint_addresses(lan_address)
    _check_directory(admin_dir)
    _check_directory(serving_dir)
    root = _load_provisioned_root(admin_dir)
    leaf_presence = [
        (serving_dir / _LEAF_CERT).exists(),
        (serving_dir / _LEAF_KEY).exists(),
        (serving_dir / _FULLCHAIN).exists(),
    ]
    if not all(leaf_presence):
        raise ValueError(_ERROR)
    _load_leaf(serving_dir, root, addresses, allow_expired=True)
    _store_leaf(serving_dir, root, addresses)
    return _fingerprint(root.certificate)


def ensure(admin_dir: Path, serving_dir: Path, lan_address: str) -> str:
    """Validate normal serving state and renew only a still-valid leaf in its renewal window."""
    addresses = _endpoint_addresses(lan_address)
    _check_directory(admin_dir)
    _check_directory(serving_dir)
    root = _load_provisioned_root(admin_dir)
    leaf = _load_leaf(serving_dir, root, addresses)
    if leaf.not_valid_after_utc - _now() <= timedelta(days=30):
        _store_leaf(serving_dir, root, addresses)
    return _fingerprint(root.certificate)


def reissue(
    admin_dir: Path,
    serving_dir: Path,
    current_lan_address: str,
    lan_address: str,
) -> str:
    """Explicitly issue new serving material for an endpoint under the existing root."""
    current_addresses = _endpoint_addresses(current_lan_address)
    requested_addresses = _endpoint_addresses(lan_address)
    _check_directory(admin_dir)
    _check_directory(serving_dir)
    root = _load_provisioned_root(admin_dir)
    _load_leaf(serving_dir, root, current_addresses)
    _store_leaf(serving_dir, root, requested_addresses)
    return _fingerprint(root.certificate)


def fingerprint(admin_dir: Path) -> str:
    root = _load_provisioned_root(admin_dir)
    return _fingerprint(root.certificate)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="threads-platform-controller-tls")
    subparsers = parser.add_subparsers(dest="operation", required=True)
    for operation in ("provision", "renew", "ensure"):
        subparser = subparsers.add_parser(operation)
        subparser.add_argument("--admin-dir", type=Path, required=True)
        subparser.add_argument("--serving-dir", type=Path, required=True)
        subparser.add_argument("--lan-address", required=True)
    reissue_parser = subparsers.add_parser("reissue")
    reissue_parser.add_argument("--admin-dir", type=Path, required=True)
    reissue_parser.add_argument("--serving-dir", type=Path, required=True)
    reissue_parser.add_argument("--current-lan-address", required=True)
    reissue_parser.add_argument("--lan-address", required=True)
    fingerprint_parser = subparsers.add_parser("fingerprint")
    fingerprint_parser.add_argument("--admin-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.operation == "provision":
            result = provision(args.admin_dir, args.serving_dir, args.lan_address)
        elif args.operation == "renew":
            result = renew(args.admin_dir, args.serving_dir, args.lan_address)
        elif args.operation == "ensure":
            result = ensure(args.admin_dir, args.serving_dir, args.lan_address)
        elif args.operation == "reissue":
            result = reissue(
                args.admin_dir,
                args.serving_dir,
                args.current_lan_address,
                args.lan_address,
            )
        else:
            result = fingerprint(args.admin_dir)
    except Exception:
        print(_ERROR, file=sys.stderr)
        return 2
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
