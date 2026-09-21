from __future__ import annotations

import pytest

from autosport.holdout_disclosure import (
    DisclosureChannel,
    DisclosureKind,
    HoldoutDisclosureError,
    HoldoutDisclosureGate,
)
from autosport.point_in_time_evidence import (
    HoldoutAlreadyConsumedError,
    HoldoutConsumptionLedger,
)
from autosport.scientific_registry import DatasetSnapshot


_MANIFEST_A = "a" * 64
_MANIFEST_B = "b" * 64
_DISCLOSED_AT = "2026-09-21T12:00:00Z"


def _snapshot(
    *,
    snapshot_id: str = "confirmation-a",
    manifest_sha256: str = _MANIFEST_A,
    source_identity: str = "provider:canonical-feed",
    license_identity: str = "terms:v1",
) -> DatasetSnapshot:
    return DatasetSnapshot(
        dataset_snapshot_id=snapshot_id,
        manifest_sha256=manifest_sha256,
        source_identity=source_identity,
        license_identity=license_identity,
        causal_cutoff="2026-09-20T10:00:00Z",
        available_at_utc="2026-09-20T10:01:00Z",
    )


def _ledger(tmp_path) -> HoldoutConsumptionLedger:
    return HoldoutConsumptionLedger(
        tmp_path / "holdout-consumption.json",
        authority_root=tmp_path / "machine-authority",
    )


_OUTCOME_KINDS = (
    DisclosureKind.RAW_LABEL,
    DisclosureKind.EVENT_OUTCOME,
    DisclosureKind.AGGREGATE_SCORE,
    DisclosureKind.PASS_FAIL,
)
_PUBLIC_CHANNELS = (
    DisclosureChannel.UI,
    DisclosureChannel.HUMAN,
    DisclosureChannel.LLM,
    DisclosureChannel.EXPORT,
    DisclosureChannel.API,
    DisclosureChannel.LOG,
)


@pytest.mark.parametrize("channel", _PUBLIC_CHANNELS)
@pytest.mark.parametrize("kind", _OUTCOME_KINDS)
def test_every_adaptive_outcome_disclosure_consumes_canonical_holdout(
    tmp_path, channel: DisclosureChannel, kind: DisclosureKind
) -> None:
    snapshot = _snapshot()
    ledger = _ledger(tmp_path)
    gate = HoldoutDisclosureGate(ledger)

    decision = gate.record(
        dataset_snapshot=snapshot,
        research_protocol_id="protocol-v1",
        confirmation_trial_family_id="family-v1",
        channel=channel,
        kind=kind,
        accessible_to_adaptive_actor=True,
        disclosed_at_utc=_DISCLOSED_AT,
    )

    assert decision.consumed is True
    assert decision.consumption is not None
    assert decision.consumption.holdout_freshness_id == ledger.freshness_id(
        dataset_snapshot=snapshot,
        confirmation_trial_family_id="family-v1",
    )
    assert decision.proves_holdout_untouched is False
    assert decision.grants_promotion_authority is False
    with pytest.raises(HoldoutAlreadyConsumedError):
        ledger.assert_unused(
            dataset_snapshot=snapshot,
            research_protocol_id="protocol-v1",
            confirmation_trial_family_id="family-v1",
        )


@pytest.mark.parametrize("channel", tuple(DisclosureChannel))
def test_non_outcome_metadata_does_not_consume_confirmation_capacity(
    tmp_path, channel: DisclosureChannel
) -> None:
    snapshot = _snapshot()
    ledger = _ledger(tmp_path)
    decision = HoldoutDisclosureGate(ledger).record(
        dataset_snapshot=snapshot,
        research_protocol_id="protocol-v1",
        confirmation_trial_family_id="family-v1",
        channel=channel,
        kind=DisclosureKind.NON_OUTCOME_METADATA,
        accessible_to_adaptive_actor=True,
        disclosed_at_utc=_DISCLOSED_AT,
    )

    assert decision.consumed is False
    assert decision.proves_holdout_untouched is False
    ledger.assert_unused(
        dataset_snapshot=snapshot,
        research_protocol_id="protocol-v1",
        confirmation_trial_family_id="family-v1",
    )


