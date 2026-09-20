from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from autosport.monotonic_workspace_authority import (
    MonotonicAuthorityConfigurationError,
    MonotonicAuthorityIntegrityError,
    MonotonicWorkspaceAuthority,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _authority(
    workspace: Path,
    authority_root: Path,
    *,
    workspace_instance_id: str | None,
) -> MonotonicWorkspaceAuthority:
    return MonotonicWorkspaceAuthority(
        workspace=workspace,
        workspace_instance_id=workspace_instance_id,
        domain="test-domain",
        key="test-key",
        authority_root=authority_root,
    )


def test_full_workspace_deletion_cannot_remint_identity_while_machine_root_survives(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    authority_root = tmp_path / "machine-state"
    authority_root.mkdir()
    authority = _authority(
        workspace,
        authority_root,
        workspace_instance_id="workspace-instance-a",
    )
    state = _sha("generation-1")
    binding = _sha("binding-1")
    authority.prepare(
        tx_id="tx-1",
        observed_state_sha256=None,
        intended_state_sha256=state,
        semantic_binding_sha256=binding,
    )
    authority.commit(
        tx_id="tx-1",
        observed_state_sha256=state,
        semantic_binding_sha256=binding,
    )
    assert authority.workspace_binding.path_binding_path.is_file()

    shutil.rmtree(workspace)
    workspace.mkdir()

    with pytest.raises(MonotonicAuthorityConfigurationError, match="conflicts"):
        _authority(
            workspace,
            authority_root,
            workspace_instance_id="workspace-instance-b",
        )

    rebound = _authority(workspace, authority_root, workspace_instance_id=None)
    assert rebound.workspace_instance_id == "workspace-instance-a"
    with pytest.raises(MonotonicAuthorityIntegrityError, match="workspace identity binding"):
        rebound.recover(observed_state_sha256=None)


def test_path_binding_is_not_created_by_repeated_truly_pristine_recovery(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    authority_root = tmp_path / "machine-state"
    authority_root.mkdir()
    authority = _authority(
        workspace,
        authority_root,
        workspace_instance_id="workspace-instance-a",
    )

    authority.recover(observed_state_sha256=None)
    authority.recover(observed_state_sha256=None)

    assert not authority.workspace_binding_path.exists()
    assert not authority.workspace_binding.path_binding_path.exists()
    assert not authority.namespace_marker_path.exists()
    assert not authority.records_dir.exists()
