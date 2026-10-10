from __future__ import annotations

from pathlib import Path

from autosport.windows_surface_contract import SURFACES, surface_detail_lines


_ROOT = Path(__file__).resolve().parents[1]
_APP_JS = _ROOT / "src" / "autosport" / "windows_web" / "app.js"


def test_webview_surface_state_projects_localized_detail_not_machine_phase() -> None:
    javascript = _APP_JS.read_text(encoding="utf-8")

    assert "const surfaceDetails = Array.from(" in javascript
    assert (
        'setValueIfChanged(byId(302), surfaceDetails[0] || "СТАН: невідомий");'
        in javascript
    )
    assert "setValueIfChanged(byId(302), state.surface_state" not in javascript


def test_every_surface_phase_has_ukrainian_operator_projection() -> None:
    for surface in SURFACES:
        details = surface_detail_lines(surface)
        assert details
        visible_phase = details[0]

        assert visible_phase.startswith("СТАН:")
        assert visible_phase != surface.phase
        assert surface.phase not in visible_phase
