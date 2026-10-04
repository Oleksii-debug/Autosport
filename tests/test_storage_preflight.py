from __future__ import annotations

from pathlib import Path

import pytest

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


def test_workspace_atomic_preflight_cleans_source_when_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "Autosport" / "workspace"

    def fail_replace(_source, _destination) -> None:
        raise PermissionError("replace denied")

    monkeypatch.setattr(storage_preflight.os, "replace", fail_replace)

    with pytest.raises(PermissionError, match="replace denied"):
        probe_workspace_writable(workspace)

    assert workspace.is_dir()
    assert list(workspace.iterdir()) == []


def test_webview_preflight_leaves_no_probe_artifact(tmp_path: Path) -> None:
    storage = tmp_path / "Користувач Тест" / "Autosport" / "webview2"

    probe_webview_storage_writable(storage)

    assert storage.is_dir()
    assert list(storage.iterdir()) == []
