from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_WEB_ROOT = _ROOT / "src" / "autosport" / "windows_web"


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
        role = attr_map.get("role")
        if folded == "main" or (
            isinstance(role, str) and role.casefold() == "main"
        ):
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
    assert len(parser.h1s) == 1, (
        "semantic operator document must not contain additional blank h1"
    )
    return _Orientation(
        title=title,
        main_landmarks=parser.main_landmarks,
        level_one_headings=nonblank_h1s,
    )


def _canonical_documents() -> tuple[Path, ...]:
    assert _WEB_ROOT.is_dir(), "canonical windows_web resource root is missing"
    documents = tuple(sorted(_WEB_ROOT.rglob("*.html")))
    assert documents, (
        "WebView2 orientation gate must inspect at least one canonical HTML document"
    )
    return documents


def test_canonical_webview_documents_have_orientation_semantics() -> None:
    failures: list[str] = []
    for path in _canonical_documents():
        try:
            _document_orientation(path.read_text(encoding="utf-8"))
        except AssertionError as exc:
            failures.append(f"{path.relative_to(_ROOT)}: {exc}")

    assert not failures, (
        "canonical WebView2 documents need deterministic screen-reader "
        "orientation semantics: one title, one main landmark, one non-empty h1:\n"
        + "\n".join(failures)
    )


def test_orientation_parser_accepts_semantic_document() -> None:
    result = _document_orientation(
        "<html><head><title>Автоспорт</title></head>"
        "<body><main><h1>Автоспорт</h1></main></body></html>"
    )
    assert result.title == "Автоспорт"
    assert result.main_landmarks == 1
    assert result.level_one_headings == ("Автоспорт",)


def test_orientation_parser_accepts_role_main_equivalent() -> None:
    result = _document_orientation(
        "<html><head><title>Автоспорт</title></head>"
        '<body><div role="main"><h1>Автоспорт</h1></div></body></html>'
    )
    assert result.main_landmarks == 1


@pytest.mark.parametrize(
    ("html", "match"),
    [
        (
            "<html><head><title> </title></head>"
            "<body><main><h1>Автоспорт</h1></main></body></html>",
            "title must not be blank",
        ),
        (
            "<html><head><title>Автоспорт</title></head>"
            "<body><main><h1>Автоспорт</h1></main>"
            '<div role="main"></div></body></html>',
            "exactly one main landmark",
        ),
        (
            "<html><head><title>Автоспорт</title></head>"
            "<body><main><h2>Автоспорт</h2></main></body></html>",
            "exactly one non-empty h1",
        ),
        (
            "<html><head><title>Автоспорт</title></head>"
            "<body><main><h1>Автоспорт</h1><h1> </h1></main></body></html>",
            "additional blank h1",
        ),
    ],
)
def test_orientation_parser_rejects_ambiguous_documents(
    html: str,
    match: str,
) -> None:
    with pytest.raises(AssertionError, match=match):
        _document_orientation(html)
