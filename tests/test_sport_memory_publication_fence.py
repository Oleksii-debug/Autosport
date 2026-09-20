from __future__ import annotations

from threading import Event, Thread, current_thread

import pytest

import autosport.opponent_intelligence as opponent_module
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
from autosport.sport_memory_runtime import SportMemoryScope


SHA_A = "a" * 64
SHA_B = "b" * 64
T0 = "2026-09-19T08:00:00Z"
T1 = "2026-09-20T09:00:00Z"
T2 = "2026-09-20T10:00:00Z"
T3 = "2026-09-20T10:00:01Z"


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


def _performance(event_id: str, opponent_alias: str, evidence: str) -> ObservedPerformance:
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
        evidence_sha256=evidence,
        truth=EvidenceTruth.OBSERVED,
    )


def _canonical_stores(tmp_path):
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
    opponent = OpponentIntelligenceStore.initialize_pristine(
        tmp_path / "opponent-intelligence.json", identity
    )
    opponent.record_performance(_performance("event-1", "Blair", SHA_A))
    return identity, opponent


def _scope() -> SportMemoryScope:
    return SportMemoryScope(
        sport_id="tennis",
        league_entity_id="league-tour-a",
        market_context_id="match-outcome",
    )


def test_bound_materialization_cannot_overwrite_concurrent_source_generation(
    tmp_path, monkeypatch
):
    identity, opponent = _canonical_stores(tmp_path)
    checkpoint_path = tmp_path / "sport-memory-authority.json"
    runtime_path = tmp_path / "sport-memory.json"
    runtime = initialize_or_open_bound_sport_memory_runtime(
        runtime_path,
        checkpoint_path,
        identity,
        opponent,
    )

    original_build = OpponentIntelligenceStore.build_snapshots
    original_atomic = opponent_module.atomic_write_json
    writer_loaded = Event()
    writer_publish_started = Event()
    writer_done = Event()
    writer_errors: list[BaseException] = []
    writer_holder: list[Thread] = []

    def instrumented_atomic(path, payload):
        if current_thread().name == "concurrent-source-writer":
            writer_publish_started.set()
        return original_atomic(path, payload)

    monkeypatch.setattr(opponent_module, "atomic_write_json", instrumented_atomic)

    def source_writer() -> None:
        try:
            fresh_identity = ParticipantIdentityRegistry(identity.path)
            fresh_opponent = OpponentIntelligenceStore(opponent.path, fresh_identity)
            writer_loaded.set()
            fresh_opponent.record_performance(
                _performance("event-2", "Casey", SHA_B)
            )
        except BaseException as exc:  # captured for the main test thread
            writer_errors.append(exc)
        finally:
            writer_done.set()

    def interleaving_build(self, **kwargs):
        if not writer_holder:
            writer = Thread(
                target=source_writer,
                name="concurrent-source-writer",
                daemon=True,
            )
            writer_holder.append(writer)
            writer.start()
            assert writer_loaded.wait(timeout=1.0)
            assert writer_publish_started.wait(timeout=1.0)
            # The competing whole-file source publisher has reached its atomic
            # publication route, but must remain fenced until this bound materialize
            # transaction publishes and verifies its derived snapshot projection.
            assert not writer_done.wait(timeout=0.05)
        return original_build(self, **kwargs)

    monkeypatch.setattr(
        OpponentIntelligenceStore,
        "build_snapshots",
        interleaving_build,
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
    assert artifact.support == 1

    writer = writer_holder[0]
    writer.join(timeout=2.0)
    assert not writer.is_alive()
    assert writer_done.is_set()
    assert writer_errors == []

    final_identity = ParticipantIdentityRegistry(identity.path)
    final_opponent = OpponentIntelligenceStore(opponent.path, final_identity)
    assert {
        record.observation.event_id
        for record in final_opponent._performances.values()
    } == {"event-1", "event-2"}

    # The later source generation is canonical and must make the old authority
    # checkpoint unusable instead of being silently erased by stale snapshot bytes.
    with pytest.raises(SportMemoryCheckpointError, match="root drift"):
        open_bound_sport_memory_runtime(
            runtime_path,
            checkpoint_path,
            final_identity,
            final_opponent,
        )
