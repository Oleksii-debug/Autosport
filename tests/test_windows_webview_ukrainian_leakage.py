from __future__ import annotations

import ast
from html.parser import HTMLParser
from pathlib import Path

from autosport.windows_webview_shell import _safe_exception_text


_ROOT = Path(__file__).resolve().parents[1]
_HTML_PATH = _ROOT / "src/autosport/windows_web/index.html"
_JS_PATH = _ROOT / "src/autosport/windows_web/app.js"
_SHELL_PATH = _ROOT / "src/autosport/windows_webview_shell.py"

_ACCESSIBLE_TEXT_ATTRIBUTES = {
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
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag in {"script", "style"}:
            self._ignored_depth += 1
            return
        for name, value in attrs:
            if name in _ACCESSIBLE_TEXT_ATTRIBUTES and value:
                self.fragments.append(value.strip())

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        value = data.strip()
        if value:
            self.fragments.append(value)


def _static_operator_copy() -> str:
    parser = _OperatorCopyParser()
    parser.feed(_HTML_PATH.read_text(encoding="utf-8"))
    return "\n".join(parser.fragments)


def _product_runtime_projection_literals() -> str:
    tree = ast.parse(_SHELL_PATH.read_text(encoding="utf-8"))
    poll_workers = next(
        child
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "AutosportWebController"
        for child in node.body
        if isinstance(child, ast.FunctionDef) and child.name == "_poll_workers"
    )

    fragments: list[str] = []
    for node in ast.walk(poll_workers):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
            and target.attr == "product_runtime_status"
            for target in node.targets
        ):
            continue
        fragments.extend(
            child.value
            for child in ast.walk(node.value)
            if isinstance(child, ast.Constant) and isinstance(child.value, str)
        )
    return "\n".join(fragments)


def test_static_webview_operator_copy_has_no_known_app_owned_english_leakage() -> None:
    copy = _static_operator_copy()

    # These are Autosport-owned presentation words/phrases, not provider names,
    # canonical IDs, currencies, hashes, shortcuts, WebView2, or other machine
    # truth that #1592 explicitly permits to remain untranslated.
    forbidden = (
        "Human/NVDA verification",
        "PAPER replay",
        "evidence",
        "preview",
        "Python runtime",
        "live snapshot",
        "recovery",
        "assistive technology",
        "controls",
        "Machine semantic/UIA checks",
        "exact release artifact",
    )

    leaked = tuple(token for token in forbidden if token in copy)
    assert not leaked, f"app-owned English operator copy leaked into WebView2 DOM/UIA text: {leaked!r}"


def test_dynamic_bridge_failure_copy_does_not_name_python_transport_layer() -> None:
    javascript = _JS_PATH.read_text(encoding="utf-8")

    # The operator needs a Ukrainian product-level failure, not an internal
    # implementation-layer phrase such as "Python bridge".
    assert "Python bridge" not in javascript


def test_runtime_tick_projection_localizes_app_owned_status_terms() -> None:
    projection = _product_runtime_projection_literals()

    forbidden = ("PAPER runtime", "delta", "settlement")
    leaked = tuple(token for token in forbidden if token in projection)
    assert not leaked, f"runtime status leaks app-owned English terms: {leaked!r}"


def test_normal_operator_error_projection_hides_python_exception_class_name() -> None:
    rendered = _safe_exception_text(
        ValueError("Authorization: Bearer secret-provider-token")
    )

    # Raw detail is already fenced by #1598. #1592 additionally requires that
    # a Python exception class is not the normal operator-facing explanation.
    assert "ValueError" not in rendered
    assert "Bearer" not in rendered
    assert "secret-provider-token" not in rendered
    assert any("\u0400" <= char <= "\u04ff" for char in rendered)
