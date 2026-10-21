"""Plan 6 §2Б: source-level opener regression tests against Autosport parser.

These checks are not Windows UIA or human NVDA acceptance.
"""
from __future__ import annotations

import pytest

from autosport.windows_webview_audit import (
    _SemanticShellParser,
    _has_revealable_panel_name,
    _reject_missing_accessible_names,
)


def parse(opener: str) -> _SemanticShellParser:
    parser = _SemanticShellParser()
    parser.feed(
        opener + '<div id="owner-panel" hidden>'
        '<label for="309">Мета</label><input id="309"></div>'
    )
    parser.close()
    return parser


@pytest.mark.parametrize("opener", [
    '<button id="305" aria-controls="owner-panel" aria-expanded="false">Відкрити</button>',
    '<button id="305" type="submit" aria-controls="owner-panel" aria-expanded="false">Відкрити</button>',
    '<button id="305" type="reset" aria-controls="owner-panel" aria-expanded="false">Відкрити</button>',
    '<button id="305" type="button" aria-controls="owner-panel" aria-expanded="false"></button>',
    '<button id="305" type="button" aria-label="" aria-controls="owner-panel" aria-expanded="false"></button>',
    '<button id="305" type="button" aria-labelledby="missing" aria-controls="owner-panel" aria-expanded="false">Відкрити</button>',
    '<div aria-disabled="true"><button id="305" type="button" aria-controls="owner-panel" aria-expanded="false">Відкрити</button></div>',
    '<div aria-disabled="true"><div aria-disabled="false"><button id="305" type="button" aria-controls="owner-panel" aria-expanded="false">Відкрити</button></div></div>',
    '<fieldset disabled><button id="305" type="button" aria-controls="owner-panel" aria-expanded="false">Відкрити</button></fieldset>',
    '<button id="305" type="button" tabindex="-1" aria-controls="owner-panel" aria-expanded="false">Відкрити</button>',
    '<button id="305" type="button" aria-disabled="true" aria-controls="owner-panel" aria-expanded="false">Відкрити</button>',
])
def test_unsafe_opener_cannot_prove_revealability(opener: str) -> None:
    p = parse(opener)
    assert _has_revealable_panel_name(p, "309") is False
    failures: list[str] = []
    _reject_missing_accessible_names(p, failures)
    assert "id=309: missing nonblank accessible name/label" in failures


@pytest.mark.parametrize("opener", [
    '<button id="305" type="button" aria-controls="owner-panel" aria-expanded="false">Відкрити</button>',
    '<button id="305" type="button" aria-label="Розкрити" aria-controls="owner-panel" aria-expanded="false"></button>',
    '<span id="opener-label">Розкрити</span><button id="305" type="button" aria-labelledby="opener-label" aria-controls="owner-panel" aria-expanded="false"></button>',
    '<div aria-disabled="false"><button id="305" type="button" aria-controls="owner-panel" aria-expanded="false">Відкрити</button></div>',
    '<div aria-disabled="true"><button id="other" type="button">Заблоковано</button></div><button id="305" type="button" aria-controls="owner-panel" aria-expanded="false">Відкрити</button>',
])
def test_valid_opener_keeps_revealable_label(opener: str) -> None:
    p = parse(opener)
    assert _has_revealable_panel_name(p, "309") is True
    failures: list[str] = []
    _reject_missing_accessible_names(p, failures)
    assert "id=309: missing nonblank accessible name/label" not in failures
