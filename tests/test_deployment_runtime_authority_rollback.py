from __future__ import annotations

from pathlib import Path

import pytest

from autosport.deployment_runtime_authority import (
    STORE_SCHEMA,
    STORE_SCHEMA_VERSION,
    DeploymentRuntimeAuthorityStore,
)
from autosport.learning_environment import EnvironmentIdentity, Episode
from autosport.monotonic_workspace_authority import (
    AuthorityPhase,
    MonotonicAuthorityRollbackError,
    MonotonicWorkspaceAuthority,
    MonotonicWorkspaceAuthorityError,
)
from autosport.workspace_lock import WorkspaceEconomicLock


_ACTION_MEANINGS = (
    ("BACK", "Back the represented outcome."),
    ("LAY", "Lay the represented outcome."),
)


def _authority_root(tmp_path: Path) -> Path:
    return (tmp_path.parent / f"{tmp_path.name}-machine-authority").resolve(
        strict=False
    )


def _runtime_inputs(index: int) -> tuple[EnvironmentIdentity, Episode]:
    environment = EnvironmentIdentity(
        source_id="deployment-runtime-test-source",
        config_id="deployment-runtime-test-config",
        data_id=f"deployment-runtime-test-data-{index}",
        protocol_id="deployment-runtime-test-protocol",
        cutoff_ts=f"2026-01-0{index + 1}T00:00:00Z",
        seed=index,
    )
    episode = Episode(
        environment_id=environment.environment_id,
        episode_key=f"deployment-runtime-episode-{index}",
        policy_id="deployment-runtime-test-policy",
        admissible_actions=("BACK", "LAY"),
    )
    return environment, episode


def _append(store: DeploymentRuntimeAuthorityStore, index: int) -> str:
    environment, episode = _runtime_inputs(index)
    record = store.append(
        environment=environment,
        episode=episode,
        action_semantics_version="1",
        action_semantics_meanings=_ACTION_MEANINGS,
    )
    return record.runtime_authority_id


def test_existing_valid_store_without_independent_history_fails_closed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    authority_root = _authority_root(tmp_path)
    DeploymentRuntimeAuthorityStore._write_atomic_path(
        path,
        {
            "schema": STORE_SCHEMA,
            "schema_version": STORE_SCHEMA_VERSION,
            "records": [],
        },
    )

    with pytest.raises(MonotonicAuthorityRollbackError):
        DeploymentRuntimeAuthorityStore(
            path,
            authority_root=authority_root,
        )


def test_pristine_bootstrap_prepares_before_publish_under_one_workspace_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    authority_root = _authority_root(tmp_path)
    workspace = path.parent.resolve(strict=False)
    prepare_seen = False
    workspace_lock_depth = 0

    original_prepare = MonotonicWorkspaceAuthority.prepare
    original_enter = WorkspaceEconomicLock.__enter__
    original_exit = WorkspaceEconomicLock.__exit__
    original_write = DeploymentRuntimeAuthorityStore._write_atomic_path

    def tracked_prepare(
        authority: MonotonicWorkspaceAuthority,
        **kwargs: object,
    ) -> object:
        nonlocal prepare_seen
        result = original_prepare(authority, **kwargs)  # type: ignore[arg-type]
        if (
            authority.workspace.resolve(strict=False) == workspace
            and authority.domain == "deployment-runtime-authority"
        ):
            prepare_seen = True
            assert result.phase is AuthorityPhase.PREPARE
        return result

    def tracked_enter(lock: WorkspaceEconomicLock) -> WorkspaceEconomicLock:
        nonlocal workspace_lock_depth
        result = original_enter(lock)
        if lock.workspace.resolve(strict=False) == workspace:
            workspace_lock_depth += 1
        return result

    def tracked_exit(
        lock: WorkspaceEconomicLock,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        nonlocal workspace_lock_depth
        if lock.workspace.resolve(strict=False) == workspace:
            assert workspace_lock_depth == 1
            workspace_lock_depth -= 1
        original_exit(lock, exc_type, exc_value, traceback)

    def tracked_write(target: Path, payload: object) -> None:
        assert prepare_seen
        assert workspace_lock_depth == 1
        original_write(target, payload)  # type: ignore[arg-type]

    monkeypatch.setattr(MonotonicWorkspaceAuthority, "prepare", tracked_prepare)
    monkeypatch.setattr(WorkspaceEconomicLock, "__enter__", tracked_enter)
    monkeypatch.setattr(WorkspaceEconomicLock, "__exit__", tracked_exit)
    monkeypatch.setattr(
        DeploymentRuntimeAuthorityStore,
        "_write_atomic_path",
        staticmethod(tracked_write),
    )

    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=authority_root,
    )

    assert workspace_lock_depth == 0
    assert store.records() == ()


def test_pristine_bootstrap_crash_before_publish_aborts_and_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    authority_root = _authority_root(tmp_path)
    original_write = DeploymentRuntimeAuthorityStore._write_atomic_path

    def crash_before_publish(_path: Path, _payload: object) -> None:
        raise RuntimeError("simulated pristine crash before publish")

    monkeypatch.setattr(
        DeploymentRuntimeAuthorityStore,
        "_write_atomic_path",
        staticmethod(crash_before_publish),
    )
    with pytest.raises(RuntimeError, match="pristine crash before publish"):
        DeploymentRuntimeAuthorityStore.initialize_pristine(
            path,
            authority_root=authority_root,
        )

    assert not path.exists()
    monkeypatch.setattr(
        DeploymentRuntimeAuthorityStore,
        "_write_atomic_path",
        staticmethod(original_write),
    )

    reopened = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=authority_root,
    )
    assert reopened.records() == ()


