from html.parser import HTMLParser
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "src" / "autosport" / "windows_web"
_NATIVE_FOCUS_TAGS = {"button", "input", "select", "textarea"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


class _Document(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: dict[str, dict[str, object]] = {}
        self._active_ids: list[str | None] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        element_id = attributes.get("id")
        inherited = self._active_ids[-1] if self._active_ids else None
        active_id = element_id or inherited
        self._active_ids.append(active_id)
        if element_id is not None:
            self.elements[element_id] = {
                "tag": tag,
                "attrs": attributes,
                "text": [],
            }

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self._active_ids:
            self._active_ids.pop()

    def handle_data(self, data: str) -> None:
        if not self._active_ids:
            return
        active_id = self._active_ids[-1]
        if active_id is None or active_id not in self.elements:
            return
        text = self.elements[active_id]["text"]
        assert isinstance(text, list)
        text.append(data)


def _startup_focus_target(script: str) -> str:
    ready = re.search(
        r'window\.addEventListener\("pywebviewready",\s*async\s*\(\)\s*=>\s*\{'
        r"(?P<body>.*?)"
        r"\n\s*\}\);",
        script,
        flags=re.DOTALL,
    )
    assert ready is not None, "canonical WebView startup handler must exist"
    targets = re.findall(
        r'byId\("([^"]+)"\)\.focus\(\);',
        ready.group("body"),
    )
    assert len(targets) == 1, (
        "startup must choose exactly one deterministic focus target"
    )
    return targets[0]


def _has_meaningful_name(element: dict[str, object]) -> bool:
    attrs = element["attrs"]
    text_parts = element["text"]
    assert isinstance(attrs, dict)
    assert isinstance(text_parts, list)
    aria_label = attrs.get("aria-label")
    if isinstance(aria_label, str) and aria_label.strip():
        return True
    return bool(" ".join(str(part) for part in text_parts).strip())


def test_startup_focus_targets_named_heading_or_native_control() -> None:
    script = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
    document_text = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    target_id = _startup_focus_target(script)

    document = _Document()
    document.feed(document_text)
    assert target_id in document.elements, (
        "startup focus target must exist in canonical HTML"
    )

    element = document.elements[target_id]
    tag = element["tag"]
    attrs = element["attrs"]
    assert isinstance(tag, str)
    assert isinstance(attrs, dict)

    assert tag != "main", (
        "startup focus must land on a meaningful heading/control, "
        "not a generic main landmark"
    )
    assert tag in _HEADING_TAGS or tag in _NATIVE_FOCUS_TAGS or (
        tag == "a" and bool(attrs.get("href"))
    ), "startup target must be a semantic heading or native interactive control"
    assert _has_meaningful_name(element), (
        "startup target must expose a stable accessible name"
    )

    if tag in _HEADING_TAGS:
        assert attrs.get("tabindex") in {"-1", "0"}, (
            "heading startup target must be intentionally programmatically focusable"
        )

    tabindex = attrs.get("tabindex")
    if tabindex is not None:
        assert tabindex in {"-1", "0"}, (
            "positive tabindex must not be introduced"
        )


def test_parser_control_rejects_generic_main_target() -> None:
    document = _Document()
    document.feed('<main id="content" tabindex="-1">Робоча область</main>')
    element = document.elements["content"]
    assert element["tag"] == "main"
    assert _has_meaningful_name(element)
