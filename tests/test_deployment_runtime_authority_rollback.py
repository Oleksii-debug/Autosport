from __future__ import annotations

from pathlib import Path

import pytest

import autosport.deployment_runtime_authority as deployment_runtime_authority
from autosport.deployment_runtime_authority import (
    STORE_SCHEMA,
    STORE_SCHEMA_VERSION,
    DeploymentRuntimeAuthorityError,
    DeploymentRuntimeAuthorityRecord,
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


@pytest.mark.parametrize(
    "attribute",
    (
        "path",
        "workspace",
        "_lock",
        "_authority",
        "_semantic_binding_sha256",
    ),
)
def test_runtime_authority_bindings_are_write_once(
    tmp_path: Path,
    attribute: str,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    original = getattr(store, attribute)

    with pytest.raises(
        AttributeError,
        match="authority bindings are write-once",
    ):
        setattr(store, attribute, object())
    assert getattr(store, attribute) is original

    with pytest.raises(
        AttributeError,
        match="authority bindings are write-once",
    ):
        delattr(store, attribute)
    assert getattr(store, attribute) is original

    _append(store, 0)
    assert len(store.records()) == 1


def test_runtime_authority_write_once_guard_cannot_be_shadowed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    original_path = store.path

    with pytest.raises(
        AttributeError,
        match="authority bindings are write-once",
    ):
        store._WRITE_ONCE_AUTHORITY_BINDINGS = frozenset()
    with pytest.raises(
        AttributeError,
        match="authority bindings are write-once",
    ):
        store.path = tmp_path / "redirected-runtime-authority.json"

    assert store.path == original_path
    _append(store, 0)
    assert len(store.records()) == 1


def test_runtime_authority_instance_dict_cannot_shadow_bindings(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    originals = {
        name: getattr(store, name)
        for name in DeploymentRuntimeAuthorityStore._WRITE_ONCE_AUTHORITY_BINDINGS
    }

    # Direct dict mutation bypasses __setattr__ on ordinary Python objects. These
    # hostile entries must remain inert because the canonical bindings are data
    # descriptors backed by slots outside the instance dictionary.
    store.__dict__.update(
        {name: object() for name in originals}
    )
    store.__dict__.update(
        {
            name: object()
            for name in (
                DeploymentRuntimeAuthorityStore._WRITE_ONCE_AUTHORITY_STORAGE_BINDINGS
            )
        }
    )

    for name, original in originals.items():
        assert getattr(store, name) is original

    for storage_name in (
        DeploymentRuntimeAuthorityStore._WRITE_ONCE_AUTHORITY_STORAGE_BINDINGS
    ):
        with pytest.raises(
            AttributeError,
            match="authority bindings are write-once",
        ):
            setattr(store, storage_name, object())

    _append(store, 0)
    assert len(store.records()) == 1


def test_runtime_authority_direct_slot_semantic_rebinding_fails_closed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    _append(store, 0)

    object.__setattr__(
        store,
        "_binding_semantic_binding_sha256",
        "0" * 64,
    )

    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="binding integrity mismatch",
    ):
        store.records()


def test_runtime_authority_direct_slot_path_rebinding_fails_before_read(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    _append(store, 0)
    redirected = tmp_path / "redirected-runtime-authority.json"
    redirected.write_bytes(path.read_bytes())

    object.__setattr__(store, "_binding_path", redirected)

    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="binding integrity mismatch",
    ):
        store.records()


def test_runtime_authority_direct_authority_key_rebinding_fails_closed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    _append(store, 0)

    object.__setattr__(store._authority, "key", "redirected-runtime-authority.json")

    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="binding integrity mismatch",
    ):
        store.records()


def test_coordinated_workspace_rebinding_cannot_transfer_authority(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    _append(store, 0)

    foreign_workspace = tmp_path.parent / f"{tmp_path.name}-foreign-workspace"
    foreign_workspace.mkdir()
    foreign_path = foreign_workspace / path.name
    foreign_path.write_bytes(path.read_bytes())

    object.__setattr__(store, "_binding_path", foreign_path)
    object.__setattr__(store, "_binding_workspace", foreign_workspace)
    object.__setattr__(store._authority, "workspace", foreign_workspace)

    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="binding integrity mismatch",
    ):
        store.records()


def test_runtime_authority_rebinding_cannot_prepare_new_generation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    _append(store, 0)
    history_before = store._authority.read_history()

    object.__setattr__(
        store,
        "_binding_semantic_binding_sha256",
        "0" * 64,
    )

    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="binding integrity mismatch",
    ):
        _append(store, 1)

    assert store._authority.read_history() == history_before


def test_direct_validated_read_rechecks_binding_integrity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    _append(store, 0)
    redirected = tmp_path / "redirected-runtime-authority.json"
    redirected.write_bytes(path.read_bytes())
    object.__setattr__(store, "_binding_path", redirected)

    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="binding integrity mismatch",
    ):
        store._read_validated_records_locked()


def test_runtime_authority_rejects_semantic_binding_history_drift(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    _append(store, 0)

    payload = store._read_payload()
    state_sha256 = store._state_sha256(payload)
    foreign_binding = "f" * 64
    tx_id = "test-semantic-binding-drift"
    store._authority.prepare(
        tx_id=tx_id,
        observed_state_sha256=state_sha256,
        intended_state_sha256=state_sha256,
        semantic_binding_sha256=foreign_binding,
    )
    store._authority.commit(
        tx_id=tx_id,
        observed_state_sha256=state_sha256,
        semantic_binding_sha256=foreign_binding,
    )

    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="semantic binding history mismatch",
    ):
        store.records()


