from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]


class _IdAttributes(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.by_id: dict[str, tuple[str, dict[str, str | None]]] = {}

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        values = dict(attrs)
        element_id = values.get("id")
        if element_id is not None:
            self.by_id[element_id] = (tag, values)


def test_dynamic_disclosures_expose_state_to_screen_readers() -> None:
    html = (
        _ROOT / "src" / "autosport" / "windows_web" / "index.html"
    ).read_text(encoding="utf-8")
    javascript = (
        _ROOT / "src" / "autosport" / "windows_web" / "app.js"
    ).read_text(encoding="utf-8")

    parser = _IdAttributes()
    parser.feed(html)
    parser.close()

    for button_id, panel_id in (("305", "owner-panel"), ("330", "manual-panel")):
        tag, attrs = parser.by_id[button_id]
        assert tag == "button"
        assert attrs.get("aria-controls") == panel_id
        assert attrs.get("aria-expanded") == "false"

    assert 'byId(305).setAttribute("aria-expanded", "true")' in javascript
    assert 'byId(305).setAttribute("aria-expanded", "false")' in javascript
    assert 'byId(330).setAttribute("aria-expanded", "true")' in javascript
    assert 'byId(330).setAttribute("aria-expanded", "false")' in javascript

    assert 'byId(306).focus()' in javascript
    assert 'byId(305).focus()' in javascript
    assert 'byId(331).focus()' in javascript
    assert 'byId(330).focus()' in javascript
