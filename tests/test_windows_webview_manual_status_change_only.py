from __future__ import annotations

from html.parser import HTMLParser

from autosport.windows_webview_shell import web_shell_index_path


class _IdAttributeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.attrs_by_id: dict[str, dict[str, str | None]] = {}

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del tag
        values = dict(attrs)
        element_id = values.get("id")
        if element_id:
            self.attrs_by_id[element_id] = values


def test_manual_status_live_region_is_projected_change_only() -> None:
    index = web_shell_index_path()
    parser = _IdAttributeParser()
    parser.feed(index.read_text(encoding="utf-8"))
    parser.close()

    manual_status = parser.attrs_by_id["manual-status"]
    assert manual_status.get("role") == "status"
    assert manual_status.get("aria-live") == "polite"

    javascript = index.with_name("app.js").read_text(encoding="utf-8")

    # This surface intentionally announces semantic manual-calculation status
    # transitions.  The surrounding product state is polled every 250 ms, so an
    # unchanged value must not be written to the live region four times a second.
    assert (
        'setTextIfChanged(byId("manual-status"), state.manual.status || "")'
        in javascript
    )
    assert (
        'byId("manual-status").textContent = state.manual.status || ""'
        not in javascript
    )