def test_instance_dictionary_cannot_shadow_monotonic_verification_dispatch(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    _append(store, 0)
    valid_old_bytes = path.read_bytes()
    _append(store, 1)

    hostile_calls: list[str] = []

    def hostile_recover(_payload: object) -> None:
        hostile_calls.append("recover")
        raise AssertionError("instance recovery shadow executed")

    def hostile_records() -> tuple[object, ...]:
        hostile_calls.append("records")
        return ()

    store.__dict__["_recover_state"] = hostile_recover
    store.__dict__["records"] = hostile_records
    path.write_bytes(valid_old_bytes)

    with pytest.raises(MonotonicAuthorityRollbackError):
        store.records()

    assert hostile_calls == []



def test_runtime_authority_rejects_in_place_sealed_method_code_replacement(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    _append(store, 0)

    target = DeploymentRuntimeAuthorityStore._recover_state
    original_code = target.__code__

    def hostile(self, payload) -> None:
        del self, payload
        raise AssertionError("hostile recovery executable ran")

    assert hostile.__code__.co_freevars == original_code.co_freevars
    target.__code__ = hostile.__code__
    try:
        with pytest.raises(
            DeploymentRuntimeAuthorityError,
            match="method dispatch was replaced",
        ):
            store.records()
    finally:
        target.__code__ = original_code


def test_runtime_authority_rejects_in_place_sealed_staticmethod_code_replacement(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )

    descriptor = vars(DeploymentRuntimeAuthorityStore)["_state_sha256"]
    target = descriptor.__func__
    original_code = target.__code__

    def hostile(payload) -> str:
        del payload
        raise AssertionError("hostile state digest executable ran")

    assert hostile.__code__.co_freevars == original_code.co_freevars
    target.__code__ = hostile.__code__
    try:
        with pytest.raises(
            DeploymentRuntimeAuthorityError,
            match="method dispatch was replaced",
        ):
            _append(store, 0)
    finally:
        target.__code__ = original_code


def test_runtime_authority_store_rejects_subclass_constructor_dispatch(
    tmp_path: Path,
) -> None:
    hostile_calls = []

    class AttackerStore(DeploymentRuntimeAuthorityStore):
        def _configure(self, *args: object, **kwargs: object) -> None:
            hostile_calls.append((args, kwargs))
            raise AssertionError("hostile subclass configure executed")

    with pytest.raises(
        TypeError,
        match="exact DeploymentRuntimeAuthorityStore",
    ):
        AttackerStore(
            tmp_path / "deployment-runtime-authority.json",
            authority_root=_authority_root(tmp_path),
        )

    assert hostile_calls == []


def test_runtime_authority_pristine_rejects_subclass_before_dispatch(
    tmp_path: Path,
) -> None:
    hostile_calls = []

    class AttackerStore(DeploymentRuntimeAuthorityStore):
        def _configure(self, *args: object, **kwargs: object) -> None:
            hostile_calls.append((args, kwargs))
            raise AssertionError("hostile subclass configure executed")

    with pytest.raises(
        TypeError,
        match="exact DeploymentRuntimeAuthorityStore",
    ):
        AttackerStore.initialize_pristine(
            tmp_path / "deployment-runtime-authority.json",
            authority_root=_authority_root(tmp_path),
        )

    assert hostile_calls == []


def test_runtime_authority_rejects_monotonic_authority_alias_replacement_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hostile_calls = []

    class HostileAuthority:
        def __init__(self, *args: object, **kwargs: object) -> None:
            hostile_calls.append((args, kwargs))
            raise AssertionError("hostile authority constructor executed")

    monkeypatch.setattr(
        deployment_runtime_authority,
        "MonotonicWorkspaceAuthority",
        HostileAuthority,
    )
    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="constructor dispatch was replaced",
    ):
        DeploymentRuntimeAuthorityStore.initialize_pristine(
            tmp_path / "deployment-runtime-authority.json",
            authority_root=_authority_root(tmp_path),
        )

    assert hostile_calls == []


@pytest.mark.parametrize(
    ("attribute", "message"),
    (
        ("RLock", "local lock constructor dispatch was replaced"),
        ("WorkspaceEconomicLock", "workspace lock constructor dispatch was replaced"),
    ),
)
def test_runtime_authority_rejects_lock_constructor_alias_replacement_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attribute: str,
    message: str,
) -> None:
    hostile_calls = []

    def hostile(*args: object, **kwargs: object) -> object:
        hostile_calls.append((args, kwargs))
        raise AssertionError("hostile lock constructor executed")

    monkeypatch.setattr(deployment_runtime_authority, attribute, hostile)
    with pytest.raises(DeploymentRuntimeAuthorityError, match=message):
        DeploymentRuntimeAuthorityStore.initialize_pristine(
            tmp_path / "deployment-runtime-authority.json",
            authority_root=_authority_root(tmp_path),
        )

    assert hostile_calls == []


@pytest.mark.parametrize("attribute", ("__init__", "__new__"))
def test_runtime_authority_rejects_monotonic_constructor_surface_replacement(
    tmp_path: Path,
    attribute: str,
) -> None:
    original = vars(MonotonicWorkspaceAuthority).get(attribute)
    hostile_calls = []

    def hostile(*args: object, **kwargs: object) -> None:
        hostile_calls.append((args, kwargs))
        raise AssertionError("hostile authority constructor surface executed")

    type.__setattr__(MonotonicWorkspaceAuthority, attribute, hostile)
    try:
        with pytest.raises(
            DeploymentRuntimeAuthorityError,
            match="constructor dispatch was replaced",
        ):
            DeploymentRuntimeAuthorityStore.initialize_pristine(
                tmp_path / "deployment-runtime-authority.json",
                authority_root=_authority_root(tmp_path),
            )
    finally:
        if original is None:
            type.__delattr__(MonotonicWorkspaceAuthority, attribute)
        else:
            type.__setattr__(MonotonicWorkspaceAuthority, attribute, original)

    assert hostile_calls == []


def test_runtime_authority_rejects_monotonic_init_code_replacement(
    tmp_path: Path,
) -> None:
    target = vars(MonotonicWorkspaceAuthority)["__init__"]
    original_code = target.__code__

    def hostile(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("hostile authority initializer code executed")

    assert hostile.__code__.co_freevars == original_code.co_freevars
    target.__code__ = hostile.__code__
    try:
        with pytest.raises(
            DeploymentRuntimeAuthorityError,
            match="constructor dispatch was replaced",
        ):
            DeploymentRuntimeAuthorityStore.initialize_pristine(
                tmp_path / "deployment-runtime-authority.json",
                authority_root=_authority_root(tmp_path),
            )
    finally:
        target.__code__ = original_code


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


def test_pristine_bootstrap_has_no_post_publish_pre_authority_lock_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replacement_workspace = tmp_path.parent / f"{tmp_path.name}-replacement"
    replacement_path = replacement_workspace / "deployment-runtime-authority.json"
    replacement_root = tmp_path.parent / f"{tmp_path.name}-replacement-authority"
    replacement = DeploymentRuntimeAuthorityStore.initialize_pristine(
        replacement_path,
        authority_root=replacement_root.resolve(strict=False),
    )
    _append(replacement, 0)
    replacement_bytes = replacement_path.read_bytes()

    path = tmp_path / "deployment-runtime-authority.json"
    authority_root = _authority_root(tmp_path)
    workspace = path.parent.resolve(strict=False)
    target_commit_seen = False
    replacement_performed = False
    injecting = False

    original_commit = MonotonicWorkspaceAuthority.commit
    original_exit = WorkspaceEconomicLock.__exit__

    def tracked_commit(
        authority: MonotonicWorkspaceAuthority,
        **kwargs: object,
    ) -> object:
        nonlocal target_commit_seen
        result = original_commit(authority, **kwargs)  # type: ignore[arg-type]
        if (
            authority.workspace.resolve(strict=False) == workspace
            and authority.domain == "deployment-runtime-authority"
        ):
            target_commit_seen = True
        return result

    def adversarial_exit(
        lock: WorkspaceEconomicLock,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        nonlocal injecting, replacement_performed
        original_exit(lock, exc_type, exc_value, traceback)
        if (
            not injecting
            and not target_commit_seen
            and lock.workspace.resolve(strict=False) == workspace
            and path.exists()
        ):
            injecting = True
            try:
                with WorkspaceEconomicLock(workspace):
                    path.write_bytes(replacement_bytes)
                replacement_performed = True
            finally:
                injecting = False

    monkeypatch.setattr(MonotonicWorkspaceAuthority, "commit", tracked_commit)
    monkeypatch.setattr(WorkspaceEconomicLock, "__exit__", adversarial_exit)

    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=authority_root,
    )

    assert target_commit_seen
    assert not replacement_performed
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


def test_local_publish_directory_sync_precedes_monotonic_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    authority_root = _authority_root(tmp_path)
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=authority_root,
    )
    directory_sync_seen = False

    original_sync = deployment_runtime_authority._fsync_directory
    original_commit = MonotonicWorkspaceAuthority.commit

    def tracked_sync(directory: Path) -> None:
        nonlocal directory_sync_seen
        original_sync(directory)
        if directory.resolve(strict=False) == store.workspace:
            directory_sync_seen = True

    def tracked_commit(
        authority: MonotonicWorkspaceAuthority,
        **kwargs: object,
    ) -> object:
        if authority is store._authority:
            assert directory_sync_seen
        return original_commit(authority, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        deployment_runtime_authority,
        "_fsync_directory",
        tracked_sync,
    )
    monkeypatch.setattr(MonotonicWorkspaceAuthority, "commit", tracked_commit)

    _append(store, 0)

    assert directory_sync_seen


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

