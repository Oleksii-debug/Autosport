"""Plan 6 §2Б: emergency STOP must remain keyboard reachable.

Static source checks only; no Windows UIA or human NVDA claim.
"""
from __future__ import annotations

import pytest

from autosport.windows_webview_audit import (
    _SemanticShellParser,
    _reject_inaccessible_emergency_stop,
)

STOP = '<button id="emergency-stop-action" type="button">Зупинити</button>'


def audit(html: str) -> list[str]:
    parser = _SemanticShellParser()
    parser.feed(html)
    parser.close()
    failures: list[str] = []
    _reject_inaccessible_emergency_stop(parser, failures)
    return failures


@pytest.mark.parametrize("markup", [
    '<button id="emergency-stop-action" type="button" disabled>Зупинити</button>',
    '<button id="emergency-stop-action" type="button" disabled="disabled">Зупинити</button>',
    '<div aria-disabled="true">' + STOP + '</div>',
    '<section aria-disabled="true"><div aria-disabled="false">' + STOP + '</div></section>',
    '<fieldset disabled>' + STOP + '</fieldset>',
    '<fieldset disabled><div>' + STOP + '</div></fieldset>',
])
def test_stop_must_reject_native_or_inherited_disabled(markup: str) -> None:
    assert audit(markup), "STOP is unreachable: static audit must fail closed"


@pytest.mark.parametrize("markup", [
    STOP,
    '<div aria-disabled="true"></div>' + STOP,
    '<div aria-disabled="false">' + STOP + '</div>',
    '<input aria-disabled="true">' + STOP,
    '<img aria-disabled="true" alt="">' + STOP,
    '<br aria-disabled="true">' + STOP,
    '<fieldset disabled></fieldset>' + STOP,
])
def test_stop_must_accept_keyboard_reachable_control(markup: str) -> None:
    assert audit(markup) == []


@pytest.mark.parametrize("markup", [
    '<div aria-disabled="true"><input aria-disabled="false">' + STOP + '</div>',
    '<div aria-disabled="true"><br aria-disabled="true">' + STOP + '</div>',
])
def test_void_child_does_not_clear_real_disabled_stop_ancestor(markup: str) -> None:
    assert audit(markup)
