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
    tmp_path: Path,
    *,
    workspace_instance_id: str | None = "workspace-instance-1",
) -> MonotonicWorkspaceAuthority:
    workspace = tmp_path / "workspace"
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


def _commit(authority: MonotonicWorkspaceAuthority) -> str:
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
    return state


def test_whole_journal_loss_cannot_rebootstrap_used_authority(tmp_path: Path) -> None:
    authority = _authority(tmp_path)
    _commit(authority)

    assert authority.namespace_marker_path.is_file()
    assert not authority.namespace_marker_path.is_relative_to(authority.journal_dir)
    shutil.rmtree(authority.journal_dir)

    reopened = _authority(tmp_path)
    with pytest.raises(MonotonicAuthorityIntegrityError, match="history is missing"):
        reopened.recover(observed_state_sha256=None)


def test_same_lexical_path_reparse_cannot_remint_workspace_identity(
    tmp_path: Path,
) -> None:
    authority = _authority(tmp_path)
    state = _commit(authority)
    workspace = authority.workspace
    authority_root = authority.authority_root

    shutil.rmtree(workspace)
    replacement = tmp_path / "replacement-target"
    replacement.mkdir()
    try:
        workspace.symlink_to(replacement, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("directory symlink/junction creation is unavailable on this runner")

    with pytest.raises(MonotonicAuthorityConfigurationError, match="conflicts"):
        MonotonicWorkspaceAuthority(
            workspace=workspace,
            workspace_instance_id="workspace-instance-2",
            domain="test-domain",
            key="test-key",
            authority_root=authority_root,
        )

    rebound = MonotonicWorkspaceAuthority(
        workspace=workspace,
        workspace_instance_id=None,
        domain="test-domain",
        key="test-key",
        authority_root=authority_root,
    )
    assert rebound.workspace_instance_id == "workspace-instance-1"
    with pytest.raises(
        MonotonicAuthorityIntegrityError,
        match="workspace identity binding is missing",
    ):
        rebound.recover(observed_state_sha256=state)
