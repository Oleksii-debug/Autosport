"""Plan 6 §3А: rejected persisted navigation cannot masquerade as UI success.

A real Node test executes the helper extracted from the shipped WebView2 app.js.
It does not claim full Windows, NVDA or complete Section 3А qualification.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest


APP = Path(__file__).resolve().parents[1] / "src" / "autosport" / "windows_web" / "app.js"


def _exact_helper(script: str) -> str:
    start = "  function reconcileRejectedSurfaceSelection("
    end = "\n  function ownerValues() {"
    assert script.count(start) == 1
    remainder = script.split(start, 1)[1]
    assert end in remainder
    return start + remainder.split(end, 1)[0]


def test_durable_navigation_rejection_is_reconciled_by_actual_ui_handler():
    script = APP.read_text(encoding="utf-8")
    helper = _exact_helper(script)
    event = script.split('  byId(301).addEventListener("change", async () => {', 1)[1]
    event = event.split("\n  });", 1)[0]
    assert 'const requestedKey = navigation.value;' in event
    assert 'await dispatch("surface.select", { surface_key: requestedKey })' in event
    assert 'reconcileRejectedSurfaceSelection(navigation, requestedKey, result, latestState);' in event
    # A timeout/bridge loss remains UNKNOWN; the UI may not invent a rollback.
    assert 'result.status === "rejected"' in helper
    assert 'node.value === requestedKey' in helper


def test_exact_shipped_helper_rejects_false_success_and_preserves_newer_input():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Native Node unavailable; source handler contract is checked")
    helper = _exact_helper(APP.read_text(encoding="utf-8"))
    program = (
        '"use strict";\nconst assert = require("node:assert/strict");\n'
        + helper
        + """
const choice = (value) => ({ value });
const old = { surface_key: "home_dashboard" };
const fail = { status: "rejected" };
const success = { status: "completed" };
let select = choice("market_mirror");
reconcileRejectedSurfaceSelection(select, "market_mirror", fail, old);
assert.equal(select.value, "home_dashboard", "failed save must restore last confirmed screen");
select = choice("market_mirror");
reconcileRejectedSurfaceSelection(select, "market_mirror", success, old);
assert.equal(select.value, "market_mirror", "successful save must not roll back");
select = choice("market_mirror");
reconcileRejectedSurfaceSelection(select, "market_mirror", null, old);
assert.equal(select.value, "market_mirror", "uncertain transport result must not invent rollback");
select = choice("research_agents");
reconcileRejectedSurfaceSelection(select, "market_mirror", fail, old);
assert.equal(select.value, "research_agents", "older rejected request must not clobber newer selection");
select = choice("market_mirror");
reconcileRejectedSurfaceSelection(select, "market_mirror", fail, null);
assert.equal(select.value, "market_mirror", "no verified state must not invent a fallback");
select = choice("market_mirror");
reconcileRejectedSurfaceSelection(select, "market_mirror", fail, { surface_key: 3 });
assert.equal(select.value, "market_mirror", "malformed state must not change selector");
console.log("PASS: six source-exact rejected-navigation cases");
"""
    )
    executed = subprocess.run(
        [node, "-e", program],
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert executed.returncode == 0, executed.stderr
    assert "six source-exact" in executed.stdout
