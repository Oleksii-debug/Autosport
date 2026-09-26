from __future__ import annotations

from hashlib import sha256
import json

import pytest

from autosport.learning_environment import EvidenceTruth
from autosport.opponent_intelligence import (
    ObservedPerformance,
    OpponentIntelligenceStore,
)
from autosport.participant_identity import (
    AliasRecord,
    EntityIdentity,
    EntityKind,
    ParticipantIdentityRegistry,
)
from autosport.sport_memory_checkpoint import (
    SportMemoryCheckpointError,
    initialize_or_open_bound_sport_memory_runtime,
    load_verified_sport_memory_authority_checkpoint,
    open_bound_sport_memory_runtime,
)
from autosport.sport_memory_runtime import SportMemoryRuntime, SportMemoryScope


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


def _performance(
    *,
    event_id: str = "event-1",
    opponent_alias: str = "Blair",
    score: str = "1",
    evidence_sha256: str = SHA_A,
) -> ObservedPerformance:
    return ObservedPerformance(
        event_id=event_id,
        source_id="provider-a",
        subject_alias="Alex",
        opponent_alias=opponent_alias,
        sport_id="tennis",
        league_alias="Tour A",
        market_context_id="match-outcome",
        score=score,
        observed_at=T1,
        available_at=T1,
        recorded_at=T1,
        evidence_sha256=evidence_sha256,
        truth=EvidenceTruth.OBSERVED,
    )


def _canonical_stores(tmp_path, *, populated: bool = False):
    identity_path = tmp_path / "participant-identity.json"
    opponent_path = tmp_path / "opponent-intelligence.json"
    identity = ParticipantIdentityRegistry.initialize_pristine(identity_path)
    if populated:
        for item in (
            _entity("p-alex", EntityKind.PARTICIPANT),
            _entity("p-blair", EntityKind.PARTICIPANT),
            _entity("p-casey", EntityKind.PARTICIPANT),
            _entity("league-tour-a", EntityKind.LEAGUE),
        ):
            identity.add_entity(item)
        for alias in (
            _alias("Alex", "p-alex"),
            _alias("Blair", "p-blair"),
            _alias("Casey", "p-casey"),
            _alias("Tour A", "league-tour-a"),
        ):
            identity.add_alias(alias)
    opponent = OpponentIntelligenceStore.initialize_pristine(
        opponent_path, identity
    )
    if populated:
        opponent.record_performance(_performance())
    return identity, opponent


def _paths(tmp_path):
    return (
        tmp_path / "sport-memory-authority.json",
        tmp_path / "sport-memory.json",
    )


def _scope() -> SportMemoryScope:
    return SportMemoryScope(
        sport_id="tennis",
        league_entity_id="league-tour-a",
        market_context_id="match-outcome",
    )


def test_bound_runtime_captures_exact_roots_and_reopens_same_generation(tmp_path):
    identity, opponent = _canonical_stores(tmp_path)
    checkpoint_path, runtime_path = _paths(tmp_path)

    runtime = initialize_or_open_bound_sport_memory_runtime(
        runtime_path,
        checkpoint_path,
        identity,
        opponent,
    )
    checkpoint = load_verified_sport_memory_authority_checkpoint(
        checkpoint_path,
        identity,
        opponent,
    )

    assert runtime.authority_generation_sha256 == checkpoint.generation_sha256

    reopened_identity = ParticipantIdentityRegistry(identity.path)
    reopened_opponent = OpponentIntelligenceStore(
        opponent.path, reopened_identity
    )
    reopened = open_bound_sport_memory_runtime(
        runtime_path,
        checkpoint_path,
        reopened_identity,
        reopened_opponent,
    )

    assert reopened.authority_generation_sha256 == checkpoint.generation_sha256
    assert reopened.authority_generation_sha256 == runtime.authority_generation_sha256


