from __future__ import annotations

from copy import copy, deepcopy
from dataclasses import replace
import json

import pytest

from autosport.nvda_human_acceptance import (
    HUMAN_NVDA_ORIGIN,
    REQUIRED_JOURNEY_IDS,
)
from autosport.nvda_manual_acceptance import (
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
T0 = "2026-09-23T01:20:00.000000+00:00"
T1 = "2026-09-23T01:21:00.000000+00:00"
T2 = "2026-09-23T01:22:00.000000+00:00"


def _transcript() -> dict[str, object]:
    return {
        "schema_version": 1,
        "artifact_sha256": ARTIFACT_SHA,
        "source_sha": SOURCE_SHA,
        "windows_version": "Windows 11 25H2 build 26200",
        "nvda_version": "NVDA 2026.2",
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
        )
    with pytest.raises(
        NvdaManualAcceptanceStateError,
        match="did not pass canonical structural validation",
    ):
        ledger.resolve_current(
            transcript=transcript,
            expected_artifact_sha256=ARTIFACT_SHA,
            expected_source_sha="d" * 40,
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
            protocol_version="future-unowned-protocol",
        )


def test_writer_lock_fails_closed(tmp_path):
    transcript = _transcript()
    ledger = _ledger(tmp_path)
    ledger._lock_path.write_text("occupied", encoding="utf-8")
    try:
        with pytest.raises(
            NvdaManualAcceptanceStateError,
            match="writer lock exists",
        ):
            _record(ledger, transcript)
    finally:
        ledger._lock_path.unlink()


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
        reviewer_ref="reviewer-opaque-01",
        reviewer_attestation=secret_phrase,
        reviewed_at=T0,
        decision=ManualNvdaDecision.ACCEPT_PHYSICAL_NVDA,
    )

    raw = ledger.path.read_text(encoding="utf-8")
    assert secret_phrase not in raw
    assert "reviewer_attestation_sha256" in raw
    assert PROTOCOL_VERSION in raw
