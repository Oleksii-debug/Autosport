from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import multiprocessing as mp
import os
from pathlib import Path

import pytest

import autosport.deployment_runtime_authority as runtime_authority
from autosport.deployment_runtime_authority import (
    DeploymentRuntimeAuthorityStore,
)
from autosport.learning_environment import EnvironmentIdentity, Episode


def _runtime_inputs(label: str) -> tuple[
    EnvironmentIdentity,
    Episode,
    str,
    tuple[tuple[str, str], ...],
]:
    environment = EnvironmentIdentity(
        source_id="paper-provider",
        config_id=f"config-{label}",
        data_id=f"dataset-{label}",
        protocol_id="protocol-v1",
        cutoff_ts="2026-09-21T20:00:00Z",
        seed=7,
    )
    episode = Episode(
        environment_id=environment.environment_id,
        episode_key=f"episode-{label}",
        policy_id=f"policy-{label}",
        admissible_actions=("WAIT",),
    )
    return (
        environment,
        episode,
        "paper-actions-v1",
        (("WAIT", "Observe only; do not create an external effect."),),
    )


def _append(
    store: DeploymentRuntimeAuthorityStore,
    label: str,
):
    environment, episode, version, meanings = _runtime_inputs(label)
    return store.append(
        environment=environment,
        episode=episode,
        action_semantics_version=version,
        action_semantics_meanings=meanings,
    )


def _process_append(
    path: str,
    label: str,
    start_event,
    result_queue,
) -> None:
    store = DeploymentRuntimeAuthorityStore(path)
    if not start_event.wait(timeout=10):
        result_queue.put(("error", label, "start-timeout"))
        return
    try:
        record = _append(store, label)
    except BaseException as exc:
        result_queue.put(("error", label, f"{type(exc).__name__}: {exc}"))
        return
    result_queue.put(("ok", label, record.runtime_authority_id))


def test_append_holds_durable_path_fence_across_read_publish_and_verify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "runtime-authority.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(path)

    lock_depth = 0
    lock_entries = 0

    @contextmanager
    def observed_lock(candidate: str | Path):
        nonlocal lock_depth, lock_entries
        assert Path(candidate) == path
        lock_entries += 1
        lock_depth += 1
        try:
            yield
        finally:
            lock_depth -= 1

    original_read = store._read_validated_records
    original_write = store._write_atomic_path

    def guarded_read():
        assert lock_depth == 1, "runtime authority read escaped durable path fence"
        return original_read()

    def guarded_write(candidate: Path, payload):
        assert lock_depth == 1, "runtime authority publication escaped durable path fence"
        return original_write(candidate, payload)

    monkeypatch.setattr(runtime_authority, "durable_path_lock", observed_lock)
    monkeypatch.setattr(store, "_read_validated_records", guarded_read)
    monkeypatch.setattr(store, "_write_atomic_path", guarded_write)

    record = _append(store, "guarded")

    assert record.runtime_authority_id
    assert lock_entries == 1
    assert lock_depth == 0


def test_pristine_initialization_is_serialized_by_durable_path_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "runtime-authority.json"
    lock_depth = 0

    @contextmanager
    def observed_lock(candidate: str | Path):
        nonlocal lock_depth
        assert Path(candidate) == path
        lock_depth += 1
        try:
            yield
        finally:
            lock_depth -= 1

    original_write = DeploymentRuntimeAuthorityStore._write_atomic_path

    def guarded_write(candidate: Path, payload):
        assert lock_depth == 1, "pristine publication escaped durable path fence"
        return original_write(candidate, payload)

    monkeypatch.setattr(runtime_authority, "durable_path_lock", observed_lock)
    monkeypatch.setattr(
        DeploymentRuntimeAuthorityStore,
        "_write_atomic_path",
        staticmethod(guarded_write),
    )

    store = DeploymentRuntimeAuthorityStore.initialize_pristine(path)

    assert store.records() == ()
    assert lock_depth == 0


