"""Bounded Plan-6 Section 4B malformed accessible-name regressions, AS-05.

Machine/source assertions only: HUMAN_TESTED=false, NVDA_VERIFIED=false.
"""
from pathlib import Path
from unittest.mock import patch

import pytest

from autosport import windows_webview_audit as audit


def _check(tmp_path: Path, html: str, keyboard: bool) -> dict:
    index, app = audit._asset_paths()
    (tmp_path / "index.html").write_text(html, encoding="utf-8")
    (tmp_path / "app.js").write_bytes(app.read_bytes())
    (tmp_path / "emergency_stop.js").write_bytes(
        index.with_name("emergency_stop.js").read_bytes()
    )
    with patch.object(
        audit, "_asset_paths",
        return_value=(tmp_path / "index.html", tmp_path / "app.js"),
    ):
        return (
            audit.inspect_keyboard_contract()
            if keyboard
            else audit.inspect_semantic_shell()
        )


@pytest.mark.parametrize("keyboard", [False, True])
@pytest.mark.parametrize(
    ("old", "new", "control"),
    [
        (
            ">Активувати аварійний STOP</button>",
            "> \t </button>",
            "emergency-stop-action",
        ),
        (
            '<label for="product-source-select">Джерело даних для тривалої симуляційної роботи</label>',
            '<label for="product-source-select">  \t  </label>',
            "product-source-select",
        ),
    ],
)
def test_blank_critical_name_fails_closed(
    tmp_path: Path, keyboard: bool, old: str, new: str, control: str
) -> None:
    index, _ = audit._asset_paths()
    html = index.read_text(encoding="utf-8")
    assert html.count(old) == 1, "canonical HTML fixture changed"
    result = _check(tmp_path, html.replace(old, new, 1), keyboard)
    assert result["status"] == "FAIL", result
    assert any(control in message and "accessible name" in message
               for message in result["failures"]), result
    assert result["human_tested"] is False
    assert result["nvda_verified"] is False
    assert result["real_money_execution"] is False


@pytest.mark.parametrize("keyboard", [False, True])
def test_nested_button_name_remains_valid(tmp_path: Path, keyboard: bool) -> None:
    index, _ = audit._asset_paths()
    html = index.read_text(encoding="utf-8")
    old = ">Активувати аварійний STOP</button>"
    assert html.count(old) == 1
    result = _check(
        tmp_path,
        html.replace(old, "><span>Активувати аварійний STOP</span></button>", 1),
        keyboard,
    )
    assert result["status"] == "PASS", result


@pytest.mark.parametrize("keyboard", [False, True])
def test_blank_aria_labelledby_has_precedence_over_button_text(
    tmp_path: Path, keyboard: bool
) -> None:
    index, _ = audit._asset_paths()
    html = index.read_text(encoding="utf-8")
    old = 'id="emergency-stop-action" type="button"'
    assert html.count(old) == 1
    # Valid target exists, but is empty; own button text must not mask it.
    html = html.replace(
        old,
        old + ' aria-labelledby="as05-empty-label"',
        1,
    )
    html = html.replace(
        "</main>",
        '<span id="as05-empty-label"></span></main>',
        1,
    )
    result = _check(tmp_path, html, keyboard)
    assert result["status"] == "FAIL", result
    assert any(
        "emergency-stop-action" in message and "accessible name" in message
        for message in result["failures"]
    ), result

@pytest.mark.parametrize("keyboard", [False, True])
@pytest.mark.parametrize(
    ("attribute", "diagnostic"),
    [
        ("hidden", "hidden/inert"),
        ("inert", "hidden/inert"),
        ('aria-hidden="true"', "aria-hidden"),
        ('aria-hidden="invalid"', "aria-hidden"),
        ('aria-disabled="true"', "aria-disabled"),
        ('role="none"', "native button role"),
        ('role="presentation"', "native button role"),
        ('role="link"', "native button role"),
        ('tabindex="-1"', "normal Tab order"),
        ('style="display: none"', "inline CSS"),
        ('style="visibility: hidden"', "inline CSS"),
    ],
)
def test_emergency_stop_static_accessibility_exclusion_fails_closed(
    tmp_path: Path, keyboard: bool, attribute: str, diagnostic: str
) -> None:
    index, _ = audit._asset_paths()
    html = index.read_text(encoding="utf-8")
    anchor = 'id="emergency-stop-action" type="button"'
    assert html.count(anchor) == 1, "canonical HTML fixture changed"
    result = _check(tmp_path, html.replace(anchor, anchor + " " + attribute, 1), keyboard)
    assert result["status"] == "FAIL", result
    assert any(
        "emergency-stop-action" in message and diagnostic in message
        for message in result["failures"]
    ), result
    assert result["human_tested"] is False
    assert result["nvda_verified"] is False
    assert result["real_money_execution"] is False


@pytest.mark.parametrize("keyboard", [False, True])
def test_emergency_stop_explicit_accessible_states_are_permitted(
    tmp_path: Path, keyboard: bool
) -> None:
    index, _ = audit._asset_paths()
    html = index.read_text(encoding="utf-8")
    anchor = 'id="emergency-stop-action" type="button"'
    assert html.count(anchor) == 1
    result = _check(
        tmp_path,
        html.replace(
            anchor,
            anchor + ' aria-hidden="false" aria-disabled="false" role="button" tabindex="0"',
            1,
        ),
        keyboard,
    )
    assert result["status"] == "PASS", result
    assert result["human_tested"] is False
    assert result["nvda_verified"] is False
    assert result["real_money_execution"] is False