def test_post_init_hardlink_split_cannot_bootstrap_alias_authority(
    tmp_path: Path,
) -> None:
    """A valid-old hard-link alias must not mint a second runtime authority."""

    path = tmp_path / "deployment-runtime-authority.json"
    alias = tmp_path / "deployment-runtime-authority-alias.json"
    authority_root = _authority_root(tmp_path)
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=authority_root,
    )
    first_id = _append(store, 0)
    valid_old_bytes = path.read_bytes()

    try:
        alias.hardlink_to(path)
    except OSError as exc:
        pytest.skip(f"hard links unavailable in this environment: {exc}")

    assert alias.read_bytes() == valid_old_bytes
    second_id = _append(store, 1)

    assert second_id != first_id
    assert path.read_bytes() != valid_old_bytes
    assert alias.read_bytes() == valid_old_bytes
    assert len(store.records()) == 2

    with pytest.raises(MonotonicAuthorityRollbackError):
        DeploymentRuntimeAuthorityStore(
            alias,
            authority_root=authority_root,
        )

    assert alias.read_bytes() == valid_old_bytes



def test_runtime_authority_rejects_hashlib_module_rebinding_before_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    _append(store, 0)
    hostile_calls: list[bytes] = []

    class HostileHashlib:
        @staticmethod
        def sha256(value: bytes) -> object:
            hostile_calls.append(value)
            raise AssertionError("hostile SHA-256 executed")

    monkeypatch.setattr(deployment_runtime_authority, "hashlib", HostileHashlib)
    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="digest dispatch was replaced",
    ):
        store.records()

    assert hostile_calls == []


def test_runtime_authority_rejects_sha256_member_rebinding_before_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    _append(store, 0)
    hostile_calls: list[bytes] = []

    def hostile_sha256(value: bytes) -> object:
        hostile_calls.append(value)
        raise AssertionError("hostile SHA-256 executed")

    monkeypatch.setattr(
        deployment_runtime_authority.hashlib,
        "sha256",
        hostile_sha256,
    )
    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="digest dispatch was replaced",
    ):
        store.records()

    assert hostile_calls == []


def test_runtime_authority_rejects_json_module_rebinding_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    _append(store, 0)
    hostile_calls: list[object] = []

    class HostileJson:
        @staticmethod
        def dumps(value: object, **kwargs: object) -> str:
            hostile_calls.append((value, kwargs))
            raise AssertionError("hostile JSON serialization executed")

        @staticmethod
        def loads(value: str) -> object:
            hostile_calls.append(value)
            raise AssertionError("hostile JSON parsing executed")

    monkeypatch.setattr(deployment_runtime_authority, "json", HostileJson)
    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="JSON serialization dispatch was replaced",
    ):
        store.records()

    assert hostile_calls == []


def test_runtime_authority_rejects_json_dumps_member_rebinding_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    _append(store, 0)
    hostile_calls: list[object] = []

    def hostile_dumps(value: object, **kwargs: object) -> str:
        hostile_calls.append((value, kwargs))
        raise AssertionError("hostile JSON serialization executed")

    monkeypatch.setattr(
        deployment_runtime_authority.json,
        "dumps",
        hostile_dumps,
    )
    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="JSON serialization dispatch was replaced",
    ):
        store.records()

    assert hostile_calls == []


def test_runtime_authority_rejects_canonical_json_alias_rebinding_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    _append(store, 0)
    hostile_calls: list[object] = []

    def hostile_encoder(value: object) -> str:
        hostile_calls.append(value)
        raise AssertionError("hostile canonical encoder executed")

    monkeypatch.setattr(
        deployment_runtime_authority,
        "_canonical_json",
        hostile_encoder,
    )
    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="digest dispatch was replaced",
    ):
        store.records()

    assert hostile_calls == []


def test_runtime_authority_rejects_canonical_json_code_replacement_before_read(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    _append(store, 0)
    target = deployment_runtime_authority._canonical_json
    original_code = target.__code__

    def hostile_encoder(value: object) -> str:
        del value
        raise AssertionError("hostile canonical encoder code executed")

    assert hostile_encoder.__code__.co_freevars == original_code.co_freevars
    target.__code__ = hostile_encoder.__code__
    try:
        with pytest.raises(
            DeploymentRuntimeAuthorityError,
            match="digest dispatch was replaced",
        ):
            store.records()
    finally:
        target.__code__ = original_code


def test_runtime_authority_rejects_uuid_module_rebinding_before_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    hostile_calls: list[None] = []

    class HostileUuid:
        @staticmethod
        def uuid4() -> object:
            hostile_calls.append(None)
            raise AssertionError("hostile uuid4 executed")

    monkeypatch.setattr(deployment_runtime_authority, "uuid", HostileUuid)
    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="transaction-id dispatch was replaced",
    ):
        _append(store, 0)

    assert hostile_calls == []


def test_runtime_authority_rejects_uuid4_member_rebinding_before_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    hostile_calls: list[None] = []

    def hostile_uuid4() -> object:
        hostile_calls.append(None)
        raise AssertionError("hostile uuid4 executed")

    monkeypatch.setattr(
        deployment_runtime_authority.uuid,
        "uuid4",
        hostile_uuid4,
    )
    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="transaction-id dispatch was replaced",
    ):
        _append(store, 0)

    assert hostile_calls == []


