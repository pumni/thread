from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

import threads_platform.standalone.nurture as nurture_module
from threads_platform.standalone.nurture import (
    NurturePresetError,
    NurturePresetV1,
    get_nurture_preset,
    validate_nurture_preset_v1,
)


def _document() -> dict[str, Any]:
    return {
        "version": 1,
        "id": "recruitment",
        "keyword_queries": ["job search"],
        "tag_queries": [],
        "include_terms": ["career"],
        "exclude_terms": [],
        "watched_public_usernames": [],
        "per_source_page_limit": 25,
        "max_total_discovery_candidates": 50,
        "max_selected_candidates": 10,
        "max_browser_enrichments": 5,
        "seen_cooldown_seconds": 86_400,
        "max_replies_per_explicit_run": 1,
        "max_own_post_mutations_per_explicit_run": 1,
        "engagement_enabled": True,
        "owner_public_username": None,
        "max_owned_threads_inspected_per_explicit_run": 0,
        "own_content_cooldown_seconds": 86_400,
        "own_content_due_interval_seconds": 86_400,
    }


def _assert_invalid(document: object) -> None:
    with pytest.raises(NurturePresetError, match="^INVALID_NURTURE_PRESET$"):
        validate_nurture_preset_v1(document)


def test_exact_v1_document_is_validated_to_an_immutable_model() -> None:
    preset = validate_nurture_preset_v1(_document())

    assert isinstance(preset, NurturePresetV1)
    assert preset.version == 1
    assert preset.id == "recruitment"
    assert preset.keyword_queries == ("job search",)
    assert preset.tag_queries == ()
    assert preset.include_terms == ("career",)


@pytest.mark.parametrize("field", ["version", "id", "keyword_queries"])
def test_missing_fields_are_rejected(field: str) -> None:
    document = _document()
    del document[field]
    _assert_invalid(document)


def test_unknown_fields_are_rejected() -> None:
    document = _document()
    document["ranking_expression"] = "career -> 100"
    _assert_invalid(document)


@pytest.mark.parametrize("version", [True, 1.0, 0, 2, "1"])
def test_invalid_or_bool_version_is_rejected(version: object) -> None:
    document = _document()
    document["version"] = version
    _assert_invalid(document)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (field, value)
        for field in (
            "per_source_page_limit",
            "max_total_discovery_candidates",
            "max_selected_candidates",
            "max_browser_enrichments",
            "seen_cooldown_seconds",
            "max_replies_per_explicit_run",
            "max_own_post_mutations_per_explicit_run",
            "max_owned_threads_inspected_per_explicit_run",
            "own_content_cooldown_seconds",
            "own_content_due_interval_seconds",
        )
        for value in (True, 1.0)
    ],
)
def test_bool_and_float_are_rejected_for_every_numeric_policy_field(
    field: str, value: object
) -> None:
    document = _document()
    document[field] = value
    _assert_invalid(document)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("per_source_page_limit", 0),
        ("per_source_page_limit", 51),
        ("max_total_discovery_candidates", 0),
        ("max_total_discovery_candidates", 101),
        ("max_selected_candidates", 0),
        ("max_selected_candidates", 11),
        ("max_browser_enrichments", -1),
        ("max_browser_enrichments", 6),
        ("seen_cooldown_seconds", -1),
        ("seen_cooldown_seconds", 31_536_001),
        ("max_replies_per_explicit_run", -1),
        ("max_replies_per_explicit_run", 4),
        ("max_own_post_mutations_per_explicit_run", -1),
        ("max_own_post_mutations_per_explicit_run", 4),
        ("max_owned_threads_inspected_per_explicit_run", -1),
        ("max_owned_threads_inspected_per_explicit_run", 11),
        ("own_content_cooldown_seconds", 31_536_001),
        ("own_content_due_interval_seconds", 0),
        ("own_content_due_interval_seconds", 31_536_001),
    ],
)
def test_numeric_policy_bounds_are_enforced(field: str, value: int) -> None:
    document = _document()
    document[field] = value
    _assert_invalid(document)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "Bad_slug"),
        ("id", "a" * 49),
        ("keyword_queries", []),
        ("keyword_queries", ["query"] * 17),
        ("keyword_queries", ["q" * 121]),
        ("keyword_queries", ["query\nsecret"]),
        ("tag_queries", ["tag"] * 17),
        ("include_terms", ["term"] * 33),
        ("include_terms", ["t" * 81]),
        ("exclude_terms", ["term"] * 33),
        ("watched_public_usernames", [f"user{index}" for index in range(17)]),
        ("watched_public_usernames", ["https://threads.example/user"]),
        ("watched_public_usernames", ["x" * 31]),
        ("owner_public_username", "bad/name"),
        ("tag_queries", None),
    ],
)
def test_collection_and_string_bounds_are_enforced(field: str, value: object) -> None:
    document = _document()
    document[field] = value
    _assert_invalid(document)


def test_total_query_bound_and_dependent_policy_bounds_are_enforced() -> None:
    document = _document()
    document["keyword_queries"] = ["keyword"] * 9
    document["tag_queries"] = ["tag"] * 8
    _assert_invalid(document)

    document = _document()
    document["max_total_discovery_candidates"] = 5
    document["max_selected_candidates"] = 6
    _assert_invalid(document)

    document = _document()
    document["max_selected_candidates"] = 3
    document["max_browser_enrichments"] = 4
    _assert_invalid(document)

    document = _document()
    document["max_owned_threads_inspected_per_explicit_run"] = 1
    _assert_invalid(document)


def test_non_boolean_engagement_flag_is_rejected() -> None:
    document = _document()
    document["engagement_enabled"] = 1
    _assert_invalid(document)


def test_builtin_recruitment_uses_public_validator_and_returns_fresh_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_validator = nurture_module.validate_nurture_preset_v1
    validated: list[object] = []

    def record_validation(value: object) -> NurturePresetV1:
        validated.append(value)
        return original_validator(value)

    monkeypatch.setattr(nurture_module, "validate_nurture_preset_v1", record_validation)
    preset = get_nurture_preset("recruitment")
    another = get_nurture_preset("recruitment")

    assert len(validated) == 2
    assert preset.id == "recruitment"
    assert preset is not another
    assert len(preset.keyword_queries) + len(preset.tag_queries) <= 16


@pytest.mark.parametrize("preset_id", ["unknown", "RECRUITMENT", True, None])
def test_unknown_preset_id_fails_with_bounded_error(preset_id: object) -> None:
    with pytest.raises(NurturePresetError, match="^PRESET_NOT_FOUND$"):
        get_nurture_preset(preset_id)


def test_preset_repr_and_validation_errors_do_not_echo_query_data() -> None:
    document = _document()
    document["keyword_queries"] = ["private-query-sentinel"]
    preset = validate_nurture_preset_v1(document)
    assert "private-query-sentinel" not in repr(preset)

    invalid_document = deepcopy(document)
    invalid_document["unexpected"] = "private-query-sentinel"
    with pytest.raises(NurturePresetError) as error:
        validate_nurture_preset_v1(invalid_document)
    assert "private-query-sentinel" not in str(error.value)
