from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def challenge_message(challenge_id: UUID, nonce: str) -> bytes:
    return (
        b"threads-platform-worker-auth-v1\n"
        + str(challenge_id).encode("ascii")
        + b"\n"
        + nonce.encode("ascii")
    )


def verify_worker_signature(public_key: bytes, message: bytes, signature: bytes) -> bool:
    if len(public_key) != 32 or len(signature) != 64:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
    except InvalidSignature, ValueError:
        return False
    return True
