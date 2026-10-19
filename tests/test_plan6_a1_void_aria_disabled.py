"""Plan 6 §2Б: product tests for A1 parser void-element ARIA state leakage.

Run in Autosport with: python -m pytest -q tests/test_plan6_a1_void_aria_disabled.py
These are source-level tests, not Windows UIA or human NVDA evidence.
"""
from __future__ import annotations

import pytest

from autosport.windows_webview_audit import (
    _SemanticShellParser,
    _has_revealable_panel_name,
)

VALID_OPENER = (
    '<button id="305" type="button" aria-controls="owner-panel" '
    'aria-expanded="false">Відкрити</button>'
)
PANEL = '<div id="owner-panel" hidden><label for="309">Мета</label><input id="309"></div>'


def _revealable(prefix: str, *, suffix: str = "") -> bool:
    parser = _SemanticShellParser()
    parser.feed(prefix + VALID_OPENER + PANEL + suffix)
    parser.close()
    return _has_revealable_panel_name(parser, "309")


@pytest.mark.parametrize("void_tag", [
    '<input aria-disabled="true">',
    '<img aria-disabled="true" src="x" alt="">',
    '<br aria-disabled="true">',
    '<hr aria-disabled="true">',
])
def test_disabled_void_sibling_does_not_disable_reveal_opener(void_tag: str) -> None:
    assert _revealable(void_tag) is True


def test_nonvoid_disabled_ancestor_still_blocks_reveal_opener() -> None:
    assert _revealable('<div aria-disabled="true">', suffix="</div>") is False


def test_closed_disabled_sibling_does_not_block_reveal_opener() -> None:
    assert _revealable('<div aria-disabled="true"><input></div>') is True


def test_nonvoid_disabled_ancestor_is_not_cleared_by_void_child() -> None:
    assert _revealable('<div aria-disabled="true"><input aria-disabled="false">', suffix="</div>") is False


def test_enabled_void_sibling_is_neutral() -> None:
    assert _revealable('<input aria-disabled="false">') is True
