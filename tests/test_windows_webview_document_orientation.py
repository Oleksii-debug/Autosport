from __future__ import annotations

import ast
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path


@dataclass(frozen=True, slots=True)
class _EmbeddedDocument:
    path: Path
    html: str


@dataclass(frozen=True, slots=True)
class _Orientation:
    title: str
    main_landmarks: int
    level_one_headings: tuple[str, ...]


class _OrientationParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._title_depth = 0
        self._h1_depth = 0
        self._title_parts: list[str] = []
        self._h1_parts: list[str] = []
        self.titles: list[str] = []
        self.h1s: list[str] = []
        self.main_landmarks = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        folded = tag.casefold()
        attr_map = {name.casefold(): value for name, value in attrs}
        if folded == "title":
            self._title_depth += 1
            if self._title_depth == 1:
                self._title_parts = []
        if folded == "h1":
            self._h1_depth += 1
            if self._h1_depth == 1:
                self._h1_parts = []
        if folded == "main" or attr_map.get("role", "").casefold() == "main":
            self.main_landmarks += 1

    def handle_endtag(self, tag: str) -> None:
        folded = tag.casefold()
        if folded == "title" and self._title_depth:
            self._title_depth -= 1
            if self._title_depth == 0:
                self.titles.append("".join(self._title_parts).strip())
        if folded == "h1" and self._h1_depth:
            self._h1_depth -= 1
            if self._h1_depth == 0:
                self.h1s.append("".join(self._h1_parts).strip())

    def handle_data(self, data: str) -> None:
        if self._title_depth:
            self._title_parts.append(data)
        if self._h1_depth:
            self._h1_parts.append(data)


def _document_orientation(html: str) -> _Orientation:
    parser = _OrientationParser()
    parser.feed(html)
    parser.close()
    assert len(parser.titles) == 1, (
        "semantic operator document must expose exactly one <title>"
    )
    title = parser.titles[0]
    assert title, "semantic operator document title must not be blank"
    assert parser.main_landmarks == 1, (
        "semantic operator document must expose exactly one main landmark"
    )
    nonblank_h1s = tuple(value for value in parser.h1s if value)
    assert len(nonblank_h1s) == 1, (
        "semantic operator document must expose exactly one non-empty h1"
    )
    return _Orientation(
        title=title,
        main_landmarks=parser.main_landmarks,
        level_one_headings=nonblank_h1s,
    )


def _full_html_literals(source: str, *, filename: str) -> tuple[str, ...]:
    tree = ast.parse(source, filename=filename)
    documents: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or type(node.value) is not str:
            continue
        value = node.value
        folded = value.casefold()
        if "<html" in folded and "</html>" in folded:
            documents.append(value)
    return tuple(documents)


def _canonical_webview_documents(repo_root: Path) -> tuple[_EmbeddedDocument, ...]:
    source_root = repo_root / "src" / "autosport"
    assert source_root.is_dir(), "canonical src/autosport package is missing"

    documents: list[_EmbeddedDocument] = []
    for path in sorted(source_root.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        folded_source = source.casefold()
        if (
            "webview" not in path.name.casefold()
            and "autosportwebcontroller" not in folded_source
            and "chrome.webview" not in folded_source
        ):
            continue
        for html in _full_html_literals(source, filename=str(path)):
            documents.append(_EmbeddedDocument(path=path, html=html))

    assert documents, (
        "canonical WebView2 source must expose at least one complete embedded "
        "semantic HTML document to the orientation conformance gate"
    )
    return tuple(documents)


def test_canonical_webview_documents_have_orientation_semantics() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    documents = _canonical_webview_documents(repo_root)

    failures: list[str] = []
    for document in documents:
        try:
            _document_orientation(document.html)
        except AssertionError as exc:
            failures.append(f"{document.path}: {exc}")

    assert not failures, (
        "canonical WebView2 operator documents need deterministic screen-reader "
        "orientation semantics (one title, one main landmark, one non-empty h1):\n"
        + "\n".join(failures)
    )


def test_orientation_parser_accepts_semantic_document() -> None:
    result = _document_orientation(
        "<html><head><title>Autosport</title></head>"
        "<body><main><h1>Autosport</h1></main></body></html>"
    )
    assert result.title == "Autosport"
    assert result.main_landmarks == 1
    assert result.level_one_headings == ("Autosport",)


def test_orientation_parser_accepts_role_main_equivalent() -> None:
    result = _document_orientation(
        "<html><head><title>Autosport</title></head>"
        '<body><div role="main"><h1>Autosport</h1></div></body></html>'
    )
    assert result.main_landmarks == 1


def test_orientation_parser_rejects_blank_title() -> None:
    try:
        _document_orientation(
            "<html><head><title> </title></head>"
            "<body><main><h1>Autosport</h1></main></body></html>"
        )
    except AssertionError as exc:
        assert "title must not be blank" in str(exc)
    else:
        raise AssertionError("blank title unexpectedly passed")


def test_orientation_parser_rejects_duplicate_main_landmark() -> None:
    try:
        _document_orientation(
            "<html><head><title>Autosport</title></head>"
            "<body><main><h1>Autosport</h1></main>"
            '<div role="main"></div></body></html>'
        )
    except AssertionError as exc:
        assert "exactly one main landmark" in str(exc)
    else:
        raise AssertionError("duplicate main landmark unexpectedly passed")


def test_orientation_parser_rejects_missing_h1() -> None:
    try:
        _document_orientation(
            "<html><head><title>Autosport</title></head>"
            "<body><main><h2>Autosport</h2></main></body></html>"
        )
    except AssertionError as exc:
        assert "exactly one non-empty h1" in str(exc)
    else:
        raise AssertionError("missing h1 unexpectedly passed")
