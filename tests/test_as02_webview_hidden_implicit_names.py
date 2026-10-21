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


# Exact canonical #1598 regression: packaged Windows accessibility audit on
# 74d29aef523235b3316eea5775d6f3584b9b09a7 now FAILS before external UIA.
# The last production parser change excludes implicit labels under *all* hidden
# ancestors, including two intentionally collapsed but keyboard-revealable
# panels. A label/control sharing the SAME verifiably revealable panel must be
# named when that panel is expanded. The negative label-only-hidden tests above
# must remain red until fixed; reverting the protection is not acceptable.
@pytest.mark.parametrize(
    "panel_id, opener_id, control_id, control_markup",
    [
        pytest.param("owner-panel", "305", "309", '<input id="309" type="text">', id="owner-goal"),
        pytest.param("owner-panel", "305", "326", '<input id="326" type="text">', id="owner-restricted-markets"),
        pytest.param("manual-panel", "330", "331", '<select id="331"><option>Додавання</option></select>', id="manual-operation"),
        pytest.param("manual-panel", "330", "332", '<textarea id="332"></textarea>', id="manual-input"),
        pytest.param("manual-panel", "330", "334", '<textarea id="334"></textarea>', id="manual-output"),
    ],
)
def test_cohidden_control_and_label_share_a_revealable_panel(
    panel_id: str, opener_id: str, control_id: str, control_markup: str
) -> None:
    html = (
        f'<button id="{opener_id}" type="button" aria-controls="{panel_id}" '
        'aria-expanded="false">Відкрити панель</button>'
        f'<div id="{panel_id}" hidden>'
        f'<label for="{control_id}">Назва елемента</label>{control_markup}</div>'
    )
    failures = _name_failures(html)
    assert f"id={control_id}: missing nonblank accessible name/label" not in failures, failures


@pytest.mark.parametrize("hidden_label_markup", [
    pytest.param('<label for="309" hidden>Назва елемента</label>', id="label-hidden-locally"),
    pytest.param('<div hidden><label for="309">Назва елемента</label></div>', id="label-extra-hidden-ancestor"),
    pytest.param('<label for="309" inert>Назва елемента</label>', id="label-inert"),
])
def test_revealable_panel_must_not_rescue_still_hidden_implicit_label(
    hidden_label_markup: str,
) -> None:
    html = (
        '<button id="305" aria-controls="owner-panel" aria-expanded="false">Відкрити</button>'
        '<div id="owner-panel" hidden>'
        + hidden_label_markup
        + '<input id="309" type="text"></div>'
    )
    failures = _name_failures(html)
    assert "id=309: missing nonblank accessible name/label" in failures, failures


def test_shipped_collapsible_panels_preserve_critical_accessible_names() -> None:
    # This is source/package HTML, not a claim of computed WebView2 UIA or NVDA.
    from autosport.windows_webview_shell import web_shell_index_path

    failures = _name_failures(web_shell_index_path().read_text(encoding="utf-8"))
    affected = ("309", "326", "331", "332", "334")
    missing = [
        failure for failure in failures
        if any(failure.startswith(f"id={control_id}: missing nonblank accessible name")
               for control_id in affected)
    ]
    assert not missing, missing
