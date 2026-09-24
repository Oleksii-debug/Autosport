from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from autosport.opponent_intelligence import (
    DownstreamInvalidation,
    InvalidationReason,
    InvalidationTarget,
    OpponentIntelligenceError,
    OpponentIntelligenceStore,
    RatingSnapshot,
    RecomputeStatus,
    SnapshotState,
)
from autosport.participant_identity import IdentityView
from autosport.participant_strength import (
    ParticipantStrengthError,
    StrengthSnapshotPair,
    emit_registered_strength_forecast,
)
from test_participant_strength import (
    T3,
    T4,
    T5,
    _pair,
    _registered_strength_lineage,
)


def _payload_sha256(payload: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _self_consistent_unissued_snapshot(
    participant_id: str,
    *,
    rating: str,
    evidence_byte: str,
) -> RatingSnapshot:
    snapshot = RatingSnapshot(
        snapshot_id="0" * 64,
        participant_entity_id=participant_id,
        sport_id="table-tennis",
        league_id="league-a",
        market_context_id="match-winner",
        view=IdentityView.AS_KNOWN_AT_DECISION,
        causal_cutoff=T3,
        published_at=T3,
        algorithm_family="bounded-mean-score",
        algorithm_version="mean-score-v1",
        config_sha256="c" * 64,
        min_support=2,
        max_age_seconds=86400,
        code_sha256="d" * 64,
        dependency_sha256="e" * 64,
        predecessor_snapshot_ids=(),
        input_performance_ids=(evidence_byte * 64,),
        input_digest=evidence_byte * 64,
        support=4,
        effective_sample=4,
        opponent_count=4,
        rating=rating,
        uncertainty="0.1",
        state=SnapshotState.SUPPORTED,
    )
    return replace(
        snapshot,
        snapshot_id=_payload_sha256(snapshot.payload(include_id=False)),
    )


def test_registered_forecast_rejects_self_hashed_unissued_rating_snapshots(
    tmp_path,
) -> None:
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
    assert subject.snapshot_id == _payload_sha256(subject.payload(include_id=False))
    assert opponent.snapshot_id == _payload_sha256(opponent.payload(include_id=False))

    evidence = StrengthSnapshotPair(subject, opponent, T4)

    authority = object.__new__(OpponentIntelligenceStore)
    authority._ratings = {}
    authority._invalidations = {}

    with pytest.raises(
        ParticipantStrengthError,
        match="rating snapshot lacks product-owned origin authority",
    ):
        emit_registered_strength_forecast(
            registry=registry,
            artifact_store=artifacts,
            opponent_store=authority,
            evidence=evidence,
            model_version_id="model-lineage",
            strategy_version_id="strategy-lineage",
            quote_key="event-forged:match-winner:participant-a",
        )


def _authority_for(evidence: StrengthSnapshotPair) -> OpponentIntelligenceStore:
    authority = object.__new__(OpponentIntelligenceStore)
    authority._ratings = {
        evidence.subject.snapshot_id: evidence.subject,
        evidence.opponent.snapshot_id: evidence.opponent,
    }
    authority._invalidations = {}
    return authority


def test_registered_forecast_rejects_same_id_different_rating_payload(tmp_path) -> None:
    registry, artifacts = _registered_strength_lineage(
        tmp_path,
        dataset_cutoff=T3,
        dataset_available_at=T3,
        model_created_at=T3,
        strategy_created_at=T3,
    )
    canonical = _pair(decision_at=T4)
    authority = _authority_for(canonical)
    substituted = StrengthSnapshotPair(
        replace(canonical.subject, rating="0.99"),
        canonical.opponent,
        canonical.decision_at,
    )

    with pytest.raises(
        ParticipantStrengthError,
        match="subject rating snapshot does not match product-owned evidence",
    ):
        emit_registered_strength_forecast(
            registry=registry,
            artifact_store=artifacts,
            opponent_store=authority,
            evidence=substituted,
            model_version_id="model-lineage",
            strategy_version_id="strategy-lineage",
            quote_key="event-substitution:match-winner:participant-a",
        )


def test_rating_snapshot_invalidation_is_causal_not_retroactive() -> None:
    evidence = _pair(decision_at=T4)
    authority = _authority_for(evidence)
    invalidation = DownstreamInvalidation(
        invalidation_id="9" * 64,
        target_kind=InvalidationTarget.RATING_SNAPSHOT,
        target_id=evidence.subject.snapshot_id,
        reason=InvalidationReason.OUTCOME_CORRECTION,
        evidence_id="8" * 64,
        detected_at=T5,
        recompute_status=RecomputeStatus.REQUIRED,
    )
    authority._invalidations = {invalidation.invalidation_id: invalidation}

    assert (
        authority.resolve_rating_snapshot(
            evidence.subject.snapshot_id,
            as_of=T4,
        )
        == evidence.subject
    )
    with pytest.raises(
        OpponentIntelligenceError,
        match="rating snapshot was invalidated by the decision time",
    ):
        authority.resolve_rating_snapshot(
            evidence.subject.snapshot_id,
            as_of=T5,
        )
