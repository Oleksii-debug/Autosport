"""Plan 6 §3А: rejected PAPER config commands restore server-confirmed selectors.

Node executes the exact shipped helper; the three real DOM event handlers are
checked for use of the canonical dispatch and verified-state projection.
No Windows/NVDA/live-bookmaker completion is implied by these focused tests.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest


JS = Path(__file__).resolve().parents[1] / "src/autosport/windows_web/app.js"


def _helper(source: str) -> str:
    start = "  function reconcileRejectedControlSelection("
    end = "\n  function reconcileRejectedSurfaceSelection("
    assert source.count(start) == 1
    fragment = source.split(start, 1)[1]
    assert end in fragment
    return start + fragment.split(end, 1)[0]


@pytest.mark.parametrize("control,action,verified", [
    ("106", "strategy.set", "strategy_id"),
    ("103", "replay_speed.set", "replay_speed"),
    ("104", "live_mode.set", "live_mode"),
])
def test_canonical_select_handlers_reconcile_confirmed_rejection(control, action, verified):
    source = JS.read_text(encoding="utf-8")
    marker = '  byId(' + control + ').addEventListener("change", async () => {'
    assert source.count(marker) == 1
    handler = source.split(marker, 1)[1].split("\n  });", 1)[0]
    assert 'const requested = control.value;' in handler
    assert 'await dispatch("' + action + '"' in handler
    assert 'reconcileRejectedControlSelection(' in handler
    assert 'latestState.' + verified in handler
    assert "result.status === \"rejected\"" in _helper(source)


def test_actual_webview_js_selector_rollback_logic_executes_in_node():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node unavailable; real source/event wiring checked above")
    script = (
        '"use strict";\nconst assert = require("node:assert/strict");\n'
        + _helper(JS.read_text(encoding="utf-8"))
        + """
const fail = { status: "rejected" };
const ok = { status: "completed" };
const choice = (value) => ({ value });
let control = choice("new");
reconcileRejectedControlSelection(control, "new", fail, "old");
assert.equal(control.value, "old");
control = choice("new");
reconcileRejectedControlSelection(control, "new", ok, "old");
assert.equal(control.value, "new");
control = choice("new");
reconcileRejectedControlSelection(control, "new", null, "old");
assert.equal(control.value, "new");
control = choice("even-newer");
reconcileRejectedControlSelection(control, "new", fail, "old");
assert.equal(control.value, "even-newer");
control = choice("new");
reconcileRejectedControlSelection(control, "new", fail, null);
assert.equal(control.value, "new");
control = choice("new");
reconcileRejectedControlSelection(control, "new", fail, "new");
assert.equal(control.value, "new");
console.log("PASS: six source-exact config selector cases");
"""
    )
    run = subprocess.run([node, "-e", script], capture_output=True, text=True,
                         timeout=15, check=False)
    assert run.returncode == 0, run.stderr
    assert "six source-exact" in run.stdout
