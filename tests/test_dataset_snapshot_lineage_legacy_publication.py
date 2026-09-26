from __future__ import annotations

import hashlib

import pytest

from autosport import _dataset_snapshot_lineage_publication as publication_module
from autosport import (
    _dataset_snapshot_lineage_publication_provenance as provenance_module,
)
from autosport import (
    _dataset_snapshot_lineage_publication_trust_root as trust_root_module,
)
from autosport.dataset_snapshot_lineage import (
    DatasetSnapshotLineageAuthority,
    DatasetSnapshotUnprovenError,
    membership_manifest_sha256,
)
from autosport.integrity import atomic_write_json
from autosport.monotonic_workspace_authority import MonotonicAuthorityRollbackError
from autosport.scientific_registry import DatasetSnapshot, ScientificRegistry


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _members(*values: str) -> tuple[str, ...]:
    return tuple(_sha(value) for value in values)


def _append_snapshot(
    registry: ScientificRegistry,
    *,
    snapshot_id: str,
    members: tuple[str, ...],
    cutoff: str,
    available_at: str,
) -> None:
    registry.append(
        DatasetSnapshot(
            dataset_snapshot_id=snapshot_id,
            manifest_sha256=membership_manifest_sha256(members),
            source_identity="lawful-provider:paper",
            license_identity="paper-license-v1",
            causal_cutoff=cutoff,
            available_at_utc=available_at,
        )
    )


def _install_legacy_v1_chain(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    custom_authority_root=None,
    environment_authority_root=None,
):
    production_root = tmp_path.parent / f"{tmp_path.name}-production-machine"
    production_root = production_root.resolve(strict=False)
    monkeypatch.setattr(
        trust_root_module,
        "_machine_account_authority_root",
        lambda: production_root,
    )
    generic_authority_root = (
        environment_authority_root
        if environment_authority_root is not None
        else custom_authority_root
        if custom_authority_root is not None
        else production_root
    )
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str(generic_authority_root.resolve(strict=False)),
    )

    registry = ScientificRegistry.initialize_pristine(tmp_path / "registry.json")
    root_members = _members("a")
    child_members = _members("a", "b")
    _append_snapshot(
        registry,
        snapshot_id="training",
        members=root_members,
        cutoff="2026-09-20T01:00:00Z",
        available_at="2026-09-20T01:01:00Z",
    )
    _append_snapshot(
        registry,
        snapshot_id="deployment",
        members=child_members,
        cutoff="2026-09-20T02:00:00Z",
        available_at="2026-09-20T02:01:00Z",
    )
    path = tmp_path / "dataset-snapshot-lineage.json"
    if environment_authority_root is not None:
        authority = DatasetSnapshotLineageAuthority.initialize_pristine(
            path,
            registry,
        )
        restart_root = None
    elif custom_authority_root is None:
        authority = DatasetSnapshotLineageAuthority.initialize_pristine(
            path,
            registry,
            authority_root=production_root,
        )
        restart_root = production_root
    else:
        authority = DatasetSnapshotLineageAuthority.initialize_pristine(
            path,
            registry,
            authority_root=custom_authority_root,
        )
        restart_root = custom_authority_root
    assert authority._read_and_verify() == ()

    root_entry = registry.get("DatasetSnapshot", "training")
    child_entry = registry.get("DatasetSnapshot", "deployment")
    assert root_entry is not None
    assert child_entry is not None
    root = authority._new_record(
        root_entry,
        root_members,
        None,
        proof_registered_at=None,
    )
    child = authority._new_record(
        child_entry,
        child_members,
        root,
        proof_registered_at=None,
    )
    records = (root, child)
    observed = authority._state_sha256(())
    intended = authority._state_sha256(records)
    binding = authority._semantic_binding_sha256(intended)
    tx_id = "legacy-schema-v1-test-fixture"
    authority.monotonic_authority.prepare(
        tx_id=tx_id,
        observed_state_sha256=observed,
        intended_state_sha256=intended,
        semantic_binding_sha256=binding,
    )
    atomic_write_json(authority.path, authority._state_payload(records))
    authority.monotonic_authority.commit(
        tx_id=tx_id,
        observed_state_sha256=intended,
        semantic_binding_sha256=binding,
    )
    if restart_root is None:
        restarted = DatasetSnapshotLineageAuthority(authority.path, registry)
    else:
        restarted = DatasetSnapshotLineageAuthority(
            authority.path,
            registry,
            authority_root=restart_root,
            workspace_instance_id=authority.monotonic_authority.workspace_instance_id,
        )
    return restarted, root, child, root_members, child_members


