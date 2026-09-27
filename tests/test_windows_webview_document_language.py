from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

import pytest


_CANONICAL_LANGUAGE = "uk-UA"
_ROOT = Path(__file__).resolve().parents[1]
_WEB_ROOT = _ROOT / "src" / "autosport" / "windows_web"


class _DocumentLanguageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.html_roots = 0
        self.languages: list[str | None] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.casefold() != "html":
            return
        self.html_roots += 1
        lang_values = [
            value for name, value in attrs if name.casefold() == "lang"
        ]
        if len(lang_values) > 1:
            raise AssertionError(
                "<html> must not declare duplicate lang attributes"
            )
        self.languages.append(lang_values[0] if lang_values else None)


def _declared_language(html: str) -> str:
    parser = _DocumentLanguageParser()
    parser.feed(html)
    parser.close()
    assert parser.html_roots == 1, (
        "canonical WebView document must contain exactly one <html> root"
    )
    assert len(parser.languages) == 1
    language = parser.languages[0]
    assert language is not None and language.strip(), (
        "canonical WebView document must declare a non-empty html lang"
    )
    return language


def _canonical_documents() -> tuple[Path, ...]:
    assert _WEB_ROOT.is_dir(), "canonical windows_web resource root is missing"
    documents = tuple(sorted(_WEB_ROOT.rglob("*.html")))
    assert documents, (
        "WebView2 language gate must inspect at least one canonical HTML document"
    )
    return documents


def test_canonical_webview_documents_declare_ukrainian_language() -> None:
    failures: list[str] = []
    for path in _canonical_documents():
        language = _declared_language(path.read_text(encoding="utf-8"))
        if language != _CANONICAL_LANGUAGE:
            failures.append(f"{path.relative_to(_ROOT)}: lang={language!r}")

    assert not failures, (
        "canonical WebView2 document language must be exact uk-UA so browser "
        "accessibility and screen-reader pronunciation do not depend on "
        "heuristic language detection:\n" + "\n".join(failures)
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
    assert _declared_language(html) == expected


def test_document_language_parser_rejects_missing_language() -> None:
    with pytest.raises(AssertionError, match="non-empty html lang"):
        _declared_language(
            "<!doctype html><html><body></body></html>"
        )


def test_document_language_parser_rejects_duplicate_html_roots() -> None:
    with pytest.raises(AssertionError, match="exactly one <html> root"):
        _declared_language(
            '<html lang="uk-UA"></html><html lang="uk-UA"></html>'
        )


def test_document_language_parser_rejects_duplicate_lang_attributes() -> None:
    with pytest.raises(AssertionError, match="duplicate lang attributes"):
        _declared_language(
            '<html lang="uk-UA" lang="en"><body></body></html>'
        )
