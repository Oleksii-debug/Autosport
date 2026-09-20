from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from autosport.monotonic_workspace_authority import (
    FULL_MACHINE_ROLLBACK_RESISTANT,
    WORKSPACE_ROLLBACK_RESISTANT_WHILE_MACHINE_AUTHORITY_SURVIVES,
    AuthorityPhase,
    MonotonicAuthorityConfigurationError,
    MonotonicAuthorityConflictError,
    MonotonicAuthorityIntegrityError,
    MonotonicAuthorityRecoveryRequiredError,
    MonotonicAuthorityRollbackError,
    MonotonicWorkspaceAuthority,
    RecoveryDisposition,
    default_monotonic_authority_root,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _authority(
    tmp_path: Path,
    *,
    workspace_name: str = "workspace",
    workspace_instance_id: str | None = "workspace-instance-1",
) -> MonotonicWorkspaceAuthority:
    workspace = tmp_path / workspace_name
    workspace.mkdir(exist_ok=True)
    authority_root = tmp_path / "machine-state"
    authority_root.mkdir(exist_ok=True)
    return MonotonicWorkspaceAuthority(
        workspace=workspace,
        workspace_instance_id=workspace_instance_id,
        domain="test-domain",
        key="test-key",
        authority_root=authority_root,
    )


def _commit(
    authority: MonotonicWorkspaceAuthority,
    *,
    tx_id: str,
    previous: str | None,
    state: str,
    binding: str,
) -> None:
    authority.prepare(
        tx_id=tx_id,
        observed_state_sha256=previous,
        intended_state_sha256=state,
        semantic_binding_sha256=binding,
    )
    authority.commit(
        tx_id=tx_id,
        observed_state_sha256=state,
        semantic_binding_sha256=binding,
    )


def test_prepare_commit_reopen_validates_exact_current_state(tmp_path: Path) -> None:
    authority = _authority(tmp_path)
    state = _sha("generation-1")
    binding = _sha("binding-1")

    pristine = authority.recover(observed_state_sha256=None)
    assert pristine.disposition is RecoveryDisposition.PRISTINE

    prepared = authority.prepare(
        tx_id="tx-1",
        observed_state_sha256=None,
        intended_state_sha256=state,
        semantic_binding_sha256=binding,
    )
    committed = authority.commit(
        tx_id="tx-1",
        observed_state_sha256=state,
        semantic_binding_sha256=binding,
    )

    assert prepared.phase is AuthorityPhase.PREPARE
    assert committed.phase is AuthorityPhase.COMMIT
    assert committed.generation == 1
    assert authority.namespace_marker_path.is_file()
    assert authority.workspace_binding_path.is_file()

    reopened = _authority(tmp_path)
    recovered = reopened.recover(observed_state_sha256=state)
    assert recovered.disposition is RecoveryDisposition.CURRENT
    assert recovered.committed_generation == 1
    assert recovered.committed_state_sha256 == state


def test_reopen_without_injected_id_resolves_durable_workspace_identity(
    tmp_path: Path,
) -> None:
    authority = _authority(tmp_path)
    state = _sha("generation-1")
    _commit(
        authority,
        tx_id="tx-1",
        previous=None,
        state=state,
        binding=_sha("binding-1"),
    )

    reopened = _authority(tmp_path, workspace_instance_id=None)
    assert reopened.workspace_instance_id == "workspace-instance-1"
    assert reopened.recover(observed_state_sha256=state).committed_generation == 1


def test_same_workspace_cannot_remint_instance_identity_after_commit(
    tmp_path: Path,
) -> None:
    authority = _authority(tmp_path)
    state = _sha("generation-1")
    _commit(
        authority,
        tx_id="tx-1",
        previous=None,
        state=state,
        binding=_sha("binding-1"),
    )

    with pytest.raises(MonotonicAuthorityConfigurationError, match="conflicts"):
        MonotonicWorkspaceAuthority(
            workspace=authority.workspace,
            workspace_instance_id="workspace-instance-2",
            domain="test-domain",
            key="test-key",
            authority_root=authority.authority_root,
        )


def test_deleted_workspace_binding_cannot_remint_identity_at_same_path(
    tmp_path: Path,
) -> None:
    authority = _authority(tmp_path)
    state = _sha("generation-1")
    _commit(
        authority,
        tx_id="tx-1",
        previous=None,
        state=state,
        binding=_sha("binding-1"),
    )
    shutil.rmtree(authority.workspace / ".autosport")

    with pytest.raises(MonotonicAuthorityConfigurationError, match="conflicts"):
        MonotonicWorkspaceAuthority(
            workspace=authority.workspace,
            workspace_instance_id="workspace-instance-2",
            domain="test-domain",
            key="test-key",
            authority_root=authority.authority_root,
        )

    rebound = MonotonicWorkspaceAuthority(
        workspace=authority.workspace,
        workspace_instance_id=None,
        domain="test-domain",
        key="test-key",
        authority_root=authority.authority_root,
    )
    assert rebound.workspace_instance_id == "workspace-instance-1"
    with pytest.raises(MonotonicAuthorityIntegrityError, match="workspace identity binding"):
        rebound.recover(observed_state_sha256=state)


def test_moved_workspace_keeps_identity_and_registers_new_path(tmp_path: Path) -> None:
    authority = _authority(tmp_path, workspace_name="before-move")
    state = _sha("generation-1")
    _commit(
        authority,
        tx_id="tx-1",
        previous=None,
        state=state,
        binding=_sha("binding-1"),
    )

    moved_workspace = tmp_path / "after-move"
    authority.workspace.rename(moved_workspace)
    moved = MonotonicWorkspaceAuthority(
        workspace=moved_workspace,
        workspace_instance_id=None,
        domain="test-domain",
        key="test-key",
        authority_root=authority.authority_root,
    )

    assert moved.workspace_instance_id == "workspace-instance-1"
    assert moved.recover(observed_state_sha256=state).committed_generation == 1
    assert moved.workspace_binding.path_binding_path.is_file()


def test_f0_stale_writer_cannot_overwrite_newer_committed_tip(tmp_path: Path) -> None:
    authority = _authority(tmp_path)
    g1 = _sha("g1")
    g2 = _sha("g2")
    binding1 = _sha("binding-1")
    binding2 = _sha("binding-2")

    _commit(
        authority,
        tx_id="writer-a",
        previous=None,
        state=g1,
        binding=binding1,
    )

    with pytest.raises(MonotonicAuthorityRollbackError):
        authority.prepare(
            tx_id="writer-b",
            observed_state_sha256=None,
            intended_state_sha256=g2,
            semantic_binding_sha256=binding2,
        )

    assert authority.recover(observed_state_sha256=g1).committed_state_sha256 == g1


def test_f1_restored_older_workspace_snapshot_is_rejected(tmp_path: Path) -> None:
    authority = _authority(tmp_path)
    g1 = _sha("g1")
    g2 = _sha("g2")

    _commit(authority, tx_id="tx-1", previous=None, state=g1, binding=_sha("b1"))
    _commit(authority, tx_id="tx-2", previous=g1, state=g2, binding=_sha("b2"))

    with pytest.raises(MonotonicAuthorityRollbackError):
        authority.recover(observed_state_sha256=g1)


def test_f2_deleted_workspace_state_does_not_rebootstrap_pristine(tmp_path: Path) -> None:
    authority = _authority(tmp_path)
    g1 = _sha("g1")
    _commit(authority, tx_id="tx-1", previous=None, state=g1, binding=_sha("b1"))

    with pytest.raises(MonotonicAuthorityRollbackError):
        authority.recover(observed_state_sha256=None)

    with pytest.raises(MonotonicAuthorityRollbackError):
        authority.prepare(
            tx_id="fresh-looking-tx",
            observed_state_sha256=None,
            intended_state_sha256=_sha("replacement"),
            semantic_binding_sha256=_sha("replacement-binding"),
        )


def test_committed_tx_prepare_retry_rejects_old_snapshot_and_deletion(
    tmp_path: Path,
) -> None:
    authority = _authority(tmp_path)
    g0 = _sha("g0")
    g1 = _sha("g1")
    b0 = _sha("b0")
    b1 = _sha("b1")
    _commit(authority, tx_id="tx-0", previous=None, state=g0, binding=b0)
    _commit(authority, tx_id="tx-1", previous=g0, state=g1, binding=b1)

    with pytest.raises(MonotonicAuthorityRollbackError):
        authority.prepare(
            tx_id="tx-1",
            observed_state_sha256=g0,
            intended_state_sha256=g1,
            semantic_binding_sha256=b1,
        )
    with pytest.raises(MonotonicAuthorityRollbackError):
        authority.prepare(
            tx_id="tx-1",
            observed_state_sha256=None,
            intended_state_sha256=g1,
            semantic_binding_sha256=b1,
        )

    retry = authority.prepare(
        tx_id="tx-1",
        observed_state_sha256=g1,
        intended_state_sha256=g1,
        semantic_binding_sha256=b1,
    )
    assert retry.phase is AuthorityPhase.COMMIT
    assert len(authority.read_history()) == 4


def test_old_committed_tx_cannot_be_replayed_after_newer_generation(
    tmp_path: Path,
) -> None:
    authority = _authority(tmp_path)
    g1 = _sha("g1")
    g2 = _sha("g2")
    b1 = _sha("b1")
    b2 = _sha("b2")
    _commit(authority, tx_id="tx-1", previous=None, state=g1, binding=b1)
    _commit(authority, tx_id="tx-2", previous=g1, state=g2, binding=b2)

    with pytest.raises(MonotonicAuthorityConflictError, match="no longer"):
        authority.prepare(
            tx_id="tx-1",
            observed_state_sha256=g1,
            intended_state_sha256=g1,
            semantic_binding_sha256=b1,
        )
    with pytest.raises(MonotonicAuthorityConflictError, match="no longer"):
        authority.commit(
            tx_id="tx-1",
            observed_state_sha256=g1,
            semantic_binding_sha256=b1,
        )


def test_missing_authority_history_with_local_state_fails_closed(tmp_path: Path) -> None:
    authority = _authority(tmp_path)
    state = _sha("committed")
    _commit(
        authority,
        tx_id="tx-1",
        previous=None,
        state=state,
        binding=_sha("binding"),
    )

    shutil.rmtree(authority.records_dir)
    reopened = _authority(tmp_path)
    with pytest.raises(MonotonicAuthorityIntegrityError, match="history is missing"):
        reopened.recover(observed_state_sha256=state)


def test_missing_authority_history_and_missing_local_state_cannot_rebootstrap(
    tmp_path: Path,
) -> None:
    authority = _authority(tmp_path)
    state = _sha("committed")
    _commit(
        authority,
        tx_id="tx-1",
        previous=None,
        state=state,
        binding=_sha("binding"),
    )
    assert authority.namespace_marker_path.exists()

    shutil.rmtree(authority.records_dir)
    reopened = _authority(tmp_path)
    with pytest.raises(MonotonicAuthorityIntegrityError, match="history is missing"):
        reopened.recover(observed_state_sha256=None)


def test_missing_namespace_marker_is_recovered_from_valid_nonempty_history(
    tmp_path: Path,
) -> None:
    authority = _authority(tmp_path)
    state = _sha("g1")
    _commit(
        authority,
        tx_id="tx-1",
        previous=None,
        state=state,
        binding=_sha("binding"),
    )
    authority.namespace_marker_path.unlink()

    reopened = _authority(tmp_path)
    assert reopened.recover(observed_state_sha256=state).committed_generation == 1
    assert reopened.namespace_marker_path.is_file()


def test_crash_after_prepare_before_local_publish_is_deterministically_aborted(
    tmp_path: Path,
) -> None:
    authority = _authority(tmp_path)
    intended = _sha("not-yet-published")
    authority.prepare(
        tx_id="tx-prepare-only",
        observed_state_sha256=None,
        intended_state_sha256=intended,
        semantic_binding_sha256=_sha("binding-1"),
    )

    recovered = authority.recover(observed_state_sha256=None)
    assert recovered.disposition is RecoveryDisposition.ABORTED_PREPARE
    assert recovered.committed_generation == 0
    assert recovered.committed_state_sha256 is None
    assert recovered.record is not None
    assert recovered.record.phase is AuthorityPhase.ABORT

    next_prepare = authority.prepare(
        tx_id="tx-after-abort",
        observed_state_sha256=None,
        intended_state_sha256=_sha("next"),
        semantic_binding_sha256=_sha("binding-2"),
    )
    assert next_prepare.generation == 2


def test_crash_after_local_publish_requires_semantic_proof_then_commits(
    tmp_path: Path,
) -> None:
    authority = _authority(tmp_path)
    intended = _sha("published-before-crash")
    binding = _sha("binding")
    authority.prepare(
        tx_id="tx-1",
        observed_state_sha256=None,
        intended_state_sha256=intended,
        semantic_binding_sha256=binding,
    )

    with pytest.raises(MonotonicAuthorityRecoveryRequiredError):
        authority.recover(observed_state_sha256=intended)
    with pytest.raises(MonotonicAuthorityRecoveryRequiredError):
        authority.recover(
            observed_state_sha256=intended,
            tx_id="tx-1",
            semantic_binding_sha256=_sha("wrong-binding"),
        )

    recovered = authority.recover(
        observed_state_sha256=intended,
        tx_id="tx-1",
        semantic_binding_sha256=binding,
    )
    assert recovered.disposition is RecoveryDisposition.COMMITTED_PREPARE
    assert recovered.committed_generation == 1
    assert authority.recover(observed_state_sha256=intended).disposition is RecoveryDisposition.CURRENT


def test_prepare_prefix_with_conflicting_local_bytes_fails_closed(tmp_path: Path) -> None:
    authority = _authority(tmp_path)
    intended = _sha("intended")
    binding = _sha("binding")
    authority.prepare(
        tx_id="tx-1",
        observed_state_sha256=None,
        intended_state_sha256=intended,
        semantic_binding_sha256=binding,
    )

    with pytest.raises(MonotonicAuthorityConflictError, match="neither"):
        authority.recover(observed_state_sha256=_sha("unexpected-local-state"))
    with pytest.raises(MonotonicAuthorityConflictError, match="neither"):
        authority.prepare(
            tx_id="tx-1",
            observed_state_sha256=_sha("unexpected-local-state"),
            intended_state_sha256=intended,
            semantic_binding_sha256=binding,
        )


def test_duplicate_same_tx_is_idempotent_but_changed_digest_is_rejected(
    tmp_path: Path,
) -> None:
    authority = _authority(tmp_path)
    intended = _sha("g1")
    binding = _sha("binding")
    first = authority.prepare(
        tx_id="stable-tx",
        observed_state_sha256=None,
        intended_state_sha256=intended,
        semantic_binding_sha256=binding,
    )
    duplicate = authority.prepare(
        tx_id="stable-tx",
        observed_state_sha256=None,
        intended_state_sha256=intended,
        semantic_binding_sha256=binding,
    )
    assert duplicate == first
    assert len(authority.read_history()) == 1

    with pytest.raises(MonotonicAuthorityConflictError, match="same tx_id"):
        authority.prepare(
            tx_id="stable-tx",
            observed_state_sha256=None,
            intended_state_sha256=_sha("changed"),
            semantic_binding_sha256=binding,
        )


def test_copy_with_reused_workspace_identity_cannot_reset_history(tmp_path: Path) -> None:
    original = _authority(tmp_path, workspace_name="original")
    g1 = _sha("g1")
    g2 = _sha("g2")
    _commit(original, tx_id="tx-1", previous=None, state=g1, binding=_sha("b1"))
    _commit(original, tx_id="tx-2", previous=g1, state=g2, binding=_sha("b2"))

    copied_workspace = tmp_path / "copied-old-snapshot"
    copied_workspace.mkdir()
    shutil.copytree(original.workspace / ".autosport", copied_workspace / ".autosport")
    copied = _authority(
        tmp_path,
        workspace_name="copied-old-snapshot",
        workspace_instance_id=None,
    )
    assert copied.workspace_instance_id == original.workspace_instance_id

    with pytest.raises(MonotonicAuthorityRollbackError):
        copied.recover(observed_state_sha256=g1)

    assert copied.recover(observed_state_sha256=g2).committed_generation == 2


def test_copied_workspace_marker_rejects_different_injected_identity(
    tmp_path: Path,
) -> None:
    original = _authority(tmp_path, workspace_name="original")
    state = _sha("g1")
    _commit(original, tx_id="tx-1", previous=None, state=state, binding=_sha("b1"))

    copied_workspace = tmp_path / "copied"
    copied_workspace.mkdir()
    shutil.copytree(original.workspace / ".autosport", copied_workspace / ".autosport")

    with pytest.raises(MonotonicAuthorityConfigurationError, match="conflicts"):
        MonotonicWorkspaceAuthority(
            workspace=copied_workspace,
            workspace_instance_id="different-instance",
            domain="test-domain",
            key="test-key",
            authority_root=original.authority_root,
        )


def test_authority_root_inside_workspace_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(MonotonicAuthorityConfigurationError, match="disjoint"):
        MonotonicWorkspaceAuthority(
            workspace=workspace,
            workspace_instance_id="workspace-1",
            domain="domain",
            key="key",
            authority_root=workspace / "machine-state",
        )


def test_workspace_inside_authority_root_is_rejected(tmp_path: Path) -> None:
    authority_root = tmp_path / "machine-state"
    workspace = authority_root / "workspace"
    workspace.mkdir(parents=True)
    with pytest.raises(MonotonicAuthorityConfigurationError, match="disjoint"):
        MonotonicWorkspaceAuthority(
            workspace=workspace,
            workspace_instance_id="workspace-1",
            domain="domain",
            key="key",
            authority_root=authority_root,
        )


def test_symlinked_authority_root_back_into_workspace_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inside = workspace / "authority-target"
    inside.mkdir()
    alias = tmp_path / "authority-alias"
    try:
        alias.symlink_to(inside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable on this runner")

    with pytest.raises(MonotonicAuthorityConfigurationError, match="disjoint"):
        MonotonicWorkspaceAuthority(
            workspace=workspace,
            workspace_instance_id="workspace-1",
            domain="domain",
            key="key",
            authority_root=alias,
        )


def test_missing_middle_authority_record_is_integrity_failure(tmp_path: Path) -> None:
    authority = _authority(tmp_path)
    state = _sha("g1")
    _commit(
        authority,
        tx_id="tx-1",
        previous=None,
        state=state,
        binding=_sha("binding"),
    )
    records = sorted(authority.records_dir.iterdir())
    assert len(records) == 2
    records[0].unlink()

    with pytest.raises(MonotonicAuthorityIntegrityError, match="gap"):
        authority.read_history()


def test_tampered_authority_record_hash_is_rejected(tmp_path: Path) -> None:
    authority = _authority(tmp_path)
    authority.prepare(
        tx_id="tx-1",
        observed_state_sha256=None,
        intended_state_sha256=_sha("g1"),
        semantic_binding_sha256=_sha("binding"),
    )
    record_path = next(authority.records_dir.iterdir())
    payload = json.loads(record_path.read_text(encoding="utf-8"))
    payload["domain"] = "tampered-domain"
    record_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MonotonicAuthorityIntegrityError):
        authority.read_history()


def test_tampered_namespace_binding_is_rejected(tmp_path: Path) -> None:
    authority = _authority(tmp_path)
    authority.prepare(
        tx_id="tx-1",
        observed_state_sha256=None,
        intended_state_sha256=_sha("g1"),
        semantic_binding_sha256=_sha("binding"),
    )
    payload = json.loads(authority.namespace_marker_path.read_text(encoding="utf-8"))
    payload["domain"] = "tampered-domain"
    authority.namespace_marker_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MonotonicAuthorityIntegrityError, match="binding"):
        authority.read_history()


