from __future__ import annotations

import json
from pathlib import Path

import pytest

import autosport.integrity as integrity
import autosport.storage_preflight as storage_preflight
from autosport.storage_preflight import (
    probe_webview_storage_writable,
    probe_workspace_writable,
)


@pytest.mark.parametrize(
    "probe",
    [probe_workspace_writable, probe_webview_storage_writable],
)
def test_storage_preflight_requires_absolute_root(
    tmp_path: Path,
    probe,
) -> None:
    del tmp_path
    with pytest.raises(ValueError, match="must be absolute"):
        probe(Path("relative-storage-root"))


def test_workspace_atomic_preflight_leaves_no_probe_or_lock_artifact(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "Користувач Тест" / "Autosport" / "workspace"

    probe_workspace_writable(workspace)

    assert workspace.is_dir()
    assert list(workspace.iterdir()) == []


def test_workspace_atomic_preflight_invokes_canonical_atomic_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "Autosport" / "workspace"
    calls: list[tuple[Path, dict[str, object], bool]] = []

    def write_probe(path: str | Path, payload: dict[str, object]) -> None:
        destination = Path(path)
        calls.append((destination, dict(payload), destination.exists()))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        destination.with_name(f".{destination.name}.lock").write_bytes(b"lock")

    monkeypatch.setattr(storage_preflight, "atomic_write_json", write_probe)

    probe_workspace_writable(workspace)

    assert len(calls) == 1
    destination, payload, reservation_existed = calls[0]
    assert destination.name.endswith(".json")
    assert destination.name.startswith(".autosport-workspace-write-probe-")
    assert destination.parent == workspace
    assert reservation_existed is True
    assert payload == {
        "probe": "autosport workspace atomic publish probe",
        "schema_version": 1,
    }
    assert list(workspace.iterdir()) == []


def test_workspace_atomic_preflight_cleans_probe_when_canonical_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "Autosport" / "workspace"

    def fail_replace(_source, _destination) -> None:
        raise PermissionError("replace denied")

    monkeypatch.setattr(integrity.os, "replace", fail_replace)

    with pytest.raises(PermissionError, match="replace denied"):
        probe_workspace_writable(workspace)

    assert workspace.is_dir()
    assert list(workspace.iterdir()) == []


def test_workspace_atomic_preflight_rejects_canonical_writer_readback_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "Autosport" / "workspace"

    def write_drifted_probe(path: str | Path, _payload: dict[str, object]) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            '{"probe":"forged","schema_version":1}\n',
            encoding="utf-8",
        )

    monkeypatch.setattr(
        storage_preflight,
        "atomic_write_json",
        write_drifted_probe,
    )

    with pytest.raises(
        OSError,
        match="canonical atomic publication probe readback did not match",
    ):
        probe_workspace_writable(workspace)

    assert workspace.is_dir()
    assert list(workspace.iterdir()) == []


def test_workspace_atomic_preflight_rejects_malformed_canonical_writer_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "Autosport" / "workspace"

    def write_malformed_probe(path: str | Path, _payload: dict[str, object]) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("{", encoding="utf-8")

    monkeypatch.setattr(
        storage_preflight,
        "atomic_write_json",
        write_malformed_probe,
    )

    with pytest.raises(
        OSError,
        match="canonical atomic publication probe could not be read back",
    ):
        probe_workspace_writable(workspace)

    assert workspace.is_dir()
    assert list(workspace.iterdir()) == []


def test_webview_preflight_leaves_no_probe_artifact(tmp_path: Path) -> None:
    storage = tmp_path / "Користувач Тест" / "Autosport" / "webview2"

    probe_webview_storage_writable(storage)

    assert storage.is_dir()
    assert list(storage.iterdir()) == []
