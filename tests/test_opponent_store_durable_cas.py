from __future__ import annotations

import pytest

from autosport.learning_environment import EvidenceTruth
from autosport.opponent_intelligence import (
    ObservedPerformance,
    OpponentIntelligenceError,
    OpponentIntelligenceStore,
)
from autosport.participant_identity import (
    AliasRecord,
    EntityIdentity,
    EntityKind,
    ParticipantIdentityRegistry,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
T0 = "2026-09-20T08:00:00Z"
T1 = "2026-09-20T10:00:00Z"


def _entity(entity_id: str, kind: EntityKind) -> EntityIdentity:
    return EntityIdentity(
        entity_id,
        kind,
        f"provider:{entity_id}",
        SHA_A,
        T0,
        T0,
    )


def _alias(text: str, entity_id: str) -> AliasRecord:
    return AliasRecord(
        "provider-a",
        text,
        entity_id,
        T0,
        None,
        T0,
        SHA_A,
        T0,
    )


def _identity(tmp_path) -> ParticipantIdentityRegistry:
    identity = ParticipantIdentityRegistry.initialize_pristine(
        tmp_path / "participant-identity.json"
    )
    for entity in (
        _entity("p-alex", EntityKind.PARTICIPANT),
        _entity("p-blair", EntityKind.PARTICIPANT),
        _entity("p-casey", EntityKind.PARTICIPANT),
        _entity("league-tour-a", EntityKind.LEAGUE),
    ):
        identity.add_entity(entity)
    for alias in (
        _alias("Alex", "p-alex"),
        _alias("Blair", "p-blair"),
        _alias("Casey", "p-casey"),
        _alias("Tour A", "league-tour-a"),
    ):
        identity.add_alias(alias)
    return identity


def _performance(
    event_id: str,
    opponent_alias: str,
    evidence_sha256: str,
) -> ObservedPerformance:
    return ObservedPerformance(
        event_id=event_id,
        source_id="provider-a",
        subject_alias="Alex",
        opponent_alias=opponent_alias,
        sport_id="tennis",
        league_alias="Tour A",
        market_context_id="match-outcome",
        score="1",
        observed_at=T1,
        available_at=T1,
        recorded_at=T1,
        evidence_sha256=evidence_sha256,
        truth=EvidenceTruth.OBSERVED,
    )


def test_independent_stale_store_cannot_erase_intervening_commit(tmp_path):
    identity = _identity(tmp_path)
    path = tmp_path / "opponent-intelligence.json"
    first = OpponentIntelligenceStore.initialize_pristine(path, identity)
    stale = OpponentIntelligenceStore(path, identity)

    committed = first.record_performance(
        _performance("event-1", "Blair", SHA_A)
    )

    with pytest.raises(OpponentIntelligenceError, match="durable root changed"):
        stale._persist()

    reopened = OpponentIntelligenceStore(path, identity)
    assert tuple(reopened._performances) == (committed.performance_id,)


def test_store_rebinds_root_after_each_successful_publication(tmp_path):
    identity = _identity(tmp_path)
    path = tmp_path / "opponent-intelligence.json"
    store = OpponentIntelligenceStore.initialize_pristine(path, identity)

    first = store.record_performance(
        _performance("event-1", "Blair", SHA_A)
    )
    second = store.record_performance(
        _performance("event-2", "Casey", SHA_B)
    )

    reopened = OpponentIntelligenceStore(path, identity)
    assert set(reopened._performances) == {
        first.performance_id,
        second.performance_id,
    }