def test_tampered_workspace_identity_binding_is_rejected(tmp_path: Path) -> None:
    authority = _authority(tmp_path)
    _commit(
        authority,
        tx_id="tx-1",
        previous=None,
        state=_sha("g1"),
        binding=_sha("b1"),
    )
    payload = json.loads(authority.workspace_binding_path.read_text(encoding="utf-8"))
    payload["workspace_instance_id"] = "forged-instance"
    authority.workspace_binding_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MonotonicAuthorityIntegrityError, match="binding"):
        _authority(tmp_path, workspace_instance_id=None)


def test_default_machine_root_is_application_state_not_default_workspace_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AUTOSPORT_MONOTONIC_AUTHORITY_ROOT", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    local_app_data = tmp_path / "LocalAppData"
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))

    assert default_monotonic_authority_root() == (
        local_app_data
        / "Autosport"
        / "application-state"
        / "monotonic-authority-v1"
    )


def test_explicit_authority_override_must_be_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOSPORT_MONOTONIC_AUTHORITY_ROOT", "relative/state")
    with pytest.raises(MonotonicAuthorityConfigurationError, match="absolute"):
        default_monotonic_authority_root()


def test_local_primitive_documents_exact_threat_boundary() -> None:
    assert WORKSPACE_ROLLBACK_RESISTANT_WHILE_MACHINE_AUTHORITY_SURVIVES is True
    assert FULL_MACHINE_ROLLBACK_RESISTANT is False
