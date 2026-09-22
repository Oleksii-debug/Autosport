from __future__ import annotations

from copy import deepcopy

import pytest

from autosport.nvda_human_acceptance import (
    HUMAN_NVDA_ORIGIN,
    REQUIRED_JOURNEY_IDS,
    STATUS_STRUCTURALLY_COMPLETE,
    NvdaHumanAcceptanceError,
    NvdaHumanAcceptanceStructuralResult,
    build_manual_nvda_transcript_template,
    validate_human_nvda_acceptance_transcript,
)

ARTIFACT_SHA = "a" * 64
SOURCE_SHA = "b" * 40


def _valid_transcript() -> dict[str, object]:
    return {
        "schema_version": 1,
        "artifact_sha256": ARTIFACT_SHA,
        "source_sha": SOURCE_SHA,
        "windows_version": "Windows 11 24H2 build 26100",
        "nvda_version": "NVDA 2026.2",
        "keyboard_only": True,
        "mouse_used": False,
        "evidence_origin": HUMAN_NVDA_ORIGIN,
        "human_tester_attestation": (
            "I performed this session manually with NVDA and keyboard only."
        ),
        "journeys": [
            {
                "journey_id": journey_id,
                "human_passed": True,
                "steps": [
                    {
                        "keyboard_input": "Tab, Enter",
                        "expected_accessible_result": f"Accessible result for {journey_id}",
                        "actual_nvda_speech": f"NVDA speech observed for {journey_id}",
                        "focus_before": "Main window",
                        "focus_after": "Expected control",
                        "human_passed": True,
                    }
                ],
            }
            for journey_id in REQUIRED_JOURNEY_IDS
        ],
    }


def _validate(transcript: object) -> NvdaHumanAcceptanceStructuralResult:
    return validate_human_nvda_acceptance_transcript(
        transcript,
        expected_artifact_sha256=ARTIFACT_SHA,
        expected_source_sha=SOURCE_SHA,
    )


def test_valid_transcript_is_structural_only_and_never_promotes_truth() -> None:
    result = _validate(_valid_transcript())

    assert result.status == STATUS_STRUCTURALLY_COMPLETE
    assert result.human_tested is False
    assert result.nvda_verified is False
    assert result.manual_truth_promotion_required is True
    assert result.real_money_execution is False
    assert result.whole_product_complete is False


def test_generated_template_is_intentionally_invalid() -> None:
    template = build_manual_nvda_transcript_template(
        artifact_sha256=ARTIFACT_SHA,
        source_sha=SOURCE_SHA,
    )

    with pytest.raises(NvdaHumanAcceptanceError):
        _validate(template)


@pytest.mark.parametrize(
    "field, replacement, expected",
    [
        ("artifact_sha256", "c" * 64, ARTIFACT_SHA),
        ("source_sha", "c" * 40, SOURCE_SHA),
    ],
)
def test_exact_artifact_and_source_identity_are_required(
    field: str, replacement: str, expected: str
) -> None:
    transcript = _valid_transcript()
    transcript[field] = replacement

    with pytest.raises(NvdaHumanAcceptanceError):
        _validate(transcript)
    assert expected != transcript[field]


@pytest.mark.parametrize("value", ["A" * 64, "a" * 63, "g" * 64, 1, True])
def test_malformed_sha_fails_closed(value: object) -> None:
    transcript = _valid_transcript()
    transcript["artifact_sha256"] = value

    with pytest.raises(NvdaHumanAcceptanceError):
        _validate(transcript)


@pytest.mark.parametrize("value", ["B" * 40, "b" * 39, "g" * 40, 1, True])
def test_malformed_source_git_sha_fails_closed(value: object) -> None:
    transcript = _valid_transcript()
    transcript["source_sha"] = value

    with pytest.raises(NvdaHumanAcceptanceError):
        _validate(transcript)


@pytest.mark.parametrize("origin", ["AUTOMATED_UIA", "SYNTHETIC_SPEECH", "TEMPLATE_ONLY", "CI"])
def test_nonhuman_evidence_origin_cannot_pass(origin: str) -> None:
    transcript = _valid_transcript()
    transcript["evidence_origin"] = origin

    with pytest.raises(NvdaHumanAcceptanceError):
        _validate(transcript)


def test_keyboard_only_must_be_exact_true_bool() -> None:
    for bad in (False, 1, "true"):
        transcript = _valid_transcript()
        transcript["keyboard_only"] = bad
        with pytest.raises(NvdaHumanAcceptanceError):
            _validate(transcript)


def test_mouse_used_must_be_exact_false_bool() -> None:
    for bad in (True, 0, "false"):
        transcript = _valid_transcript()
        transcript["mouse_used"] = bad
        with pytest.raises(NvdaHumanAcceptanceError):
            _validate(transcript)


