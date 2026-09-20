from __future__ import annotations

import pytest

from autosport.opponent_intelligence import (
    InvalidationReason,
    InvalidationTarget,
    OpponentIntelligenceError,
    OpponentIntelligenceStore,
)
from autosport.participant_identity import ParticipantIdentityRegistry


SHA_A = "a" * 64
SHA_B = "b" * 64
T0 = "2026-09-20T10:00:00Z"


def test_independent_stale_store_cannot_erase_intervening_commit(tmp_path):
    identity = ParticipantIdentityRegistry.initialize_pristine(
        tmp_path / "participant-identity.json"
    )
    path = tmp_path / "opponent-intelligence.json"
    first = OpponentIntelligenceStore.initialize_pristine(path, identity)
    stale = OpponentIntelligenceStore(path, identity)

    invalidation = first._make_invalidation(
        InvalidationTarget.OPPONENT_EDGE,
        SHA_A,
        InvalidationReason.OUTCOME_CORRECTION,
        SHA_B,
        T0,
    )
    committed = {invalidation.invalidation_id: invalidation}
    first._persist_state(invalidations=committed)
    first._invalidations = committed

    with pytest.raises(OpponentIntelligenceError, match="durable root changed"):
        stale._persist()

    reopened = OpponentIntelligenceStore(path, identity)
    assert reopened.invalidations() == (invalidation,)


def test_store_rebinds_root_after_each_successful_publication(tmp_path):
    identity = ParticipantIdentityRegistry.initialize_pristine(
        tmp_path / "participant-identity.json"
    )
    path = tmp_path / "opponent-intelligence.json"
    store = OpponentIntelligenceStore.initialize_pristine(path, identity)

    first = store._make_invalidation(
        InvalidationTarget.OPPONENT_EDGE,
        SHA_A,
        InvalidationReason.OUTCOME_CORRECTION,
        SHA_B,
        T0,
    )
    state = {first.invalidation_id: first}
    store._persist_state(invalidations=state)
    store._invalidations = state

    second = store._make_invalidation(
        InvalidationTarget.RATING_SNAPSHOT,
        SHA_B,
        InvalidationReason.IDENTITY_CORRECTION,
        SHA_A,
        T0,
    )
    state = {**state, second.invalidation_id: second}
    store._persist_state(invalidations=state)
    store._invalidations = state

    reopened = OpponentIntelligenceStore(path, identity)
    assert reopened.invalidations() == tuple(
        sorted(
            (first, second),
            key=lambda item: (
                item.detected_at,
                item.target_kind.value,
                item.target_id,
                item.invalidation_id,
            ),
        )
    )
