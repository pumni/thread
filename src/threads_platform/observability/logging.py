import logging
import re
from collections.abc import Mapping
from typing import Any, cast

import structlog
from pydantic import SecretBytes, SecretStr
from structlog.typing import EventDict

REDACTED = "[REDACTED]"

_SENSITIVE_KEY_PARTS = (
    "authorization",
    "token",
    "enrollmentcode",
    "secret",
    "password",
    "privatekey",
    "signature",
    "cookie",
    "credentialref",
)
_SENSITIVE_KEY_SEPARATORS = re.compile(r"[^a-z0-9]+")
_SENSITIVE_STRING_PATTERNS = (
    re.compile(r"\benv://THREADS_PLATFORM_THREADS_TOKEN_[A-Z0-9_]+\b", re.IGNORECASE),
    re.compile(r"\bBearer\s+[^\s,;\"'<>]+", re.IGNORECASE),
    re.compile(r"\bTH\|[^\s,;\"'<>]+", re.IGNORECASE),
    re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
        re.IGNORECASE,
    ),
)


def sanitize_log_event(
    _: Any,
    __: str,
    event_dict: EventDict,
) -> EventDict:
    """Copy and recursively redact a structlog event before it is rendered."""
    return cast(EventDict, _sanitize_mapping(cast(Mapping[object, Any], event_dict)))


def _sanitize_mapping(value: Mapping[object, Any]) -> dict[object, Any]:
    sanitized: dict[object, Any] = {}
    for key, item in value.items():
        sanitized[key] = REDACTED if _is_sensitive_key(key) else _sanitize_value(item)
    return sanitized


def _is_sensitive_key(key: object) -> bool:
    normalized = _SENSITIVE_KEY_SEPARATORS.sub("", str(key).casefold())
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, (SecretStr, SecretBytes, bytes, bytearray, memoryview)):
        return REDACTED
    if isinstance(value, BaseException):
        return f"{type(value).__name__}: {REDACTED}"
    if isinstance(value, Mapping):
        return _sanitize_mapping(cast(Mapping[object, Any], value))
    if isinstance(value, list):
        return [_sanitize_value(item) for item in cast(list[Any], value)]
    if isinstance(value, tuple):
        return tuple(_sanitize_value(item) for item in cast(tuple[Any, ...], value))
    if isinstance(value, str):
        for pattern in _SENSITIVE_STRING_PATTERNS:
            value = pattern.sub(REDACTED, value)
        return value
    return value


def configure_logging(log_level: str) -> None:
    logging.basicConfig(level=log_level, format="%(message)s", force=True)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            sanitize_log_event,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