def test_injected_truth_field_is_rejected() -> None:
    transcript = _valid_transcript()
    transcript["nvda_verified"] = True

    with pytest.raises(NvdaHumanAcceptanceError):
        _validate(transcript)


def test_missing_journey_is_rejected() -> None:
    transcript = _valid_transcript()
    journeys = transcript["journeys"]
    assert isinstance(journeys, list)
    journeys.pop()

    with pytest.raises(NvdaHumanAcceptanceError):
        _validate(transcript)


def test_wrong_journey_order_is_rejected() -> None:
    transcript = _valid_transcript()
    journeys = transcript["journeys"]
    assert isinstance(journeys, list)
    journeys[0], journeys[1] = journeys[1], journeys[0]

    with pytest.raises(NvdaHumanAcceptanceError):
        _validate(transcript)


def test_journey_failure_is_rejected() -> None:
    transcript = _valid_transcript()
    journeys = transcript["journeys"]
    assert isinstance(journeys, list)
    journeys[0]["human_passed"] = False

    with pytest.raises(NvdaHumanAcceptanceError):
        _validate(transcript)


def test_step_failure_is_rejected() -> None:
    transcript = _valid_transcript()
    journeys = transcript["journeys"]
    assert isinstance(journeys, list)
    journeys[0]["steps"][0]["human_passed"] = False

    with pytest.raises(NvdaHumanAcceptanceError):
        _validate(transcript)


@pytest.mark.parametrize(
    "field",
    [
        "keyboard_input",
        "expected_accessible_result",
        "actual_nvda_speech",
        "focus_before",
        "focus_after",
    ],
)
def test_every_step_requires_observed_nonempty_evidence(field: str) -> None:
    transcript = _valid_transcript()
    journeys = transcript["journeys"]
    assert isinstance(journeys, list)
    journeys[0]["steps"][0][field] = ""

    with pytest.raises(NvdaHumanAcceptanceError):
        _validate(transcript)


def test_unknown_step_field_is_rejected() -> None:
    transcript = _valid_transcript()
    journeys = transcript["journeys"]
    assert isinstance(journeys, list)
    journeys[0]["steps"][0]["screenshot_passed"] = True

    with pytest.raises(NvdaHumanAcceptanceError):
        _validate(transcript)


def test_unknown_journey_field_is_rejected() -> None:
    transcript = _valid_transcript()
    journeys = transcript["journeys"]
    assert isinstance(journeys, list)
    journeys[0]["automated_passed"] = True

    with pytest.raises(NvdaHumanAcceptanceError):
        _validate(transcript)


def test_schema_version_rejects_bool_and_other_versions() -> None:
    for bad in (True, 0, 2, "1"):
        transcript = _valid_transcript()
        transcript["schema_version"] = bad
        with pytest.raises(NvdaHumanAcceptanceError):
            _validate(transcript)


def test_human_attestation_must_be_nonempty_trimmed_text() -> None:
    for bad in ("", "  ", " padded ", 1):
        transcript = _valid_transcript()
        transcript["human_tester_attestation"] = bad
        with pytest.raises(NvdaHumanAcceptanceError):
            _validate(transcript)


def test_transcript_digest_is_deterministic_for_exact_evidence() -> None:
    first = _valid_transcript()
    second = deepcopy(first)

    assert _validate(first).transcript_sha256 == _validate(second).transcript_sha256


def test_transcript_digest_changes_when_observed_speech_changes() -> None:
    first = _valid_transcript()
    second = deepcopy(first)
    second["journeys"][0]["steps"][0]["actual_nvda_speech"] = "Different human-observed speech"

    assert _validate(first).transcript_sha256 != _validate(second).transcript_sha256


def test_structural_result_truth_fields_are_not_constructor_arguments() -> None:
    with pytest.raises(TypeError):
        NvdaHumanAcceptanceStructuralResult(
            transcript_sha256="c" * 64,
            human_tested=True,  # type: ignore[call-arg]
            nvda_verified=True,  # type: ignore[call-arg]
        )


def test_structural_result_rejects_noncanonical_digest() -> None:
    with pytest.raises(NvdaHumanAcceptanceError):
        NvdaHumanAcceptanceStructuralResult(transcript_sha256="C" * 64)


def test_structural_result_cannot_be_minted_by_direct_construction() -> None:
    with pytest.raises(NvdaHumanAcceptanceError):
        NvdaHumanAcceptanceStructuralResult(transcript_sha256="c" * 64)


def test_structural_result_cannot_be_subclassed_to_bypass_issuer() -> None:
    with pytest.raises(TypeError, match="may not be subclassed"):
        class ForgedStructuralResult(NvdaHumanAcceptanceStructuralResult):
            pass


def test_custom_container_types_do_not_cross_canonical_boundary() -> None:
    transcript = _valid_transcript()
    with pytest.raises(NvdaHumanAcceptanceError):
        _validate(tuple(transcript.items()))
