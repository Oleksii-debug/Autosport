from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import autosport.integrity as integrity
import autosport.operator_source_store as operator_source_store
from autosport.operator_source_config import OperatorSourceSelectionState
from autosport.operator_source_store import (
    OperatorSourceConfigStore,
    OperatorSourceStoreError,
)


class _FailingPathLock:
    def __enter__(self):
        raise PermissionError("sidecar lock denied")

    def __exit__(self, exc_type, exc, tb):
        return False


def test_missing_store_is_configuration_required(tmp_path: Path):
    store = OperatorSourceConfigStore(tmp_path / "operator-source.json")
    assert store.read() is None
    result = store.resolve(admin_override_source_id=None)
    assert result.state is OperatorSourceSelectionState.CONFIGURATION_REQUIRED
    assert result.runtime_authorized is False


def test_atomic_write_round_trip_survives_reopen(tmp_path: Path):
    path = tmp_path / "operator-source.json"
    first = OperatorSourceConfigStore(path)
    written = first.write_source_id("betfair-exchange")
    reopened = OperatorSourceConfigStore(path)
    assert reopened.read() == written
    assert reopened.resolve(admin_override_source_id=None).source_id == "betfair-exchange"


def test_canonical_atomic_writer_format_is_accepted(tmp_path: Path):
    path = tmp_path / "operator-source.json"
    store = OperatorSourceConfigStore(path)
    store.write_source_id("paper-fixture")
    raw = path.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert "\n  \"source_id\"" in raw
    assert store.read().source_id == "paper-fixture"


def test_source_id_tamper_without_integrity_update_is_rejected(tmp_path: Path):
    path = tmp_path / "operator-source.json"
    store = OperatorSourceConfigStore(path)
    store.write_source_id("betfair-exchange")
    obj = json.loads(path.read_text(encoding="utf-8"))
    obj["source_id"] = "paper-fixture"
    path.write_text(json.dumps(obj), encoding="utf-8")
    with pytest.raises(OperatorSourceStoreError, match="integrity"):
        store.read()
    result = store.resolve(admin_override_source_id=None)
    assert result.state is OperatorSourceSelectionState.INVALID
    assert result.source_id is None


def test_duplicate_keys_are_corruption(tmp_path: Path):
    path = tmp_path / "operator-source.json"
    path.write_text(
        '{"schema":"autosport.operator-source-config",'
        '"schema":"autosport.operator-source-config",'
        '"schema_version":1,"source_id":"a","integrity_sha256":"x"}',
        encoding="utf-8",
    )
    store = OperatorSourceConfigStore(path)
    with pytest.raises(OperatorSourceStoreError, match="corrupt"):
        store.read()


@pytest.mark.parametrize("bad_version", [True, 1.0, "1", None, 2])
def test_schema_aliases_and_wrong_versions_fail_closed(tmp_path: Path, bad_version):
    path = tmp_path / "operator-source.json"
    store = OperatorSourceConfigStore(path)
    store.write_source_id("betfair-exchange")
    obj = json.loads(path.read_text(encoding="utf-8"))
    obj["schema_version"] = bad_version
    path.write_text(json.dumps(obj), encoding="utf-8")
    with pytest.raises(OperatorSourceStoreError):
        store.read()


def test_unknown_secret_field_is_rejected_without_echo(tmp_path: Path):
    path = tmp_path / "operator-source.json"
    store = OperatorSourceConfigStore(path)
    store.write_source_id("betfair-exchange")
    obj = json.loads(path.read_text(encoding="utf-8"))
    obj["session_token"] = "SUPER-SECRET-XYZ"
    path.write_text(json.dumps(obj), encoding="utf-8")
    with pytest.raises(OperatorSourceStoreError) as exc:
        store.read()
    assert "SUPER-SECRET" not in str(exc.value)


def test_oversized_file_fails_before_json_decode(tmp_path: Path):
    path = tmp_path / "operator-source.json"
    path.write_bytes(b"x" * 4097)
    with pytest.raises(OperatorSourceStoreError, match="size"):
        OperatorSourceConfigStore(path).read()


def test_lock_acquisition_failure_is_store_error_and_invalid_state(
    tmp_path: Path,
    monkeypatch,
):
    store = OperatorSourceConfigStore(tmp_path / "operator-source.json")
    monkeypatch.setattr(
        operator_source_store,
        "durable_path_lock",
        lambda _: _FailingPathLock(),
    )

    with pytest.raises(OperatorSourceStoreError, match="cannot be read"):
        store.read()
    result = store.resolve(admin_override_source_id=None)
    assert result.state is OperatorSourceSelectionState.INVALID
    assert result.source_id is None
    assert result.runtime_authorized is False