@pytest.mark.parametrize("kind", _OUTCOME_KINDS)
def test_outcome_signal_kept_inside_sealed_evaluator_does_not_consume(
    tmp_path, kind: DisclosureKind
) -> None:
    snapshot = _snapshot()
    ledger = _ledger(tmp_path)
    decision = HoldoutDisclosureGate(ledger).record(
        dataset_snapshot=snapshot,
        research_protocol_id="protocol-v1",
        confirmation_trial_family_id="family-v1",
        channel=DisclosureChannel.SEALED_EVALUATOR,
        kind=kind,
        accessible_to_adaptive_actor=False,
        disclosed_at_utc=_DISCLOSED_AT,
    )

    assert decision.consumed is False
    ledger.assert_unused(
        dataset_snapshot=snapshot,
        research_protocol_id="protocol-v1",
        confirmation_trial_family_id="family-v1",
    )


def test_accessible_signal_consumes_even_if_channel_is_named_sealed_evaluator(
    tmp_path,
) -> None:
    snapshot = _snapshot()
    ledger = _ledger(tmp_path)

    decision = HoldoutDisclosureGate(ledger).record(
        dataset_snapshot=snapshot,
        research_protocol_id="protocol-v1",
        confirmation_trial_family_id="family-v1",
        channel=DisclosureChannel.SEALED_EVALUATOR,
        kind=DisclosureKind.PASS_FAIL,
        accessible_to_adaptive_actor=True,
        disclosed_at_utc=_DISCLOSED_AT,
    )

    assert decision.consumed is True


def test_exact_resume_is_idempotent_across_disclosure_channel_and_granularity(
    tmp_path,
) -> None:
    snapshot = _snapshot()
    ledger = _ledger(tmp_path)
    gate = HoldoutDisclosureGate(ledger)

    first = gate.record(
        dataset_snapshot=snapshot,
        research_protocol_id="protocol-v1",
        confirmation_trial_family_id="family-v1",
        channel=DisclosureChannel.UI,
        kind=DisclosureKind.AGGREGATE_SCORE,
        accessible_to_adaptive_actor=True,
        disclosed_at_utc=_DISCLOSED_AT,
    )
    resumed = gate.record(
        dataset_snapshot=snapshot,
        research_protocol_id="protocol-v1",
        confirmation_trial_family_id="family-v1",
        channel=DisclosureChannel.EXPORT,
        kind=DisclosureKind.RAW_LABEL,
        accessible_to_adaptive_actor=True,
        disclosed_at_utc="2026-09-21T12:05:00Z",
    )

    assert first.consumption == resumed.consumption
    assert len(ledger.records()) == 1


def test_protocol_relabel_cannot_make_disclosed_physical_holdout_fresh(
    tmp_path,
) -> None:
    snapshot = _snapshot()
    ledger = _ledger(tmp_path)
    gate = HoldoutDisclosureGate(ledger)

    gate.record(
        dataset_snapshot=snapshot,
        research_protocol_id="protocol-v1",
        confirmation_trial_family_id="family-v1",
        channel=DisclosureChannel.API,
        kind=DisclosureKind.EVENT_OUTCOME,
        accessible_to_adaptive_actor=True,
        disclosed_at_utc=_DISCLOSED_AT,
    )

    with pytest.raises(HoldoutAlreadyConsumedError):
        gate.record(
            dataset_snapshot=snapshot,
            research_protocol_id="protocol-renamed-v2",
            confirmation_trial_family_id="family-v1",
            channel=DisclosureChannel.LOG,
            kind=DisclosureKind.PASS_FAIL,
            accessible_to_adaptive_actor=True,
            disclosed_at_utc="2026-09-21T12:06:00Z",
        )


