"""Plan 6 §3А: faulted WebView dispatch keeps subsequent keyboard focus.

Execute the exact shipped JavaScript dispatch function in Node, with a deferred
bridge response. This is a source/Node test, NOT native Windows or NVDA proof.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest


JS = Path(__file__).resolve().parents[1] / "src/autosport/windows_web/app.js"


def _actual_dispatch() -> str:
    source = JS.read_text(encoding="utf-8")
    start = "  async function dispatch("
    end = "\n  globalThis.autosportDispatch = dispatch;"
    assert source.count(start) == 1
    assert source.count(end) == 1
    return start + source.split(start, 1)[1].split(end, 1)[0]


def test_delayed_bridge_failure_checks_operator_focus_epoch() -> None:
    dispatch = _actual_dispatch()
    assert "operatorFocusEpoch === focusEpochAtRequest" in dispatch
    assert "document.activeElement === focusedElementAtRequest" in dispatch
    assert "focusOperatorTarget(errorNode);" in dispatch
    assert "unconfirmedDispatchResult = true;" in dispatch
    assert "setHiddenIfChanged(dispatchUncertainAck, false);" in dispatch


def test_exact_bridge_dispatch_preserves_newer_keyboard_focus_in_node() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node unavailable; exact shipped focus guard checked above")
    program = (
        '"use strict";\nconst assert = require("node:assert/strict");\n'
        + """
let operatorFocusEpoch = 0;
let unconfirmedDispatchResult = false;
const focusEvents = [];
const announcements = [];
const statusNode = { textContent: "" };
const errorNode = { hidden: true };
const dispatchUncertainAck = { hidden: true };
const document = { activeElement: { id: "replay-button" } };
const invalidateStateProjection = () => {};
const requestId = () => "focus-regression-request";
const setTextIfChanged = (node, value) => { node.textContent = value; };
const setHiddenIfChanged = (node, value) => { node.hidden = Boolean(value); };
const announce = (message, assertive) => { announcements.push([message, assertive]); };
const focusOperatorTarget = (node) => {
  focusEvents.push(node);
  document.activeElement = node;
};
const focusResult = () => {};
const refreshState = async () => {};
"""
        + _actual_dispatch()
        + """
(async () => {
  globalThis.pywebview = {
    api: { dispatch: async () => { throw new Error("transport"); } },
  };
  assert.equal(await dispatch("replay.run"), null);
  assert.equal(focusEvents.length, 1, "immediate fault should focus warning");
  assert.equal(focusEvents[0], errorNode);
  assert.equal(unconfirmedDispatchResult, true);
  assert.equal(dispatchUncertainAck.hidden, false);
  focusEvents.length = 0;
  const oldFocus = { id: "old-control" };
  const newFocus = { id: "other-control" };
  document.activeElement = oldFocus;
  let failPending;
  globalThis.pywebview.api.dispatch = () => new Promise(
    (_, reject) => { failPending = reject; },
  );
  const inFlight = dispatch("replay.run");
  document.activeElement = newFocus;
  operatorFocusEpoch += 1;
  failPending(new Error("late bridge failure with sensitive detail"));
  assert.equal(await inFlight, null);
  assert.equal(document.activeElement, newFocus, "must not steal later focus");
  assert.equal(focusEvents.length, 0, "late failure should only announce");
  assert.equal(dispatchUncertainAck.hidden, false);
  assert.equal(unconfirmedDispatchResult, true);
  assert.equal(announcements.at(-1)[1], true);
  assert.equal(announcements.at(-1)[0].includes("sensitive detail"), false);
  console.log("PASS: same-focus failure and delayed-focus-failure");
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    )
    run = subprocess.run(
        [node, "-e", program], capture_output=True, text=True,
        timeout=15, check=False,
    )
    assert run.returncode == 0, run.stderr
    assert "PASS: same-focus failure and delayed-focus-failure" in run.stdout
