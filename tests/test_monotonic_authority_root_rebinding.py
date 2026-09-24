from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

import autosport.monotonic_workspace_binding as workspace_binding
from autosport.monotonic_workspace_authority import (
    MonotonicAuthorityConfigurationError,
    MonotonicAuthorityIntegrityError,
    MonotonicWorkspaceAuthority,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_native_machine_state_base_ignores_process_path_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = workspace_binding._native_machine_state_base()

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "redirected-localappdata"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "redirected-xdg-state"))
    monkeypatch.setenv("HOME", str(tmp_path / "redirected-home"))

    assert workspace_binding._native_machine_state_base() == first


def test_workspace_binding_rollback_cannot_redirect_surviving_authority_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Isolate the machine-stable pre-selection registry from the runner's real
    # application state while keeping it physically separate from M1 and M2.
    machine_state_home = tmp_path / "stable-machine-state"
    monkeypatch.setattr(
        workspace_binding,
        "_native_machine_state_base",
        lambda: machine_state_home,
    )
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "process-state-s1"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "process-state-s1"))
    monkeypatch.setenv("HOME", str(tmp_path / "process-home-s1"))

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root_m1 = tmp_path / "machine-root-m1"
    root_m1.mkdir()
    authority = MonotonicWorkspaceAuthority(
        workspace=workspace,
        workspace_instance_id="workspace-instance-1",
        domain="test-domain",
        key="test-key",
        authority_root=root_m1,
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

    assert authority.workspace_binding.machine_path_binding_path.is_file()
    assert authority.workspace_binding.machine_identity_binding_path.is_file()

    # Reproduce the hard boundary: the workspace-local marker/root pin is rolled
    # back/deleted while the original M1 machine authority remains intact. A
    # supported configuration change then points at a fresh physical M2 root.
    shutil.rmtree(workspace / ".autosport")
    root_m2 = tmp_path / "machine-root-m2"
    root_m2.mkdir()
    # Redirect every process path environment variable consumed by the previous
    # implementation. The private native account-state selector remains S1, so
    # the surviving S1/M1 receipt must still fence fresh M2.
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "process-state-s2"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "process-state-s2"))
    monkeypatch.setenv("HOME", str(tmp_path / "process-home-s2"))

    with pytest.raises(
        MonotonicAuthorityConfigurationError,
        match="stable machine root binding",
    ):
        MonotonicWorkspaceAuthority(
            workspace=workspace,
            workspace_instance_id=None,
            domain="test-domain",
            key="test-key",
            authority_root=root_m2,
        )

    # The stable registry must still resolve the immutable identity when M1 is
    # selected again. Missing workspace-local evidence remains fail-closed rather
    # than being silently regenerated from caller-controlled state.
    reopened = MonotonicWorkspaceAuthority(
        workspace=workspace,
        workspace_instance_id=None,
        domain="test-domain",
        key="test-key",
        authority_root=root_m1,
    )
    assert reopened.workspace_instance_id == "workspace-instance-1"
    with pytest.raises(
        MonotonicAuthorityIntegrityError,
        match="workspace identity binding",
    ):
        reopened.recover(observed_state_sha256=state)
