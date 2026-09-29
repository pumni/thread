import json
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any

import pytest

from threads_platform.tools.threads_live_evidence import (
    PacketClassification,
    PacketRejectedError,
    ThreadsLiveEvidencePacket,
    fingerprint_from_stdin,
    fingerprint_opaque_value,
    main,
    validate_packet_json,
)

_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "examples"
    / "threads-live-evidence-v1.template.json"
)


def _template_packet() -> dict[str, Any]:
    return json.loads(_TEMPLATE_PATH.read_text(encoding="utf-8"))


def _rejection(packet: dict[str, Any]) -> str:
    with pytest.raises(PacketRejectedError) as exc_info:
        validate_packet_json(json.dumps(packet))
    return repr(exc_info.value.issues)


def test_valid_template_is_accepted_as_template_only() -> None:
    packet = validate_packet_json(_TEMPLATE_PATH.read_text(encoding="utf-8"))

    assert isinstance(packet, ThreadsLiveEvidencePacket)
    assert packet.packet_classification is PacketClassification.TEMPLATE_ONLY_NOT_LIVE_EVIDENCE
    assert all(case.result.value == "NOT_AVAILABLE" for case in packet.cases)
    assert not packet.critical_path_readiness.token_provider_ready_evidence
    assert not packet.critical_path_readiness.polling_cursor_ready_evidence
    assert not packet.critical_path_readiness.full_tp002_ready


def test_partial_live_packet_records_observations_without_claiming_readiness() -> None:
    document = _template_packet()
    document["schema_version"] = "threads-live-evidence-v1"
    document["packet_classification"] = "SCRUBBED_LIVE_EVIDENCE"
    document["observed_at_utc"] = "2026-09-29T05:00:00Z"
    document["operator_alias"] = "OPERATOR1"
    document["reviewer_alias"] = "REVIEWER1"
    document["environment_classification"] = "META_DEVELOPMENT_APP_DEDICATED_TEST_ACCOUNT"
    document["official_source_references_reviewed"] = [
        "https://www.postman.com/meta/threads/request/34203612-b3b2c12a-7ce6-4d86-a3c6-6d31e3b66ea1"
    ]
    for case in document["cases"]:
        case["evidence_class"] = "LIVE_SCRUBBED"
    document["cases"][0]["http_status"] = 401
    document["cases"][0]["result"] = "FAIL"
    document["cases"][0]["scrubbed_error_observation"] = {
        "error_category": "AUTHENTICATION",
        "message_present": True,
    }

    packet = validate_packet_json(json.dumps(document))

    assert packet.packet_classification is PacketClassification.SCRUBBED_LIVE_EVIDENCE
    assert not packet.critical_path_readiness.token_provider_ready_evidence
    assert not packet.critical_path_readiness.polling_cursor_ready_evidence


def test_live_classification_without_any_observation_is_rejected() -> None:
    document = _template_packet()
    document["packet_classification"] = "SCRUBBED_LIVE_EVIDENCE"
    document["observed_at_utc"] = "2026-09-29T05:00:00Z"
    document["operator_alias"] = "OPERATOR1"
    document["reviewer_alias"] = "REVIEWER1"
    document["environment_classification"] = "META_DEVELOPMENT_APP_DEDICATED_TEST_ACCOUNT"
    document["official_source_references_reviewed"] = [
        "https://developers.facebook.com/docs/threads"
    ]
    for case in document["cases"]:
        case["evidence_class"] = "LIVE_SCRUBBED"

    assert "at least one scrubbed observation" in _rejection(document)


@pytest.mark.parametrize(
    "field_name",
    ("access_token", "app_secret", "client_secret", "authorization_code"),
)
def test_credential_field_names_are_rejected_recursively(field_name: str) -> None:
    packet = _template_packet()
    packet["unexpected_nested"] = {"items": [{field_name: "SYNTHETIC_SENTINEL_VALUE"}]}

    diagnostic = _rejection(packet)

    assert "credential-bearing field name" in diagnostic
    assert "SYNTHETIC_SENTINEL_VALUE" not in diagnostic


@pytest.mark.parametrize("header_name", ("Authorization", "aUtHoRiZaTiOn", "Proxy-Authorization"))
def test_authorization_headers_are_rejected_case_insensitively(header_name: str) -> None:
    packet = _template_packet()
    packet["cases"][0]["unexpected"] = {header_name: "SYNTHETIC_SENTINEL_HEADER"}

    diagnostic = _rejection(packet)

    assert "authorization header name is prohibited" in diagnostic
    assert "SYNTHETIC_SENTINEL_HEADER" not in diagnostic


def test_bearer_value_in_notes_is_rejected_without_echo() -> None:
    packet = _template_packet()
    packet["cases"][0]["notes"] = "Bearer SYNTHETIC_SENTINEL_BEARER"

    diagnostic = _rejection(packet)

    assert "bearer credential pattern" in diagnostic
    assert "SYNTHETIC_SENTINEL_BEARER" not in diagnostic


def test_threads_token_prefix_is_rejected() -> None:
    packet = _template_packet()
    packet["cases"][0]["notes"] = "TH|SYNTHETIC_SENTINEL_TOKEN"

    diagnostic = _rejection(packet)

    assert "Threads token-like prefix" in diagnostic
    assert "SYNTHETIC_SENTINEL_TOKEN" not in diagnostic


def test_secret_bearing_url_is_rejected() -> None:
    packet = _template_packet()
    packet["official_source_references_reviewed"] = [
        "https://www.postman.com/meta/threads?client_secret=SYNTHETIC_SENTINEL_QUERY"
    ]

    diagnostic = _rejection(packet)

    assert "secret-bearing source URL query" in diagnostic
    assert "SYNTHETIC_SENTINEL_QUERY" not in diagnostic


