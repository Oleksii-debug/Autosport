from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from autosport.integrity import atomic_write_json
from autosport.point_in_time_authority import (
    AvailabilityWitnessAuthority,
    RevisionPolicyAuthority,
    SourceRevisionAuthority,
    SourceRevisionAuthorityError,
    SourceRevisionAuthorityStore,
)


BASE = datetime(2026, 1, 1, tzinfo=UTC)
PROVIDER_SOURCE = SourceRevisionAuthorityStore.PROVIDER_SOURCE_ID
GENERIC_KIND = "caller-generic-v1"


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _policy(source: str, policy_id: str = "policy:v1") -> RevisionPolicyAuthority:
    payload = {
        "schema": "autosport.revision_availability_policy",
        "schema_version": 1,
        "source_identity": source,
        "policy_version": "1",
        "witness_kind": GENERIC_KIND,
        "availability_semantics": "source_as_of<=available_at",
    }
    return RevisionPolicyAuthority.create(
        revision_policy_id=policy_id,
        policy_content_json=json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ),
        frozen_at=BASE,
    )


def _witness(source: str, witness_id: str = "witness:v1") -> AvailabilityWitnessAuthority:
    revision_sha = hashlib.sha256(f"{source}:revision".encode("utf-8")).hexdigest()
    payload = {
        "schema": "autosport.source_availability_witness",
        "schema_version": 1,
        "source_identity": source,
        "source_revision": "revision:1",
        "source_revision_sha256": revision_sha,
        "witness_kind": GENERIC_KIND,
        "source_as_of": _iso(BASE + timedelta(seconds=1)),
        "available_at": _iso(BASE + timedelta(seconds=2)),
    }
    return AvailabilityWitnessAuthority.create(
        availability_witness_id=witness_id,
        witness_content_json=json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ),
        recorded_at=BASE + timedelta(seconds=3),
    )


def _revision(
    source: str,
    policy: RevisionPolicyAuthority,
    witness: AvailabilityWitnessAuthority,
) -> SourceRevisionAuthority:
    return SourceRevisionAuthority(
        source_revision_authority_id=f"revision-authority:{source}",
        source_identity=source,
        source_revision=witness.source_revision,
        source_revision_sha256=witness.source_revision_sha256,
        revision_policy_id=policy.revision_policy_id,
        revision_policy_record_sha256=policy.authority_sha256,
        availability_witness_id=witness.availability_witness_id,
        availability_witness_sha256=witness.witness_content_sha256,
        availability_witness_record_sha256=witness.authority_sha256,
        witness_kind=GENERIC_KIND,
        source_as_of=witness.source_as_of,
        available_at=witness.available_at,
        recorded_at=BASE + timedelta(seconds=4),
    )


def test_provider_source_identity_cannot_fall_back_to_generic_authority(tmp_path) -> None:
    store = SourceRevisionAuthorityStore.initialize_pristine(tmp_path / "pit")
    policy = _policy(PROVIDER_SOURCE)
    witness = _witness(PROVIDER_SOURCE)
    revision = _revision(PROVIDER_SOURCE, policy, witness)

    with pytest.raises(SourceRevisionAuthorityError, match="canonical provider source"):
        store.register_policy(policy)
    with pytest.raises(SourceRevisionAuthorityError, match="canonical provider source"):
        store.register_witness(witness)
    with pytest.raises(SourceRevisionAuthorityError, match="canonical provider source"):
        store.register_revision(revision)


def test_reopen_rejects_preexisting_generic_provider_source_state(tmp_path) -> None:
    workspace = tmp_path / "pit"
    store = SourceRevisionAuthorityStore.initialize_pristine(workspace)
    policy = _policy(PROVIDER_SOURCE)

    # Simulate a state written before the source-identity reservation existed.
    state = store._read()
    state["policies"] = [*state["policies"], store._stored_policy(policy)]
    atomic_write_json(store.path, state)

    with pytest.raises(
        SourceRevisionAuthorityError,
        match="alternate generic availability authority",
    ):
        SourceRevisionAuthorityStore(workspace)


def test_unrelated_generic_source_keeps_existing_authority_path(tmp_path) -> None:
    source = "research:independent-source"
    store = SourceRevisionAuthorityStore.initialize_pristine(tmp_path / "pit")
    policy = _policy(source)
    witness = _witness(source)
    revision = _revision(source, policy, witness)

    store.register_policy(policy)
    store.register_witness(witness)
    store.register_revision(revision)

    resolved = store.resolve_revision(
        revision.source_revision_authority_id,
        expected_sha256=revision.authority_sha256,
    )
    assert resolved == revision