def test_write_lock_acquisition_failure_is_store_error(
    tmp_path: Path,
    monkeypatch,
):
    path = tmp_path / "operator-source.json"
    store = OperatorSourceConfigStore(path)
    monkeypatch.setattr(
        operator_source_store,
        "durable_path_lock",
        lambda _: _FailingPathLock(),
    )

    with pytest.raises(OperatorSourceStoreError, match="could not be published"):
        store.write_source_id("betfair-exchange")
    assert not path.exists()


def test_strict_json_recursion_failure_is_corruption_not_crash(
    tmp_path: Path,
    monkeypatch,
):
    path = tmp_path / "operator-source.json"
    store = OperatorSourceConfigStore(path)
    store.write_source_id("betfair-exchange")

    def raise_recursion(*args, **kwargs):
        raise RecursionError("decoder recursion limit exceeded")

    monkeypatch.setattr(operator_source_store, "strict_json_loads", raise_recursion)

    with pytest.raises(OperatorSourceStoreError, match="corrupt"):
        store.read()
    result = store.resolve(admin_override_source_id=None)
    assert result.state is OperatorSourceSelectionState.INVALID
    assert result.source_id is None
    assert result.runtime_authorized is False


def test_canonical_reencode_recursion_failure_is_integrity_error(
    tmp_path: Path,
    monkeypatch,
):
    path = tmp_path / "operator-source.json"
    store = OperatorSourceConfigStore(path)
    store.write_source_id("betfair-exchange")

    def raise_recursion(*args, **kwargs):
        raise RecursionError("encoder recursion limit exceeded")

    monkeypatch.setattr(operator_source_store.json, "dumps", raise_recursion)

    with pytest.raises(OperatorSourceStoreError, match="integrity validation"):
        store.read()
    result = store.resolve(admin_override_source_id=None)
    assert result.state is OperatorSourceSelectionState.INVALID
    assert result.source_id is None
    assert result.runtime_authorized is False


def test_bounded_deep_persisted_json_is_invalid_not_restart_crash(tmp_path: Path):
    path = tmp_path / "operator-source.json"
    depth = 1024
    nested = "[" * depth + "0" + "]" * depth
    payload = (
        '{"integrity_sha256":"x","schema":"autosport.operator-source-config",'
        '"schema_version":1,"source_id":' + nested + "}"
    )
    assert len(payload.encode("utf-8")) < 4096
    path.write_text(payload, encoding="utf-8")

    store = OperatorSourceConfigStore(path)
    result = store.resolve(admin_override_source_id=None)
    assert result.state is OperatorSourceSelectionState.INVALID
    assert result.source_id is None
    assert result.runtime_authorized is False


def test_conflicting_admin_override_remains_explicit_conflict(tmp_path: Path):
    store = OperatorSourceConfigStore(tmp_path / "operator-source.json")
    store.write_source_id("betfair-exchange")
    result = store.resolve(admin_override_source_id="paper-fixture")
    assert result.state is OperatorSourceSelectionState.CONFLICT
    assert result.source_id is None
    assert result.runtime_authorized is False


def test_matching_admin_override_converges_without_runtime_authority(tmp_path: Path):
    store = OperatorSourceConfigStore(tmp_path / "operator-source.json")
    store.write_source_id("betfair-exchange")
    result = store.resolve(admin_override_source_id="betfair-exchange")
    assert result.state is OperatorSourceSelectionState.CONFIGURED
    assert result.source_id == "betfair-exchange"
    assert result.runtime_authorized is False


def test_stray_temporary_file_cannot_replace_last_good_state(tmp_path: Path):
    path = tmp_path / "operator-source.json"
    store = OperatorSourceConfigStore(path)
    original = store.write_source_id("betfair-exchange")
    (tmp_path / ".operator-source.json.crash.tmp").write_text(
        '{"source_id":"paper-fixture"}', encoding="utf-8"
    )
    assert store.read() == original


def test_replacement_is_whole_record_not_torn_mix(tmp_path: Path):
    store = OperatorSourceConfigStore(tmp_path / "operator-source.json")
    first = store.write_source_id("betfair-exchange")
    second = store.write_source_id("paper-fixture")
    assert first.source_id == "betfair-exchange"
    assert second.source_id == "paper-fixture"
    assert store.read() == second


def test_invalid_admin_override_does_not_modify_persisted_choice(tmp_path: Path):
    store = OperatorSourceConfigStore(tmp_path / "operator-source.json")
    original = store.write_source_id("betfair-exchange")
    result = store.resolve(admin_override_source_id="pkg.mod:factory")
    assert result.state is OperatorSourceSelectionState.INVALID
    assert store.read() == original

