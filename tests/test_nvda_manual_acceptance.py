from __future__ import annotations

from copy import copy, deepcopy
from dataclasses import replace
import hashlib
import json

import pytest

import autosport.nvda_manual_acceptance as nvda_manual_module
from autosport.nvda_human_acceptance import (
    HUMAN_NVDA_ORIGIN,
    REQUIRED_JOURNEY_IDS,
)
from autosport.nvda_manual_acceptance import (
    _ManualNvdaWriterLock,
    ManualNvdaAcceptanceLedger,
    ManualNvdaAcceptanceResolution,
    ManualNvdaDecision,
    NvdaManualAcceptanceIntegrityError,
    NvdaManualAcceptanceStateError,
    PROTOCOL_VERSION,
    verify_manual_nvda_acceptance_resolution,
)


ARTIFACT_SHA = "a" * 64
SOURCE_SHA = "b" * 40
WEBVIEW2_RUNTIME_WITNESS = {
    "schema_version": 1,
    "renderer": "edgechromium",
    "browser_version_string": "154.0.2847.51",
    "observation_source": "native_core_webview2_environment",
    "real_money_execution": False,
    "human_tested": False,
    "nvda_verified": False,
    "whole_product_complete": False,
}
RUNTIME_WITNESS_SHA = hashlib.sha256(
    (
        json.dumps(
            WEBVIEW2_RUNTIME_WITNESS,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
).hexdigest()
T0 = "2026-09-23T01:20:00.000000+00:00"
T1 = "2026-09-23T01:21:00.000000+00:00"
T2 = "2026-09-23T01:22:00.000000+00:00"


def _transcript() -> dict[str, object]:
    return {
        "schema_version": 2,
        "artifact_sha256": ARTIFACT_SHA,
        "source_sha": SOURCE_SHA,
        "windows_version": "Windows 11 25H2 build 26200",
        "nvda_version": "NVDA 2026.2",
        "webview2_runtime_witness": dict(WEBVIEW2_RUNTIME_WITNESS),
        "keyboard_only": True,
        "mouse_used": False,
        "evidence_origin": HUMAN_NVDA_ORIGIN,
        "human_tester_attestation": (
            "I performed this physical Windows and NVDA session manually."
        ),
        "journeys": [
            {
                "journey_id": journey_id,
                "human_passed": True,
                "steps": [
                    {
                        "keyboard_input": "Tab, Enter",
                        "expected_accessible_result": (
                            f"Expected accessible result for {journey_id}"
                        ),
                        "actual_nvda_speech": (
                            f"Observed NVDA speech for {journey_id}"
                        ),
                        "focus_before": "Autosport main window",
                        "focus_after": f"Expected control for {journey_id}",
                        "human_passed": True,
                    }
                ],
            }
            for journey_id in REQUIRED_JOURNEY_IDS
        ],
    }


def _ledger(tmp_path):
    return ManualNvdaAcceptanceLedger(
        tmp_path / "Папка з пробілами" / "manual-nvda.jsonl"
    )


def _record(
    ledger: ManualNvdaAcceptanceLedger,
    transcript: object,
    *,
    decision: ManualNvdaDecision = ManualNvdaDecision.ACCEPT_PHYSICAL_NVDA,
    reviewed_at: str = T0,
    reviewer_ref: str = "reviewer-opaque-01",
    reviewer_attestation: str = (
        "I inspected the physical Windows and NVDA evidence and take "
        "responsibility for this manual decision."
    ),
):
    return ledger.record_decision(
        transcript=transcript,
        expected_artifact_sha256=ARTIFACT_SHA,
        expected_source_sha=SOURCE_SHA,
        expected_webview2_runtime_witness_sha256=RUNTIME_WITNESS_SHA,
        reviewer_ref=reviewer_ref,
        reviewer_attestation=reviewer_attestation,
        reviewed_at=reviewed_at,
        decision=decision,
    )


def _resolve(ledger: ManualNvdaAcceptanceLedger, transcript: object):
    return ledger.resolve_current(
        transcript=transcript,
        expected_artifact_sha256=ARTIFACT_SHA,
        expected_source_sha=SOURCE_SHA,
        expected_webview2_runtime_witness_sha256=RUNTIME_WITNESS_SHA,
    )


def test_accept_is_durable_and_re_resolves_without_truth_promotion(tmp_path):
    transcript = _transcript()
    ledger = _ledger(tmp_path)
    record = _record(ledger, transcript)

    restarted = ManualNvdaAcceptanceLedger(ledger.path)
    resolution = _resolve(restarted, transcript)

    assert resolution is not None
    assert resolution.record == record
    assert resolution.accepted_manual_decision is True
    assert resolution.reviewer_identity_verified is False
    assert resolution.human_tested is False
    assert resolution.nvda_verified is False
    assert resolution.manual_truth_promotion_required is True
    assert resolution.real_money_execution is False
    assert resolution.whole_product_complete is False


def test_resolution_cannot_be_directly_constructed_or_replaced(tmp_path):
    transcript = _transcript()
    ledger = _ledger(tmp_path)
    _record(ledger, transcript)
    resolution = _resolve(ledger, transcript)
    assert resolution is not None

    with pytest.raises(NvdaManualAcceptanceStateError, match="resolver-issued"):
        ManualNvdaAcceptanceResolution()  # type: ignore[call-arg]
    with pytest.raises(NvdaManualAcceptanceStateError, match="resolver-issued"):
        replace(resolution, accepted_manual_decision=False)


def test_resolution_verifier_rejects_copy_and_tamper(tmp_path):
    transcript = _transcript()
    ledger = _ledger(tmp_path)
    _record(ledger, transcript)
    resolution = _resolve(ledger, transcript)
    assert resolution is not None

    assert (
        verify_manual_nvda_acceptance_resolution(
            resolution,
            expected_artifact_sha256=ARTIFACT_SHA,
            expected_source_sha=SOURCE_SHA,
            expected_webview2_runtime_witness_sha256=RUNTIME_WITNESS_SHA,
            expected_transcript_sha256=resolution.record.transcript_sha256,
        )
        is resolution
    )

    cloned = copy(resolution)
    assert cloned == resolution
    assert cloned is not resolution
    with pytest.raises(
        NvdaManualAcceptanceStateError,
        match="not a live resolver-issued authority",
    ):
        verify_manual_nvda_acceptance_resolution(
            cloned,
            expected_artifact_sha256=ARTIFACT_SHA,
            expected_source_sha=SOURCE_SHA,
            expected_webview2_runtime_witness_sha256=RUNTIME_WITNESS_SHA,
            expected_transcript_sha256=resolution.record.transcript_sha256,
        )

    tampered = _resolve(ledger, transcript)
    assert tampered is not None
    object.__setattr__(tampered, "human_tested", True)
    with pytest.raises(
        NvdaManualAcceptanceStateError,
        match="cannot promote protected truth",
    ):
        verify_manual_nvda_acceptance_resolution(
            tampered,
            expected_artifact_sha256=ARTIFACT_SHA,
            expected_source_sha=SOURCE_SHA,
            expected_webview2_runtime_witness_sha256=RUNTIME_WITNESS_SHA,
            expected_transcript_sha256=tampered.record.transcript_sha256,
        )


def test_resolution_verifier_rejects_wrong_candidate_identity(tmp_path):
    transcript = _transcript()
    ledger = _ledger(tmp_path)
    _record(ledger, transcript)
    resolution = _resolve(ledger, transcript)
    assert resolution is not None

    with pytest.raises(
        NvdaManualAcceptanceStateError,
        match="does not match the expected candidate",
    ):
        verify_manual_nvda_acceptance_resolution(
            resolution,
            expected_artifact_sha256="c" * 64,
            expected_source_sha=SOURCE_SHA,
            expected_webview2_runtime_witness_sha256=RUNTIME_WITNESS_SHA,
            expected_transcript_sha256=resolution.record.transcript_sha256,
        )


def test_exact_retry_is_idempotent(tmp_path):
    transcript = _transcript()
    ledger = _ledger(tmp_path)

    first = _record(ledger, transcript)
    second = _record(ledger, transcript)

    assert second == first
    assert len(ledger.events()) == 1


def test_accept_then_reject_preserves_history_and_current_rejects(tmp_path):
    transcript = _transcript()
    ledger = _ledger(tmp_path)

    accepted = _record(ledger, transcript, reviewed_at=T0)
    rejected = _record(
        ledger,
        transcript,
        decision=ManualNvdaDecision.REJECT_PHYSICAL_NVDA,
        reviewed_at=T1,
    )
    resolution = _resolve(ledger, transcript)

    assert [event.decision for event in ledger.events()] == [
        ManualNvdaDecision.ACCEPT_PHYSICAL_NVDA,
        ManualNvdaDecision.REJECT_PHYSICAL_NVDA,
    ]
    assert accepted.decision_id != rejected.decision_id
    assert resolution is not None
    assert resolution.record == rejected
    assert resolution.accepted_manual_decision is False


def test_reject_then_later_accept_is_explicit_successor(tmp_path):
    transcript = _transcript()
    ledger = _ledger(tmp_path)

    _record(
        ledger,
        transcript,
        decision=ManualNvdaDecision.REJECT_PHYSICAL_NVDA,
        reviewed_at=T0,
    )
    accepted = _record(ledger, transcript, reviewed_at=T1)
    resolution = _resolve(ledger, transcript)

    assert len(ledger.events()) == 2
    assert resolution is not None
    assert resolution.record == accepted
    assert resolution.accepted_manual_decision is True
    assert resolution.human_tested is False
    assert resolution.nvda_verified is False


def test_verified_accept_becomes_stale_after_later_reject_from_restarted_ledger(
    tmp_path,
):
    transcript = _transcript()
    ledger = _ledger(tmp_path)
    _record(ledger, transcript, reviewed_at=T0)
    accepted_resolution = _resolve(ledger, transcript)
    assert accepted_resolution is not None
    assert accepted_resolution.accepted_manual_decision is True

    assert (
        verify_manual_nvda_acceptance_resolution(
            accepted_resolution,
            expected_artifact_sha256=ARTIFACT_SHA,
            expected_source_sha=SOURCE_SHA,
            expected_webview2_runtime_witness_sha256=RUNTIME_WITNESS_SHA,
            expected_transcript_sha256=accepted_resolution.record.transcript_sha256,
        )
        is accepted_resolution
    )

    restarted = ManualNvdaAcceptanceLedger(ledger.path)
    _record(
        restarted,
        transcript,
        decision=ManualNvdaDecision.REJECT_PHYSICAL_NVDA,
        reviewed_at=T1,
    )

    with pytest.raises(
        NvdaManualAcceptanceStateError,
        match="no longer the current durable decision",
    ):
        verify_manual_nvda_acceptance_resolution(
            accepted_resolution,
            expected_artifact_sha256=ARTIFACT_SHA,
            expected_source_sha=SOURCE_SHA,
            expected_webview2_runtime_witness_sha256=RUNTIME_WITNESS_SHA,
            expected_transcript_sha256=accepted_resolution.record.transcript_sha256,
        )

    rejected_resolution = _resolve(restarted, transcript)
    assert rejected_resolution is not None
    assert rejected_resolution.accepted_manual_decision is False
    assert (
        verify_manual_nvda_acceptance_resolution(
            rejected_resolution,
            expected_artifact_sha256=ARTIFACT_SHA,
            expected_source_sha=SOURCE_SHA,
            expected_webview2_runtime_witness_sha256=RUNTIME_WITNESS_SHA,
            expected_transcript_sha256=rejected_resolution.record.transcript_sha256,
        )
        is rejected_resolution
    )


def test_verified_reject_becomes_stale_after_later_accept(tmp_path):
    transcript = _transcript()
    ledger = _ledger(tmp_path)
    _record(
        ledger,
        transcript,
        decision=ManualNvdaDecision.REJECT_PHYSICAL_NVDA,
        reviewed_at=T0,
    )
    rejected_resolution = _resolve(ledger, transcript)
    assert rejected_resolution is not None
    assert rejected_resolution.accepted_manual_decision is False

    _record(ledger, transcript, reviewed_at=T1)

    with pytest.raises(
        NvdaManualAcceptanceStateError,
        match="no longer the current durable decision",
    ):
        verify_manual_nvda_acceptance_resolution(
            rejected_resolution,
            expected_artifact_sha256=ARTIFACT_SHA,
            expected_source_sha=SOURCE_SHA,
            expected_webview2_runtime_witness_sha256=RUNTIME_WITNESS_SHA,
            expected_transcript_sha256=rejected_resolution.record.transcript_sha256,
        )

    accepted_resolution = _resolve(ledger, transcript)
    assert accepted_resolution is not None
    assert accepted_resolution.accepted_manual_decision is True
    assert (
        verify_manual_nvda_acceptance_resolution(
            accepted_resolution,
            expected_artifact_sha256=ARTIFACT_SHA,
            expected_source_sha=SOURCE_SHA,
            expected_webview2_runtime_witness_sha256=RUNTIME_WITNESS_SHA,
            expected_transcript_sha256=accepted_resolution.record.transcript_sha256,
        )
        is accepted_resolution
    )


def test_unrelated_candidate_successor_does_not_stale_current_resolution(tmp_path):
    transcript = _transcript()
    ledger = _ledger(tmp_path)
    _record(ledger, transcript, reviewed_at=T0)
    resolution = _resolve(ledger, transcript)
    assert resolution is not None

    other = deepcopy(transcript)
    other["journeys"][0]["steps"][0]["actual_nvda_speech"] = (
        "Different physical NVDA observation for another transcript candidate."
    )
    _record(
        ledger,
        other,
        decision=ManualNvdaDecision.REJECT_PHYSICAL_NVDA,
        reviewed_at=T1,
    )

    assert (
        verify_manual_nvda_acceptance_resolution(
            resolution,
            expected_artifact_sha256=ARTIFACT_SHA,
            expected_source_sha=SOURCE_SHA,
            expected_webview2_runtime_witness_sha256=RUNTIME_WITNESS_SHA,
            expected_transcript_sha256=resolution.record.transcript_sha256,
        )
        is resolution
    )


def test_resolution_verifier_rejects_issued_ledger_path_rebinding(tmp_path):
    transcript = _transcript()
    ledger = _ledger(tmp_path)
    _record(ledger, transcript, reviewed_at=T0)
    resolution = _resolve(ledger, transcript)
    assert resolution is not None

    ledger.path = tmp_path / "other-manual-nvda.jsonl"

    with pytest.raises(
        NvdaManualAcceptanceStateError,
        match="ledger authority changed after issuance",
    ):
        verify_manual_nvda_acceptance_resolution(
            resolution,
            expected_artifact_sha256=ARTIFACT_SHA,
            expected_source_sha=SOURCE_SHA,
            expected_webview2_runtime_witness_sha256=RUNTIME_WITNESS_SHA,
            expected_transcript_sha256=resolution.record.transcript_sha256,
        )


def test_resolution_verifier_rejects_fingerprint_authority_rebinding(
    tmp_path,
    monkeypatch,
) -> None:
    transcript = _transcript()
    ledger = _ledger(tmp_path)
    _record(ledger, transcript)
    resolution = _resolve(ledger, transcript)
    assert resolution is not None
    issued_fingerprint = nvda_manual_module._resolution_fingerprint(resolution)
    object.__setattr__(resolution, "human_tested", True)
    hostile_called = False

    def hostile_fingerprint(_resolution):
        nonlocal hostile_called
        hostile_called = True
        return issued_fingerprint

    monkeypatch.setattr(
        nvda_manual_module,
        "_resolution_fingerprint",
        hostile_fingerprint,
    )

    with pytest.raises(
        NvdaManualAcceptanceStateError,
        match="resolution verifier authority changed",
    ):
        verify_manual_nvda_acceptance_resolution(
            resolution,
            expected_artifact_sha256=ARTIFACT_SHA,
            expected_source_sha=SOURCE_SHA,
            expected_webview2_runtime_witness_sha256=RUNTIME_WITNESS_SHA,
            expected_transcript_sha256=resolution.record.transcript_sha256,
        )
    assert hostile_called is False


def test_resolution_issuance_rejects_structural_authority_rebinding(
    tmp_path,
    monkeypatch,
) -> None:
    transcript = _transcript()
    ledger = _ledger(tmp_path)
    _record(ledger, transcript)
    hostile_called = False

    def hostile_structural(*_args, **_kwargs):
        nonlocal hostile_called
        hostile_called = True
        raise AssertionError("hostile structural authority executed")

    monkeypatch.setattr(
        nvda_manual_module,
        "_structural_result",
        hostile_structural,
    )

    with pytest.raises(
        NvdaManualAcceptanceStateError,
        match="resolution issuance authority changed",
    ):
        _resolve(ledger, transcript)
    assert hostile_called is False


def test_record_decision_rejects_private_writer_rebinding(
    tmp_path,
    monkeypatch,
) -> None:
    ledger = _ledger(tmp_path)
    hostile_called = False

    def hostile_writer(self, **_kwargs):
        nonlocal hostile_called
        del self
        hostile_called = True
        raise AssertionError("hostile private writer executed")

    monkeypatch.setattr(
        ManualNvdaAcceptanceLedger,
        "_record_structural_decision",
        hostile_writer,
    )

    with pytest.raises(
        NvdaManualAcceptanceStateError,
        match="decision issuance authority changed",
    ):
        _record(ledger, _transcript())
    assert hostile_called is False


def test_resolution_issuance_rejects_private_resolver_rebinding(
    tmp_path,
    monkeypatch,
) -> None:
    transcript = _transcript()
    ledger = _ledger(tmp_path)
    _record(ledger, transcript)
    hostile_called = False

    def hostile_resolver(self, **_kwargs):
        nonlocal hostile_called
        del self
        hostile_called = True
        raise AssertionError("hostile private resolver executed")

    monkeypatch.setattr(
        ManualNvdaAcceptanceLedger,
        "_resolve_structural_current",
        hostile_resolver,
    )

    with pytest.raises(
        NvdaManualAcceptanceStateError,
        match="resolution issuance authority changed",
    ):
        _resolve(ledger, transcript)
    assert hostile_called is False


def test_resolution_verifier_rejects_instance_events_override(tmp_path):
    transcript = _transcript()
    ledger = _ledger(tmp_path)
    _record(ledger, transcript, reviewed_at=T0)
    resolution = _resolve(ledger, transcript)
    assert resolution is not None
    hostile_called = False

    def hostile_events():
        nonlocal hostile_called
        hostile_called = True
        return (resolution.record,)

    ledger.events = hostile_events  # type: ignore[method-assign]

    with pytest.raises(
        NvdaManualAcceptanceStateError,
        match="ledger authority changed after issuance",
    ):
        verify_manual_nvda_acceptance_resolution(
            resolution,
            expected_artifact_sha256=ARTIFACT_SHA,
            expected_source_sha=SOURCE_SHA,
            expected_webview2_runtime_witness_sha256=RUNTIME_WITNESS_SHA,
            expected_transcript_sha256=resolution.record.transcript_sha256,
        )

    assert hostile_called is False


def test_structural_validator_rebinding_cannot_mint_manual_decision(
    tmp_path,
    monkeypatch,
) -> None:
    ledger = _ledger(tmp_path)
    hostile_called = False

    def hostile_validate(*_args, **_kwargs):
        nonlocal hostile_called
        hostile_called = True
        raise AssertionError("hostile structural validator executed")

    monkeypatch.setattr(
        nvda_manual_module,
        "validate_human_nvda_acceptance_transcript",
        hostile_validate,
    )

    with pytest.raises(
        NvdaManualAcceptanceStateError,
        match="structural validator authority changed",
    ):
        _record(ledger, _transcript())
    assert hostile_called is False
    assert ledger.events() == ()


def test_structural_verifier_rebinding_cannot_mint_manual_decision(
    tmp_path,
    monkeypatch,
) -> None:
    ledger = _ledger(tmp_path)
    hostile_called = False

    def hostile_verify(*_args, **_kwargs):
        nonlocal hostile_called
        hostile_called = True
        raise AssertionError("hostile structural verifier executed")

    monkeypatch.setattr(
        nvda_manual_module,
        "verify_human_nvda_acceptance_structural_result",
        hostile_verify,
    )

    with pytest.raises(
        NvdaManualAcceptanceStateError,
        match="structural validator authority changed",
    ):
        _record(ledger, _transcript())
    assert hostile_called is False
    assert ledger.events() == ()


def test_record_decision_rejects_structural_result_global_rebinding(
    tmp_path,
    monkeypatch,
) -> None:
    ledger = _ledger(tmp_path)
    hostile_called = False

    def hostile_structural(*_args, **_kwargs):
        nonlocal hostile_called
        hostile_called = True
        raise AssertionError("hostile structural result executed")

    monkeypatch.setattr(
        nvda_manual_module,
        "_structural_result",
        hostile_structural,
    )

    with pytest.raises(
        NvdaManualAcceptanceStateError,
        match="decision issuance authority changed",
    ):
        _record(ledger, _transcript())
    assert hostile_called is False
    assert ledger.events() == ()


def test_resolution_registry_is_not_module_visible() -> None:
    assert not hasattr(nvda_manual_module, "_ISSUED_RESOLUTIONS")
    assert not hasattr(nvda_manual_module, "_REGISTER_RESOLUTION_WITNESS")
    assert not hasattr(nvda_manual_module, "_LOOKUP_RESOLUTION_WITNESS")


def test_fake_lookup_global_cannot_override_private_resolution_registry(
    tmp_path,
    monkeypatch,
) -> None:
    transcript = _transcript()
    ledger = _ledger(tmp_path)
    _record(ledger, transcript, reviewed_at=T0)
    resolution = _resolve(ledger, transcript)
    assert resolution is not None
    hostile_called = False

    def hostile_lookup(_resolution):
        nonlocal hostile_called
        hostile_called = True
        raise AssertionError("module-global hostile lookup executed")

    monkeypatch.setattr(
        nvda_manual_module,
        "_LOOKUP_RESOLUTION_WITNESS",
        hostile_lookup,
        raising=False,
    )

    assert (
        verify_manual_nvda_acceptance_resolution(
            resolution,
            expected_artifact_sha256=ARTIFACT_SHA,
            expected_source_sha=SOURCE_SHA,
            expected_webview2_runtime_witness_sha256=RUNTIME_WITNESS_SHA,
            expected_transcript_sha256=resolution.record.transcript_sha256,
        )
        is resolution
    )
    assert hostile_called is False


def test_fake_register_global_cannot_override_private_resolution_issuance(
    tmp_path,
    monkeypatch,
) -> None:
    transcript = _transcript()
    ledger = _ledger(tmp_path)
    _record(ledger, transcript, reviewed_at=T0)
    hostile_called = False

    def hostile_register(**_kwargs):
        nonlocal hostile_called
        hostile_called = True
        raise AssertionError("module-global hostile register executed")

    monkeypatch.setattr(
        nvda_manual_module,
        "_REGISTER_RESOLUTION_WITNESS",
        hostile_register,
        raising=False,
    )

    resolution = _resolve(ledger, transcript)
    assert resolution is not None
    assert hostile_called is False
    assert (
        verify_manual_nvda_acceptance_resolution(
            resolution,
            expected_artifact_sha256=ARTIFACT_SHA,
            expected_source_sha=SOURCE_SHA,
            expected_webview2_runtime_witness_sha256=RUNTIME_WITNESS_SHA,
            expected_transcript_sha256=resolution.record.transcript_sha256,
        )
        is resolution
    )


def test_fake_registry_global_cannot_mint_a_copied_resolution(
    tmp_path,
    monkeypatch,
) -> None:
    transcript = _transcript()
    ledger = _ledger(tmp_path)
    _record(ledger, transcript, reviewed_at=T0)
    resolution = _resolve(ledger, transcript)
    assert resolution is not None
    cloned = copy(resolution)

    monkeypatch.setattr(
        nvda_manual_module,
        "_ISSUED_RESOLUTIONS",
        {id(cloned): object()},
        raising=False,
    )

    with pytest.raises(
        NvdaManualAcceptanceStateError,
        match="not a live resolver-issued authority",
    ):
        verify_manual_nvda_acceptance_resolution(
            cloned,
            expected_artifact_sha256=ARTIFACT_SHA,
            expected_source_sha=SOURCE_SHA,
            expected_webview2_runtime_witness_sha256=RUNTIME_WITNESS_SHA,
            expected_transcript_sha256=resolution.record.transcript_sha256,
        )


def test_resolution_verifier_rejects_instance_events_locked_override(
    tmp_path,
) -> None:
    transcript = _transcript()
    ledger = _ledger(tmp_path)
    _record(ledger, transcript, reviewed_at=T0)
    resolution = _resolve(ledger, transcript)
    assert resolution is not None
    hostile_called = False

    def hostile_events_locked():
        nonlocal hostile_called
        hostile_called = True
        return (resolution.record,)

    ledger._events_locked = hostile_events_locked  # type: ignore[method-assign]

    with pytest.raises(
        NvdaManualAcceptanceStateError,
        match="ledger reader changed after issuance: _events_locked",
    ):
        verify_manual_nvda_acceptance_resolution(
            resolution,
            expected_artifact_sha256=ARTIFACT_SHA,
            expected_source_sha=SOURCE_SHA,
            expected_webview2_runtime_witness_sha256=RUNTIME_WITNESS_SHA,
            expected_transcript_sha256=resolution.record.transcript_sha256,
        )

    assert hostile_called is False


def test_resolution_verifier_rejects_class_read_anchor_rebinding(
    tmp_path,
    monkeypatch,
) -> None:
    transcript = _transcript()
    ledger = _ledger(tmp_path)
    _record(ledger, transcript, reviewed_at=T0)
    resolution = _resolve(ledger, transcript)
    assert resolution is not None
    hostile_called = False

    def hostile_read_anchor(self):
        nonlocal hostile_called
        del self
        hostile_called = True
        return None

    monkeypatch.setattr(
        ManualNvdaAcceptanceLedger,
        "_read_anchor",
        hostile_read_anchor,
    )

    with pytest.raises(
        NvdaManualAcceptanceStateError,
        match="ledger reader changed after issuance: _read_anchor",
    ):
        verify_manual_nvda_acceptance_resolution(
            resolution,
            expected_artifact_sha256=ARTIFACT_SHA,
            expected_source_sha=SOURCE_SHA,
            expected_webview2_runtime_witness_sha256=RUNTIME_WITNESS_SHA,
            expected_transcript_sha256=resolution.record.transcript_sha256,
        )

    assert hostile_called is False


def test_resolution_verifier_rejects_writer_lock_dispatch_rebinding(
    tmp_path,
    monkeypatch,
) -> None:
    transcript = _transcript()
    ledger = _ledger(tmp_path)
    _record(ledger, transcript, reviewed_at=T0)
    resolution = _resolve(ledger, transcript)
    assert resolution is not None
    hostile_called = False

    def hostile_acquire(self):
        nonlocal hostile_called
        del self
        hostile_called = True
        raise AssertionError("hostile writer-lock acquire executed")

    monkeypatch.setattr(_ManualNvdaWriterLock, "acquire", hostile_acquire)

    with pytest.raises(
        NvdaManualAcceptanceStateError,
        match="writer-lock dispatch changed after issuance: acquire",
    ):
        verify_manual_nvda_acceptance_resolution(
            resolution,
            expected_artifact_sha256=ARTIFACT_SHA,
            expected_source_sha=SOURCE_SHA,
            expected_webview2_runtime_witness_sha256=RUNTIME_WITNESS_SHA,
            expected_transcript_sha256=resolution.record.transcript_sha256,
        )

    assert hostile_called is False


def test_resolution_verifier_rechecks_reader_graph_after_inflight_mutation(
    tmp_path,
    monkeypatch,
) -> None:
    transcript = _transcript()
    ledger = _ledger(tmp_path)
    _record(ledger, transcript, reviewed_at=T0)
    resolution = _resolve(ledger, transcript)
    assert resolution is not None

    original_parse = nvda_manual_module._parse_json_object
    original_read_anchor = ManualNvdaAcceptanceLedger._read_anchor
    mutated = False
    hostile_anchor_called = False

    def hostile_read_anchor(self):
        nonlocal hostile_anchor_called
        hostile_anchor_called = True
        return original_read_anchor(self)

    def mutating_parse(raw, *, what):
        nonlocal mutated
        if not mutated:
            mutated = True
            monkeypatch.setattr(
                ManualNvdaAcceptanceLedger,
                "_read_anchor",
                hostile_read_anchor,
            )
        return original_parse(raw, what=what)

    monkeypatch.setattr(
        nvda_manual_module,
        "_parse_json_object",
        mutating_parse,
    )

    with pytest.raises(
        NvdaManualAcceptanceStateError,
        match="ledger reader changed after issuance: _read_anchor",
    ):
        verify_manual_nvda_acceptance_resolution(
            resolution,
            expected_artifact_sha256=ARTIFACT_SHA,
            expected_source_sha=SOURCE_SHA,
            expected_webview2_runtime_witness_sha256=RUNTIME_WITNESS_SHA,
            expected_transcript_sha256=resolution.record.transcript_sha256,
        )

    assert mutated is True
    assert hostile_anchor_called is True


def test_new_decision_must_not_backdate_or_reuse_timestamp(tmp_path):
    transcript = _transcript()
    ledger = _ledger(tmp_path)
    _record(ledger, transcript, reviewed_at=T1)

    with pytest.raises(NvdaManualAcceptanceStateError, match="later reviewed_at"):
        _record(
            ledger,
            transcript,
            decision=ManualNvdaDecision.REJECT_PHYSICAL_NVDA,
            reviewed_at=T0,
        )
    with pytest.raises(NvdaManualAcceptanceStateError, match="later reviewed_at"):
        _record(
            ledger,
            transcript,
            decision=ManualNvdaDecision.REJECT_PHYSICAL_NVDA,
            reviewed_at=T1,
        )


def test_changed_transcript_has_no_inherited_decision(tmp_path):
    transcript = _transcript()
    ledger = _ledger(tmp_path)
    _record(ledger, transcript)

    changed = deepcopy(transcript)
    changed["journeys"][0]["steps"][0][
        "actual_nvda_speech"
    ] = "Different physical NVDA speech"

    assert _resolve(ledger, changed) is None
    assert len(ledger.events()) == 1


def test_wrong_artifact_or_source_cannot_reuse_decision(tmp_path):
    transcript = _transcript()
    ledger = _ledger(tmp_path)
    _record(ledger, transcript)

    with pytest.raises(
        NvdaManualAcceptanceStateError,
        match="did not pass canonical structural validation",
    ):
        ledger.resolve_current(
            transcript=transcript,
            expected_artifact_sha256="c" * 64,
            expected_source_sha=SOURCE_SHA,
            expected_webview2_runtime_witness_sha256=RUNTIME_WITNESS_SHA,
        )
    with pytest.raises(
        NvdaManualAcceptanceStateError,
        match="did not pass canonical structural validation",
    ):
        ledger.resolve_current(
            transcript=transcript,
            expected_artifact_sha256=ARTIFACT_SHA,
            expected_source_sha="d" * 40,
            expected_webview2_runtime_witness_sha256=RUNTIME_WITNESS_SHA,
        )


def test_runtime_witness_drift_cannot_reuse_manual_decision(tmp_path):
    transcript = _transcript()
    ledger = _ledger(tmp_path)
    _record(ledger, transcript)

    changed = deepcopy(transcript)
    witness = changed["webview2_runtime_witness"]
    assert isinstance(witness, dict)
    witness["browser_version_string"] = "155.0.3000.1"
    changed_sha = hashlib.sha256(
        (
            json.dumps(
                witness,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    ).hexdigest()

    assert (
        ledger.resolve_current(
            transcript=changed,
            expected_artifact_sha256=ARTIFACT_SHA,
            expected_source_sha=SOURCE_SHA,
            expected_webview2_runtime_witness_sha256=changed_sha,
        )
        is None
    )


def test_wrong_runtime_witness_anchor_cannot_record_or_resolve(tmp_path):
    transcript = _transcript()
    ledger = _ledger(tmp_path)
    wrong = "c" * 64

    with pytest.raises(
        NvdaManualAcceptanceStateError,
        match="did not pass canonical structural validation",
    ):
        ledger.record_decision(
            transcript=transcript,
            expected_artifact_sha256=ARTIFACT_SHA,
            expected_source_sha=SOURCE_SHA,
            expected_webview2_runtime_witness_sha256=wrong,
            reviewer_ref="reviewer-opaque-01",
            reviewer_attestation="Manual review performed.",
            reviewed_at=T0,
            decision=ManualNvdaDecision.ACCEPT_PHYSICAL_NVDA,
        )

    _record(ledger, transcript)
    with pytest.raises(
        NvdaManualAcceptanceStateError,
        match="did not pass canonical structural validation",
    ):
        ledger.resolve_current(
            transcript=transcript,
            expected_artifact_sha256=ARTIFACT_SHA,
            expected_source_sha=SOURCE_SHA,
            expected_webview2_runtime_witness_sha256=wrong,
        )


def test_incomplete_or_machine_only_transcript_cannot_be_recorded(tmp_path):
    ledger = _ledger(tmp_path)
    transcript = _transcript()
    transcript["mouse_used"] = True

    with pytest.raises(
        NvdaManualAcceptanceStateError,
        match="did not pass canonical structural validation",
    ):
        _record(ledger, transcript)
    assert ledger.events() == ()


@pytest.mark.parametrize(
    "reviewed_at",
    [
        "2026-09-23T01:20:00Z",
        "2026-09-23T03:20:00.000000+02:00",
        "2026-09-23T01:20:00+00:00",
        "2026-09-23T01:20:00.000000",
    ],
)
def test_review_time_requires_exact_canonical_utc(tmp_path, reviewed_at):
    with pytest.raises(NvdaManualAcceptanceStateError, match="canonical UTC"):
        _record(_ledger(tmp_path), _transcript(), reviewed_at=reviewed_at)


def test_reviewer_reference_and_attestation_are_required(tmp_path):
    ledger = _ledger(tmp_path)

    with pytest.raises(NvdaManualAcceptanceStateError, match="reviewer_ref"):
        _record(ledger, _transcript(), reviewer_ref="")
    with pytest.raises(
        NvdaManualAcceptanceStateError,
        match="reviewer_attestation",
    ):
        _record(ledger, _transcript(), reviewer_attestation="")


def test_unknown_protocol_cannot_mint_or_resolve_decision(tmp_path):
    transcript = _transcript()
    ledger = _ledger(tmp_path)

    with pytest.raises(NvdaManualAcceptanceStateError, match="protocol_version"):
        ledger.record_decision(
            transcript=transcript,
            expected_artifact_sha256=ARTIFACT_SHA,
            expected_source_sha=SOURCE_SHA,
            expected_webview2_runtime_witness_sha256=RUNTIME_WITNESS_SHA,
            reviewer_ref="reviewer-opaque-01",
            reviewer_attestation="Manual review performed.",
            reviewed_at=T0,
            decision=ManualNvdaDecision.ACCEPT_PHYSICAL_NVDA,
            protocol_version="future-unowned-protocol",
        )

    _record(ledger, transcript)
    with pytest.raises(NvdaManualAcceptanceStateError, match="protocol_version"):
        ledger.resolve_current(
            transcript=transcript,
            expected_artifact_sha256=ARTIFACT_SHA,
            expected_source_sha=SOURCE_SHA,
            expected_webview2_runtime_witness_sha256=RUNTIME_WITNESS_SHA,
            protocol_version="future-unowned-protocol",
        )


def test_active_writer_lock_fails_closed(tmp_path):
    transcript = _transcript()
    ledger = _ledger(tmp_path)

    with _ManualNvdaWriterLock(ledger.path):
        with pytest.raises(
            NvdaManualAcceptanceStateError,
            match="writer is active",
        ):
            _record(ledger, transcript)


def test_stale_writer_lock_path_does_not_block_restart(tmp_path):
    transcript = _transcript()
    ledger = _ledger(tmp_path)
    ledger._lock_path.write_text("stale-after-crash", encoding="utf-8")

    record = _record(ledger, transcript)

    assert record.decision is ManualNvdaDecision.ACCEPT_PHYSICAL_NVDA
    assert ledger._lock_path.read_text(encoding="utf-8") == "stale-after-crash"
    restarted = ManualNvdaAcceptanceLedger(ledger.path)
    resolution = _resolve(restarted, transcript)
    assert resolution is not None
    assert resolution.record == record


def test_event_tamper_is_detected(tmp_path):
    transcript = _transcript()
    ledger = _ledger(tmp_path)
    _record(ledger, transcript)

    event = json.loads(ledger.path.read_text(encoding="utf-8"))
    event["payload"]["reviewer_ref"] = "forged-reviewer"
    ledger.path.write_text(
        json.dumps(
            event,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(NvdaManualAcceptanceIntegrityError):
        ledger.events()


def test_unterminated_final_ledger_record_fails_closed(tmp_path):
    ledger = _ledger(tmp_path)
    _record(ledger, _transcript())

    raw = ledger.path.read_bytes()
    assert raw.endswith(b"\n")
    ledger.path.write_bytes(raw[:-1])

    with pytest.raises(
        NvdaManualAcceptanceIntegrityError,
        match="canonical trailing newline",
    ):
        ledger.events()


def test_unterminated_anchor_file_fails_closed(tmp_path):
    ledger = _ledger(tmp_path)
    _record(ledger, _transcript())

    raw = ledger._anchor_path.read_bytes()
    assert raw.endswith(b"\n")
    ledger._anchor_path.write_bytes(raw[:-1])

    with pytest.raises(
        NvdaManualAcceptanceIntegrityError,
        match="anchor lacks canonical trailing newline",
    ):
        ledger.events()


def test_unterminated_pending_file_fails_closed_before_recovery(tmp_path):
    ledger = _ledger(tmp_path)
    _record(ledger, _transcript())
    event = json.loads(ledger.path.read_text(encoding="utf-8"))
    pending = ledger._pending_record(
        prior_event_count=0,
        prior_root=None,
        event=event,
    )
    ledger._pending_path.write_text(
        json.dumps(
            pending,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    raw = ledger._pending_path.read_bytes()
    assert raw.endswith(b"\n")
    ledger._pending_path.write_bytes(raw[:-1])

    with pytest.raises(
        NvdaManualAcceptanceIntegrityError,
        match="pending manual NVDA decision lacks canonical trailing newline",
    ):
        ledger.events()


def test_tail_deletion_is_detected_by_anchor(tmp_path):
    transcript = _transcript()
    ledger = _ledger(tmp_path)
    _record(ledger, transcript, reviewed_at=T0)
    _record(
        ledger,
        transcript,
        decision=ManualNvdaDecision.REJECT_PHYSICAL_NVDA,
        reviewed_at=T1,
    )

    lines = ledger.path.read_text(encoding="utf-8").splitlines()
    ledger.path.write_text(lines[0] + "\n", encoding="utf-8")

    with pytest.raises(
        NvdaManualAcceptanceIntegrityError,
        match="anchor does not match",
    ):
        ledger.events()


def test_missing_anchor_for_nonempty_ledger_fails_closed(tmp_path):
    ledger = _ledger(tmp_path)
    _record(ledger, _transcript())
    ledger._anchor_path.unlink()

    with pytest.raises(
        NvdaManualAcceptanceIntegrityError,
        match="anchor is missing",
    ):
        ledger.events()


def test_duplicate_json_key_fails_closed(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger.path.write_text(
        '{"schema_version":1,"schema_version":1}\n',
        encoding="utf-8",
    )

    with pytest.raises(
        NvdaManualAcceptanceIntegrityError,
        match="duplicate JSON key",
    ):
        ledger.events()


def test_no_decision_returns_none_for_valid_exact_transcript(tmp_path):
    ledger = _ledger(tmp_path)
    assert _resolve(ledger, _transcript()) is None


def test_ledger_keeps_reviewer_attestation_private_by_hash(tmp_path):
    ledger = _ledger(tmp_path)
    secret_phrase = "Manual reviewer statement that should not be persisted raw."
    ledger.record_decision(
        transcript=_transcript(),
        expected_artifact_sha256=ARTIFACT_SHA,
        expected_source_sha=SOURCE_SHA,
        expected_webview2_runtime_witness_sha256=RUNTIME_WITNESS_SHA,
        reviewer_ref="reviewer-opaque-01",
        reviewer_attestation=secret_phrase,
        reviewed_at=T0,
        decision=ManualNvdaDecision.ACCEPT_PHYSICAL_NVDA,
    )

    raw = ledger.path.read_text(encoding="utf-8")
    assert secret_phrase not in raw
    assert "reviewer_attestation_sha256" in raw
    assert PROTOCOL_VERSION in raw