def test_runtime_authority_rejects_json_loads_member_rebinding_before_parse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    _append(store, 0)
    hostile_calls: list[str] = []

    def hostile_loads(value: str) -> object:
        hostile_calls.append(value)
        raise AssertionError("hostile JSON parsing executed")

    monkeypatch.setattr(
        deployment_runtime_authority.json,
        "loads",
        hostile_loads,
    )
    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="JSON parsing dispatch was replaced",
    ):
        store.records()

    assert hostile_calls == []


def test_runtime_authority_rejects_json_loads_code_replacement_before_parse(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    _append(store, 0)
    target = deployment_runtime_authority.json.loads
    original_code = target.__code__

    def hostile_loads(value: str) -> object:
        del value
        raise AssertionError("hostile JSON parser code executed")

    assert hostile_loads.__code__.co_freevars == original_code.co_freevars
    target.__code__ = hostile_loads.__code__
    try:
        with pytest.raises(
            DeploymentRuntimeAuthorityError,
            match="JSON parsing dispatch was replaced",
        ):
            store.records()
    finally:
        target.__code__ = original_code


def test_runtime_authority_rejects_uuid4_code_replacement_before_prepare(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    target = deployment_runtime_authority.uuid.uuid4
    original_code = target.__code__

    def hostile_uuid4() -> object:
        raise AssertionError("hostile uuid4 code executed")

    assert hostile_uuid4.__code__.co_freevars == original_code.co_freevars
    target.__code__ = hostile_uuid4.__code__
    try:
        with pytest.raises(
            DeploymentRuntimeAuthorityError,
            match="transaction-id dispatch was replaced",
        ):
            _append(store, 0)
    finally:
        target.__code__ = original_code


def test_runtime_authority_rejects_digest_alias_rebinding_at_binding_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    hostile_calls: list[object] = []

    def hostile_digest(value: object) -> str:
        hostile_calls.append(value)
        return store._semantic_binding_sha256

    monkeypatch.setattr(
        deployment_runtime_authority,
        "_digest",
        hostile_digest,
    )
    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="binding validator dispatch was replaced",
    ):
        store.records()

    assert hostile_calls == []


def test_runtime_authority_rejects_digest_code_replacement_at_binding_boundary(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    target = deployment_runtime_authority._digest
    original_code = target.__code__

    def hostile_digest(value: object) -> str:
        del value
        raise AssertionError("hostile digest code executed")

    assert hostile_digest.__code__.co_freevars == original_code.co_freevars
    target.__code__ = hostile_digest.__code__
    try:
        with pytest.raises(
            DeploymentRuntimeAuthorityError,
            match="binding validator dispatch was replaced",
        ):
            store.records()
    finally:
        target.__code__ = original_code


def test_runtime_authority_rejects_sha_validator_alias_rebinding_at_binding_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    hostile_calls: list[object] = []

    def hostile_sha(value: object, name: str) -> str:
        hostile_calls.append((value, name))
        return store._semantic_binding_sha256

    monkeypatch.setattr(
        deployment_runtime_authority,
        "_sha",
        hostile_sha,
    )
    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="binding validator dispatch was replaced",
    ):
        store.records()

    assert hostile_calls == []


def test_runtime_authority_rejects_sha_validator_code_replacement_at_binding_boundary(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    target = deployment_runtime_authority._sha
    original_code = target.__code__

    def hostile_sha(value: object, name: str) -> str:
        del value, name
        raise AssertionError("hostile SHA validator code executed")

    assert hostile_sha.__code__.co_freevars == original_code.co_freevars
    target.__code__ = hostile_sha.__code__
    try:
        with pytest.raises(
            DeploymentRuntimeAuthorityError,
            match="binding validator dispatch was replaced",
        ):
            store.records()
    finally:
        target.__code__ = original_code


def test_runtime_authority_rejects_configure_descriptor_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hostile_calls: list[object] = []

    def hostile_configure(
        self: object,
        path: object,
        *,
        authority_root: object,
    ) -> None:
        hostile_calls.append((self, path, authority_root))
        raise AssertionError("hostile runtime authority configure executed")

    monkeypatch.setattr(
        DeploymentRuntimeAuthorityStore,
        "_configure",
        hostile_configure,
    )
    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="method dispatch was replaced",
    ):
        DeploymentRuntimeAuthorityStore.initialize_pristine(
            tmp_path / "deployment-runtime-authority.json",
            authority_root=_authority_root(tmp_path),
        )

    assert hostile_calls == []


def test_runtime_authority_rejects_configure_code_replacement(
    tmp_path: Path,
) -> None:
    target = DeploymentRuntimeAuthorityStore._configure
    original_code = target.__code__

    def hostile_configure(
        self: object,
        path: object,
        *,
        authority_root: object,
    ) -> None:
        del self, path, authority_root
        raise AssertionError("hostile runtime authority configure code executed")

    assert hostile_configure.__code__.co_freevars == original_code.co_freevars
    target.__code__ = hostile_configure.__code__
    try:
        with pytest.raises(
            DeploymentRuntimeAuthorityError,
            match="method dispatch was replaced",
        ):
            DeploymentRuntimeAuthorityStore.initialize_pristine(
                tmp_path / "deployment-runtime-authority.json",
                authority_root=_authority_root(tmp_path),
            )
    finally:
        target.__code__ = original_code


@pytest.mark.parametrize(
    ("attribute", "replacement"),
    (
        ("STORE_SCHEMA", "hostile.runtime.store"),
        ("STORE_SCHEMA_VERSION", 999),
        ("RECORD_SCHEMA", "hostile.runtime.record"),
        ("RECORD_SCHEMA_VERSION", 999),
        ("_EMPTY_CHAIN_SHA256", "f" * 64),
        ("_HEX", frozenset("0123456789abcdefg")),
        ("_AUTHORITY_DOMAIN", "hostile-runtime-authority"),
        ("_AUTHORITY_BINDING_SCHEMA", "hostile.runtime.binding"),
        ("_AUTHORITY_BINDING_SCHEMA_VERSION", 999),
    ),
)
def test_runtime_authority_rejects_static_contract_rebinding_before_authority_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attribute: str,
    replacement: object,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    authority_root = _authority_root(tmp_path)
    monkeypatch.setattr(
        deployment_runtime_authority,
        attribute,
        replacement,
    )

    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="static contract was replaced",
    ):
        DeploymentRuntimeAuthorityStore.initialize_pristine(
            path,
            authority_root=authority_root,
        )

    assert not path.exists()
    assert not authority_root.exists()