def test_v1_proof_never_retro_authorizes_but_exact_reobservation_unblocks_future(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority, root, child, _, child_members = _install_legacy_v1_chain(
        tmp_path, monkeypatch
    )

    with pytest.raises(
        DatasetSnapshotUnprovenError,
        match="lacks authority-owned publication evidence",
    ):
        authority.require_descendant_as_of(
            descendant_snapshot_id="deployment",
            ancestor_snapshot_id="training",
            as_of="2099-01-01T00:00:00Z",
        )

    monkeypatch.setattr(
        publication_module,
        "_authority_now_utc",
        lambda: "2026-09-20T03:00:00Z",
    )
    reobserved = authority.register(
        snapshot_id="deployment",
        member_sha256=child_members,
        parent_snapshot_id="training",
    )

    # The compatibility repair never rewrites the immutable v1 proof identity.
    assert reobserved.proof_sha256 == child.proof_sha256
    assert reobserved.proof_registered_at is None
    assert authority.record("training").proof_sha256 == root.proof_sha256

    with pytest.raises(
        DatasetSnapshotUnprovenError,
        match="not causally available",
    ):
        authority.require_descendant_as_of(
            descendant_snapshot_id="deployment",
            ancestor_snapshot_id="training",
            as_of="2026-09-20T02:59:59Z",
        )

    assert authority.require_descendant_as_of(
        descendant_snapshot_id="deployment",
        ancestor_snapshot_id="training",
        as_of="2026-09-20T03:00:00Z",
    ).proof_sha256 == child.proof_sha256


def test_legacy_reobservation_retry_is_idempotent_and_keeps_first_witness_time(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority, _, child, _, child_members = _install_legacy_v1_chain(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(
        publication_module,
        "_authority_now_utc",
        lambda: "2026-09-20T03:00:00Z",
    )
    first = authority.register(
        snapshot_id="deployment",
        member_sha256=child_members,
        parent_snapshot_id="training",
    )
    assert first.proof_sha256 == child.proof_sha256

    monkeypatch.setattr(
        publication_module,
        "_authority_now_utc",
        lambda: (_ for _ in ()).throw(
            AssertionError("exact retry must preserve first publication witness")
        ),
    )
    retried = authority.register(
        snapshot_id="deployment",
        member_sha256=child_members,
        parent_snapshot_id="training",
    )
    assert retried == first
    assert authority.require_descendant_as_of(
        descendant_snapshot_id="deployment",
        ancestor_snapshot_id="training",
        as_of="2026-09-20T03:00:00Z",
    ) == child


def test_deleting_legacy_publication_witness_fails_closed_under_machine_authority(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority, _, _, _, child_members = _install_legacy_v1_chain(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(
        publication_module,
        "_authority_now_utc",
        lambda: "2026-09-20T03:00:00Z",
    )
    authority.register(
        snapshot_id="deployment",
        member_sha256=child_members,
        parent_snapshot_id="training",
    )
    publication = publication_module.LegacyLineagePublicationAuthority(authority)
    publication.path.unlink()

    with pytest.raises(MonotonicAuthorityRollbackError):
        authority.require_descendant_as_of(
            descendant_snapshot_id="deployment",
            ancestor_snapshot_id="training",
            as_of="2026-09-20T04:00:00Z",
        )


def test_forged_backdated_witness_and_matching_generic_journal_cannot_authorize(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority, root, child, _, child_members = _install_legacy_v1_chain(
        tmp_path, monkeypatch
    )
    publication = publication_module.LegacyLineagePublicationAuthority.initialize_pristine(
        authority
    )
    existing = publication._read_and_recover()
    assert existing == ()

    # Model the exact falsifier: a caller writes structurally valid, backdated witness
    # bytes and also mints the matching generic monotonic PREPARE/COMMIT history.
    forged = (
        publication_module.LegacyLineagePublicationWitness(
            snapshot_id=root.snapshot_id,
            proof_sha256=root.proof_sha256,
            dataset_record_sha256=root.dataset_record_sha256,
            registered_at="2026-09-20T01:01:00Z",
        ),
        publication_module.LegacyLineagePublicationWitness(
            snapshot_id=child.snapshot_id,
            proof_sha256=child.proof_sha256,
            dataset_record_sha256=child.dataset_record_sha256,
            registered_at="2026-09-20T02:01:00Z",
        ),
    )
    observed = publication._state_sha256(existing)
    intended = publication._state_sha256(forged)
    binding = publication._semantic_binding_sha256(intended)
    tx_id = "caller-forged-legacy-publication"
    publication.monotonic_authority.prepare(
        tx_id=tx_id,
        observed_state_sha256=observed,
        intended_state_sha256=intended,
        semantic_binding_sha256=binding,
    )
    atomic_write_json(publication.path, publication._state_payload(forged))
    publication.monotonic_authority.commit(
        tx_id=tx_id,
        observed_state_sha256=intended,
        semantic_binding_sha256=binding,
    )

    with pytest.raises(
        DatasetSnapshotUnprovenError,
        match="lacks authority-owned publication evidence",
    ):
        authority.require_descendant_as_of(
            descendant_snapshot_id="deployment",
            ancestor_snapshot_id="training",
            as_of="2026-09-20T02:30:00Z",
        )

    # A real exact re-observation can recover availability, but only from NOW onward;
    # it must never inherit the forged earlier timestamp.
    monkeypatch.setattr(
        publication_module,
        "_authority_now_utc",
        lambda: "2026-09-20T03:00:00Z",
    )
    reobserved = authority.register(
        snapshot_id="deployment",
        member_sha256=child_members,
        parent_snapshot_id="training",
    )
    assert reobserved.proof_sha256 == child.proof_sha256

    with pytest.raises(DatasetSnapshotUnprovenError, match="not causally available"):
        authority.require_descendant_as_of(
            descendant_snapshot_id="deployment",
            ancestor_snapshot_id="training",
            as_of="2026-09-20T02:59:59Z",
        )
    assert authority.require_descendant_as_of(
        descendant_snapshot_id="deployment",
        ancestor_snapshot_id="training",
        as_of="2026-09-20T03:00:00Z",
    ).proof_sha256 == child.proof_sha256


def test_injected_root_cannot_authorize_even_with_matching_hmac_issuance(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attacker_root = tmp_path.parent / f"{tmp_path.name}-attacker-machine"
    authority, root, child, _, _ = _install_legacy_v1_chain(
        tmp_path,
        monkeypatch,
        custom_authority_root=attacker_root,
    )
    publication = publication_module.LegacyLineagePublicationAuthority.initialize_pristine(
        authority
    )
    existing = publication._read_and_recover()
    assert existing == ()

    forged = (
        publication_module.LegacyLineagePublicationWitness(
            snapshot_id=root.snapshot_id,
            proof_sha256=root.proof_sha256,
            dataset_record_sha256=root.dataset_record_sha256,
            registered_at="2026-09-20T01:01:00Z",
        ),
        publication_module.LegacyLineagePublicationWitness(
            snapshot_id=child.snapshot_id,
            proof_sha256=child.proof_sha256,
            dataset_record_sha256=child.dataset_record_sha256,
            registered_at="2026-09-20T02:01:00Z",
        ),
    )
    observed = publication._state_sha256(existing)
    intended = publication._state_sha256(forged)
    binding = publication._semantic_binding_sha256(intended)
    publication.monotonic_authority.prepare(
        tx_id="attacker-generic-publication",
        observed_state_sha256=observed,
        intended_state_sha256=intended,
        semantic_binding_sha256=binding,
    )
    atomic_write_json(publication.path, publication._state_payload(forged))
    publication.monotonic_authority.commit(
        tx_id="attacker-generic-publication",
        observed_state_sha256=intended,
        semantic_binding_sha256=binding,
    )

    # Exercise the previous exact bypass all the way through the same imported
    # signing helpers: even a correctly signed record written into the production
    # credential location cannot promote a lineage whose generic root was injected.
    key = provenance_module._load_or_create_key(publication)
    issued = tuple(
        provenance_module._IssuanceRecord.issue(
            publication,
            witness,
            published_at=witness.registered_at,
            key=key,
        )
        for witness in forged
    )
    provenance_module._write_issuances(publication, issued)

    with pytest.raises(
        DatasetSnapshotUnprovenError,
        match="lacks authority-owned publication evidence",
    ):
        authority.require_descendant_as_of(
            descendant_snapshot_id="deployment",
            ancestor_snapshot_id="training",
            as_of="2026-09-20T02:30:00Z",
        )


def test_environment_redirected_default_root_cannot_authorize_with_matching_issuance(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attacker_root = tmp_path.parent / f"{tmp_path.name}-environment-attacker-machine"
    authority, root, child, _, _ = _install_legacy_v1_chain(
        tmp_path,
        monkeypatch,
        environment_authority_root=attacker_root,
    )
    assert authority.monotonic_authority.authority_root.resolve(strict=False) == (
        attacker_root.resolve(strict=False)
    )

    publication = publication_module.LegacyLineagePublicationAuthority.initialize_pristine(
        authority
    )
    existing = publication._read_and_recover()
    assert existing == ()
    forged = (
        publication_module.LegacyLineagePublicationWitness(
            snapshot_id=root.snapshot_id,
            proof_sha256=root.proof_sha256,
            dataset_record_sha256=root.dataset_record_sha256,
            registered_at="2026-09-20T01:01:00Z",
        ),
        publication_module.LegacyLineagePublicationWitness(
            snapshot_id=child.snapshot_id,
            proof_sha256=child.proof_sha256,
            dataset_record_sha256=child.dataset_record_sha256,
            registered_at="2026-09-20T02:01:00Z",
        ),
    )
    observed = publication._state_sha256(existing)
    intended = publication._state_sha256(forged)
    binding = publication._semantic_binding_sha256(intended)
    tx_id = "environment-redirected-generic-publication"
    publication.monotonic_authority.prepare(
        tx_id=tx_id,
        observed_state_sha256=observed,
        intended_state_sha256=intended,
        semantic_binding_sha256=binding,
    )
    atomic_write_json(publication.path, publication._state_payload(forged))
    publication.monotonic_authority.commit(
        tx_id=tx_id,
        observed_state_sha256=intended,
        semantic_binding_sha256=binding,
    )

    # Even caller-reachable signing helpers cannot upgrade a lineage whose generic
    # root was selected through the process environment: the production lineage
    # predicate is independently rooted in the OS account location.
    key = provenance_module._load_or_create_key(publication)
    issued = tuple(
        provenance_module._IssuanceRecord.issue(
            publication,
            witness,
            published_at=witness.registered_at,
            key=key,
        )
        for witness in forged
    )
    provenance_module._write_issuances(publication, issued)

    with pytest.raises(
        DatasetSnapshotUnprovenError,
        match="lacks authority-owned publication evidence",
    ):
        authority.require_descendant_as_of(
            descendant_snapshot_id="deployment",
            ancestor_snapshot_id="training",
            as_of="2026-09-20T02:30:00Z",
        )
