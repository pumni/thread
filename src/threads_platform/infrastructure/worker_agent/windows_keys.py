import ctypes
import os
from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from threads_platform.infrastructure.worker_agent.identity import create_file_if_absent
from threads_platform.infrastructure.worker_agent.local_state import LocalDataRoot
from threads_platform.workers.key_store import WorkerDeviceIdentity, WorkerKeyStore

KEY_FILE_MAGIC = b"TPW-DPAPI-ED25519-1\0"
KEY_CONTEXT = b"threads-platform-worker-key-v1\0"


class WorkerKeyStoreError(ValueError):
    pass


class WorkerDataProtector(Protocol):
    def protect(self, plaintext: bytes) -> bytes: ...

    def unprotect(self, ciphertext: bytes) -> bytes: ...


class WindowsDPAPIDataProtector:
    def __init__(self, worker_id: UUID) -> None:
        if os.name != "nt":
            raise WorkerKeyStoreError("Windows DPAPI is available only on Windows")
        self._entropy = KEY_CONTEXT + worker_id.bytes

    def protect(self, plaintext: bytes) -> bytes:
        return self._transform(plaintext, protect=True)

    def unprotect(self, ciphertext: bytes) -> bytes:
        return self._transform(ciphertext, protect=False)

    def _transform(self, payload: bytes, *, protect: bool) -> bytes:
        if not payload:
            raise WorkerKeyStoreError("worker private identity payload is empty")
        load_library = getattr(ctypes, "WinDLL", None)
        if load_library is None:
            raise WorkerKeyStoreError("Windows DPAPI is unavailable")
        crypt32 = load_library("crypt32", use_last_error=True)
        kernel32 = load_library("kernel32", use_last_error=True)
        crypt_function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
        crypt_function.restype = ctypes.c_int
        if protect:
            crypt_function.argtypes = [
                ctypes.POINTER(_DataBlob),
                ctypes.c_wchar_p,
                ctypes.POINTER(_DataBlob),
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.POINTER(_DataBlob),
            ]
        else:
            crypt_function.argtypes = [
                ctypes.POINTER(_DataBlob),
                ctypes.c_void_p,
                ctypes.POINTER(_DataBlob),
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.POINTER(_DataBlob),
            ]
        local_free = kernel32.LocalFree
        local_free.argtypes = [ctypes.c_void_p]
        local_free.restype = ctypes.c_void_p

        input_blob, input_buffer = _data_blob(payload)
        entropy_blob, entropy_buffer = _data_blob(self._entropy)
        output_blob = _DataBlob()
        try:
            if protect:
                succeeded = crypt_function(
                    ctypes.byref(input_blob),
                    None,
                    ctypes.byref(entropy_blob),
                    None,
                    None,
                    0x1,
                    ctypes.byref(output_blob),
                )
            else:
                succeeded = crypt_function(
                    ctypes.byref(input_blob),
                    None,
                    ctypes.byref(entropy_blob),
                    None,
                    None,
                    0x1,
                    ctypes.byref(output_blob),
                )
            if not succeeded:
                code = ctypes.get_last_error()
                raise WorkerKeyStoreError(f"Windows DPAPI operation failed ({code})")
            return ctypes.string_at(output_blob.pb_data, output_blob.length)
        finally:
            if output_blob.pb_data:
                local_free(ctypes.cast(output_blob.pb_data, ctypes.c_void_p))
            # Keep buffers alive through the native call.
            _ = input_buffer, entropy_buffer


class DPAPIWorkerKeyStore(WorkerKeyStore):
    def __init__(
        self,
        data_root: LocalDataRoot,
        *,
        protector_factory: Callable[[UUID], WorkerDataProtector] = WindowsDPAPIDataProtector,
    ) -> None:
        self._data_root = data_root
        self._protector_factory = protector_factory

    def load_or_create(self, worker_id: UUID) -> WorkerDeviceIdentity:
        path = self._data_root.child("worker", f"{worker_id}.device-key.dpapi")
        protector = self._protector_factory(worker_id)
        try:
            protected = path.read_bytes()
        except FileNotFoundError:
            private_key = Ed25519PrivateKey.generate()
            serialized = private_key.private_bytes(
                encoding=serialization.Encoding.DER,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
            encrypted = KEY_FILE_MAGIC + protector.protect(serialized)
            try:
                create_file_if_absent(path, encrypted)
            except FileExistsError:
                pass
            else:
                return WorkerDeviceIdentity(private_key)
            try:
                protected = path.read_bytes()
            except OSError as error:
                raise WorkerKeyStoreError(
                    "persisted worker private identity is unreadable"
                ) from error
        except OSError as error:
            raise WorkerKeyStoreError("persisted worker private identity is unreadable") from error

        if not protected.startswith(KEY_FILE_MAGIC):
            raise WorkerKeyStoreError("persisted worker private identity is corrupt")
        try:
            private_bytes = protector.unprotect(protected[len(KEY_FILE_MAGIC) :])
            key = serialization.load_der_private_key(private_bytes, password=None)
        except Exception as error:
            raise WorkerKeyStoreError("persisted worker private identity is corrupt") from error
        if not isinstance(key, Ed25519PrivateKey):
            raise WorkerKeyStoreError("persisted worker identity has an unsupported key type")
        return WorkerDeviceIdentity(key)


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_ulong),
        ("pb_data", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _data_blob(payload: bytes) -> tuple[_DataBlob, object]:
    buffer = ctypes.create_string_buffer(payload)
    blob = _DataBlob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    return blob, buffer
