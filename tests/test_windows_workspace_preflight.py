from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from autosport import integrity, windows_entry


def test_workspace_probe_supports_spaces_and_ukrainian_unicode(tmp_path: Path) -> None:
    workspace = tmp_path / "дані Autosport з пробілом"

    windows_entry._probe_workspace_writable(workspace)

    assert workspace.is_dir()
    assert list(workspace.iterdir()) == []


@pytest.mark.parametrize(
    "error",
    (
        PermissionError("access denied"),
        OSError("device unavailable"),
    ),
)
def test_interactive_gui_fails_before_gui_import_when_workspace_is_unwritable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error: OSError,
) -> None:
    workspace = tmp_path / "readonly workspace"
    monkeypatch.setenv("AUTOSPORT_WORKSPACE", str(workspace))
    monkeypatch.setattr(
        "autosport.webview2_runtime_deployment.ensure_webview2_runtime",
        lambda: SimpleNamespace(available=True),
    )

    def fail_probe(candidate: Path) -> None:
        assert candidate == workspace
        raise error

    captured: dict[str, object] = {}

    def capture_error(candidate: Path, observed: OSError) -> None:
        captured["workspace"] = candidate
        captured["error"] = observed

    def forbidden_gui_main() -> int:
        raise AssertionError("GUI must not open after workspace write preflight failure")

    monkeypatch.setattr(windows_entry, "_probe_workspace_writable", fail_probe)
    monkeypatch.setattr(windows_entry, "_show_workspace_access_error", capture_error)
    monkeypatch.setitem(
        sys.modules,
        "autosport.windows_gui",
        SimpleNamespace(main=forbidden_gui_main),
    )

    assert windows_entry._run_interactive_gui() == 2
    assert captured == {"workspace": workspace, "error": error}


@pytest.mark.parametrize("failing_primitive", ("fsync", "replace", "path_lock"))
def test_interactive_gui_fails_before_gui_import_when_atomic_publish_primitive_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failing_primitive: str,
) -> None:
    workspace = tmp_path / "atomic publish workspace"
    monkeypatch.setenv("AUTOSPORT_WORKSPACE", str(workspace))
    monkeypatch.setattr(
        "autosport.webview2_runtime_deployment.ensure_webview2_runtime",
        lambda: SimpleNamespace(available=True),
    )

    if failing_primitive == "fsync":
        def fail_fsync(_fd: int) -> None:
            raise OSError("fsync unavailable")

        monkeypatch.setattr(windows_entry.os, "fsync", fail_fsync)
    elif failing_primitive == "replace":
        def fail_replace(_source: object, _destination: object) -> None:
            raise PermissionError("atomic replace denied")

        monkeypatch.setattr(windows_entry.os, "replace", fail_replace)
    else:
        def fail_path_lock(_handle: object) -> None:
            raise OSError("durable path lock unavailable")

        # Exercise the real durable_path_lock context manager and fail at its
        # OS-lock acquisition boundary after the disposable sidecar is opened.
        monkeypatch.setattr(integrity, "_lock_handle", fail_path_lock)

    captured: dict[str, object] = {}

    def capture_error(candidate: Path, observed: OSError) -> None:
        captured["workspace"] = candidate
        captured["error"] = observed

    def forbidden_gui_main() -> int:
        raise AssertionError("GUI must not open when atomic workspace publication is unavailable")

    monkeypatch.setattr(windows_entry, "_show_workspace_access_error", capture_error)
    monkeypatch.setitem(
        sys.modules,
        "autosport.windows_gui",
        SimpleNamespace(main=forbidden_gui_main),
    )

    assert windows_entry._run_interactive_gui() == 2
    assert captured["workspace"] == workspace
    assert isinstance(captured["error"], OSError)
    assert list(workspace.iterdir()) == []


def test_workspace_access_error_is_actionable_and_single_line(tmp_path: Path) -> None:
    workspace = tmp_path / "робоча папка"
    error = PermissionError("access denied\nsecondary detail")

    message = windows_entry._workspace_access_error_message(workspace, error)

    assert str(workspace) in message
    assert "PermissionError: access denied secondary detail" in message
    assert "AUTOSPORT_WORKSPACE" in message
    assert "абсолютний шлях" in message
    assert "доступної для запису" in message
    assert "Права адміністратора не потрібні" in message
    assert "access denied\nsecondary detail" not in message
