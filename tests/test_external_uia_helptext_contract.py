from __future__ import annotations

import re
from pathlib import Path

from autosport.localization import text


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "external_uia_audit.ps1"

HELP_TEXT_KEYS = {
    "choose_dataset": "ui.accessibility.choose_dataset.description",
    "run_replay": "ui.accessibility.run_replay.description",
    "replay_speed": "ui.accessibility.replay_speed.description",
    "live_mode": "ui.accessibility.live_mode.description",
    "live_refresh": "ui.accessibility.live_refresh.description",
    "strategy": "ui.accessibility.strategy.description",
    "research_plan": "ui.accessibility.research_plan.description",
    "repair_workspace": "ui.accessibility.repair_workspace.description",
    "tickets": "ui.accessibility.tickets.description",
    "log": "ui.accessibility.log.description",
    "live_quotes": "ui.accessibility.live_quotes.description",
    "evaluation": "ui.accessibility.evaluation.description",
    "bankroll": "ui.accessibility.bankroll.description",
    "shell_navigation": "ui.windows.shell.accessibility.navigation.description",
    "shell_state": "ui.windows.shell.accessibility.state.description",
    "shell_open": "ui.windows.shell.accessibility.open.description",
    "shell_details": "ui.windows.shell.accessibility.details.description",
    "owner_economic_open": "ui.windows.owner_authority.accessibility.open.description",
    "owner_economic_status": "ui.windows.owner_authority.accessibility.status.description",
    "owner_economic_readback": "ui.windows.owner_authority.accessibility.readback.description",
    "manual_calculation_open": "ui.windows.manual_calculation.accessibility.open.description",
    "manual_calculation_operation": "ui.windows.manual_calculation.uia.operation.description",
    "manual_calculation_input": "ui.windows.manual_calculation.uia.input.description",
    "manual_calculation_calculate": "ui.windows.manual_calculation.uia.calculate.description",
    "manual_calculation_result": "ui.windows.manual_calculation.uia.result.description",
    "manual_calculation_clear": "ui.windows.manual_calculation.uia.clear.description",
    "manual_calculation_close": "ui.windows.manual_calculation.uia.close.description",
}

_ENTRY_RE = re.compile(
    r"\[ordered\]@\{ key = '([^']+)'; automation_id = '([^']+)'; "
    r"name = '([^']+)'; help_text = '([^']+)';"
)
_KEY_RE = re.compile(r"\[ordered\]@\{ key = '([^']+)';")


def _expected_block(script: str) -> str:
    return script.split("$expected = @(", 1)[1].split(")\n\nfunction Test-Pattern", 1)[0]


def test_external_uia_specs_bind_every_critical_control_to_canonical_help_text() -> None:
    script = SCRIPT_PATH.read_text(encoding="utf-8")
    block = _expected_block(script)
    keys = _KEY_RE.findall(block)
    entries = {key: (automation_id, name, help_text) for key, automation_id, name, help_text in _ENTRY_RE.findall(block)}

    assert len(keys) == len(set(keys))
    assert set(entries) == set(keys) == set(HELP_TEXT_KEYS)
    for key, (_automation_id, _name, help_text) in entries.items():
        assert help_text
        assert help_text == text(HELP_TEXT_KEYS[key])


def test_external_uia_gate_reads_records_and_fail_closes_on_help_text_mismatch() -> None:
    script = SCRIPT_PATH.read_text(encoding="utf-8")

    assert "$currentHelpText = [string]$element.Current.HelpText" in script
    assert "expected_help_text = $spec.help_text" in script
    assert "help_text = $currentHelpText" in script
    assert "if ($currentHelpText -ne [string]$spec.help_text)" in script
    assert "external UIA HelpText mismatch" in script


def test_external_uia_help_text_gate_preserves_machine_only_truth_boundary() -> None:
    script = SCRIPT_PATH.read_text(encoding="utf-8")

    assert "human_tested = $false" in script
    assert "nvda_verified = $false" in script
    assert "real_money_execution = $false" in script