def test_failed_canonical_replace_preserves_last_good_config_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "operator-source.json"
    store = OperatorSourceConfigStore(path)
    original = store.write_source_id("betfair-exchange")
    original_bytes = path.read_bytes()
    durable_entries = {item.name for item in tmp_path.iterdir()}

    def fail_replace(_source, _destination) -> None:
        raise OSError("simulated disk-full or sharing failure")

    monkeypatch.setattr(integrity.os, "replace", fail_replace)

    with pytest.raises(OperatorSourceStoreError, match="could not be published"):
        store.write_source_id("paper-fixture")

    assert path.read_bytes() == original_bytes
    assert OperatorSourceConfigStore(path).read() == original
    assert {item.name for item in tmp_path.iterdir()} == durable_entries


def test_post_publish_ack_failure_is_restart_resolvable_and_retry_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "operator-source.json"
    store = OperatorSourceConfigStore(path)
    store.write_source_id("betfair-exchange")

    def lose_acknowledgement() -> None:
        raise OperatorSourceStoreError("simulated acknowledgement loss")

    monkeypatch.setattr(store, "read", lose_acknowledgement)

    with pytest.raises(OperatorSourceStoreError, match="acknowledgement loss"):
        store.write_source_id("paper-fixture")

    restarted = OperatorSourceConfigStore(path)
    committed = restarted.read()
    assert committed is not None
    assert committed.source_id == "paper-fixture"

    retried = restarted.write_source_id("paper-fixture")
    assert retried == committed
    assert OperatorSourceConfigStore(path).read() == committed

def test_symlinked_operator_source_config_is_rejected_without_following_target(
    tmp_path: Path,
) -> None:
    external = tmp_path / "external"
    external.mkdir()
    target = external / "operator-source.json"
    OperatorSourceConfigStore(target).write_source_id("paper-fixture")

    path = tmp_path / "operator-source.json"
    try:
        path.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable on this runner: {exc}")

    store = OperatorSourceConfigStore(path)
    with pytest.raises(OperatorSourceStoreError, match="cannot be read"):
        store.read()
    result = store.resolve(admin_override_source_id=None)
    assert result.state is OperatorSourceSelectionState.INVALID
    assert result.source_id is None
    assert path.is_symlink()
    assert OperatorSourceConfigStore(target).read().source_id == "paper-fixture"


def test_hard_linked_operator_source_config_is_rejected(
    tmp_path: Path,
) -> None:
    target = tmp_path / "external-operator-source.json"
    OperatorSourceConfigStore(target).write_source_id("paper-fixture")
    path = tmp_path / "operator-source.json"
    try:
        os.link(target, path)
    except OSError as exc:
        pytest.skip(f"hard links unavailable on this runner: {exc}")

    store = OperatorSourceConfigStore(path)
    with pytest.raises(OperatorSourceStoreError, match="cannot be read"):
        store.read()
    result = store.resolve(admin_override_source_id=None)
    assert result.state is OperatorSourceSelectionState.INVALID
    assert result.source_id is None


def test_operator_source_config_path_replacement_during_read_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "operator-source.json"
    replacement = tmp_path / "replacement.json"
    original = OperatorSourceConfigStore(path).write_source_id("betfair-exchange")
    OperatorSourceConfigStore(replacement).write_source_id("paper-fixture")

    real_open = integrity._open_read_only_no_follow_descriptor
    calls = 0

    def redirect_verification_open(candidate: Path) -> int:
        nonlocal calls
        if Path(candidate) == path:
            calls += 1
            if calls == 2:
                return real_open(replacement)
        return real_open(candidate)

    with monkeypatch.context() as patcher:
        patcher.setattr(
            integrity,
            "_open_read_only_no_follow_descriptor",
            redirect_verification_open,
        )
        with pytest.raises(OperatorSourceStoreError, match="cannot be read"):
            OperatorSourceConfigStore(path).read()

    assert OperatorSourceConfigStore(path).read() == original


def test_oversized_operator_source_config_read_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "operator-source.json"
    path.write_bytes(b"x" * (1024 * 1024))
    requested_sizes: list[int] = []
    real_read = integrity.os.read

    def bounded_read(descriptor: int, size: int) -> bytes:
        requested_sizes.append(size)
        return real_read(descriptor, size)

    monkeypatch.setattr(integrity.os, "read", bounded_read)

    with pytest.raises(OperatorSourceStoreError, match="size"):
        OperatorSourceConfigStore(path).read()

    assert requested_sizes
    assert max(requested_sizes) <= 4097