def test_concurrent_distinct_store_instances_preserve_every_append(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime-authority.json"
    DeploymentRuntimeAuthorityStore.initialize_pristine(path)
    labels = tuple(f"worker-{index:02d}" for index in range(16))
    stores = tuple(DeploymentRuntimeAuthorityStore(path) for _ in labels)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(_append, store, label)
            for store, label in zip(stores, labels, strict=True)
        ]
        issued = tuple(future.result(timeout=10) for future in futures)

    restarted = DeploymentRuntimeAuthorityStore(path)
    durable = restarted.records()

    assert len(durable) == len(labels)
    assert {record.runtime_authority_id for record in durable} == {
        record.runtime_authority_id for record in issued
    }
    assert len({record.record_sha256 for record in durable}) == len(labels)
    for left, right in zip(durable, durable[1:], strict=False):
        assert right.previous_record_sha256 == left.record_sha256


def test_spawned_processes_preserve_one_linear_append_chain(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime-authority.json"
    DeploymentRuntimeAuthorityStore.initialize_pristine(path)
    labels = tuple(f"process-{index:02d}" for index in range(4))
    context = mp.get_context("spawn")
    start_event = context.Event()
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_process_append,
            args=(str(path), label, start_event, result_queue),
        )
        for label in labels
    ]

    for process in processes:
        process.start()
    start_event.set()

    for process in processes:
        process.join(timeout=20)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
            pytest.fail("spawned runtime-authority writer did not terminate")
        assert process.exitcode == 0

    results = [result_queue.get(timeout=5) for _ in processes]
    result_queue.close()
    result_queue.join_thread()

    assert all(result[0] == "ok" for result in results), results
    issued_ids = {result[2] for result in results}
    durable = DeploymentRuntimeAuthorityStore(path).records()

    assert len(durable) == len(labels)
    assert {record.runtime_authority_id for record in durable} == issued_ids
    for left, right in zip(durable, durable[1:], strict=False):
        assert right.previous_record_sha256 == left.record_sha256


def test_file_symlink_alias_converges_to_canonical_store_path(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime-authority.json"
    alias = tmp_path / "runtime-authority-alias.json"
    DeploymentRuntimeAuthorityStore.initialize_pristine(path)
    try:
        alias.symlink_to(path)
    except OSError as exc:
        pytest.skip(f"file symlink unavailable in this environment: {exc}")

    store = DeploymentRuntimeAuthorityStore(alias)
    issued = _append(store, "symlink-alias")

    assert store.path == path.resolve(strict=True)
    assert alias.is_symlink()
    assert DeploymentRuntimeAuthorityStore(path).get(issued.runtime_authority_id) == issued


def test_hard_link_alias_fails_closed_before_append(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime-authority.json"
    alias = tmp_path / "runtime-authority-hardlink.json"
    DeploymentRuntimeAuthorityStore.initialize_pristine(path)
    try:
        os.link(path, alias)
    except OSError as exc:
        pytest.skip(f"hard links unavailable in this environment: {exc}")

    with pytest.raises(
        runtime_authority.DeploymentRuntimeAuthorityError,
        match="hard-linked",
    ):
        DeploymentRuntimeAuthorityStore(alias)


def test_concurrent_exact_retry_stays_single_record(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime-authority.json"
    DeploymentRuntimeAuthorityStore.initialize_pristine(path)
    stores = tuple(DeploymentRuntimeAuthorityStore(path) for _ in range(12))

    with ThreadPoolExecutor(max_workers=6) as pool:
        issued = tuple(
            future.result(timeout=10)
            for future in (
                pool.submit(_append, store, "same-runtime")
                for store in stores
            )
        )

    durable = DeploymentRuntimeAuthorityStore(path).records()

    assert len(durable) == 1
    assert len({record.runtime_authority_id for record in issued}) == 1
    assert all(record == durable[0] for record in issued)
