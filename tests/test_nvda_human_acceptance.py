from __future__ import annotations

from copy import copy, deepcopy
from dataclasses import replace
import hashlib
import json
import pickle

import pytest

import autosport.nvda_human_acceptance as nvda_module
from autosport.nvda_human_acceptance import (
    HUMAN_NVDA_ORIGIN,
    REQUIRED_JOURNEY_IDS,
    STATUS_STRUCTURALLY_COMPLETE,
    NvdaHumanAcceptanceError,
    NvdaHumanAcceptanceStructuralResult,
    build_manual_nvda_transcript_template,
    validate_human_nvda_acceptance_transcript,
    verify_human_nvda_acceptance_structural_result,
)

ARTIFACT_SHA = "a" * 64
SOURCE_SHA = "b" * 40


def _runtime_witness_sha(transcript: dict[str, object]) -> str:
    witness = transcript["webview2_runtime_witness"]
    assert isinstance(witness, dict)
    payload = (
        json.dumps(
            witness,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _valid_transcript() -> dict[str, object]:
    return {
        "schema_version": 2,
        "artifact_sha256": ARTIFACT_SHA,
        "source_sha": SOURCE_SHA,
        "windows_version": "Windows 11 24H2 build 26100",
        "nvda_version": "NVDA 2026.2",
        "webview2_runtime_witness": {
            "schema_version": 1,
            "renderer": "edgechromium",
            "browser_version_string": "154.0.2847.51",
            "observation_source": "native_core_webview2_environment",
            "real_money_execution": False,
            "human_tested": False,
            "nvda_verified": False,
            "whole_product_complete": False,
        },
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


def _validate(
    transcript: object,
    *,
    expected_runtime_witness_sha256: str | None = None,
) -> NvdaHumanAcceptanceStructuralResult:
    if isinstance(transcript, dict) and expected_runtime_witness_sha256 is None:
        expected_runtime_witness_sha256 = _runtime_witness_sha(transcript)
    if expected_runtime_witness_sha256 is None:
        expected_runtime_witness_sha256 = "d" * 64
    return validate_human_nvda_acceptance_transcript(
        transcript,
        expected_artifact_sha256=ARTIFACT_SHA,
        expected_source_sha=SOURCE_SHA,
        expected_webview2_runtime_witness_sha256=expected_runtime_witness_sha256,
    )


def _verify(
    result: object,
    *,
    artifact_sha256: str = ARTIFACT_SHA,
    source_sha: str = SOURCE_SHA,
    runtime_witness_sha256: str | None = None,
) -> NvdaHumanAcceptanceStructuralResult:
    if runtime_witness_sha256 is None:
        runtime_witness_sha256 = getattr(
            result,
            "webview2_runtime_witness_sha256",
            "d" * 64,
        )
    return verify_human_nvda_acceptance_structural_result(
        result,
        expected_artifact_sha256=artifact_sha256,
        expected_source_sha=source_sha,
        expected_webview2_runtime_witness_sha256=runtime_witness_sha256,
    )


def test_valid_transcript_is_structural_only_and_never_promotes_truth() -> None:
    result = _validate(_valid_transcript())

    assert result.status == STATUS_STRUCTURALLY_COMPLETE
    assert result.human_tested is False
    assert result.nvda_verified is False
    assert result.manual_truth_promotion_required is True
    assert result.real_money_execution is False
    assert result.whole_product_complete is False


def test_validator_issued_result_is_live_structural_authority() -> None:
    transcript = _valid_transcript()
    result = _validate(transcript)

    assert _verify(result) is result
    assert result.artifact_sha256 == ARTIFACT_SHA
    assert result.source_sha == SOURCE_SHA
    assert result.windows_version == transcript["windows_version"]
    assert result.nvda_version == transcript["nvda_version"]
    assert result.webview2_runtime_browser_version == "154.0.2847.51"
    assert len(result.webview2_runtime_witness_sha256) == 64
    assert result.evidence_origin == HUMAN_NVDA_ORIGIN
    assert len(result.human_tester_attestation_sha256) == 64
    assert len(result.journey_content_sha256) == 64


def test_authority_use_rebinds_expected_artifact_and_source() -> None:
    result = _validate(_valid_transcript())

    with pytest.raises(NvdaHumanAcceptanceError, match="expected artifact"):
        _verify(result, artifact_sha256="c" * 64)
    with pytest.raises(NvdaHumanAcceptanceError, match="expected source"):
        _verify(result, source_sha="c" * 40)


def test_shallow_and_deep_copies_do_not_inherit_live_issuance() -> None:
    result = _validate(_valid_transcript())

    for copied in (copy(result), deepcopy(result)):
        assert copied == result
        assert copied is not result
        with pytest.raises(
            NvdaHumanAcceptanceError,
            match="not a live validator-issued authority",
        ):
            _verify(copied)


def test_pickle_reconstruction_does_not_inherit_live_issuance() -> None:
    result = _validate(_valid_transcript())

    reconstructed = pickle.loads(pickle.dumps(result))
    assert reconstructed == result
    assert reconstructed is not result
    with pytest.raises(
        NvdaHumanAcceptanceError,
        match="not a live validator-issued authority",
    ):
        _verify(reconstructed)


def test_dataclass_replace_cannot_mint_successor_authority() -> None:
    result = _validate(_valid_transcript())

    with pytest.raises((TypeError, NvdaHumanAcceptanceError)):
        replace(result, transcript_sha256="c" * 64)


@pytest.mark.parametrize(
    "field_name",
    (
        "transcript_sha256",
        "artifact_sha256",
        "source_sha",
        "windows_version",
        "nvda_version",
        "webview2_runtime_browser_version",
        "webview2_runtime_witness_sha256",
        "evidence_origin",
        "human_tester_attestation_sha256",
        "journey_content_sha256",
        "status",
        "human_tested",
        "nvda_verified",
        "manual_truth_promotion_required",
        "real_money_execution",
        "whole_product_complete",
    ),
)
def test_structural_result_field_descriptor_rebinding_is_rejected(field_name):
    with pytest.raises(
        TypeError,
        match="NVDA structural result authority surface is sealed",
    ):
        setattr(NvdaHumanAcceptanceStructuralResult, field_name, object())

    with pytest.raises(
        TypeError,
        match="NVDA structural result authority surface is sealed",
    ):
        delattr(NvdaHumanAcceptanceStructuralResult, field_name)


def test_same_object_payload_mutation_invalidates_live_issuance() -> None:
    result = _validate(_valid_transcript())
    object.__setattr__(result, "source_sha", "c" * 40)

    with pytest.raises(
        NvdaHumanAcceptanceError,
        match="payload changed after validator issuance",
    ):
        _verify(result)


def test_unregistered_object_new_forgery_is_not_structural_authority() -> None:
    issued = _validate(_valid_transcript())
    forged = object.__new__(NvdaHumanAcceptanceStructuralResult)
    for field_name in (
        "transcript_sha256",
        "artifact_sha256",
        "source_sha",
        "windows_version",
        "nvda_version",
        "webview2_runtime_browser_version",
        "webview2_runtime_witness_sha256",
        "evidence_origin",
        "human_tester_attestation_sha256",
        "journey_content_sha256",
        "status",
        "human_tested",
        "nvda_verified",
        "manual_truth_promotion_required",
        "real_money_execution",
        "whole_product_complete",
    ):
        object.__setattr__(forged, field_name, getattr(issued, field_name))

    with pytest.raises(
        NvdaHumanAcceptanceError,
        match="not a live validator-issued authority",
    ):
        _verify(forged)


def test_module_global_registry_rebinding_cannot_mint_structural_authority(
    monkeypatch,
) -> None:
    issued = _validate(_valid_transcript())
    forged = object.__new__(NvdaHumanAcceptanceStructuralResult)
    for field_name in (
        "transcript_sha256",
        "artifact_sha256",
        "source_sha",
        "windows_version",
        "nvda_version",
        "webview2_runtime_browser_version",
        "webview2_runtime_witness_sha256",
        "evidence_origin",
        "human_tester_attestation_sha256",
        "journey_content_sha256",
        "status",
        "human_tested",
        "nvda_verified",
        "manual_truth_promotion_required",
        "real_money_execution",
        "whole_product_complete",
    ):
        object.__setattr__(forged, field_name, getattr(issued, field_name))

    monkeypatch.setattr(
        nvda_module,
        "_ISSUED_STRUCTURAL_RESULTS",
        {
            id(forged): (
                forged,
                nvda_module._structural_result_fingerprint(forged),
            )
        },
        raising=False,
    )

    with pytest.raises(
        NvdaHumanAcceptanceError,
        match="not a live validator-issued authority",
    ):
        _verify(forged)


def test_module_global_registry_rebinding_cannot_revoke_real_issuance(
    monkeypatch,
) -> None:
    issued = _validate(_valid_transcript())
    monkeypatch.setattr(
        nvda_module,
        "_ISSUED_STRUCTURAL_RESULTS",
        {},
        raising=False,
    )

    assert _verify(issued) is issued


def test_live_structural_issuance_registry_evicts_oldest_authority_fail_closed() -> None:
    first = None
    latest = None
    for index in range(257):
        transcript = _valid_transcript()
        transcript["human_tester_attestation"] = (
            "I performed this session manually with NVDA and keyboard only. "
            f"issuance {index}"
        )
        latest = _validate(transcript)
        if index == 0:
            first = latest

    assert first is not None
    assert latest is not None
    with pytest.raises(
        NvdaHumanAcceptanceError,
        match="not a live validator-issued authority",
    ):
        _verify(first)
    assert _verify(latest) is latest


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
    for bad in (True, 0, 1, 3, "2"):
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


@pytest.mark.parametrize(
    "field, replacement",
    [
        ("renderer", "mshtml"),
        ("observation_source", "registry_preflight"),
        ("human_tested", True),
        ("nvda_verified", True),
        ("real_money_execution", True),
        ("whole_product_complete", True),
    ],
)
def test_runtime_witness_authority_fields_fail_closed(
    field: str,
    replacement: object,
) -> None:
    transcript = _valid_transcript()
    witness = transcript["webview2_runtime_witness"]
    assert isinstance(witness, dict)
    witness[field] = replacement

    with pytest.raises(NvdaHumanAcceptanceError):
        _validate(transcript)


@pytest.mark.parametrize(
    "browser_version",
    ["", " 154.0.2847.51", "154.0.2847.51\n", "x" * 257, 154],
)
def test_runtime_witness_browser_version_must_match_shell_contract(
    browser_version: object,
) -> None:
    transcript = _valid_transcript()
    witness = transcript["webview2_runtime_witness"]
    assert isinstance(witness, dict)
    witness["browser_version_string"] = browser_version

    with pytest.raises(NvdaHumanAcceptanceError):
        _validate(transcript)


@pytest.mark.parametrize("schema_version", [True, 0, 2, "1"])
def test_runtime_witness_schema_version_is_exact(schema_version: object) -> None:
    transcript = _valid_transcript()
    witness = transcript["webview2_runtime_witness"]
    assert isinstance(witness, dict)
    witness["schema_version"] = schema_version

    with pytest.raises(NvdaHumanAcceptanceError):
        _validate(transcript)


def test_runtime_witness_unknown_field_is_rejected() -> None:
    transcript = _valid_transcript()
    witness = transcript["webview2_runtime_witness"]
    assert isinstance(witness, dict)
    witness["machine_passed"] = True

    with pytest.raises(NvdaHumanAcceptanceError):
        _validate(transcript)


def test_runtime_witness_changes_structural_transcript_identity() -> None:
    first = _valid_transcript()
    second = deepcopy(first)
    witness = second["webview2_runtime_witness"]
    assert isinstance(witness, dict)
    witness["browser_version_string"] = "154.0.2847.99"

    first_result = _validate(first)
    second_result = _validate(second)

    assert first_result.transcript_sha256 != second_result.transcript_sha256
    assert (
        first_result.webview2_runtime_witness_sha256
        != second_result.webview2_runtime_witness_sha256
    )


def test_transcript_rejects_valid_shaped_but_unexpected_runtime_witness() -> None:
    transcript = _valid_transcript()
    expected_before_mutation = _runtime_witness_sha(transcript)
    witness = transcript["webview2_runtime_witness"]
    assert isinstance(witness, dict)
    witness["browser_version_string"] = "154.0.2847.99"

    with pytest.raises(
        NvdaHumanAcceptanceError,
        match="independently expected runtime-witness file",
    ):
        _validate(
            transcript,
            expected_runtime_witness_sha256=expected_before_mutation,
        )


def test_result_use_rebinds_expected_runtime_witness_identity() -> None:
    transcript = _valid_transcript()
    result = _validate(transcript)

    with pytest.raises(
        NvdaHumanAcceptanceError,
        match="expected WebView2 runtime witness",
    ):
        _verify(result, runtime_witness_sha256="e" * 64)


@pytest.mark.parametrize(
    "value",
    ["E" * 64, "e" * 63, "g" * 64, 1, True],
)
def test_expected_runtime_witness_digest_must_be_canonical(value: object) -> None:
    transcript = _valid_transcript()

    with pytest.raises(NvdaHumanAcceptanceError):
        validate_human_nvda_acceptance_transcript(
            transcript,
            expected_artifact_sha256=ARTIFACT_SHA,
            expected_source_sha=SOURCE_SHA,
            expected_webview2_runtime_witness_sha256=value,  # type: ignore[arg-type]
        )

