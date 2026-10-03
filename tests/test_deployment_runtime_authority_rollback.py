from __future__ import annotations

from pathlib import Path

import pytest

from autosport.deployment_runtime_authority import DeploymentRuntimeAuthorityStore
from autosport.learning_environment import EnvironmentIdentity, Episode
from autosport.monotonic_workspace_authority import MonotonicAuthorityRollbackError


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