def test_snapshot_alias_cannot_make_disclosed_physical_holdout_fresh(tmp_path) -> None:
    original = _snapshot(snapshot_id="confirmation-a")
    alias = _snapshot(snapshot_id="renamed-snapshot")
    ledger = _ledger(tmp_path)
    gate = HoldoutDisclosureGate(ledger)

    gate.record(
        dataset_snapshot=original,
        research_protocol_id="protocol-v1",
        confirmation_trial_family_id="family-v1",
        channel=DisclosureChannel.HUMAN,
        kind=DisclosureKind.RAW_LABEL,
        accessible_to_adaptive_actor=True,
        disclosed_at_utc=_DISCLOSED_AT,
    )

    with pytest.raises(HoldoutAlreadyConsumedError):
        gate.record(
            dataset_snapshot=alias,
            research_protocol_id="protocol-v2",
            confirmation_trial_family_id="family-v1",
            channel=DisclosureChannel.LLM,
            kind=DisclosureKind.AGGREGATE_SCORE,
            accessible_to_adaptive_actor=True,
            disclosed_at_utc="2026-09-21T12:07:00Z",
        )


def test_disjoint_holdout_family_retains_independent_confirmation_capacity(
    tmp_path,
) -> None:
    snapshot = _snapshot()
    ledger = _ledger(tmp_path)
    gate = HoldoutDisclosureGate(ledger)

    gate.record(
        dataset_snapshot=snapshot,
        research_protocol_id="protocol-v1",
        confirmation_trial_family_id="family-a",
        channel=DisclosureChannel.UI,
        kind=DisclosureKind.PASS_FAIL,
        accessible_to_adaptive_actor=True,
        disclosed_at_utc=_DISCLOSED_AT,
    )

    ledger.assert_unused(
        dataset_snapshot=snapshot,
        research_protocol_id="protocol-v1",
        confirmation_trial_family_id="family-b",
    )


def test_disjoint_manifest_retains_independent_confirmation_capacity(tmp_path) -> None:
    first = _snapshot(manifest_sha256=_MANIFEST_A)
    second = _snapshot(
        snapshot_id="confirmation-b",
        manifest_sha256=_MANIFEST_B,
    )
    ledger = _ledger(tmp_path)
    gate = HoldoutDisclosureGate(ledger)

    gate.record(
        dataset_snapshot=first,
        research_protocol_id="protocol-v1",
        confirmation_trial_family_id="family-v1",
        channel=DisclosureChannel.EXPORT,
        kind=DisclosureKind.EVENT_OUTCOME,
        accessible_to_adaptive_actor=True,
        disclosed_at_utc=_DISCLOSED_AT,
    )

    ledger.assert_unused(
        dataset_snapshot=second,
        research_protocol_id="protocol-v1",
        confirmation_trial_family_id="family-v1",
    )


def test_gate_rejects_caller_substituted_policy_types(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    gate = HoldoutDisclosureGate(ledger)

    with pytest.raises(HoldoutDisclosureError):
        gate.record(
            dataset_snapshot=_snapshot(),
            research_protocol_id="protocol-v1",
            confirmation_trial_family_id="family-v1",
            channel="API",  # type: ignore[arg-type]
            kind=DisclosureKind.PASS_FAIL,
            accessible_to_adaptive_actor=True,
            disclosed_at_utc=_DISCLOSED_AT,
        )
    with pytest.raises(HoldoutDisclosureError):
        gate.record(
            dataset_snapshot=_snapshot(),
            research_protocol_id="protocol-v1",
            confirmation_trial_family_id="family-v1",
            channel=DisclosureChannel.API,
            kind=DisclosureKind.PASS_FAIL,
            accessible_to_adaptive_actor=1,  # type: ignore[arg-type]
            disclosed_at_utc=_DISCLOSED_AT,
        )
