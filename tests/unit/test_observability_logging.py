import json

import pytest
import structlog
from pydantic import SecretBytes, SecretStr

from threads_platform.observability.logging import REDACTED, sanitize_log_event

_SENTINEL = "SYNTHETIC_OBSERVABILITY_SECRET_SENTINEL"


@pytest.mark.parametrize(
    "key",
    [
        "authorization",
        "Proxy_Authorization",
        "access_token",
        "refresh_token",
        "session_token",
        "enrollment_code",
        "app_secret",
        "client_secret",
        "secret",
        "password",
        "private_key",
        "signature",
        "cookie",
        "cookies",
        "credential_ref",
        "proxy_credential_ref",
    ],
)
def test_sensitive_keys_are_redacted_case_insensitively(key: str) -> None:
    source = {"nested": [{key: _SENTINEL}]}

    sanitized = sanitize_log_event(None, "info", source)

    assert sanitized["nested"][0][key] == REDACTED
    assert _SENTINEL not in json.dumps(sanitized)


@pytest.mark.parametrize(
    ("value", "visible_sentinel"),
    [
        (f"authorization failed for Bearer {_SENTINEL}", _SENTINEL),
        (f"remote token TH|{_SENTINEL}", _SENTINEL),
        (
            f"-----BEGIN PRIVATE KEY-----\n{_SENTINEL}\n-----END PRIVATE KEY-----",
            _SENTINEL,
        ),
        (f"invalid reference env://THREADS_PLATFORM_THREADS_TOKEN_{_SENTINEL}", _SENTINEL),
    ],
)
def test_sensitive_string_patterns_are_redacted(value: str, visible_sentinel: str) -> None:
    sanitized = sanitize_log_event(None, "info", {"message": value})

    assert visible_sentinel not in sanitized["message"]
    assert REDACTED in sanitized["message"]


def test_secretstr_and_private_bytes_are_redacted_recursively() -> None:
    source = {
        "data": [SecretStr(_SENTINEL), (SecretBytes(_SENTINEL.encode()), b"private bytes")],
    }

    sanitized = sanitize_log_event(None, "info", source)
    rendered = json.dumps(sanitized)

    assert sanitized["data"][0] == REDACTED
    assert sanitized["data"][1] == (REDACTED, REDACTED)
    assert _SENTINEL not in rendered
    assert "private bytes" not in rendered


def test_sanitizer_does_not_mutate_the_original_event_mapping() -> None:
    source = {
        "authorization": _SENTINEL,
        "nested": [{"access_token": _SENTINEL}, ("Bearer " + _SENTINEL,)],
        "command_id": "cmd-correlation-safe",
    }
    before = {
        "authorization": _SENTINEL,
        "nested": [{"access_token": _SENTINEL}, ("Bearer " + _SENTINEL,)],
        "command_id": "cmd-correlation-safe",
    }

    sanitized = sanitize_log_event(None, "info", source)

    assert source == before
    assert sanitized is not source
    assert sanitized["nested"] is not source["nested"]
    assert sanitized["command_id"] == "cmd-correlation-safe"


def test_formatted_exception_is_sanitized_before_json_rendering() -> None:
    bearer_sentinel = f"Bearer {_SENTINEL}"
    credential_ref_sentinel = "env://THREADS_PLATFORM_THREADS_TOKEN_SYNTHETIC_V1"
    try:
        raise RuntimeError(f"{bearer_sentinel} {credential_ref_sentinel}")
    except RuntimeError as error:
        event = {
            "event": "operation_failed",
            "exc_info": (type(error), error, error.__traceback__),
        }

    formatted = structlog.processors.format_exc_info(None, "error", event)
    sanitized = sanitize_log_event(None, "error", formatted)
    rendered_value = structlog.processors.JSONRenderer()(None, "error", sanitized)
    rendered = rendered_value.decode() if isinstance(rendered_value, bytes) else rendered_value

    assert "RuntimeError" in rendered
    assert "Traceback" in rendered
    assert _SENTINEL not in rendered
    assert credential_ref_sentinel not in rendered
    assert bearer_sentinel not in rendered
    assert REDACTED in rendered
