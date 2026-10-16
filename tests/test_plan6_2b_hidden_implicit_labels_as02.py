"""Plan 6 Section 2Б: hidden HTML text cannot silently name a keyboard control.

These are source-level fail-closed semantic contract tests, not rendered Windows
UI Automation or human NVDA acceptance.
"""
from __future__ import annotations

import pytest

from autosport.windows_webview_audit import (
    _SemanticShellParser,
    _reject_missing_accessible_names,
)


def _failures(html: str) -> list[str]:
    parser = _SemanticShellParser()
    parser.feed(html)
    failures: list[str] = []
    _reject_missing_accessible_names(parser, failures)
    return failures


_CONTROL = '<input id="205" type="text" readonly>'
_LABEL = '<label for="205">Віртуальний банк</label>'
_EXPECTED = "id=205: missing nonblank accessible name/label"


@pytest.mark.parametrize(
    "hidden_attribute",
    [
        "hidden",
        "inert",
        'aria-hidden="true"',
        'aria-hidden="TRUE"',
        'style="display: none"',
        'style="visibility: hidden"',
    ],
)
@pytest.mark.parametrize("ancestor_tag", ["section", "div"])
def test_label_under_hidden_ancestor_cannot_supply_implicit_name(
    hidden_attribute: str, ancestor_tag: str
) -> None:
    html = (
        f"<{ancestor_tag} {hidden_attribute}>{_LABEL}</{ancestor_tag}>"
        + _CONTROL
    )
    assert _EXPECTED in _failures(html)


@pytest.mark.parametrize(
    "hidden_attribute",
    [
        "hidden",
        "inert",
        'aria-hidden="true"',
        'style="display:none"',
        'style="visibility:hidden"',
    ],
)
def test_directly_hidden_label_cannot_supply_implicit_name(
    hidden_attribute: str,
) -> None:
    html = f'<label for="205" {hidden_attribute}>Private</label>' + _CONTROL
    assert _EXPECTED in _failures(html)


@pytest.mark.parametrize("hidden_attribute", ["hidden", "inert", 'aria-hidden="true"'])
def test_only_hidden_label_child_cannot_name_control(
    hidden_attribute: str,
) -> None:
    html = (
        f'<label for="205"><span {hidden_attribute}>Private</span></label>'
        + _CONTROL
    )
    assert _EXPECTED in _failures(html)


def test_visible_associated_label_still_names_control() -> None:
    assert _EXPECTED not in _failures(_LABEL + _CONTROL)


def test_visible_sibling_is_not_hidden_by_another_label() -> None:
    html = '<label for="205" hidden>Hidden</label>' + _LABEL + _CONTROL
    assert _EXPECTED not in _failures(html)


def test_explicit_aria_label_remains_valid_when_implicit_label_is_hidden() -> None:
    html = (
        '<section hidden>' + _LABEL + '</section>'
        + '<input id="205" type="text" readonly aria-label="Explicit">'
    )
    assert _EXPECTED not in _failures(html)


def test_explicit_aria_labelledby_can_reference_hidden_text() -> None:
    html = (
        '<span id="accessible-label" hidden>Explicit</span>'
        + '<input id="205" type="text" aria-labelledby="accessible-label">'
    )
    assert _EXPECTED not in _failures(html)


def test_blank_explicit_aria_labelledby_is_not_rescued_by_visible_label() -> None:
    html = (
        '<span id="accessible-label" hidden></span>'
        + _LABEL
        + '<input id="205" type="text" aria-labelledby="accessible-label">'
    )
    assert _EXPECTED in _failures(html)
