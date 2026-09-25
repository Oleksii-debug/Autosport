from __future__ import annotations

from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
APP = _ROOT / "src" / "autosport" / "windows_web" / "app.js"
INDEX = _ROOT / "src" / "autosport" / "windows_web" / "index.html"


def test_webview_startup_focuses_named_native_operator_control() -> None:
    javascript = APP.read_text(encoding="utf-8")
    html = INDEX.read_text(encoding="utf-8")

    ready = javascript.index('window.addEventListener("pywebviewready", async () => {')
    poll = javascript.index("window.setInterval(refreshState, 250)", ready)
    startup = javascript[ready:poll]

    assert "await refreshState();" in startup
    assert "byId(301).focus();" in startup
    assert 'byId("main-content").focus();' not in startup

    assert '<label for="301">Екран</label>' in html
    assert 'id="301"' in html
    assert 'aria-label="Навігація екранами Автоспорт"' in html
    assert 'tabindex="-1"' in html  # programmatic heading targets stay out of native Tab order


def test_initial_focus_reuses_existing_keyboard_navigation_authority() -> None:
    javascript = APP.read_text(encoding="utf-8")

    # Startup and the documented product F2 shortcut converge on the same native
    # select instead of inventing another focus-only control or positive tabindex.
    assert javascript.count("byId(301).focus();") >= 2
    assert 'event.key === "F2"' in javascript
    assert 'event.key === "F8"' in javascript
