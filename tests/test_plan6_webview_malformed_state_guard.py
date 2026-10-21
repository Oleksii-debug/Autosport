"""Plan 6 §2Б: source-exact WebView state must fail closed before UI projection."""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest


_APP = Path(__file__).resolve().parents[1] / "src/autosport/windows_web/app.js"


def _source() -> str:
    return _APP.read_text(encoding="utf-8")


def test_untrusted_bridge_state_is_validated_before_projection() -> None:
    script = _source()
    api = script.split("  async function apiState() {", 1)[1].split(
        "\n  async function dispatch(", 1
    )[0]
    assert "return validateOperatorSnapshot(response.state);" in api
    assert "return response.state;" not in api
    assert script.index("function validateOperatorSnapshot(value)") < script.index(
        "  async function apiState() {"
    )


def test_failed_refresh_freezes_actionable_controls_not_emergency_stop() -> None:
    script = _source()
    refresh = script.split("  async function refreshState() {", 1)[1].split(
        "\n  function selectedSurfaceTarget()", 1
    )[0]
    failure = refresh.split("} catch (_error) {", 1)[1]
    assert failure.index("if (requestEpoch === stateProjectionEpoch)") < failure.index(
        "freezeUnverifiedOperatorControls();"
    )
    assert failure.index("freezeUnverifiedOperatorControls();") < failure.index(
        'announce("Не вдалося оновити стан застосунку.", true);'
    )
    freeze = script.split("  function freezeUnverifiedOperatorControls() {", 1)[1].split(
        "\n  async function apiState() {", 1
    )[0]
    for id_value in (
        "product-runtime-start",
        "product-runtime-stop",
        "owner-confirm",
        "product-source-save",
    ):
        assert id_value in freeze
    assert "emergency-stop-action" not in freeze
    assert "byId(301)" not in freeze


def test_source_exact_snapshot_validator_rejects_malformed_bridge_values() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Native Node unavailable; source contract is still checked")
    script = _source()
    validator = "  function validateOperatorSnapshot(value) {" + script.split(
        "  function validateOperatorSnapshot(value) {", 1
    )[1].split("\n  // A broken state refresh", 1)[0]
    program = (
        '"use strict";\n'
        'const assert = require("node:assert/strict");\n'
        + validator
        + """
const valid = {
  owner: { summary: "Дозволено", can_initialize: true, defaults: {}, lines: [], review_lines: [] },
  manual: { operations: [], status: "Готово", result: "" },
  surfaces: [], strategy_choices: [], surface_details: [], live_quotes: [],
  tickets: [], evaluation: [], log: [],
  product_source: { choices: [] },
  product_runtime: { can_start: true, can_stop: false, economic_snapshot: {} },
};
assert.equal(validateOperatorSnapshot(valid), valid);
const invalid = [
  null, {}, [], { owner: [], manual: {} },
  { ...valid, owner: null }, { ...valid, manual: [] },
  { ...valid, surfaces: {} }, { ...valid, tickets: "stale" },
  { ...valid, log: {} }, { ...valid, owner: { ...valid.owner, lines: {} } },
  { ...valid, manual: { operations: {} } },
  { ...valid, product_source: { choices: {} } },
  { ...valid, product_runtime: { can_start: "yes", can_stop: false } },
  { ...valid, product_runtime: { economic_snapshot: [] } },
  { ...valid, status: { private: "never expose" } },
];
for (const sample of invalid) {
  assert.throws(() => validateOperatorSnapshot(sample), /Некоректний стан/);
}
console.log("PASS: source-exact WebView snapshot guard rejects " + invalid.length + " malformed states");
"""
    )
    run = subprocess.run(
        [node, "-e", program],
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    assert "PASS: source-exact" in run.stdout
