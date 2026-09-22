from __future__ import annotations

from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]


def test_webview_help_does_not_advertise_forbidden_ctrl_r_shortcuts() -> None:
    """Operator guidance must match the canonical WebView keyboard contract.

    The WebView keyboard audit intentionally forbids a JavaScript ``key === "r"``
    interception so browser/screen-reader behavior is preserved. Therefore product
    guidance cannot claim Ctrl+R/Ctrl+Shift+R as Autosport replay/recovery shortcuts.
    Replay and recovery remain available through semantic native buttons.
    """

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

    # The truthful correction must not strand keyboard-only operators: the native
    # semantic action controls remain discoverable and actionable by Tab/Enter.
    assert 'id="102" type="button"' in html
    assert 'id="108" type="button"' in html
