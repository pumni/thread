from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import cast

from cryptography.hazmat.primitives import serialization
from windows_root_ca_support import (
    cleanup_test_certificate_authority_fixture,
    create_test_certificate_authority,
    save_test_certificate_authority,
    write_fixture_marker,
    write_root_store_snapshot,
)

_ROOT_STORE_HELPER = Path(__file__).parents[1] / "scripts" / "windows_root_store.py"
_REPOSITORY_ROOT = Path(__file__).parents[2].resolve()


def _list_root_store(scope: str) -> set[str]:
    try:
        result = subprocess.run(
            [sys.executable, str(_ROOT_STORE_HELPER), "list", "--scope", scope],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=15.0,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"timed out inspecting the {scope} root store") from None
    if result.returncode != 0:
        raise RuntimeError(f"could not inspect the {scope} root store")
    values: object = json.loads(result.stdout.decode("utf-8"))
    if not isinstance(values, list):
        raise RuntimeError(f"the {scope} root-store response is invalid")
    items = cast(list[object], values)
    if any(not isinstance(value, str) for value in items):
        raise RuntimeError(f"the {scope} root-store response has invalid values")
    return set(cast(list[str], items))


def prepare_fixture() -> Path:
    directory = Path(tempfile.mkdtemp(prefix="thread-currentuser-root-ca-")).resolve()
    if directory == _REPOSITORY_ROOT or _REPOSITORY_ROOT in directory.parents:
        raise RuntimeError("the validation fixture must be created outside the repository")

    try:
        current_user_roots = _list_root_store("current-user")
        local_machine_roots = _list_root_store("local-machine")
        authority = create_test_certificate_authority()
        certificate_der = authority.certificate.public_bytes(serialization.Encoding.DER)
        thumbprint = hashlib.sha1(certificate_der).hexdigest().upper()
        if thumbprint in current_user_roots or thumbprint in local_machine_roots:
            raise RuntimeError("generated validation CA already exists in a root store")

        save_test_certificate_authority(authority, directory)
        write_root_store_snapshot(directory, current_user_roots, local_machine_roots)
        write_fixture_marker(directory)
    except Exception:
        cleanup_test_certificate_authority_fixture(directory)
        raise

    print(f"WINDOWS_ROOT_CA_FIXTURE directory={directory}")
    print(f"WINDOWS_ROOT_CA_CERTIFICATE file={directory / 'synthetic-worker-control-root.cer'}")
    print("WINDOWS_ROOT_CA_FIXTURE private_key_written=yes root_store_snapshots_written=yes")
    return directory


def main() -> int:
    if sys.platform != "win32":
        print("This fixture must be prepared on Windows.", file=sys.stderr)
        return 2
    try:
        prepare_fixture()
    except Exception as error:
        print(f"WINDOWS_ROOT_CA_FIXTURE result=failed reason={type(error).__name__}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