def test_runtime_authority_revalidates_static_contract_before_existing_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    pristine_bytes = path.read_bytes()

    monkeypatch.setattr(
        deployment_runtime_authority,
        "_AUTHORITY_DOMAIN",
        "hostile-runtime-authority",
    )
    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="static contract was replaced",
    ):
        store.records()

    assert path.read_bytes() == pristine_bytes


def test_runtime_authority_rejects_record_type_alias_rebinding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )

    class HostileRecord:
        pass

    monkeypatch.setattr(
        deployment_runtime_authority,
        "DeploymentRuntimeAuthorityRecord",
        HostileRecord,
    )
    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="record type dispatch was replaced",
    ):
        store.records()


@pytest.mark.parametrize("name", ("create", "from_dict"))
def test_runtime_authority_rejects_record_classmethod_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    hostile_calls: list[object] = []

    def hostile(cls: object, *args: object, **kwargs: object) -> object:
        hostile_calls.append((cls, args, kwargs))
        raise AssertionError("hostile runtime record codec executed")

    monkeypatch.setattr(
        DeploymentRuntimeAuthorityRecord,
        name,
        classmethod(hostile),
    )
    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="record codec dispatch was replaced",
    ):
        store.records()

    assert hostile_calls == []


def test_runtime_authority_rejects_record_method_code_replacement(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    target = vars(DeploymentRuntimeAuthorityRecord)["to_dict"]
    original_code = target.__code__

    def hostile(self: object) -> dict[str, object]:
        del self
        raise AssertionError("hostile runtime record encoder code executed")

    assert hostile.__code__.co_freevars == original_code.co_freevars
    target.__code__ = hostile.__code__
    try:
        with pytest.raises(
            DeploymentRuntimeAuthorityError,
            match="record codec dispatch was replaced",
        ):
            store.records()
    finally:
        target.__code__ = original_code


def test_runtime_authority_rejects_record_property_code_replacement(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    descriptor = vars(DeploymentRuntimeAuthorityRecord)["identity_payload"]
    target = descriptor.fget
    assert target is not None
    original_code = target.__code__

    def hostile(self: object) -> dict[str, object]:
        del self
        raise AssertionError("hostile runtime record identity code executed")

    assert hostile.__code__.co_freevars == original_code.co_freevars
    target.__code__ = hostile.__code__
    try:
        with pytest.raises(
            DeploymentRuntimeAuthorityError,
            match="record codec dispatch was replaced",
        ):
            store.records()
    finally:
        target.__code__ = original_code


def test_runtime_authority_rejects_record_classmethod_code_replacement(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    descriptor = vars(DeploymentRuntimeAuthorityRecord)["from_dict"]
    target = descriptor.__func__
    original_code = target.__code__

    def hostile(cls: object, raw: object) -> object:
        del cls, raw
        raise AssertionError("hostile runtime record parser code executed")

    assert hostile.__code__.co_freevars == original_code.co_freevars
    target.__code__ = hostile.__code__
    try:
        with pytest.raises(
            DeploymentRuntimeAuthorityError,
            match="record codec dispatch was replaced",
        ):
            store.records()
    finally:
        target.__code__ = original_code


def test_runtime_authority_rejects_record_codec_guard_alias_rebinding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    hostile_calls: list[None] = []

    def hostile_guard() -> None:
        hostile_calls.append(None)

    monkeypatch.setattr(
        deployment_runtime_authority,
        "_assert_canonical_record_codec",
        hostile_guard,
    )
    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="record codec guard dispatch was replaced",
    ):
        store.records()

    assert hostile_calls == []


def test_runtime_authority_rejects_record_codec_guard_code_replacement(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    target = deployment_runtime_authority._assert_canonical_record_codec
    original_code = target.__code__

    def hostile_guard() -> None:
        raise AssertionError("hostile record codec guard executed")

    assert hostile_guard.__code__.co_freevars == original_code.co_freevars
    target.__code__ = hostile_guard.__code__
    try:
        with pytest.raises(
            DeploymentRuntimeAuthorityError,
            match="record codec guard dispatch was replaced",
        ):
            store.records()
    finally:
        target.__code__ = original_code


def test_runtime_authority_rejects_record_codec_requirement_alias_rebinding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    hostile_calls: list[None] = []

    def hostile_requirement() -> None:
        hostile_calls.append(None)

    monkeypatch.setattr(
        deployment_runtime_authority,
        "_require_canonical_record_codec",
        hostile_requirement,
    )
    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="record codec requirement dispatch was replaced",
    ):
        store.records()

    assert hostile_calls == []


def test_runtime_authority_rejects_record_codec_requirement_code_replacement(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    target = deployment_runtime_authority._require_canonical_record_codec
    original_code = target.__code__

    def hostile_requirement() -> None:
        raise AssertionError("hostile record codec requirement executed")

    assert hostile_requirement.__code__.co_freevars == original_code.co_freevars
    target.__code__ = hostile_requirement.__code__
    try:
        with pytest.raises(
            DeploymentRuntimeAuthorityError,
            match="record codec requirement dispatch was replaced",
        ):
            store.records()
    finally:
        target.__code__ = original_code


@pytest.mark.parametrize(
    "helper_name",
    (
        "_text",
        "_timestamp",
        "_environment_payload",
        "_action_semantics_payload",
        "_environment_from_payload",
        "_action_semantics_from_payload",
    ),
)
def test_runtime_authority_rejects_record_helper_alias_rebinding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    helper_name: str,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    hostile_calls: list[object] = []

    def hostile(*args: object, **kwargs: object) -> object:
        hostile_calls.append((args, kwargs))
        raise AssertionError("hostile runtime record helper executed")

    monkeypatch.setattr(
        deployment_runtime_authority,
        helper_name,
        hostile,
    )
    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="record helper dispatch was replaced",
    ):
        store.records()

    assert hostile_calls == []


@pytest.mark.parametrize(
    "helper_name",
    (
        "_timestamp",
        "_environment_payload",
        "_action_semantics_payload",
        "_action_semantics_from_payload",
    ),
)
def test_runtime_authority_rejects_record_helper_code_replacement(
    tmp_path: Path,
    helper_name: str,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    target = getattr(deployment_runtime_authority, helper_name)
    original_code = target.__code__

    def hostile(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("hostile runtime record helper code executed")

    assert hostile.__code__.co_freevars == original_code.co_freevars
    target.__code__ = hostile.__code__
    try:
        with pytest.raises(
            DeploymentRuntimeAuthorityError,
            match="record helper dispatch was replaced",
        ):
            store.records()
    finally:
        target.__code__ = original_code


@pytest.mark.parametrize("type_name", ("EnvironmentIdentity", "Episode"))
def test_runtime_authority_rejects_semantic_type_alias_rebinding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    type_name: str,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )

    class HostileSemanticType:
        pass

    monkeypatch.setattr(
        deployment_runtime_authority,
        type_name,
        HostileSemanticType,
    )
    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="semantic type dispatch was replaced",
    ):
        store.records()


def test_runtime_authority_rejects_instant_alias_rebinding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    hostile_calls: list[object] = []

    def hostile(value: object, name: str) -> object:
        hostile_calls.append((value, name))
        raise AssertionError("hostile instant parser executed")

    monkeypatch.setattr(deployment_runtime_authority, "_instant", hostile)
    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="record helper dispatch was replaced",
    ):
        store.records()

    assert hostile_calls == []


def test_runtime_authority_rejects_instant_code_replacement(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    target = deployment_runtime_authority._instant
    original_code = target.__code__

    def hostile(value: object, name: str) -> object:
        del value, name
        raise AssertionError("hostile instant parser code executed")

    assert hostile.__code__.co_freevars == original_code.co_freevars
    target.__code__ = hostile.__code__
    try:
        with pytest.raises(
            DeploymentRuntimeAuthorityError,
            match="record helper dispatch was replaced",
        ):
            store.records()
    finally:
        target.__code__ = original_code


@pytest.mark.parametrize(
    "field_name",
    (
        "namespace_sha256",
        "journal_dir",
        "records_dir",
        "namespace_marker_path",
        "authority_root_activation_path",
        "authority_root_binding_path",
        "workspace_binding_path",
    ),
)
def test_runtime_authority_rejects_derived_namespace_path_rebinding(
    tmp_path: Path,
    field_name: str,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    replacement: object = (
        "f" * 64
        if field_name == "namespace_sha256"
        else tmp_path / f"redirected-{field_name}.json"
    )
    object.__setattr__(store._authority, field_name, replacement)

    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="binding integrity mismatch",
    ):
        store.records()


@pytest.mark.parametrize(
    "field_name",
    ("workspace_locator", "workspace_locator_sha256", "path_binding_path"),
)
def test_runtime_authority_rejects_workspace_binding_topology_rebinding(
    tmp_path: Path,
    field_name: str,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    binding = store._authority.workspace_binding
    replacement: object = (
        tmp_path / "redirected-workspace-binding.json"
        if field_name == "path_binding_path"
        else ("f" * 64 if field_name.endswith("sha256") else "hostile-workspace")
    )
    object.__setattr__(binding, field_name, replacement)

    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="binding integrity mismatch",
    ):
        store.records()


@pytest.mark.parametrize(
    "field_name",
    (
        "workspace_locator",
        "workspace_locator_sha256",
        "authority_root_locator",
        "authority_root_locator_sha256",
        "authority_root_resolved",
        "authority_root_resolved_sha256",
        "binding_path",
        "store_root",
    ),
)
def test_runtime_authority_rejects_root_selection_context_rebinding(
    tmp_path: Path,
    field_name: str,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    context = store._authority.authority_root_selection.context
    if field_name in {"binding_path", "store_root"}:
        replacement: object = tmp_path / f"redirected-{field_name}"
    elif field_name.endswith("sha256"):
        replacement = "f" * 64
    else:
        replacement = f"hostile-{field_name}"
    object.__setattr__(context, field_name, replacement)

    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="nested binding type mismatch|binding integrity mismatch",
    ):
        store.records()


@pytest.mark.parametrize(
    "field_name",
    ("workspace_binding", "authority_root_selection"),
)
def test_runtime_authority_rejects_nested_binding_object_replacement(
    tmp_path: Path,
    field_name: str,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    object.__setattr__(store._authority, field_name, object())

    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="nested binding type mismatch",
    ):
        store.records()


@pytest.mark.parametrize(
    ("binding_type_name", "method_name"),
    (
        ("WorkspaceIdentityBinding", "validate_existing"),
        ("WorkspaceIdentityBinding", "ensure_bound"),
        ("AuthorityRootSelectionBinding", "validate_existing"),
        ("AuthorityRootSelectionBinding", "ensure_bound"),
        ("AuthorityRootSelectionBinding", "validate_namespace_activation"),
        ("AuthorityRootSelectionBinding", "ensure_namespace_activated"),
    ),
)
def test_runtime_authority_rejects_nested_binding_descriptor_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    binding_type_name: str,
    method_name: str,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    binding_type = getattr(deployment_runtime_authority, binding_type_name)
    hostile_calls: list[object] = []

    def hostile(*args: object, **kwargs: object) -> object:
        hostile_calls.append((args, kwargs))
        raise AssertionError("hostile nested binding method executed")

    monkeypatch.setattr(binding_type, method_name, hostile)
    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="nested binding dispatch was replaced",
    ):
        store.records()

    assert hostile_calls == []


@pytest.mark.parametrize(
    ("binding_type_name", "method_name"),
    (
        ("WorkspaceIdentityBinding", "validate_existing"),
        ("AuthorityRootSelectionBinding", "validate_namespace_activation"),
    ),
)
def test_runtime_authority_rejects_nested_binding_code_replacement(
    tmp_path: Path,
    binding_type_name: str,
    method_name: str,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    binding_type = getattr(deployment_runtime_authority, binding_type_name)
    target = vars(binding_type)[method_name]
    original_code = target.__code__

    def hostile(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("hostile nested binding method code executed")

    assert hostile.__code__.co_freevars == original_code.co_freevars
    target.__code__ = hostile.__code__
    try:
        with pytest.raises(
            DeploymentRuntimeAuthorityError,
            match="nested binding dispatch was replaced",
        ):
            store.records()
    finally:
        target.__code__ = original_code


def test_runtime_authority_rejects_path_normalization_module_rebinding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )

    class HostileOs:
        path = object()

    monkeypatch.setattr(deployment_runtime_authority, "os", HostileOs)
    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="path-normalization dispatch was replaced",
    ):
        store.records()


@pytest.mark.parametrize(
    "helper_name",
    (
        "_new_local_lock",
        "_workspace_economic_lock",
        "_construct_monotonic_authority",
        "_assert_canonical_monotonic_authority_constructor",
    ),
)
def test_runtime_authority_rejects_construction_helper_alias_rebinding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    helper_name: str,
) -> None:
    hostile_calls: list[object] = []

    def hostile(*args: object, **kwargs: object) -> object:
        hostile_calls.append((args, kwargs))
        raise AssertionError("hostile construction helper executed")

    monkeypatch.setattr(deployment_runtime_authority, helper_name, hostile)
    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="construction helper dispatch was replaced|constructor guard dispatch was replaced",
    ):
        DeploymentRuntimeAuthorityStore.initialize_pristine(
            tmp_path / "deployment-runtime-authority.json",
            authority_root=_authority_root(tmp_path),
        )

    assert hostile_calls == []


