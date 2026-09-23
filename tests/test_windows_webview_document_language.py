from __future__ import annotations

import ast
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

import pytest


_CANONICAL_UKRAINIAN_DOCUMENT_LANGUAGE = "uk-UA"


@dataclass(frozen=True, slots=True)
class _EmbeddedDocument:
    path: Path
    html: str


class _DocumentLanguageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.html_start_tags = 0
        self.languages: list[str | None] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.casefold() != "html":
            return
        self.html_start_tags += 1
        lang_values = [
            value for name, value in attrs if name.casefold() == "lang"
        ]
        if len(lang_values) > 1:
            raise AssertionError("<html> must not declare duplicate lang attributes")
        self.languages.append(lang_values[0] if lang_values else None)


def _declared_document_language(html: str) -> str:
    parser = _DocumentLanguageParser()
    parser.feed(html)
    parser.close()
    assert parser.html_start_tags == 1, (
        "canonical semantic document must contain exactly one <html> root"
    )
    assert len(parser.languages) == 1
    language = parser.languages[0]
    assert language is not None and language.strip(), (
        "canonical semantic document must declare a non-empty html lang"
    )
    return language


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
        "semantic HTML document to the language conformance gate"
    )
    return tuple(documents)


def test_canonical_webview_documents_declare_ukrainian_language() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    documents = _canonical_webview_documents(repo_root)

    failures: list[str] = []
    for document in documents:
        language = _declared_document_language(document.html)
        if language != _CANONICAL_UKRAINIAN_DOCUMENT_LANGUAGE:
            failures.append(f"{document.path}: lang={language!r}")

    assert not failures, (
        "canonical WebView2 operator document language must match the "
        "Ukrainian-first product locale uk-UA so browser accessibility and "
        "screen-reader language semantics do not depend on heuristic language "
        "detection:\n" + "\n".join(failures)
    )


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ('<!doctype html><html lang="uk-UA"><body></body></html>', "uk-UA"),
        ('<!doctype html><html lang="en"><body></body></html>', "en"),
    ],
)
def test_document_language_parser_is_nonvacuous(
    html: str,
    expected: str,
) -> None:
    assert _declared_document_language(html) == expected


def test_document_language_parser_rejects_missing_language() -> None:
    with pytest.raises(AssertionError, match="non-empty html lang"):
        _declared_document_language("<!doctype html><html><body></body></html>")


def test_document_language_parser_rejects_duplicate_html_roots() -> None:
    with pytest.raises(AssertionError, match="exactly one <html> root"):
        _declared_document_language(
            '<html lang="uk-UA"></html><html lang="uk-UA"></html>'
        )
