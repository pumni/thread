from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import re
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Any, cast

_CERT_STORE_PROV_SYSTEM_W = 10
_CERT_SYSTEM_STORE_CURRENT_USER = 0x00010000
_CERT_SYSTEM_STORE_LOCAL_MACHINE = 0x00020000
_CERT_STORE_OPEN_EXISTING_FLAG = 0x00004000
_CERT_STORE_READONLY_FLAG = 0x00008000
_CERT_STORE_ADD_NEW = 1
_X509_ASN_ENCODING = 0x00000001
_PKCS_7_ASN_ENCODING = 0x00010000
_CERT_ENCODING = _X509_ASN_ENCODING | _PKCS_7_ASN_ENCODING

_win_dll: Any = getattr(ctypes, "WinDLL", None)
_get_last_error: Any = getattr(ctypes, "get_last_error", None)
if _win_dll is None or _get_last_error is None:
    raise SystemExit("Windows CryptoAPI is available only on Windows")


class _CertContext(ctypes.Structure):
    _fields_ = [
        ("dwCertEncodingType", wintypes.DWORD),
        ("pbCertEncoded", ctypes.POINTER(wintypes.BYTE)),
        ("cbCertEncoded", wintypes.DWORD),
        ("pCertInfo", ctypes.c_void_p),
        ("hCertStore", ctypes.c_void_p),
    ]


_crypt32: Any = _win_dll("Crypt32.dll", use_last_error=True)

_cert_open_store: Any = _crypt32.CertOpenStore
_cert_open_store.argtypes = [
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.c_void_p,
]
_cert_open_store.restype = ctypes.c_void_p

_cert_close_store: Any = _crypt32.CertCloseStore
_cert_close_store.argtypes = [ctypes.c_void_p, wintypes.DWORD]
_cert_close_store.restype = wintypes.BOOL

_cert_enum_certificates: Any = _crypt32.CertEnumCertificatesInStore
_cert_enum_certificates.argtypes = [ctypes.c_void_p, ctypes.POINTER(_CertContext)]
_cert_enum_certificates.restype = ctypes.POINTER(_CertContext)

_cert_free_context: Any = _crypt32.CertFreeCertificateContext
_cert_free_context.argtypes = [ctypes.POINTER(_CertContext)]
_cert_free_context.restype = wintypes.BOOL

_cert_add_encoded_certificate: Any = _crypt32.CertAddEncodedCertificateToStore
_cert_add_encoded_certificate.argtypes = [
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.BYTE),
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
]
_cert_add_encoded_certificate.restype = wintypes.BOOL

_cert_delete_certificate: Any = _crypt32.CertDeleteCertificateFromStore
_cert_delete_certificate.argtypes = [ctypes.POINTER(_CertContext)]
_cert_delete_certificate.restype = wintypes.BOOL


def _stage(name: str) -> None:
    print(f"WINCRYPT_STAGE stage={name}", file=sys.stderr, flush=True)


class _WinCryptError(RuntimeError):
    def __init__(self, action: str, error_code: int) -> None:
        self.action = action
        self.error_code = error_code
        super().__init__(action)


def _raise_wincrypt_error(action: str) -> None:
    raise _WinCryptError(action, int(_get_last_error()))


def _open_root_store(scope: str, *, read_only: bool) -> int:
    store_location = {
        "current-user": _CERT_SYSTEM_STORE_CURRENT_USER,
        "local-machine": _CERT_SYSTEM_STORE_LOCAL_MACHINE,
    }[scope]
    flags = store_location | _CERT_STORE_OPEN_EXISTING_FLAG
    if read_only:
        flags |= _CERT_STORE_READONLY_FLAG
    _stage(f"open_{scope.replace('-', '_')}_root_start")
    store_name = ctypes.c_wchar_p("Root")
    handle = _cert_open_store(
        ctypes.c_void_p(_CERT_STORE_PROV_SYSTEM_W),
        0,
        None,
        flags,
        ctypes.cast(store_name, ctypes.c_void_p),
    )
    if not handle:
        _raise_wincrypt_error("open_root_store")
    _stage(f"open_{scope.replace('-', '_')}_root_done")
    return cast(int, handle)


def _close_root_store(store: int) -> None:
    if not _cert_close_store(store, 0):
        _raise_wincrypt_error("close_root_store")


def _certificate_der(context: Any) -> bytes:
    data = context.contents
    return ctypes.string_at(data.pbCertEncoded, data.cbCertEncoded)


def _list_thumbprints(scope: str) -> list[str]:
    store = _open_root_store(scope, read_only=True)
    thumbprints: set[str] = set()
    context: Any = None
    try:
        while True:
            context = _cert_enum_certificates(store, context)
            if not context:
                break
            thumbprints.add(hashlib.sha1(_certificate_der(context)).hexdigest().upper())
    finally:
        if context:
            _cert_free_context(context)
        _close_root_store(store)
    return sorted(thumbprints)


def _add_current_user_root(certificate_path: Path) -> None:
    certificate_der = certificate_path.read_bytes()
    encoded_certificate = (wintypes.BYTE * len(certificate_der)).from_buffer_copy(certificate_der)
    store = _open_root_store("current-user", read_only=False)
    try:
        _stage("add_current_user_root_start")
        if not _cert_add_encoded_certificate(
            store,
            _CERT_ENCODING,
            encoded_certificate,
            len(certificate_der),
            _CERT_STORE_ADD_NEW,
            None,
        ):
            _raise_wincrypt_error("add_current_user_root")
        _stage("add_current_user_root_done")
    finally:
        _stage("close_current_user_root_start")
        _close_root_store(store)
        _stage("close_current_user_root_done")


def _remove_current_user_root(thumbprint: str) -> bool:
    store = _open_root_store("current-user", read_only=False)
    context: Any = None
    try:
        while True:
            context = _cert_enum_certificates(store, context)
            if not context:
                return False
            found_thumbprint = hashlib.sha1(_certificate_der(context)).hexdigest().upper()
            if found_thumbprint == thumbprint:
                removed = _cert_delete_certificate(context)
                context = None
                if not removed:
                    _raise_wincrypt_error("remove_current_user_root")
                return True
    finally:
        if context:
            _cert_free_context(context)
        _close_root_store(store)


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    list_parser = commands.add_parser("list")
    list_parser.add_argument("--scope", choices=("current-user", "local-machine"), required=True)

    add_parser = commands.add_parser("add-current-user")
    add_parser.add_argument("certificate", type=Path)

    remove_parser = commands.add_parser("remove-current-user")
    remove_parser.add_argument("thumbprint")

    arguments = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Windows CryptoAPI is available only on Windows")

    try:
        if arguments.command == "list":
            print(json.dumps(_list_thumbprints(arguments.scope)))
        elif arguments.command == "add-current-user":
            _add_current_user_root(arguments.certificate)
            print("added")
        else:
            if not re.fullmatch(r"[A-Fa-f0-9]{40}", arguments.thumbprint):
                parser.error("thumbprint must be a SHA-1 certificate thumbprint")
            removed = _remove_current_user_root(arguments.thumbprint.upper())
            print("removed" if removed else "absent")
    except _WinCryptError as error:
        print(f"WINCRYPTO_FAILURE action={error.action} error_code=0x{error.error_code:08X}")
        return 1
    except OSError:
        print("WINDOWS_ROOT_STORE_FAILURE action=read_certificate_file")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
