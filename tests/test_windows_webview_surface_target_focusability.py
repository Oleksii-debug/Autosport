from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_WEB_ROOT = _ROOT / "src" / "autosport" / "windows_web"
_HTML = _WEB_ROOT / "index.html"
_JS = _WEB_ROOT / "app.js"

_NATIVE_PROGRAMMATIC_FOCUS_TAGS = {
    "button",
    "input",
    "select",
    "textarea",
}


@dataclass(frozen=True, slots=True)
class _Element:
    tag: str
    attrs: dict[str, str | None]


class _ElementByIdParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: dict[str, _Element] = {}

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attr_map = {name.casefold(): value for name, value in attrs}
        element_id = attr_map.get("id")
        if not element_id:
            return
        assert element_id not in self.elements, (
            f"duplicate DOM id cannot be a deterministic focus target: {element_id}"
        )
        self.elements[element_id] = _Element(
            tag=tag.casefold(),
            attrs=attr_map,
        )


def _surface_targets(javascript: str) -> dict[str, str]:
    function = re.search(
        r"function\s+selectedSurfaceTarget\(\)\s*\{"
        r".*?const\s+mapping\s*=\s*\{(?P<body>.*?)\};",
        javascript,
        flags=re.DOTALL,
    )
    assert function is not None, "selectedSurfaceTarget mapping is missing"

    targets: dict[str, str] = {}
    for match in re.finditer(
        r"^\s*(?P<surface>[A-Za-z0-9_]+)\s*:\s*"
        r"(?:\"(?P<quoted>[^\"]+)\"|(?P<numeric>[0-9]+))\s*,?\s*$",
        function.group("body"),
        flags=re.MULTILINE,
    ):
        surface = match.group("surface")
        target = match.group("quoted") or match.group("numeric")
        assert target is not None
        targets[surface] = target

    assert targets, "selectedSurfaceTarget mapping must not be empty"
    return targets


def _elements_by_id(html: str) -> dict[str, _Element]:
    parser = _ElementByIdParser()
    parser.feed(html)
    parser.close()
    assert parser.elements, "canonical WebView document must expose id targets"
    return parser.elements


def _is_programmatically_focusable(element: _Element) -> bool:
    if "disabled" in element.attrs:
        return False

    tabindex = element.attrs.get("tabindex")
    if tabindex is not None:
        try:
            value = int(tabindex)
        except (TypeError, ValueError):
            return False
        # Positive tabindex changes the native Tab order and is forbidden.
        return value <= 0

    if element.tag in _NATIVE_PROGRAMMATIC_FOCUS_TAGS:
        return True

    if element.tag == "a":
        href = element.attrs.get("href")
        return isinstance(href, str) and bool(href.strip())

    return False


def test_every_surface_jump_destination_is_programmatically_focusable() -> None:
    targets = _surface_targets(_JS.read_text(encoding="utf-8"))
    elements = _elements_by_id(_HTML.read_text(encoding="utf-8"))

    failures: list[str] = []
    for surface, target_id in sorted(targets.items()):
        element = elements.get(target_id)
        if element is None:
            failures.append(f"{surface}: missing target id={target_id!r}")
            continue
        if not _is_programmatically_focusable(element):
            failures.append(
                f"{surface}: id={target_id!r} <{element.tag}> is not "
                "programmatically focusable without changing native Tab order"
            )

    assert not failures, (
        "control 303 calls focus() on selectedSurfaceTarget(); every mapped "
        "destination must therefore be an existing native focus target or use "
        "tabindex=0/-1, never positive tabindex:\n" + "\n".join(failures)
    )


def test_help_about_surface_targets_help_heading() -> None:
    targets = _surface_targets(_JS.read_text(encoding="utf-8"))

    assert targets.get("help_about") == "help-heading"


@pytest.mark.parametrize(
    ("html", "element_id", "expected"),
    [
        ('<button id="x">X</button>', "x", True),
        ('<h2 id="x" tabindex="-1">X</h2>', "x", True),
        ('<table id="x" tabindex="0"></table>', "x", True),
        ('<h2 id="x">X</h2>', "x", False),
        ('<h2 id="x" tabindex="1">X</h2>', "x", False),
        ('<button id="x" disabled>X</button>', "x", False),
    ],
)
def test_focusability_oracle_is_nonvacuous(
    html: str,
    element_id: str,
    expected: bool,
) -> None:
    element = _elements_by_id(html)[element_id]

    assert _is_programmatically_focusable(element) is expected
