from __future__ import annotations

from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_APP_JS = _ROOT / "src" / "autosport" / "windows_web" / "app.js"
_SHELL = _ROOT / "src" / "autosport" / "windows_webview_shell.py"


def test_webview_live_regions_do_not_bypass_bounded_announcement_policy() -> None:
    """Dynamic status/messages must pass the canonical bounded announcement policy.

    PR #944 owns classification/deduplication: high-frequency price/market/progress
    churn is SILENT, lifecycle transitions are POLITE, and critical/recovery
    episodes are ASSERTIVE. A generic aria-live sink fed directly from controller
    status or dispatch result.message bypasses that policy even if the DOM itself is
    semantically valid.
    """

    javascript = _APP_JS.read_text(encoding="utf-8")
    shell = _SHELL.read_text(encoding="utf-8")

    # Current #1598 behavior directly maps broad controller/command messages into
    # live regions. Those paths can contain product-runtime progress and live-state
    # churn, so they must not survive composition with #944.
    assert "projectLiveState(state.status, state.last_error)" not in javascript
    assert "announce(result && result.message ?" not in javascript

    # The composed shell needs an explicit policy-approved announcement projection
    # rather than treating every visible status update as speech-worthy.
    combined = (javascript + "\n" + shell).lower()
    assert "announcement" in combined
    assert (
        "announcement_gate" in combined
        or "announcementgate" in combined
        or "announcement_decision" in combined
        or "announcementdecision" in combined
    )


def test_webview_visual_status_remains_available_without_forcing_speech() -> None:
    """Fixing announcement policy must not remove visible operator status."""

    javascript = _APP_JS.read_text(encoding="utf-8")
    html = (_ROOT / "src" / "autosport" / "windows_web" / "index.html").read_text(
        encoding="utf-8"
    )

    assert 'id="app-status"' in html
    assert 'id="error-status"' in html
    assert 'byId("app-status")' in javascript
    assert 'byId("error-status")' in javascript
