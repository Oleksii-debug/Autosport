from __future__ import annotations

import hashlib
import json
import shutil
from concurrent.futures import ThreadPoolExecutor
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
    assert authority_a.authority_root_activation_path.is_file()
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
    payload = json.loads(
        authority.authority_root_binding_path.read_text(encoding="utf-8")
    )
    payload["authority_root_locator"] = str(tmp_path / "forged-root")
    authority.authority_root_binding_path.write_text(
        json.dumps(payload), encoding="utf-8"
    )

    with pytest.raises(MonotonicAuthorityIntegrityError, match="hash mismatch"):
        _authority(tmp_path, workspace_instance_id=None)


def test_selector_crash_prefix_pins_root_before_any_journal_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "stable-application-state"))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)

    workspace = tmp_path / "selector-crash-workspace"
    workspace.mkdir()
    root_a = tmp_path / "selector-crash-root-a"
    root_b = tmp_path / "selector-crash-root-b"
    authority = MonotonicWorkspaceAuthority(
        workspace=workspace,
        workspace_instance_id="selector-crash-instance",
        domain="test-domain",
        key="test-key",
        authority_root=root_a,
    )

    # Simulate the crash prefix after the root-independent selector is durable
    # but before the per-root workspace binding/journal PREPARE exists.
    authority._ensure_authority_root_bound()
    assert authority.authority_root_binding_path.is_file()
    assert not authority.authority_root_activation_path.exists()
    assert not root_a.exists()

    with pytest.raises(
        MonotonicAuthorityConfigurationError,
        match="different monotonic authority root",
    ):
        MonotonicWorkspaceAuthority(
            workspace=workspace,
            workspace_instance_id="selector-crash-instance",
            domain="test-domain",
            key="test-key",
            authority_root=root_b,
        )

    reopened = MonotonicWorkspaceAuthority(
        workspace=workspace,
        workspace_instance_id=None,
        domain="test-domain",
        key="test-key",
        authority_root=root_a,
    )
    assert reopened.recover(observed_state_sha256=None).disposition.value == "PRISTINE"


def test_activated_root_deletion_cannot_return_pristine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "stable-application-state"))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)

    workspace = tmp_path / "activated-root-workspace"
    workspace.mkdir()
    root = tmp_path / "activated-root"
    authority = MonotonicWorkspaceAuthority(
        workspace=workspace,
        workspace_instance_id="activated-root-instance",
        domain="test-domain",
        key="test-key",
        authority_root=root,
    )
    state = _sha("activated-root-g1")
    binding = _sha("activated-root-b1")
    authority.prepare(
        tx_id="activated-root-tx",
        observed_state_sha256=None,
        intended_state_sha256=state,
        semantic_binding_sha256=binding,
    )
    authority.commit(
        tx_id="activated-root-tx",
        observed_state_sha256=state,
        semantic_binding_sha256=binding,
    )
    assert authority.authority_root_activation_path.is_file()

    shutil.rmtree(root)

    reopened = MonotonicWorkspaceAuthority(
        workspace=workspace,
        workspace_instance_id=None,
        domain="test-domain",
        key="test-key",
        authority_root=root,
    )
    with pytest.raises(
        MonotonicAuthorityIntegrityError,
        match="activated monotonic authority history is missing",
    ):
        reopened.recover(observed_state_sha256=state)


def test_physical_root_alias_is_same_root_but_retarget_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "stable-application-state"))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)

    workspace = tmp_path / "alias-workspace"
    workspace.mkdir()
    target_a = tmp_path / "physical-root-a"
    target_a.mkdir()
    alias = tmp_path / "root-alias"
    try:
        alias.symlink_to(target_a, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("directory symlink/junction creation is unavailable on this runner")

    authority = MonotonicWorkspaceAuthority(
        workspace=workspace,
        workspace_instance_id="alias-instance",
        domain="test-domain",
        key="test-key",
        authority_root=target_a,
    )
    state = _sha("alias-g1")
    binding = _sha("alias-b1")
    authority.prepare(
        tx_id="alias-tx",
        observed_state_sha256=None,
        intended_state_sha256=state,
        semantic_binding_sha256=binding,
    )
    authority.commit(
        tx_id="alias-tx",
        observed_state_sha256=state,
        semantic_binding_sha256=binding,
    )

    via_alias = MonotonicWorkspaceAuthority(
        workspace=workspace,
        workspace_instance_id=None,
        domain="test-domain",
        key="test-key",
        authority_root=alias,
    )
    assert via_alias.recover(observed_state_sha256=state).committed_generation == 1

    alias.unlink()
    target_b = tmp_path / "physical-root-b"
    target_b.mkdir()
    alias.symlink_to(target_b, target_is_directory=True)

    with pytest.raises(
        MonotonicAuthorityConfigurationError,
        match="different monotonic authority root",
    ):
        MonotonicWorkspaceAuthority(
            workspace=workspace,
            workspace_instance_id=None,
            domain="test-domain",
            key="test-key",
            authority_root=alias,
        )


def test_concurrent_first_bootstrap_selects_one_root_before_journal_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "stable-application-state"))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)

    workspace = tmp_path / "concurrent-workspace"
    workspace.mkdir()
    root_a = tmp_path / "concurrent-root-a"
    root_b = tmp_path / "concurrent-root-b"
    common = {
        "workspace": workspace,
        "workspace_instance_id": "concurrent-instance",
        "domain": "test-domain",
        "key": "test-key",
    }
    authorities = (
        MonotonicWorkspaceAuthority(authority_root=root_a, **common),
        MonotonicWorkspaceAuthority(authority_root=root_b, **common),
    )
    state = _sha("concurrent-g1")
    binding = _sha("concurrent-b1")

    def attempt(authority: MonotonicWorkspaceAuthority, tx_id: str) -> object:
        try:
            return authority.prepare(
                tx_id=tx_id,
                observed_state_sha256=None,
                intended_state_sha256=state,
                semantic_binding_sha256=binding,
            )
        except BaseException as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(
            executor.map(
                lambda args: attempt(*args),
                ((authorities[0], "tx-a"), (authorities[1], "tx-b")),
            )
        )

    successes = [result for result in results if not isinstance(result, BaseException)]
    failures = [result for result in results if isinstance(result, BaseException)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], MonotonicAuthorityConfigurationError)

    winner_root = (
        root_a
        if authorities[0].records_dir.exists()
        else root_b
    )
    loser_root = root_b if winner_root == root_a else root_a
    assert winner_root.exists()
    assert not loser_root.exists()
