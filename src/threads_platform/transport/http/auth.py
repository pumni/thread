import secrets
from typing import Protocol

from pydantic import SecretStr


class CommandAuthenticator(Protocol):
    def is_authorized(self, authorization: str | None) -> bool: ...


class BearerTokenAuthenticator:
    def __init__(self, expected_token: SecretStr | None) -> None:
        self._expected_token = expected_token

    def is_authorized(self, authorization: str | None) -> bool:
        if self._expected_token is None or authorization is None:
            return False
        scheme, separator, supplied_token = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not supplied_token:
            return False
        return secrets.compare_digest(
            supplied_token,
            self._expected_token.get_secret_value(),
        )
