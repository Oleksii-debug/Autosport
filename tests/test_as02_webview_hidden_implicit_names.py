"""Plan 6 §2Б: hidden implicit-name evidence must not false-pass WebView2.

Source-level HTML regressions only, NOT real Windows UIA or physical NVDA.
The canonical production parser is owned by PR #1598.
"""

from __future__ import annotations

import pytest

from autosport.windows_webview_audit import (
    _SemanticShellParser,
    _reject_missing_accessible_names,
)

_NAME = "Віртуальний банк"
_INPUT = '<input id="205" type="text" readonly>'


def _name_failures(html: str) -> list[str]:
    parser = _SemanticShellParser()
    parser.feed(html)
    parser.close()
    failures: list[str] = []
    _reject_missing_accessible_names(parser, failures)
    return failures


@pytest.mark.parametrize(
    "label_markup",
    [
        pytest.param(f'<label for="205" hidden>{_NAME}</label>', id="hidden-label"),
        pytest.param(f'<label for="205" inert>{_NAME}</label>', id="inert-label"),
        pytest.param(f'<label for="205" aria-hidden="true">{_NAME}</label>', id="aria-hidden-label"),
        pytest.param(f'<label for="205" style="display:none">{_NAME}</label>', id="display-none-label"),
        pytest.param(f'<label for="205" style="visibility:hidden">{_NAME}</label>', id="visibility-hidden-label"),
        pytest.param(f'<div hidden><label for="205">{_NAME}</label></div>', id="hidden-ancestor"),
        pytest.param(f'<div inert><label for="205">{_NAME}</label></div>', id="inert-ancestor"),
        pytest.param(f'<div aria-hidden="true"><label for="205">{_NAME}</label></div>', id="aria-hidden-ancestor"),
        pytest.param(f'<div style="display:none"><label for="205">{_NAME}</label></div>', id="display-none-ancestor"),
        pytest.param(f'<div style="visibility:hidden"><label for="205">{_NAME}</label></div>', id="visibility-hidden-ancestor"),
        pytest.param(f'<template><label for="205">{_NAME}</label></template>', id="template-ancestor"),
        pytest.param(f'<label for="205"><span hidden>{_NAME}</span></label>', id="hidden-descendant"),
    ],
)
def test_hidden_associated_label_does_not_name_critical_input(label_markup: str) -> None:
    failures = _name_failures(label_markup + _INPUT)
    assert "id=205: missing nonblank accessible name/label" in failures


@pytest.mark.parametrize(
    "markup",
    [
        pytest.param(f'<label for="205">{_NAME}</label>' + _INPUT, id="visible-label"),
        pytest.param(f'<label for="205" aria-hidden="false">{_NAME}</label>' + _INPUT, id="explicitly-visible-label"),
        pytest.param(f'<label for="205" hidden>{_NAME}</label>' + f'<label for="205">{_NAME}</label>' + _INPUT, id="second-visible-label"),
        pytest.param(f'<label for="205" hidden>{_NAME}</label>' + f'<input id="205" type="text" readonly aria-label="{_NAME}">', id="explicit-aria-label"),
        pytest.param(f'<span id="as02-label" hidden>{_NAME}</span><input id="205" type="text" readonly aria-labelledby="as02-label">', id="explicit-hidden-idref"),
    ],
)
def test_valid_explicit_or_visible_names_survive(markup: str) -> None:
    failures = _name_failures(markup)
    assert not any(f.startswith("id=205: missing nonblank accessible name") for f in failures), failures


@pytest.mark.parametrize(
    "button_markup,should_be_named",
    [
        pytest.param('<button id="101" hidden>Вибрати</button>', False, id="hidden-button"),
        pytest.param('<button id="101" inert>Вибрати</button>', False, id="inert-button"),
        pytest.param('<button id="101" aria-hidden="true">Вибрати</button>', False, id="aria-hidden-button"),
        pytest.param('<div hidden><button id="101">Вибрати</button></div>', False, id="hidden-ancestor"),
        pytest.param('<button id="101">Вибрати</button>', True, id="visible-button"),
    ],
)
def test_button_visibility_determines_implicit_name(
    button_markup: str, should_be_named: bool
) -> None:
    failures = _name_failures(button_markup)
    missing = "id=101: missing nonblank accessible name/label" in failures
    assert missing is not should_be_named
