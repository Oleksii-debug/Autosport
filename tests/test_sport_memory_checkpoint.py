from __future__ import annotations

import json

import pytest

from autosport.opponent_intelligence import OpponentIntelligenceStore
from autosport.participant_identity import (
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
from autosport.sport_memory_runtime import SportMemoryRuntime


SHA_A = "a" * 64


def _canonical_stores(tmp_path):
    identity_path = tmp_path / "participant-identity.json"
    opponent_path = tmp_path / "opponent-intelligence.json"
    identity = ParticipantIdentityRegistry.initialize_pristine(identity_path)
    opponent = OpponentIntelligenceStore.initialize_pristine(
        opponent_path, identity
    )
    return identity, opponent


def _paths(tmp_path):
    return (
        tmp_path / "sport-memory-authority.json",
        tmp_path / "sport-memory.json",
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


def test_valid_opponent_file_drift_is_rejected_even_when_semantics_parse(tmp_path):
    identity, opponent = _canonical_stores(tmp_path)
    checkpoint_path, runtime_path = _paths(tmp_path)
    initialize_or_open_bound_sport_memory_runtime(
        runtime_path,
        checkpoint_path,
        identity,
        opponent,
    )

    # Re-serialize the same valid opponent checkpoint with different bytes.
    # Exact persisted-root identity is intentional: a rewritten upstream root
    # must create a new common generation rather than silently reusing the old.
    raw = json.loads(opponent.path.read_text(encoding="utf-8"))
    opponent.path.write_text(
        json.dumps(raw, sort_keys=True, indent=4) + "\n",
        encoding="utf-8",
    )
    reopened_identity = ParticipantIdentityRegistry(identity.path)
    rewritten_opponent = OpponentIntelligenceStore(
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
            rewritten_opponent,
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
