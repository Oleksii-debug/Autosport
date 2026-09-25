from __future__ import annotations

from pathlib import Path

from autosport.windows_webview_shell import AutosportWebController


_ROOT = Path(__file__).resolve().parents[1]


def test_surface_selection_change_keeps_focus_in_native_selector(
    monkeypatch,
    tmp_path: Path,
) -> None:
    saved: list[tuple[Path, str]] = []

    def fake_save_surface_selection(workspace: Path, key: str) -> None:
        saved.append((Path(workspace), key))

    monkeypatch.setattr(
        "autosport.windows_webview_shell.save_surface_selection",
        fake_save_surface_selection,
    )

    controller = object.__new__(AutosportWebController)
    controller.workspace = tmp_path
    controller.surface_key = "home_dashboard"
    controller.last_error = ""

    result = controller._action_surface_select({"surface_key": "settings"})

    assert controller.surface_key == "settings"
    assert saved == [(tmp_path, "settings")]
    assert result["status"] == "completed"
    assert result["message"] == ""
    assert "focus_id" not in result


def test_only_explicit_surface_navigation_button_moves_focus() -> None:
    javascript = (
        _ROOT / "src" / "autosport" / "windows_web" / "app.js"
    ).read_text(encoding="utf-8")

    change_marker = 'byId(301).addEventListener("change", () => {'
    button_marker = 'byId(303).addEventListener("click", () => {'
    change_start = javascript.index(change_marker)
    button_start = javascript.index(button_marker)
    change_block = javascript[change_start:button_start]
    button_block = javascript[button_start : javascript.index(
        'byId(305).addEventListener("click"', button_start
    )]

    assert 'dispatch("surface.select"' in change_block
    assert ".focus()" not in change_block
    assert "selectedSurfaceTarget()" in button_block
    assert "target.focus()" in button_block