def test_bound_runtime_consumes_fresh_authority_after_canonical_files_change(tmp_path):
    identity, opponent = _canonical_stores(tmp_path)

    # Change both canonical persisted files after the caller objects were loaded,
    # while keeping each file independently valid. The bound runtime must not
    # pair the persisted authority with stale caller-owned in-memory objects.
    for path in (identity.path, opponent.path):
        raw = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(
            json.dumps(raw, sort_keys=True, indent=4) + "\n",
            encoding="utf-8",
        )

    checkpoint_path, runtime_path = _paths(tmp_path)
    runtime = initialize_or_open_bound_sport_memory_runtime(
        runtime_path,
        checkpoint_path,
        identity,
        opponent,
    )
    checkpoint = load_verified_sport_memory_authority_checkpoint(
        checkpoint_path,
        identity,
        opponent,
    )

    assert runtime.opponent_authority is not opponent
    assert isinstance(runtime.opponent_authority, OpponentIntelligenceStore)
    assert runtime.opponent_authority.identity_registry is not identity
    assert runtime.opponent_authority.path == opponent.path
    assert checkpoint.identity_root_sha256 == sha256(identity.path.read_bytes()).hexdigest()
    assert (
        load_verified_sport_memory_authority_checkpoint(
            checkpoint_path, identity, opponent
        )
        == checkpoint
    )


def test_valid_identity_rollback_is_rejected_before_sport_memory_reopen(tmp_path):
    identity, opponent = _canonical_stores(tmp_path)
    old_identity_bytes = identity.path.read_bytes()

    identity.add_entity(
        EntityIdentity(
            entity_id="participant-1",
            kind=EntityKind.PARTICIPANT,
            source_reference="source:participant-1",
            evidence_sha256=SHA_A,
            first_known_at="2026-09-20T10:00:00Z",
            available_at="2026-09-20T10:00:00Z",
        )
    )
    checkpoint_path, runtime_path = _paths(tmp_path)
    initialize_or_open_bound_sport_memory_runtime(
        runtime_path,
        checkpoint_path,
        identity,
        opponent,
    )

    # Restore a semantically valid earlier identity file. The opponent store is
    # still independently valid, so local file validation alone cannot detect
    # that this pair is no longer the committed cross-store generation.
    identity.path.write_bytes(old_identity_bytes)
    rolled_back_identity = ParticipantIdentityRegistry(identity.path)
    reopened_opponent = OpponentIntelligenceStore(
        opponent.path, rolled_back_identity
    )

    with pytest.raises(
        SportMemoryCheckpointError,
        match="identity root drift/rollback",
    ):
        open_bound_sport_memory_runtime(
            runtime_path,
            checkpoint_path,
            rolled_back_identity,
            reopened_opponent,
        )


def test_opponent_formatting_drift_does_not_redefine_source_generation(tmp_path):
    identity, opponent = _canonical_stores(tmp_path, populated=True)
    checkpoint_path, runtime_path = _paths(tmp_path)
    runtime = initialize_or_open_bound_sport_memory_runtime(
        runtime_path,
        checkpoint_path,
        identity,
        opponent,
    )
    generation = runtime.authority_generation_sha256

    # Byte-only reserialization is not a source-authority transition. The
    # canonical source projection must remain stable even though the file hash
    # changes, while the store still passes its own validation boundary.
    raw = json.loads(opponent.path.read_text(encoding="utf-8"))
    opponent.path.write_text(
        json.dumps(raw, sort_keys=True, indent=4) + "\n",
        encoding="utf-8",
    )
    reopened_identity = ParticipantIdentityRegistry(identity.path)
    rewritten_opponent = OpponentIntelligenceStore(
        opponent.path, reopened_identity
    )
    reopened = open_bound_sport_memory_runtime(
        runtime_path,
        checkpoint_path,
        reopened_identity,
        rewritten_opponent,
    )

    assert reopened.authority_generation_sha256 == generation


def test_canonical_opponent_source_drift_is_rejected(tmp_path):
    identity, opponent = _canonical_stores(tmp_path, populated=True)
    checkpoint_path, runtime_path = _paths(tmp_path)
    initialize_or_open_bound_sport_memory_runtime(
        runtime_path,
        checkpoint_path,
        identity,
        opponent,
    )

    # A new observed performance is genuine upstream evidence. It must change the
    # source-authority projection and cannot be consumed under the old generation.
    opponent.record_performance(
        _performance(
            event_id="event-2",
            opponent_alias="Casey",
            score="0",
            evidence_sha256=SHA_B,
        )
    )
    reopened_identity = ParticipantIdentityRegistry(identity.path)
    changed_opponent = OpponentIntelligenceStore(
        opponent.path, reopened_identity
    )

    with pytest.raises(
        SportMemoryCheckpointError,
        match="opponent intelligence root drift/rollback",
    ):
        open_bound_sport_memory_runtime(
            runtime_path,
            checkpoint_path,
            reopened_identity,
            changed_opponent,
        )