def test_oauth_code_exchange_url_is_rejected_without_echo() -> None:
    packet = _template_packet()
    packet["cases"][0]["notes"] = "/oauth/access_token?code=SYNTHETIC_SENTINEL_AUTHORIZATION_CODE"

    diagnostic = _rejection(packet)

    assert "OAuth exchange URL with secret query" in diagnostic
    assert "SYNTHETIC_SENTINEL_AUTHORIZATION_CODE" not in diagnostic


def test_raw_cursor_field_is_rejected() -> None:
    packet = _template_packet()
    packet["cases"][0]["unexpected"] = {"after": "SYNTHETIC_SENTINEL_CURSOR"}

    diagnostic = _rejection(packet)

    assert "raw cursor field" in diagnostic
    assert "SYNTHETIC_SENTINEL_CURSOR" not in diagnostic


def test_malformed_cursor_fingerprint_is_rejected() -> None:
    packet = _template_packet()
    packet["cases"][6]["cursor_page_observation"] = {
        "after_value_fingerprint": "sha256:SYNTHETIC_SENTINEL_BAD_FINGERPRINT"
    }

    diagnostic = _rejection(packet)

    assert "string_pattern_mismatch" in diagnostic
    assert "SYNTHETIC_SENTINEL_BAD_FINGERPRINT" not in diagnostic


def test_duplicate_case_id_is_rejected() -> None:
    packet = _template_packet()
    packet["cases"][1]["case_id"] = packet["cases"][0]["case_id"]

    assert "duplicate case ID" in _rejection(packet)


def test_non_utc_timestamp_is_rejected() -> None:
    packet = _template_packet()
    packet["observed_at_utc"] = "2026-09-29T12:00:00+07:00"

    assert "timestamp must be UTC" in _rejection(packet)


def test_unknown_field_is_rejected() -> None:
    packet = _template_packet()
    packet["unrecognized_review_field"] = "synthetic value"

    assert "extra_forbidden" in _rejection(packet)


def test_template_cannot_be_changed_into_completed_live_evidence() -> None:
    packet = _template_packet()
    packet["cases"][0]["result"] = "PASS"
    packet["cases"][0]["http_status"] = 200

    assert "template classification cannot contain live observations" in _rejection(packet)


@pytest.mark.parametrize(
    "url",
    (
        "http://www.postman.com/meta/threads",
        "".join(
            (
                "https://",
                "reviewer",
                ":",
                "SYNTHETIC_SENTINEL_PASSWORD",
                "@",
                "www.postman.com/meta/threads",
            )
        ),
    ),
)
def test_source_references_must_be_https_without_user_information(url: str) -> None:
    packet = _template_packet()
    packet["official_source_references_reviewed"] = [url]

    diagnostic = _rejection(packet)

    assert "safe HTTPS official-source URL" in diagnostic
    assert "SYNTHETIC_SENTINEL_PASSWORD" not in diagnostic


def test_fingerprint_stdin_helper_never_echoes_raw_value() -> None:
    raw_value = b"SYNTHETIC_SENTINEL_CURSOR_INPUT"
    stdout = StringIO()

    result = fingerprint_from_stdin(BytesIO(raw_value + b"\n"), stdout)

    assert result == 0
    assert stdout.getvalue() == fingerprint_opaque_value(raw_value) + "\n"
    assert "SYNTHETIC_SENTINEL_CURSOR_INPUT" not in stdout.getvalue()
    assert len(stdout.getvalue().strip()) == len("sha256:") + 64


def test_cli_does_not_echo_an_accidentally_passed_fingerprint_argument(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["fingerprint", "SYNTHETIC_SENTINEL_ARGUMENT"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "SYNTHETIC_SENTINEL_ARGUMENT" not in captured.err
    assert "standard input" in captured.err


def test_validator_diagnostic_does_not_reproduce_secret_value() -> None:
    packet = _template_packet()
    packet["cases"][0]["notes"] = "Bearer SYNTHETIC_SENTINEL_DIAGNOSTIC"

    diagnostic = _rejection(packet)

    assert "SYNTHETIC_SENTINEL_DIAGNOSTIC" not in diagnostic


def test_recursive_scan_rejects_secret_in_nested_object_and_list() -> None:
    packet = _template_packet()
    packet["unexpected_nested"] = {"children": [{"notes": "Bearer SYNTHETIC_SENTINEL_NESTED"}]}

    diagnostic = _rejection(packet)

    assert "bearer credential pattern" in diagnostic
    assert "SYNTHETIC_SENTINEL_NESTED" not in diagnostic


def test_recursive_scan_rejects_secret_pasted_into_notes() -> None:
    packet = _template_packet()
    packet["cases"][0]["notes"] = "Do not keep this: TH|SYNTHETIC_SENTINEL_NOTE"

    diagnostic = _rejection(packet)

    assert "Threads token-like prefix" in diagnostic
    assert "SYNTHETIC_SENTINEL_NOTE" not in diagnostic


def test_duplicate_json_object_key_is_rejected() -> None:
    document = _TEMPLATE_PATH.read_text(encoding="utf-8")
    key = '"schema_version": "threads-live-evidence-v1",'
    repeated_key = document.replace(key, key + " " + key, 1)

    with pytest.raises(PacketRejectedError, match="evidence packet rejected") as exc_info:
        validate_packet_json(repeated_key)

    assert "duplicate JSON object key" in repr(exc_info.value.issues)
