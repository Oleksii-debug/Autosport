from __future__ import annotations

import pytest

from autosport.opponent_intelligence import (
    OpponentIntelligenceError,
    OpponentIntelligenceStore,
)
from autosport.participant_identity import ParticipantIdentityRegistry
from autosport.participant_strength import (
    ParticipantStrengthError,
    StrengthSnapshotPair,
    emit_registered_strength_forecast,
)
from test_participant_strength import T3, T4, _registered_strength_lineage
from test_participant_strength_snapshot_origin_authority import (
    _self_consistent_unissued_snapshot,
)


def _forged_strength_inputs(tmp_path):
    registry, artifacts = _registered_strength_lineage(
        tmp_path,
        dataset_cutoff=T3,
        dataset_available_at=T3,
        model_created_at=T3,
        strategy_created_at=T3,
    )
    subject = _self_consistent_unissued_snapshot(
        "participant-a",
        rating="0.99",
        evidence_byte="1",
    )
    opponent = _self_consistent_unissued_snapshot(
        "participant-b",
        rating="0.01",
        evidence_byte="2",
    )
    return registry, artifacts, StrengthSnapshotPair(subject, opponent, T4)


def test_registered_forecast_rejects_caller_seeded_exact_opponent_store(tmp_path) -> None:
    """An exact Python store object is not durable product-origin authority."""

    registry, artifacts, evidence = _forged_strength_inputs(tmp_path)
    subject = evidence.subject
    opponent = evidence.opponent

    forged_store = object.__new__(OpponentIntelligenceStore)
    forged_store._ratings = {
        subject.snapshot_id: subject,
        opponent.snapshot_id: opponent,
    }
    forged_store._invalidations = {}

    with pytest.raises(
        ParticipantStrengthError,
        match="product-owned durable store origin authority",
    ):
        emit_registered_strength_forecast(
            registry=registry,
            artifact_store=artifacts,
            opponent_store=forged_store,
            evidence=evidence,
            model_version_id="model-lineage",
            strategy_version_id="strategy-lineage",
            quote_key="event-forged-store:match-winner:participant-a",
        )


def test_registered_forecast_rejects_instance_shadowed_origin_verifier(tmp_path) -> None:
    """An exact instance cannot replace the verifier used at positive issuance."""

    registry, artifacts, evidence = _forged_strength_inputs(tmp_path)
    forged_store = object.__new__(OpponentIntelligenceStore)
    forged_store._ratings = {
        evidence.subject.snapshot_id: evidence.subject,
        evidence.opponent.snapshot_id: evidence.opponent,
    }
    forged_store._invalidations = {}
    forged_store._verified_durable_state = lambda: {}

    with pytest.raises(
        ParticipantStrengthError,
        match="origin authority methods cannot be instance-shadowed",
    ):
        emit_registered_strength_forecast(
            registry=registry,
            artifact_store=artifacts,
            opponent_store=forged_store,
            evidence=evidence,
            model_version_id="model-lineage",
            strategy_version_id="strategy-lineage",
            quote_key="event-shadowed-store:match-winner:participant-a",
        )


def test_existing_copied_store_bytes_do_not_bootstrap_origin_authority(tmp_path) -> None:
    """Valid copied JSON is integrity evidence, not product-owned origin evidence."""

    identity = ParticipantIdentityRegistry.initialize_pristine(tmp_path / "identity.json")
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = OpponentIntelligenceStore.initialize_pristine(
        source_root / "opponent.json",
        identity,
    )

    forged_root = tmp_path / "forged"
    forged_root.mkdir()
    forged_path = forged_root / "opponent.json"
    forged_path.write_bytes(source.path.read_bytes())

    with pytest.raises(
        OpponentIntelligenceError,
        match="product-owned durable store origin authority is missing",
    ):
        OpponentIntelligenceStore(forged_path, identity)
