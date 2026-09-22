from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

import autosport.monotonic_workspace_binding as workspace_binding


def _publish(path: Path, payload: dict[str, object]) -> None:
    workspace_binding._durable_exclusive_json_create(
        path,
        payload,
        lineage_boundary=path.parent,
    )


def test_partial_serializer_bytes_are_never_visible_at_final_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "binding.json"
    payload = {"workspace_instance_id": "winner", "schema_version": 1}
    serializer_paused = threading.Event()
    release_serializer = threading.Event()
    original_dump = workspace_binding.json.dump
    errors: list[BaseException] = []

    def pausing_dump(value, handle, *args, **kwargs):  # noqa: ANN001
        handle.write('{"partial":')
        handle.flush()
        serializer_paused.set()
        if not release_serializer.wait(timeout=5):
            raise TimeoutError("test serializer release timed out")
        handle.seek(0)
        handle.truncate()
        return original_dump(value, handle, *args, **kwargs)

    monkeypatch.setattr(workspace_binding.json, "dump", pausing_dump)

    def writer() -> None:
        try:
            _publish(target, payload)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert serializer_paused.wait(timeout=5)
        assert not target.exists()
        staged = list(tmp_path.glob(f".{target.name}.*.tmp"))
        assert len(staged) == 1
        assert staged[0].read_text(encoding="utf-8") == '{"partial":'
    finally:
        release_serializer.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert errors == []
    assert json.loads(target.read_text(encoding="utf-8")) == payload
    assert list(tmp_path.glob(f".{target.name}.*.tmp")) == []


def test_failed_private_serialization_leaves_no_final_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "binding.json"

    def failing_dump(_value, handle, *args, **kwargs):  # noqa: ANN001
        del args, kwargs
        handle.write('{"partial":')
        handle.flush()
        raise RuntimeError("injected serialization failure")

    monkeypatch.setattr(workspace_binding.json, "dump", failing_dump)

    with pytest.raises(RuntimeError, match="injected serialization failure"):
        _publish(target, {"workspace_instance_id": "never-published"})

    assert not target.exists()
    assert list(tmp_path.glob(f".{target.name}.*.tmp")) == []


def test_existing_complete_binding_cannot_be_overwritten(
    tmp_path: Path,
) -> None:
    target = tmp_path / "binding.json"
    first = {"workspace_instance_id": "first", "schema_version": 1}
    second = {"workspace_instance_id": "second", "schema_version": 1}

    _publish(target, first)

    with pytest.raises(FileExistsError, match="workspace binding already exists"):
        _publish(target, second)

    assert json.loads(target.read_text(encoding="utf-8")) == first
    assert list(tmp_path.glob(f".{target.name}.*.tmp")) == []

@pytest.mark.skipif(os.name != "nt", reason="Windows-only write-through publication contract")
def test_windows_publication_uses_write_through_move(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "binding.json"
    payload = {"workspace_instance_id": "windows-winner", "schema_version": 1}
    calls: list[tuple[Path, Path]] = []
    original = workspace_binding._replace_windows_write_through

    def recording_replace(source: Path, destination: Path) -> None:
        calls.append((source, destination))
        original(source, destination)

    monkeypatch.setattr(
        workspace_binding,
        "_replace_windows_write_through",
        recording_replace,
    )

    _publish(target, payload)

    assert len(calls) == 1
    staged, destination = calls[0]
    assert destination == target
    assert not staged.exists()
    assert json.loads(target.read_text(encoding="utf-8")) == payload

