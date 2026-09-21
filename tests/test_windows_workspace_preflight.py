from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from autosport import windows_entry


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