@pytest.mark.parametrize(
    "helper_name",
    (
        "_new_local_lock",
        "_workspace_economic_lock",
        "_construct_monotonic_authority",
        "_assert_canonical_monotonic_authority_constructor",
    ),
)
def test_runtime_authority_rejects_construction_helper_code_replacement(
    tmp_path: Path,
    helper_name: str,
) -> None:
    target = getattr(deployment_runtime_authority, helper_name)
    original_code = target.__code__

    def hostile(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("hostile construction helper code executed")

    assert hostile.__code__.co_freevars == original_code.co_freevars
    target.__code__ = hostile.__code__
    try:
        with pytest.raises(
            DeploymentRuntimeAuthorityError,
            match="construction helper dispatch was replaced|constructor guard dispatch was replaced",
        ):
            DeploymentRuntimeAuthorityStore.initialize_pristine(
                tmp_path / "deployment-runtime-authority.json",
                authority_root=_authority_root(tmp_path),
            )
    finally:
        target.__code__ = original_code


@pytest.mark.parametrize(
    ("name", "replacement"),
    (
        ("Path", object),
        ("AuthorityPhase", object),
        ("RecoveryDisposition", object),
        ("MONOTONIC_AUTHORITY_ID", "hostile.monotonic.authority"),
    ),
)
def test_runtime_authority_rejects_core_dependency_alias_rebinding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    replacement: object,
) -> None:
    monkeypatch.setattr(deployment_runtime_authority, name, replacement)
    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="static contract was replaced",
    ):
        DeploymentRuntimeAuthorityStore.initialize_pristine(
            tmp_path / "deployment-runtime-authority.json",
            authority_root=_authority_root(tmp_path),
        )


