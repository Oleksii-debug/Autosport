from __future__ import annotations

import pytest

from autosport.learning_environment import EvidenceTruth
from autosport.opponent_intelligence import ObservedPerformance, OpponentIntelligenceStore
from autosport.participant_identity import (
    AliasRecord,
    EntityIdentity,
    EntityKind,
    ParticipantIdentityRegistry,
)
from autosport.sport_memory_checkpoint import (
    SportMemoryCheckpointError,
    initialize_or_open_bound_sport_memory_runtime,
    open_bound_sport_memory_runtime,
)
from autosport.sport_memory_runtime import (
    SportMemoryError,
    SportMemoryRuntime,
    SportMemoryScope,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
T0 = "2026-09-19T08:00:00Z"
T1 = "2026-09-20T09:00:00Z"
T2 = "2026-09-20T10:00:00Z"
T3 = "2026-09-20T10:00:01Z"
T4 = "2026-09-20T10:00:02Z"
T5 = "2026-09-20T10:00:03Z"


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


def _performance() -> ObservedPerformance:
    return ObservedPerformance(
        event_id="event-1",
        source_id="provider-a",
        subject_alias="Alex",
        opponent_alias="Blair",
        sport_id="tennis",
        league_alias="Tour A",
        market_context_id="match-outcome",
        score="1",
        observed_at=T1,
        available_at=T1,
        recorded_at=T1,
        evidence_sha256=SHA_A,
        truth=EvidenceTruth.OBSERVED,
    )


def _scope() -> SportMemoryScope:
    return SportMemoryScope(
        sport_id="tennis",
        league_entity_id="league-tour-a",
        market_context_id="match-outcome",
    )


def _bound_runtime(tmp_path):
    identity = ParticipantIdentityRegistry.initialize_pristine(
        tmp_path / "participant-identity.json"
    )
    for entity in (
        _entity("p-alex", EntityKind.PARTICIPANT),
        _entity("p-blair", EntityKind.PARTICIPANT),
        _entity("league-tour-a", EntityKind.LEAGUE),
    ):
        identity.add_entity(entity)
    for alias in (
        _alias("Alex", "p-alex"),
        _alias("Blair", "p-blair"),
        _alias("Tour A", "league-tour-a"),
    ):
        identity.add_alias(alias)

    opponent = OpponentIntelligenceStore.initialize_pristine(
        tmp_path / "opponent-intelligence.json", identity
    )
    opponent.record_performance(_performance())

    runtime_path = tmp_path / "sport-memory.json"
    checkpoint_path = tmp_path / "sport-memory-authority.json"
    runtime = initialize_or_open_bound_sport_memory_runtime(
        runtime_path,
        checkpoint_path,
        identity,
        opponent,
    )
    artifact = runtime.materialize(
        participant_entity_id="p-alex",
        scope=_scope(),
        causal_cutoff=T2,
        published_at=T3,
        code_sha256=SHA_A,
        dependency_sha256=SHA_B,
        min_support=1,
    )
    return identity, opponent, checkpoint_path, runtime_path, runtime, artifact


def test_bound_consumption_requires_current_generation_without_persisting(tmp_path):
    identity, _, _, runtime_path, runtime, artifact = _bound_runtime(tmp_path)
    durable_before = runtime_path.read_bytes()

    # An explicit base dispatch on the exact product-bound instance must not skip
    # the bound consumption transaction wrapper.
    with pytest.raises(SportMemoryError, match="canonical generation verification"):
        SportMemoryRuntime.record_consumption(
            runtime,
            decision_id="decision-base-bypass",
            memory_id=artifact.memory_id,
            decision_cutoff=T4,
            consumed_at=T5,
            expected_scope=_scope(),
        )
    assert runtime.consumptions_for_artifact(artifact.memory_id) == ()
    assert runtime_path.read_bytes() == durable_before

    # Advance canonical identity after the artifact was issued. The already-open
    # bound runtime must not authorize a new decision consumption under old roots.
    newer_identity = ParticipantIdentityRegistry(identity.path)
    newer_identity.add_entity(_entity("p-casey", EntityKind.PARTICIPANT))

    with pytest.raises(SportMemoryCheckpointError, match="root drift"):
        runtime.record_consumption(
            decision_id="decision-after-drift",
            memory_id=artifact.memory_id,
            decision_cutoff=T4,
            consumed_at=T5,
            expected_scope=_scope(),
        )

    assert runtime.consumptions_for_artifact(artifact.memory_id) == ()
    assert runtime_path.read_bytes() == durable_before


def test_reopened_public_base_runtime_cannot_persist_stale_consumption(tmp_path):
    identity, opponent, _, runtime_path, runtime, artifact = _bound_runtime(tmp_path)
    generation = runtime.authority_generation_sha256
    durable_before = runtime_path.read_bytes()

    # Drift the canonical identity root after the bound artifact was issued, then
    # reopen only the low-level durable file using the old readable generation.
    # This used to bypass BoundSportMemoryRuntime._refresh_bound_authority because
    # a fresh base object bound the current runtime-file root and could append.
    newer_identity = ParticipantIdentityRegistry(identity.path)
    newer_identity.add_entity(_entity("p-casey", EntityKind.PARTICIPANT))
    bypass = SportMemoryRuntime(
        runtime_path,
        opponent,
        authority_generation_sha256=generation,
    )

    with pytest.raises(SportMemoryError, match="canonical generation verification"):
        bypass.record_consumption(
            decision_id="decision-unbound-bypass",
            memory_id=artifact.memory_id,
            decision_cutoff=T4,
            consumed_at=T5,
            expected_scope=_scope(),
        )

    assert runtime_path.read_bytes() == durable_before


def test_second_open_runtime_reloads_and_preserves_first_consumption(tmp_path):
    identity, opponent, checkpoint_path, runtime_path, first, artifact = _bound_runtime(
        tmp_path
    )

    # Open a second product runtime at the exact same durable generation. Both
    # objects legitimately begin from the same runtime root R.
    second = open_bound_sport_memory_runtime(
        runtime_path,
        checkpoint_path,
        identity,
        opponent,
    )

    first_record = first.record_consumption(
        decision_id="decision-first",
        memory_id=artifact.memory_id,
        decision_cutoff=T4,
        consumed_at=T5,
        expected_scope=_scope(),
    )

    # The transaction repair replaces predecessor late-CAS rejection: a stale
    # bound runtime reloads the latest locked checkpoint, applies its
    # non-conflicting write, and preserves the already acknowledged first write.
    second_record = second.record_consumption(
        decision_id="decision-stale-second",
        memory_id=artifact.memory_id,
        decision_cutoff=T4,
        consumed_at=T5,
        expected_scope=_scope(),
    )

    second_records = {
        record.decision_id: record
        for record in second.consumptions_for_artifact(artifact.memory_id)
    }
    assert second_records == {
        "decision-first": first_record,
        "decision-stale-second": second_record,
    }

    reopened_identity = ParticipantIdentityRegistry(identity.path)
    reopened_opponent = OpponentIntelligenceStore(opponent.path, reopened_identity)
    reopened = open_bound_sport_memory_runtime(
        runtime_path,
        checkpoint_path,
        reopened_identity,
        reopened_opponent,
    )
    reopened_records = {
        record.decision_id: record
        for record in reopened.consumptions_for_artifact(artifact.memory_id)
    }
    assert reopened_records == second_records
