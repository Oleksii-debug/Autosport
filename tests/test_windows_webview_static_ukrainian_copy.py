from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_HTML = _ROOT / "src" / "autosport" / "windows_web" / "index.html"
_JS = _ROOT / "src" / "autosport" / "windows_web" / "app.js"

_ACCESSIBLE_ATTRIBUTES = {
    "aria-label",
    "aria-description",
    "placeholder",
    "title",
}


class _OperatorCopyParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self.fragments: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        folded = tag.casefold()
        if folded in {"script", "style"}:
            self._ignored_depth += 1
            return
        for name, value in attrs:
            if name.casefold() in _ACCESSIBLE_ATTRIBUTES and value:
                self.fragments.append(value.strip())

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        value = data.strip()
        if value:
            self.fragments.append(value)


def _static_operator_copy() -> str:
    parser = _OperatorCopyParser()
    parser.feed(_HTML.read_text(encoding="utf-8"))
    parser.close()
    assert parser.fragments, "static WebView operator copy must be non-empty"
    return "\n".join(parser.fragments)


def test_static_operator_copy_has_no_known_app_owned_english_leakage() -> None:
    copy = _static_operator_copy()
    forbidden = (
        "Human/NVDA verification",
        "PAPER replay",
        "PAPER runtime",
        "Python runtime",
        " evidence",
        "preview",
        "live snapshot",
        " recovery",
        "assistive technology",
        "semantic controls",
        "Machine semantic/UIA checks",
        "exact release artifact",
    )
    leaked = tuple(token for token in forbidden if token in copy)
    assert not leaked, (
        "app-owned English operator copy leaked into WebView DOM/UIA text: "
        f"{leaked!r}"
    )


def test_javascript_operator_errors_hide_internal_transport_name() -> None:
    javascript = _JS.read_text(encoding="utf-8")
    assert "Python bridge" not in javascript
    assert "Тривала PAPER-робота" not in javascript


def test_static_copy_preserves_machine_identity_exceptions() -> None:
    copy = _static_operator_copy()
    assert "WebView2" in copy
    assert "NVDA" in copy
    assert "NVDA_VERIFIED" in copy