def test_runtime_authority_rejects_local_lock_storage_rebinding(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    object.__setattr__(store, "_binding_lock", object())

    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="binding integrity mismatch",
    ):
        store.records()


@pytest.mark.parametrize("name", ("datetime", "timezone"))
def test_runtime_authority_rejects_time_dependency_alias_rebinding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )

    monkeypatch.setattr(deployment_runtime_authority, name, object())
    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="semantic type dispatch was replaced",
    ):
        store.records()


def test_runtime_authority_rejects_record_init_descriptor_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    hostile_calls: list[object] = []

    def hostile_init(self: object, *args: object, **kwargs: object) -> None:
        hostile_calls.append((self, args, kwargs))

    monkeypatch.setattr(
        DeploymentRuntimeAuthorityRecord,
        "__init__",
        hostile_init,
    )
    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="record codec dispatch was replaced",
    ):
        store.records()

    assert hostile_calls == []


def test_runtime_authority_rejects_record_init_code_replacement(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    target = vars(DeploymentRuntimeAuthorityRecord)["__init__"]
    original_code = target.__code__

    def hostile_init(self: object, *args: object, **kwargs: object) -> None:
        del self, args, kwargs
        raise AssertionError("hostile runtime record init code executed")

    assert hostile_init.__code__.co_freevars == original_code.co_freevars
    target.__code__ = hostile_init.__code__
    try:
        with pytest.raises(
            DeploymentRuntimeAuthorityError,
            match="record codec dispatch was replaced",
        ):
            store.records()
    finally:
        target.__code__ = original_code


@pytest.mark.parametrize(
    ("type_name", "member_name"),
    (
        ("EnvironmentIdentity", "__post_init__"),
        ("EnvironmentIdentity", "environment_id"),
        ("Episode", "__post_init__"),
        ("Episode", "episode_id"),
    ),
)
def test_runtime_authority_rejects_semantic_surface_descriptor_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    type_name: str,
    member_name: str,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    semantic_type = getattr(deployment_runtime_authority, type_name)
    hostile_calls: list[object] = []

    def hostile(*args: object, **kwargs: object) -> object:
        hostile_calls.append((args, kwargs))
        raise AssertionError("hostile semantic identity surface executed")

    current = vars(semantic_type)[member_name]
    replacement: object = property(hostile) if isinstance(current, property) else hostile
    monkeypatch.setattr(semantic_type, member_name, replacement)

    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="semantic type surface was replaced",
    ):
        store.records()

    assert hostile_calls == []


@pytest.mark.parametrize(
    ("type_name", "member_name"),
    (
        ("EnvironmentIdentity", "__init__"),
        ("EnvironmentIdentity", "environment_id"),
        ("Episode", "__init__"),
        ("Episode", "episode_id"),
    ),
)
def test_runtime_authority_rejects_semantic_surface_code_replacement(
    tmp_path: Path,
    type_name: str,
    member_name: str,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    semantic_type = getattr(deployment_runtime_authority, type_name)
    descriptor = vars(semantic_type)[member_name]
    target = descriptor.fget if isinstance(descriptor, property) else descriptor
    assert target is not None
    original_code = target.__code__

    def hostile(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("hostile semantic identity code executed")

    assert hostile.__code__.co_freevars == original_code.co_freevars
    target.__code__ = hostile.__code__
    try:
        with pytest.raises(
            DeploymentRuntimeAuthorityError,
            match="semantic type surface was replaced",
        ):
            store.records()
    finally:
        target.__code__ = original_code


@pytest.mark.parametrize(
    "helper_name",
    (
        "_canonical_text",
        "_timestamp",
        "_timestamp_identity",
        "_sha256_hex",
        "_stable_hash",
    ),
)
def test_runtime_authority_rejects_learning_environment_helper_rebinding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    helper_name: str,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    hostile_calls: list[object] = []

    def hostile(*args: object, **kwargs: object) -> object:
        hostile_calls.append((args, kwargs))
        raise AssertionError("hostile learning-environment helper executed")

    monkeypatch.setattr(
        deployment_runtime_authority._learning_environment,
        helper_name,
        hostile,
    )
    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="learning-environment helper was replaced",
    ):
        store.records()

    assert hostile_calls == []


@pytest.mark.parametrize(
    "helper_name",
    ("_timestamp_identity", "_stable_hash"),
)
def test_runtime_authority_rejects_learning_environment_helper_code_replacement(
    tmp_path: Path,
    helper_name: str,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    target = getattr(
        deployment_runtime_authority._learning_environment,
        helper_name,
    )
    original_code = target.__code__

    def hostile(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("hostile learning-environment helper code executed")

    assert hostile.__code__.co_freevars == original_code.co_freevars
    target.__code__ = hostile.__code__
    try:
        with pytest.raises(
            DeploymentRuntimeAuthorityError,
            match="learning-environment helper was replaced",
        ):
            store.records()
    finally:
        target.__code__ = original_code


@pytest.mark.parametrize(
    ("name", "replacement"),
    (
        ("ENVIRONMENT_SCHEMA", "hostile.learning.environment"),
        ("ENVIRONMENT_SCHEMA_VERSION", 999),
        ("hashlib", object()),
        ("json", object()),
        ("datetime", object()),
        ("timezone", object()),
    ),
)
def test_runtime_authority_rejects_learning_environment_dependency_rebinding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    replacement: object,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )

    monkeypatch.setattr(
        deployment_runtime_authority._learning_environment,
        name,
        replacement,
    )
    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="learning-environment dependency was replaced",
    ):
        store.records()


def test_runtime_authority_get_rejects_sha_rebinding_before_argument_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    hostile_calls: list[object] = []

    def hostile_sha(value: object, name: str) -> str:
        hostile_calls.append((value, name))
        return "a" * 64

    monkeypatch.setattr(deployment_runtime_authority, "_sha", hostile_sha)
    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="binding validator dispatch was replaced",
    ):
        store.get("a" * 64)

    assert hostile_calls == []


