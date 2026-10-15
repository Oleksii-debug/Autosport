"""Negative evidence for malformed semantic HTML in the packaged WebView2 shell.

These checks are machine-only. They do not assert human testing or NVDA speech.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from autosport import windows_webview_audit as audit


def _run_mutated(tmp_path: Path, html: str, *, keyboard: bool) -> dict:
    original_html, original_app = audit._asset_paths()
    modified_index = tmp_path / "index.html"
    modified_index.write_text(html, encoding="utf-8")
    modified_app = tmp_path / "app.js"
    modified_app.write_bytes(original_app.read_bytes())
    if keyboard:
        (tmp_path / "emergency_stop.js").write_bytes(
            original_html.with_name("emergency_stop.js").read_bytes()
        )
    with patch.object(audit, "_asset_paths", return_value=(modified_index, modified_app)):
        return audit.inspect_keyboard_contract() if keyboard else audit.inspect_semantic_shell()


def _assert_machine_fail(report: dict, substring: str) -> None:
    assert report["status"] == "FAIL", report
    assert any(substring in message.lower() for message in report["failures"]), report
    assert report["human_tested"] is False
    assert report["nvda_verified"] is False
    assert report["real_money_execution"] is False


@pytest.mark.parametrize("keyboard", [False, True])
def test_duplicate_emergency_stop_id_fails_both_audits(tmp_path: Path, keyboard: bool) -> None:
    index, _ = audit._asset_paths()
    html = index.read_text(encoding="utf-8")
    assert html.count('id="emergency-stop-action"') == 1
    assert html.count("</main>") == 1
    duplicate = (
        '<button id="emergency-stop-action" type="button" '
        'aria-describedby="emergency-stop-boundary emergency-stop-status">'
        'Дублікат STOP</button>'
    )
    report = _run_mutated(tmp_path, html.replace("</main>", duplicate + "</main>", 1), keyboard=keyboard)
    _assert_machine_fail(report, "duplicate html id values")


@pytest.mark.parametrize("keyboard", [False, True])
def test_duplicate_live_region_id_fails_both_audits(tmp_path: Path, keyboard: bool) -> None:
    index, _ = audit._asset_paths()
    html = index.read_text(encoding="utf-8")
    assert html.count('id="error-status"') == 1
    duplicate = '<p id="error-status" role="alert" aria-live="assertive" aria-atomic="true"></p>'
    report = _run_mutated(tmp_path, html.replace("</main>", duplicate + "</main>", 1), keyboard=keyboard)
    _assert_machine_fail(report, "duplicate html id values")


@pytest.mark.parametrize("keyboard", [False, True])
def test_repeated_id_attribute_fails_both_audits(tmp_path: Path, keyboard: bool) -> None:
    index, _ = audit._asset_paths()
    html = index.read_text(encoding="utf-8")
    original = 'id="emergency-stop-action" type="button"'
    assert html.count(original) == 1
    changed = html.replace(
        original,
        'id="emergency-stop-action" id="emergency-stop-action" type="button"',
        1,
    )
    report = _run_mutated(tmp_path, changed, keyboard=keyboard)
    _assert_machine_fail(report, "duplicate html id attributes")


def test_required_list_whitespace_only_aria_label_fails(tmp_path: Path) -> None:
    index, _ = audit._asset_paths()
    html = index.read_text(encoding="utf-8")
    old = 'aria-label="Контракт вибраного екрана"'
    assert html.count(old) == 1
    changed = html.replace(old, 'aria-label="   "', 1)
    report = _run_mutated(tmp_path, changed, keyboard=False)
    _assert_machine_fail(report, "dynamic list has no aria-label")


@pytest.mark.parametrize("keyboard", [False, True])
@pytest.mark.parametrize(
    ("old", "new", "diagnostic"),
    [
        (
            'for="301"',
            'for="missing-navigation"',
            "for references missing id=missing-navigation",
        ),
        (
            'aria-labelledby="overview-heading"',
            'aria-labelledby="missing-overview-heading"',
            "aria-labelledby references missing id=missing-overview-heading",
        ),
        (
            'aria-describedby="dataset-summary"',
            'aria-describedby="missing-dataset-summary"',
            "aria-describedby references missing id=missing-dataset-summary",
        ),
        (
            'aria-controls="owner-panel"',
            'aria-controls="missing-owner-panel"',
            "aria-controls references missing id=missing-owner-panel",
        ),
        (
            'aria-describedby="dataset-summary"',
            'aria-describedby="dataset-summary missing-help"',
            "aria-describedby references missing id=missing-help",
        ),
        (
            'aria-controls="owner-panel"',
            'aria-controls=" "',
            "malformed html id reference",
        ),
        (
            'for="301"',
            'for="301 302"',
            "malformed html id reference",
        ),
    ],
)
def test_broken_id_references_fail_both_audits(
    tmp_path: Path, keyboard: bool, old: str, new: str, diagnostic: str
) -> None:
    index, _ = audit._asset_paths()
    html = index.read_text(encoding="utf-8")
    assert html.count(old) == 1
    report = _run_mutated(tmp_path, html.replace(old, new, 1), keyboard=keyboard)
    _assert_machine_fail(report, diagnostic)


def test_unmodified_shell_keeps_machine_only_truth_boundary() -> None:
    report = audit.inspect_semantic_shell()
    assert report["status"] == "PASS", report
    assert report["human_tested"] is False
    assert report["nvda_verified"] is False
    assert report["real_money_execution"] is False
