from __future__ import annotations

from hashlib import sha256
import json

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
    BoundSportMemoryRuntime,
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


def _digest(payload: object) -> str:
    raw = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return sha256(raw.encode("utf-8")).hexdigest()


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


def _scope() -> SportMemoryScope:
    return SportMemoryScope(
        sport_id="tennis",
        league_entity_id="league-tour-a",
        market_context_id="match-outcome",
    )


def _stores(tmp_path):
    identity = ParticipantIdentityRegistry.initialize_pristine(
        tmp_path / "participant-identity.json"
    )
    for item in (
        _entity("p-alex", EntityKind.PARTICIPANT),
        _entity("p-blair", EntityKind.PARTICIPANT),
        _entity("league-tour-a", EntityKind.LEAGUE),
    ):
        identity.add_entity(item)
    for alias in (
        _alias("Alex", "p-alex"),
        _alias("Blair", "p-blair"),
        _alias("Tour A", "league-tour-a"),
    ):
        identity.add_alias(alias)

    opponent = OpponentIntelligenceStore.initialize_pristine(
        tmp_path / "opponent-intelligence.json",
        identity,
    )
    opponent.record_performance(
        ObservedPerformance(
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
    )
    return identity, opponent


def _bound_runtime(tmp_path):
    identity, opponent = _stores(tmp_path)
    checkpoint_path = tmp_path / "sport-memory-authority.json"
    runtime_path = tmp_path / "sport-memory.json"
    runtime = initialize_or_open_bound_sport_memory_runtime(
        runtime_path,
        checkpoint_path,
        identity,
        opponent,
    )
    assert type(runtime) is BoundSportMemoryRuntime
    return identity, opponent, checkpoint_path, runtime_path, runtime


def _materialize(runtime: BoundSportMemoryRuntime):
    return runtime.materialize(
        participant_entity_id="p-alex",
        scope=_scope(),
        causal_cutoff=T2,
        published_at=T3,
        code_sha256=SHA_A,
        dependency_sha256=SHA_B,
        min_support=1,
    )


def test_bound_materialization_refreshes_concrete_canonical_authority(tmp_path):
    _, opponent, _, _, runtime = _bound_runtime(tmp_path)
    artifact = _materialize(runtime)

    # A caller may mutate the public low-level attribute, but the product-owned
    # bound runtime must replace it with a freshly verified concrete store before
    # any positive materialization can run.
    runtime.opponent_authority = object()
    duplicate = _materialize(runtime)

    assert duplicate == artifact
    assert type(runtime.opponent_authority) is OpponentIntelligenceStore
    persisted = json.loads(opponent.path.read_text(encoding="utf-8"))
    assert artifact.rating_snapshot_id in {
        item["snapshot_id"] for item in persisted["rating_snapshots"]
    }
    assert artifact.feature_snapshot_id in {
        item["snapshot_id"] for item in persisted["feature_snapshots"]
    }


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("rating", "0.123456789"),
        ("uncertainty", "999"),
        ("identity_view", "RESTATED_RESEARCH"),
    ],
)
def test_bound_reopen_rejects_recomputed_artifact_projection_tamper(
    tmp_path,
    field,
    replacement,
):
    identity, opponent, checkpoint_path, runtime_path, runtime = _bound_runtime(tmp_path)
    _materialize(runtime)

    raw = json.loads(runtime_path.read_text(encoding="utf-8"))
    artifact = raw["artifacts"][0]
    artifact[field] = replacement
    artifact["memory_id"] = _digest(
        {key: value for key, value in artifact.items() if key != "memory_id"}
    )
    runtime_path.write_text(json.dumps(raw, sort_keys=True) + "\n", encoding="utf-8")

    reopened_identity = ParticipantIdentityRegistry(identity.path)
    reopened_opponent = OpponentIntelligenceStore(
        opponent.path,
        reopened_identity,
    )
    with pytest.raises(
        SportMemoryCheckpointError,
        match="canonical opponent snapshot binding mismatch",
    ):
        open_bound_sport_memory_runtime(
            runtime_path,
            checkpoint_path,
            reopened_identity,
            reopened_opponent,
        )