def test_runtime_authority_pristine_rejects_digest_rebinding_before_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hostile_calls: list[object] = []

    def hostile_digest(value: object) -> str:
        hostile_calls.append(value)
        return "a" * 64

    monkeypatch.setattr(deployment_runtime_authority, "_digest", hostile_digest)
    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="construction helper dispatch was replaced",
    ):
        DeploymentRuntimeAuthorityStore.initialize_pristine(
            tmp_path / "deployment-runtime-authority.json",
            authority_root=_authority_root(tmp_path),
        )

    assert hostile_calls == []


def test_runtime_authority_pristine_bypasses_replaced_store_new(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hostile_calls: list[object] = []

    def hostile_new(cls: object, *args: object, **kwargs: object) -> object:
        hostile_calls.append((cls, args, kwargs))
        raise AssertionError("hostile store __new__ executed")

    monkeypatch.setattr(
        DeploymentRuntimeAuthorityStore,
        "__new__",
        staticmethod(hostile_new),
    )
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        tmp_path / "deployment-runtime-authority.json",
        authority_root=_authority_root(tmp_path),
    )

    assert hostile_calls == []
    assert store.records() == ()


def test_runtime_authority_rejects_path_read_descriptor_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    path_type = type(store.path)
    hostile_calls: list[object] = []

    def hostile_read_text(self: object, *args: object, **kwargs: object) -> str:
        hostile_calls.append((self, args, kwargs))
        raise AssertionError("hostile path reader executed")

    monkeypatch.setattr(path_type, "read_text", hostile_read_text)
    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="path read dispatch was replaced|binding integrity mismatch",
    ):
        store.records()

    assert hostile_calls == []


def test_runtime_authority_rejects_path_read_code_replacement(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    target = type(store.path).read_text
    original_code = target.__code__

    def hostile_read_text(self: object, *args: object, **kwargs: object) -> str:
        del self, args, kwargs
        raise AssertionError("hostile path reader code executed")

    assert hostile_read_text.__code__.co_freevars == original_code.co_freevars
    target.__code__ = hostile_read_text.__code__
    try:
        with pytest.raises(
            DeploymentRuntimeAuthorityError,
            match="path read dispatch was replaced|binding integrity mismatch",
        ):
            store.records()
    finally:
        target.__code__ = original_code


@pytest.mark.parametrize("method_name", ("expanduser", "resolve"))
def test_runtime_authority_rejects_path_construction_dispatch_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    concrete = type(Path("."))
    hostile_calls: list[object] = []

    def hostile(self: object, *args: object, **kwargs: object) -> object:
        hostile_calls.append((self, args, kwargs))
        raise AssertionError("hostile path construction dispatch executed")

    monkeypatch.setattr(concrete, method_name, hostile)
    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="path dispatch was replaced",
    ):
        DeploymentRuntimeAuthorityStore.initialize_pristine(
            tmp_path / "deployment-runtime-authority.json",
            authority_root=_authority_root(tmp_path),
        )

    assert hostile_calls == []


@pytest.mark.parametrize("method_name", ("recover", "read_history"))
def test_runtime_authority_rejects_monotonic_read_instance_shadow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    hostile_calls: list[object] = []

    def hostile(*args: object, **kwargs: object) -> object:
        hostile_calls.append((args, kwargs))
        raise AssertionError("hostile monotonic read instance shadow executed")

    monkeypatch.setattr(store._authority, method_name, hostile)
    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="monotonic read instance dispatch was replaced",
    ):
        store.records()

    assert hostile_calls == []


@pytest.mark.parametrize("method_name", ("recover", "read_history"))
def test_runtime_authority_rejects_monotonic_read_class_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    hostile_calls: list[object] = []

    def hostile(*args: object, **kwargs: object) -> object:
        hostile_calls.append((args, kwargs))
        raise AssertionError("hostile monotonic read class dispatch executed")

    monkeypatch.setattr(MonotonicWorkspaceAuthority, method_name, hostile)
    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="monotonic read dispatch was replaced",
    ):
        store.records()

    assert hostile_calls == []


@pytest.mark.parametrize("method_name", ("recover", "read_history"))
def test_runtime_authority_rejects_monotonic_read_code_replacement(
    tmp_path: Path,
    method_name: str,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    target = vars(MonotonicWorkspaceAuthority)[method_name]
    original_code = target.__code__

    def hostile(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("hostile monotonic read code executed")

    assert hostile.__code__.co_freevars == original_code.co_freevars
    target.__code__ = hostile.__code__
    try:
        with pytest.raises(
            DeploymentRuntimeAuthorityError,
            match="monotonic read dispatch was replaced",
        ):
            store.records()
    finally:
        target.__code__ = original_code


@pytest.mark.parametrize(
    "helper_name",
    (
        "_assert_monotonic_read_recovery_dispatch",
        "_monotonic_recover",
        "_monotonic_read_history",
        "_assert_monotonic_read_helpers",
    ),
)
def test_runtime_authority_rejects_monotonic_read_helper_alias_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    helper_name: str,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    hostile_calls: list[object] = []

    def hostile(*args: object, **kwargs: object) -> object:
        hostile_calls.append((args, kwargs))
        raise AssertionError("hostile monotonic read helper executed")

    monkeypatch.setattr(deployment_runtime_authority, helper_name, hostile)
    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="monotonic read helper",
    ):
        store.records()

    assert hostile_calls == []


@pytest.mark.parametrize(
    "helper_name",
    (
        "_assert_monotonic_read_recovery_dispatch",
        "_monotonic_recover",
        "_monotonic_read_history",
        "_assert_monotonic_read_helpers",
    ),
)
def test_runtime_authority_rejects_monotonic_read_helper_code_replacement(
    tmp_path: Path,
    helper_name: str,
) -> None:
    path = tmp_path / "deployment-runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(
        path,
        authority_root=_authority_root(tmp_path),
    )
    target = getattr(deployment_runtime_authority, helper_name)
    original_code = target.__code__

    def hostile(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("hostile monotonic read helper code executed")

    assert hostile.__code__.co_freevars == original_code.co_freevars
    target.__code__ = hostile.__code__
    try:
        with pytest.raises(
            DeploymentRuntimeAuthorityError,
            match="monotonic read helper",
        ):
            store.records()
    finally:
        target.__code__ = original_code
