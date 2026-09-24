from __future__ import annotations

import hashlib

import pytest

from autosport import _dataset_snapshot_lineage_publication_trust_root as lineage_trust_root
from autosport.dataset_snapshot_lineage import (
    DatasetSnapshotLineageAuthority,
    membership_manifest_sha256,
)
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
from autosport.scientific_registry import DatasetSnapshot, ScientificRegistry


def _member(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


_MEMBERS_A = (_member("a"), _member("b"))
_MEMBERS_B = (*_MEMBERS_A, _member("c"))
_MANIFEST_A = membership_manifest_sha256(_MEMBERS_A)
_MANIFEST_B = membership_manifest_sha256(_MEMBERS_B)
_DISCLOSED_AT = "2026-09-21T12:00:00Z"


@pytest.fixture(autouse=True)
def _product_machine_authority(tmp_path, monkeypatch):
    root = (tmp_path / "product-machine-authority").resolve(strict=False)
    monkeypatch.setattr(
        lineage_trust_root,
        "_machine_account_authority_root",
        lambda: root,
    )
    return root


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
    workspace = tmp_path / "holdout-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    registry = ScientificRegistry.initialize_pristine(
        workspace / "scientific-registry.json"
    )
    snapshots = (
        (_snapshot(), _MEMBERS_A, None),
        (
            _snapshot(snapshot_id="renamed-snapshot"),
            _MEMBERS_A,
            "confirmation-a",
        ),
        (
            _snapshot(snapshot_id="confirmation-b", manifest_sha256=_MANIFEST_B),
            _MEMBERS_B,
            "renamed-snapshot",
        ),
    )
    for snapshot, _, _ in snapshots:
        registry.append(snapshot)
    authority_root = lineage_trust_root._machine_account_authority_root()
    lineage = DatasetSnapshotLineageAuthority.initialize_pristine(
        workspace / "dataset-snapshot-lineage.json",
        registry,
        authority_root=authority_root,
    )
    for snapshot, members, parent in snapshots:
        lineage.register(
            snapshot_id=snapshot.dataset_snapshot_id,
            member_sha256=members,
            parent_snapshot_id=parent,
        )
    return HoldoutConsumptionLedger(
        workspace / "holdout-consumption.json",
        lineage_authority=lineage,
    )


@pytest.mark.parametrize("channel", tuple(DisclosureChannel))
@pytest.mark.parametrize("kind", tuple(DisclosureKind))
@pytest.mark.parametrize("declared_accessibility", (True, False))
def test_every_disclosure_descriptor_consumes_canonical_holdout(
    tmp_path,
    channel: DisclosureChannel,
    kind: DisclosureKind,
    declared_accessibility: bool,
) -> None:
    snapshot = _snapshot()
    ledger = _ledger(tmp_path)

    decision = HoldoutDisclosureGate(ledger).record(
        dataset_snapshot=snapshot,
        research_protocol_id="protocol-v1",
        confirmation_trial_family_id="family-v1",
        channel=channel,
        kind=kind,
        accessible_to_adaptive_actor=declared_accessibility,
        disclosed_at_utc=_DISCLOSED_AT,
    )

    assert decision.consumed is True
    assert decision.channel is channel
    assert decision.kind is kind
    assert decision.accessible_to_adaptive_actor is declared_accessibility
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


def test_caller_declared_sealed_inaccessible_cannot_mint_freshness_exemption(
    tmp_path,
) -> None:
    snapshot = _snapshot()
    ledger = _ledger(tmp_path)

    decision = HoldoutDisclosureGate(ledger).record(
        dataset_snapshot=snapshot,
        research_protocol_id="protocol-v1",
        confirmation_trial_family_id="family-v1",
        channel=DisclosureChannel.SEALED_EVALUATOR,
        kind=DisclosureKind.NON_OUTCOME_METADATA,
        accessible_to_adaptive_actor=False,
        disclosed_at_utc=_DISCLOSED_AT,
    )

    assert decision.consumed is True
    with pytest.raises(HoldoutAlreadyConsumedError):
        ledger.assert_unused(
            dataset_snapshot=snapshot,
            research_protocol_id="protocol-v1",
            confirmation_trial_family_id="family-v1",
        )


def test_exact_resume_is_idempotent_across_all_audit_descriptor_changes(
    tmp_path,
) -> None:
    snapshot = _snapshot()
    ledger = _ledger(tmp_path)
    gate = HoldoutDisclosureGate(ledger)

    first = gate.record(
        dataset_snapshot=snapshot,
        research_protocol_id="protocol-v1",
        confirmation_trial_family_id="family-v1",
        channel=DisclosureChannel.SEALED_EVALUATOR,
        kind=DisclosureKind.NON_OUTCOME_METADATA,
        accessible_to_adaptive_actor=False,
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


def test_gate_rejects_instance_shadowed_ledger_consume(tmp_path) -> None:
    snapshot = _snapshot()
    ledger = _ledger(tmp_path)
    gate = HoldoutDisclosureGate(ledger)
    ledger.consume = lambda **_: None  # type: ignore[method-assign]

    with pytest.raises(HoldoutDisclosureError, match="instance-shadowed"):
        gate.record(
            dataset_snapshot=snapshot,
            research_protocol_id="protocol-v1",
            confirmation_trial_family_id="family-v1",
            channel=DisclosureChannel.API,
            kind=DisclosureKind.PASS_FAIL,
            accessible_to_adaptive_actor=True,
            disclosed_at_utc=_DISCLOSED_AT,
        )

    assert ledger.records() == ()


def test_gate_rejects_rebound_canonical_consume_dispatch(tmp_path, monkeypatch) -> None:
    snapshot = _snapshot()
    ledger = _ledger(tmp_path)
    gate = HoldoutDisclosureGate(ledger)
    monkeypatch.setattr(HoldoutConsumptionLedger, "consume", lambda self, **_: None)

    with pytest.raises(HoldoutDisclosureError, match="dispatch was rebound"):
        gate.record(
            dataset_snapshot=snapshot,
            research_protocol_id="protocol-v1",
            confirmation_trial_family_id="family-v1",
            channel=DisclosureChannel.API,
            kind=DisclosureKind.PASS_FAIL,
            accessible_to_adaptive_actor=True,
            disclosed_at_utc=_DISCLOSED_AT,
        )

    assert ledger.records() == ()


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
            kind="PASS_FAIL",  # type: ignore[arg-type]
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
    with pytest.raises(HoldoutDisclosureError):
        gate.record(
            dataset_snapshot=object(),  # type: ignore[arg-type]
            research_protocol_id="protocol-v1",
            confirmation_trial_family_id="family-v1",
            channel=DisclosureChannel.API,
            kind=DisclosureKind.PASS_FAIL,
            accessible_to_adaptive_actor=True,
            disclosed_at_utc=_DISCLOSED_AT,
        )
