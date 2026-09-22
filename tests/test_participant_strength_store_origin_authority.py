from __future__ import annotations

import pytest

from autosport.opponent_intelligence import OpponentIntelligenceStore
from autosport.participant_strength import (
    ParticipantStrengthError,
    StrengthSnapshotPair,
    emit_registered_strength_forecast,
)
from test_participant_strength import T3, T4, _registered_strength_lineage
from test_participant_strength_snapshot_origin_authority import (
    _self_consistent_unissued_snapshot,
)


def test_registered_forecast_rejects_caller_seeded_exact_opponent_store(tmp_path) -> None:
    """An exact Python store object is not durable product-origin authority.

    The caller can bypass constructor/persistence entirely, seed self-hashed
    RatingSnapshot DTOs into the same private dictionaries used by the current
    resolver, and pass that exact concrete store instance to registered forecast
    issuance. The positive path must instead re-resolve through a product-owned
    durable store/workspace authority.
    """

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
    evidence = StrengthSnapshotPair(subject, opponent, T4)

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