def test_materialize_derived_snapshots_preserves_bound_source_generation(tmp_path):
    identity, opponent = _canonical_stores(tmp_path, populated=True)
    checkpoint_path, runtime_path = _paths(tmp_path)
    runtime = initialize_or_open_bound_sport_memory_runtime(
        runtime_path,
        checkpoint_path,
        identity,
        opponent,
    )
    checkpoint = load_verified_sport_memory_authority_checkpoint(
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
    raw = json.loads(opponent.path.read_text(encoding="utf-8"))
    assert artifact.rating_snapshot_id in {
        item["snapshot_id"] for item in raw["rating_snapshots"]
    }
    assert artifact.feature_snapshot_id in {
        item["snapshot_id"] for item in raw["feature_snapshots"]
    }

    # build_snapshots durably wrote derived cache output into the opponent file.
    # That must not invalidate the immutable upstream generation that authorized
    # this artifact; a fresh process must reopen the bound runtime successfully.
    reopened_identity = ParticipantIdentityRegistry(identity.path)
    reopened_opponent = OpponentIntelligenceStore(
        opponent.path, reopened_identity
    )
    reopened = open_bound_sport_memory_runtime(
        runtime_path,
        checkpoint_path,
        reopened_identity,
        reopened_opponent,
    )

    assert reopened.authority_generation_sha256 == checkpoint.generation_sha256
    assert reopened.get(artifact.memory_id) == artifact
    assert (
        load_verified_sport_memory_authority_checkpoint(
            checkpoint_path, reopened_identity, reopened_opponent
        )
        == checkpoint
    )


def test_missing_derived_snapshots_fail_closed_under_same_source_generation(tmp_path):
    identity, opponent = _canonical_stores(tmp_path, populated=True)
    checkpoint_path, runtime_path = _paths(tmp_path)
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

    # Removing only derived cache records leaves performances/invalidations, and
    # therefore the immutable source generation, unchanged. The bound runtime must
    # still reject restart because its durable artifact explicitly references the
    # removed content-addressed snapshots.
    raw = json.loads(opponent.path.read_text(encoding="utf-8"))
    raw["rating_snapshots"] = [
        item
        for item in raw["rating_snapshots"]
        if item["snapshot_id"] != artifact.rating_snapshot_id
    ]
    raw["feature_snapshots"] = [
        item
        for item in raw["feature_snapshots"]
        if item["snapshot_id"] != artifact.feature_snapshot_id
    ]
    opponent.path.write_text(
        json.dumps(raw, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    reopened_identity = ParticipantIdentityRegistry(identity.path)
    pruned_opponent = OpponentIntelligenceStore(
        opponent.path, reopened_identity
    )
    with pytest.raises(
        SportMemoryCheckpointError,
        match="missing canonical opponent snapshot",
    ):
        open_bound_sport_memory_runtime(
            runtime_path,
            checkpoint_path,
            reopened_identity,
            pruned_opponent,
        )


def test_checkpoint_parser_rejects_unknown_fields_and_boolean_version(tmp_path):
    identity, opponent = _canonical_stores(tmp_path)
    checkpoint_path, runtime_path = _paths(tmp_path)
    initialize_or_open_bound_sport_memory_runtime(
        runtime_path,
        checkpoint_path,
        identity,
        opponent,
    )
    original = json.loads(checkpoint_path.read_text(encoding="utf-8"))

    with_unknown = dict(original)
    with_unknown["caller_claim"] = "trusted"
    checkpoint_path.write_text(
        json.dumps(with_unknown, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(
        SportMemoryCheckpointError,
        match="unexpected fields",
    ):
        load_verified_sport_memory_authority_checkpoint(
            checkpoint_path, identity, opponent
        )

    boolean_version = dict(original)
    boolean_version["version"] = True
    checkpoint_path.write_text(
        json.dumps(boolean_version, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(
        SportMemoryCheckpointError,
        match="unsupported.*version",
    ):
        load_verified_sport_memory_authority_checkpoint(
            checkpoint_path, identity, opponent
        )



def test_checkpoint_restart_rejects_duplicate_authority_root_key(tmp_path):
    identity, opponent = _canonical_stores(tmp_path)
    checkpoint_path, runtime_path = _paths(tmp_path)
    initialize_or_open_bound_sport_memory_runtime(
        runtime_path,
        checkpoint_path,
        identity,
        opponent,
    )
    payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint_path.write_text(
        (
            "{"
            f'"schema":{json.dumps(payload["schema"])},'
            f'"version":{payload["version"]},'
            f'"identity_root_sha256":{json.dumps("0" * 64)},'
            f'"identity_root_sha256":{json.dumps(payload["identity_root_sha256"])},'
            f'"opponent_root_sha256":{json.dumps(payload["opponent_root_sha256"])},'
            f'"generation_sha256":{json.dumps(payload["generation_sha256"])}'
            "}\n"
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        SportMemoryCheckpointError,
        match="cannot load sport-memory authority checkpoint",
    ):
        load_verified_sport_memory_authority_checkpoint(
            checkpoint_path, identity, opponent
        )


def test_opponent_source_projection_rejects_duplicate_performances_key(tmp_path):
    identity, opponent = _canonical_stores(tmp_path, populated=True)
    checkpoint_path, runtime_path = _paths(tmp_path)
    initialize_or_open_bound_sport_memory_runtime(
        runtime_path,
        checkpoint_path,
        identity,
        opponent,
    )

    raw = opponent.path.read_text(encoding="utf-8")
    assert '"performances": [' in raw
    opponent.path.write_text(
        raw.replace(
            '"performances": [',
            '"performances": [],\n  "performances": [',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        SportMemoryCheckpointError,
        match="cannot read opponent canonical store",
    ):
        load_verified_sport_memory_authority_checkpoint(
            checkpoint_path, identity, opponent
        )




def test_checkpoint_restart_normalizes_invalid_utf8_to_checkpoint_error(tmp_path):
    identity, opponent = _canonical_stores(tmp_path)
    checkpoint_path, runtime_path = _paths(tmp_path)
    initialize_or_open_bound_sport_memory_runtime(
        runtime_path,
        checkpoint_path,
        identity,
        opponent,
    )
    checkpoint_path.write_bytes(b'{"schema":"sport-memory-authority-checkpoint","bad":"\xff"}')

    with pytest.raises(
        SportMemoryCheckpointError,
        match="cannot load sport-memory authority checkpoint",
    ):
        load_verified_sport_memory_authority_checkpoint(
            checkpoint_path, identity, opponent
        )


def test_opponent_source_projection_normalizes_invalid_utf8_to_checkpoint_error(tmp_path):
    identity, opponent = _canonical_stores(tmp_path, populated=True)
    checkpoint_path, runtime_path = _paths(tmp_path)
    initialize_or_open_bound_sport_memory_runtime(
        runtime_path,
        checkpoint_path,
        identity,
        opponent,
    )
    opponent.path.write_bytes(b'{"performances":["\xff"]}')

    with pytest.raises(
        SportMemoryCheckpointError,
        match="cannot read opponent canonical store",
    ):
        load_verified_sport_memory_authority_checkpoint(
            checkpoint_path, identity, opponent
        )



def test_checkpoint_rejects_store_bound_to_different_registry_object(tmp_path):
    identity, opponent = _canonical_stores(tmp_path)
    other_identity = ParticipantIdentityRegistry.initialize_pristine(
        tmp_path / "other-identity.json"
    )
    checkpoint_path, runtime_path = _paths(tmp_path)

    with pytest.raises(
        SportMemoryCheckpointError,
        match="exact supplied identity registry",
    ):
        initialize_or_open_bound_sport_memory_runtime(
            runtime_path,
            checkpoint_path,
            other_identity,
            opponent,
        )


def test_existing_runtime_without_checkpoint_cannot_self_attest_generation(tmp_path):
    identity, opponent = _canonical_stores(tmp_path)
    checkpoint_path, runtime_path = _paths(tmp_path)
    SportMemoryRuntime.initialize_pristine(
        runtime_path,
        opponent,
        authority_generation_sha256=SHA_A,
    )

    with pytest.raises(
        SportMemoryCheckpointError,
        match="runtime exists without canonical authority checkpoint",
    ):
        initialize_or_open_bound_sport_memory_runtime(
            runtime_path,
            checkpoint_path,
            identity,
            opponent,
        )


def _bound_runtime_for_binding_tamper(root):
    root.mkdir()
    identity, opponent = _canonical_stores(root)
    checkpoint_path, runtime_path = _paths(root)
    runtime = initialize_or_open_bound_sport_memory_runtime(
        runtime_path,
        checkpoint_path,
        identity,
        opponent,
    )
    return runtime, identity, opponent, checkpoint_path, runtime_path


def test_bound_runtime_rejects_post_bind_authority_reassignment(tmp_path):
    runtime, identity, opponent, checkpoint_path, runtime_path = (
        _bound_runtime_for_binding_tamper(tmp_path / "ordinary-reassign")
    )

    attempts = {
        "path": tmp_path / "redirected-runtime.json",
        "authority_generation_sha256": SHA_B,
        "opponent_authority": opponent,
        "_bound_checkpoint_path": tmp_path / "redirected-checkpoint.json",
        "_bound_identity_selector": identity,
        "_bound_opponent_selector": opponent,
    }
    for name, value in attempts.items():
        with pytest.raises(
            SportMemoryCheckpointError,
            match="authority binding is immutable",
        ):
            setattr(runtime, name, value)

    assert runtime.path == runtime_path
    assert runtime._bound_checkpoint_path == checkpoint_path


@pytest.mark.parametrize(
    "field,value_kind",
    [
        ("path", "path"),
        ("authority_generation_sha256", "generation"),
        ("opponent_authority", "object"),
    ],
)
def test_bound_runtime_detects_direct_base_field_injection(
    tmp_path, field, value_kind
):
    runtime, _, _, _, _ = _bound_runtime_for_binding_tamper(
        tmp_path / f"dict-{field}"
    )
    if value_kind == "path":
        value = tmp_path / "redirected-runtime.json"
    elif value_kind == "generation":
        value = SHA_B
    else:
        value = object()

    runtime.__dict__[field] = value
    with pytest.raises(SportMemoryCheckpointError, match="binding changed"):
        runtime._persist()


def test_bound_runtime_detects_direct_slot_name_injection(tmp_path):
    runtime, _, _, _, _ = _bound_runtime_for_binding_tamper(
        tmp_path / "dict-slot"
    )
    runtime.__dict__["_bound_checkpoint_path"] = tmp_path / "forged.json"

    with pytest.raises(
        SportMemoryCheckpointError,
        match="direct binding injection|instance authority shadow",
    ):
        runtime._persist()


def test_bound_runtime_detects_identity_selector_path_drift(tmp_path):
    runtime, identity, _, _, _ = _bound_runtime_for_binding_tamper(
        tmp_path / "identity-selector-drift"
    )
    identity.path = tmp_path / "other-identity.json"

    with pytest.raises(
        SportMemoryCheckpointError,
        match="identity selector path changed",
    ):
        runtime._persist()


def test_bound_runtime_detects_opponent_selector_path_drift(tmp_path):
    runtime, _, opponent, _, _ = _bound_runtime_for_binding_tamper(
        tmp_path / "opponent-selector-drift"
    )
    opponent.path = tmp_path / "other-opponent.json"

    with pytest.raises(
        SportMemoryCheckpointError,
        match="opponent selector path changed",
    ):
        runtime._persist()


def test_bound_runtime_cannot_be_redirected_to_second_authority_lineage(tmp_path):
    runtime, _, _, _, _ = _bound_runtime_for_binding_tamper(
        tmp_path / "lineage-a"
    )
    _, identity_b, opponent_b, checkpoint_b, _ = _bound_runtime_for_binding_tamper(
        tmp_path / "lineage-b"
    )

    for name, value in (
        ("_bound_checkpoint_path", checkpoint_b),
        ("_bound_identity_selector", identity_b),
        ("_bound_opponent_selector", opponent_b),
        ("opponent_authority", opponent_b),
    ):
        with pytest.raises(
            SportMemoryCheckpointError,
            match="authority binding is immutable",
        ):
            setattr(runtime, name, value)


def test_verified_refresh_remains_the_only_post_bind_opponent_replacement(tmp_path):
    runtime, _, _, _, _ = _bound_runtime_for_binding_tamper(
        tmp_path / "verified-refresh"
    )
    before = runtime.opponent_authority

    verified = runtime._refresh_bound_authority()

    assert verified is runtime.opponent_authority
    assert runtime.opponent_authority is not before
    runtime._persist()
