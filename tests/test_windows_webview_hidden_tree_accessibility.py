"""Plan 6/4Б: fail-closed static accessible-name/hidden-tree regressions.

These are source-level checks only: neither physical Windows UIA nor NVDA
acceptance is represented by a PASS here.
"""
from pathlib import Path
from unittest.mock import patch

import pytest

from autosport import windows_webview_audit as audit


def _inspect(tmp_path: Path, html: str, keyboard: bool) -> dict:
    original_index, original_app = audit._asset_paths()
    index = tmp_path / "index.html"
    app = tmp_path / "app.js"
    index.write_text(html, encoding="utf-8")
    app.write_bytes(original_app.read_bytes())
    (tmp_path / "emergency_stop.js").write_bytes(
        original_index.with_name("emergency_stop.js").read_bytes()
    )
    with patch.object(audit, "_asset_paths", return_value=(index, app)):
        return audit.inspect_keyboard_contract() if keyboard else audit.inspect_semantic_shell()


def _html() -> str:
    index, _ = audit._asset_paths()
    return index.read_text(encoding="utf-8")


def _reject(result: dict) -> None:
    assert result["status"] == "FAIL", result
    assert any("emergency-stop-action" in x for x in result["failures"]), result
    assert result["human_tested"] is False
    assert result["nvda_verified"] is False
    assert result["real_money_execution"] is False


@pytest.mark.parametrize("keyboard", [False, True])
@pytest.mark.parametrize("hidden_descendant", [
    '<span aria-hidden="true">Активувати аварійний STOP</span>',
    '<span hidden>Активувати аварійний STOP</span>',
    '<span inert>Активувати аварійний STOP</span>',
    '<span style="display: none">Активувати аварійний STOP</span>',
    '<span style="visibility: hidden">Активувати аварійний STOP</span>',
])
def test_hidden_descendants_do_not_name_emergency_stop(
    tmp_path: Path, keyboard: bool, hidden_descendant: str
) -> None:
    html = _html()
    needle = ">Активувати аварійний STOP</button>"
    assert html.count(needle) == 1
    _reject(_inspect(tmp_path, html.replace(needle, ">" + hidden_descendant + "</button>", 1), keyboard))


@pytest.mark.parametrize("keyboard", [False, True])
@pytest.mark.parametrize("hidden_ancestor", [
    'aria-hidden="true"', 'hidden', 'inert',
    'style="display: none"', 'style="visibility: hidden"',
])
def test_hidden_ancestor_blocks_emergency_stop(
    tmp_path: Path, keyboard: bool, hidden_ancestor: str
) -> None:
    html = _html()
    opening = '<button id="emergency-stop-action" type="button"'
    closing = '>Активувати аварійний STOP</button>'
    assert html.count(opening) == html.count(closing) == 1
    html = html.replace(opening, f'<div {hidden_ancestor}>' + opening, 1)
    html = html.replace(closing, closing + '</div>', 1)
    _reject(_inspect(tmp_path, html, keyboard))


@pytest.mark.parametrize("keyboard", [False, True])
def test_visible_nested_stop_name_stays_accepted(tmp_path: Path, keyboard: bool) -> None:
    html = _html()
    needle = ">Активувати аварійний STOP</button>"
    assert html.count(needle) == 1
    result = _inspect(tmp_path, html.replace(
        needle, "><span>Активувати аварійний STOP</span></button>", 1
    ), keyboard)
    assert result["status"] == "PASS", result
    assert result["human_tested"] is False and result["nvda_verified"] is False


@pytest.mark.parametrize("keyboard", [False, True])
def test_explicit_hidden_idref_name_stays_accepted(tmp_path: Path, keyboard: bool) -> None:
    # The accessible-name algorithm permits explicitly referenced hidden text.
    html = _html()
    anchor = 'id="emergency-stop-action" type="button"'
    closing = ">Активувати аварійний STOP</button>"
    assert html.count(anchor) == html.count(closing) == 1
    html = html.replace(anchor, anchor + ' aria-labelledby="as05-hidden-idref"', 1)
    html = html.replace(closing, "></button>", 1)
    assert html.count("</main>") == 1
    html = html.replace(
        "</main>", '<span id="as05-hidden-idref" hidden>Активувати аварійний STOP</span></main>', 1
    )
    result = _inspect(tmp_path, html, keyboard)
    assert result["status"] == "PASS", result
    assert result["human_tested"] is False and result["nvda_verified"] is False
