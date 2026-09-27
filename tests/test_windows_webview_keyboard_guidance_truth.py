from __future__ import annotations

from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]


def test_webview_help_does_not_advertise_forbidden_ctrl_r_shortcuts() -> None:
    """Operator guidance must match the canonical WebView keyboard contract."""

    audit = (
        _ROOT / "src" / "autosport" / "windows_webview_audit.py"
    ).read_text(encoding="utf-8")
    html = (
        _ROOT / "src" / "autosport" / "windows_web" / "index.html"
    ).read_text(encoding="utf-8")
    start_here = (_ROOT / "WINDOWS_START_HERE.txt").read_text(encoding="utf-8")

    assert 'key === "r"' in audit

    for surface in (html, start_here):
        assert "Ctrl+R" not in surface
        assert "Ctrl+Shift+R" not in surface

    assert 'id="102" type="button"' in html
    assert 'id="108" type="button"' in html


def test_browser_reserved_f_keys_are_not_globally_intercepted() -> None:
    javascript = (
        _ROOT / "src" / "autosport" / "windows_web" / "app.js"
    ).read_text(encoding="utf-8")

    for key in ("F6", "F7", "F9", "F10"):
        assert f'event.key === "{key}"' not in javascript