def test_pristine_bootstrap_publish_before_commit_recovers_same_tip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    authority_root = _authority_root(tmp_path)
    original_commit = MonotonicWorkspaceAuthority.commit

    def crash_before_commit(
        _authority: MonotonicWorkspaceAuthority,
        **_kwargs: object,
    ) -> object:
        raise RuntimeError("simulated pristine crash after publish")

    monkeypatch.setattr(MonotonicWorkspaceAuthority, "commit", crash_before_commit)
    with pytest.raises(RuntimeError, match="pristine crash after publish"):
        DeploymentRuntimeAuthorityStore.initialize_pristine(
            path,
            authority_root=authority_root,
        )

    pristine_bytes = path.read_bytes()
    monkeypatch.setattr(MonotonicWorkspaceAuthority, "commit", original_commit)

    reopened = DeploymentRuntimeAuthorityStore(
        path,
        authority_root=authority_root,
    )
    assert path.read_bytes() == pristine_bytes
    assert reopened.records() == ()


def test_valid_old_store_restore_is_rejected_by_independent_authority(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    authority_root = _authority_root(tmp_path)
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=authority_root,
    )

    first_id = _append(store, 0)
    valid_old_bytes = path.read_bytes()
    second_id = _append(store, 1)
    assert [record.runtime_authority_id for record in store.records()] == [
        first_id,
        second_id,
    ]

    path.write_bytes(valid_old_bytes)

    with pytest.raises(MonotonicAuthorityRollbackError):
        DeploymentRuntimeAuthorityStore(
            path,
            authority_root=authority_root,
        )


def test_canonical_empty_store_restore_is_rejected_after_commits(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    authority_root = _authority_root(tmp_path)
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=authority_root,
    )
    pristine_bytes = path.read_bytes()

    _append(store, 0)
    _append(store, 1)
    path.write_bytes(pristine_bytes)

    with pytest.raises(MonotonicAuthorityRollbackError):
        DeploymentRuntimeAuthorityStore(
            path,
            authority_root=authority_root,
        )


def test_established_store_cannot_reselect_machine_authority_root(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    first_root = (tmp_path.parent / f"{tmp_path.name}-authority-a").resolve(
        strict=False
    )
    second_root = (tmp_path.parent / f"{tmp_path.name}-authority-b").resolve(
        strict=False
    )
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=first_root,
    )
    _append(store, 0)

    with pytest.raises(MonotonicWorkspaceAuthorityError):
        DeploymentRuntimeAuthorityStore(
            path,
            authority_root=second_root,
        )


def test_deleted_store_is_reported_as_rollback_after_authority_established(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    authority_root = _authority_root(tmp_path)
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=authority_root,
    )
    _append(store, 0)

    path.unlink()

    with pytest.raises(MonotonicAuthorityRollbackError):
        DeploymentRuntimeAuthorityStore(
            path,
            authority_root=authority_root,
        )


def test_reopen_aborts_prepare_when_publish_never_happened(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    authority_root = _authority_root(tmp_path)
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=authority_root,
    )
    first_id = _append(store, 0)
    previous_bytes = path.read_bytes()

    def crash_before_publish(
        _path: Path,
        _payload: object,
    ) -> None:
        raise RuntimeError("simulated crash before local publish")

    monkeypatch.setattr(store, "_write_atomic_path", crash_before_publish)
    with pytest.raises(RuntimeError, match="simulated crash"):
        _append(store, 1)

    assert path.read_bytes() == previous_bytes
    reopened = DeploymentRuntimeAuthorityStore(
        path,
        authority_root=authority_root,
    )
    records = reopened.records()
    assert [record.runtime_authority_id for record in records] == [first_id]

    second_id = _append(reopened, 1)
    assert second_id != first_id
    assert len(reopened.records()) == 2


def test_byte_identical_retry_after_aborted_prepare_uses_fresh_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    authority_root = _authority_root(tmp_path)
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=authority_root,
    )
    _append(store, 0)
    fixed_first_seen = "2100-01-01T00:00:00Z"
    monkeypatch.setattr(store, "_observed_now", lambda: fixed_first_seen)

    def crash_before_publish(
        _path: Path,
        _payload: object,
    ) -> None:
        raise RuntimeError("simulated crash before byte-identical publish")

    monkeypatch.setattr(store, "_write_atomic_path", crash_before_publish)
    with pytest.raises(RuntimeError, match="byte-identical"):
        _append(store, 1)

    reopened = DeploymentRuntimeAuthorityStore(
        path,
        authority_root=authority_root,
    )
    monkeypatch.setattr(reopened, "_observed_now", lambda: fixed_first_seen)
    second_id = _append(reopened, 1)

    assert len(reopened.records()) == 2
    assert reopened.records()[-1].runtime_authority_id == second_id


def test_reopen_commits_prepared_state_after_publish_before_commit_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    authority_root = _authority_root(tmp_path)
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=authority_root,
    )
    first_id = _append(store, 0)

    def crash_before_commit(**_kwargs: object) -> None:
        raise RuntimeError("simulated crash after local publish")

    monkeypatch.setattr(store._authority, "commit", crash_before_commit)
    with pytest.raises(RuntimeError, match="simulated crash"):
        _append(store, 1)

    reopened = DeploymentRuntimeAuthorityStore(
        path,
        authority_root=authority_root,
    )
    records = reopened.records()
    assert len(records) == 2
    assert records[0].runtime_authority_id == first_id
    assert records[1].runtime_authority_id != first_id

    retry_id = _append(reopened, 1)
    assert retry_id == records[1].runtime_authority_id
    assert reopened.records() == records
