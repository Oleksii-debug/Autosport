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


def test_authority_root_switch_cannot_rebootstrap_positive_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "stable-application-state"))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)

    workspace = tmp_path / "workspace-root-switch"
    workspace.mkdir()
    root_a = tmp_path / "machine-root-a"
    root_b = tmp_path / "machine-root-b"

    authority_a = MonotonicWorkspaceAuthority(
        workspace=workspace,
        workspace_instance_id="workspace-instance-a",
        domain="test-domain",
        key="test-key",
        authority_root=root_a,
    )
    g1 = _sha("root-switch-g1")
    b1 = _sha("root-switch-b1")
    authority_a.prepare(
        tx_id="tx-root-a-1",
        observed_state_sha256=None,
        intended_state_sha256=g1,
        semantic_binding_sha256=b1,
    )
    authority_a.commit(
        tx_id="tx-root-a-1",
        observed_state_sha256=g1,
        semantic_binding_sha256=b1,
    )
    g2 = _sha("root-switch-g2")
    b2 = _sha("root-switch-b2")
    authority_a.prepare(
        tx_id="tx-root-a-2",
        observed_state_sha256=g1,
        intended_state_sha256=g2,
        semantic_binding_sha256=b2,
    )
    authority_a.commit(
        tx_id="tx-root-a-2",
        observed_state_sha256=g2,
        semantic_binding_sha256=b2,
    )

    assert authority_a.authority_root_binding_path.is_file()
    assert authority_a.records_dir.is_dir()

    # Keep root A and its positive history intact, but erase the rollbackable
    # workspace tree.  A fresh root B must not become a generation-zero universe.
    shutil.rmtree(workspace)
    workspace.mkdir()

    with pytest.raises(
        MonotonicAuthorityConfigurationError,
        match="different monotonic authority root|different immutable workspace identity",
    ):
        MonotonicWorkspaceAuthority(
            workspace=workspace,
            workspace_instance_id=None,
            domain="test-domain",
            key="test-key",
            authority_root=root_b,
        )

    with pytest.raises(
        MonotonicAuthorityConfigurationError,
        match="different monotonic authority root",
    ):
        MonotonicWorkspaceAuthority(
            workspace=workspace,
            workspace_instance_id="workspace-instance-a",
            domain="test-domain",
            key="test-key",
            authority_root=root_b,
        )

    assert authority_a.records_dir.is_dir()
    assert not root_b.exists()


def test_pristine_recovery_does_not_pin_authority_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "stable-application-state"))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)

    workspace = tmp_path / "pristine-workspace"
    workspace.mkdir()
    root_a = tmp_path / "pristine-root-a"
    root_b = tmp_path / "pristine-root-b"

    first = MonotonicWorkspaceAuthority(
        workspace=workspace,
        workspace_instance_id="pristine-instance",
        domain="test-domain",
        key="test-key",
        authority_root=root_a,
    )
    assert first.recover(observed_state_sha256=None).disposition.value == "PRISTINE"
    assert not first.authority_root_binding_path.exists()

    second = MonotonicWorkspaceAuthority(
        workspace=workspace,
        workspace_instance_id="pristine-instance",
        domain="test-domain",
        key="test-key",
        authority_root=root_b,
    )
    assert second.recover(observed_state_sha256=None).disposition.value == "PRISTINE"
    assert not second.authority_root_binding_path.exists()


def test_existing_history_reconstructs_missing_root_selection_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "stable-application-state"))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)

    workspace = tmp_path / "upgrade-workspace"
    workspace.mkdir()
    root = tmp_path / "upgrade-root"
    authority = MonotonicWorkspaceAuthority(
        workspace=workspace,
        workspace_instance_id="upgrade-instance",
        domain="test-domain",
        key="test-key",
        authority_root=root,
    )
    state = _sha("upgrade-g1")
    binding = _sha("upgrade-b1")
    authority.prepare(
        tx_id="upgrade-tx",
        observed_state_sha256=None,
        intended_state_sha256=state,
        semantic_binding_sha256=binding,
    )
    authority.commit(
        tx_id="upgrade-tx",
        observed_state_sha256=state,
        semantic_binding_sha256=binding,
    )
    authority.authority_root_binding_path.unlink()

    reopened = MonotonicWorkspaceAuthority(
        workspace=workspace,
        workspace_instance_id=None,
        domain="test-domain",
        key="test-key",
        authority_root=root,
    )
    recovered = reopened.recover(observed_state_sha256=state)

    assert recovered.committed_generation == 1
    assert reopened.authority_root_binding_path.is_file()


def test_tampered_root_selection_receipt_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "stable-application-state"))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)

    authority = _authority(tmp_path)
    _commit(authority)
    payload = __import__("json").loads(
        authority.authority_root_binding_path.read_text(encoding="utf-8")
    )
    payload["authority_root_locator"] = str(tmp_path / "forged-root")
    authority.authority_root_binding_path.write_text(
        __import__("json").dumps(payload), encoding="utf-8"
    )

    with pytest.raises(MonotonicAuthorityIntegrityError, match="hash mismatch"):
        _authority(tmp_path, workspace_instance_id=None)
