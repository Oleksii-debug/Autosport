from __future__ import annotations

from pathlib import Path


_ROOT = Path(__file__).parents[1]
_STYLES = _ROOT / "src" / "autosport" / "windows_web" / "styles.css"
_INDEX = _ROOT / "src" / "autosport" / "windows_web" / "index.html"
_APP = _ROOT / "src" / "autosport" / "windows_web" / "app.js"


def test_programmatic_minus_one_focus_targets_keep_visible_focus_outline() -> None:
    styles = _STYLES.read_text(encoding="utf-8")
    html = _INDEX.read_text(encoding="utf-8")
    javascript = _APP.read_text(encoding="utf-8")

    assert '[tabindex]:focus {' in styles
    assert '[tabindex="0"]:focus {' not in styles
    assert 'outline: 3px solid currentColor;' in styles
    assert 'outline-offset: 2px;' in styles

    for target in (
        'id="main-content" tabindex="-1"',
        'id="owner-review" tabindex="-1"',
        'id="help-heading" tabindex="-1"',
    ):
        assert target in html

    assert "fallback.tabIndex = -1;" in javascript
    assert "focusOperatorTarget(byId(result.focus_id));" in javascript
    assert "focusOperatorTarget(byId(selectedSurfaceTarget()));" in javascript
