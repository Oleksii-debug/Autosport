"""A lost WebView response must remain UNKNOWN until explicitly acknowledged.

Exercises the actual canonical packaged app.js functions with Node/V8.  The
synthetic transport accepts a PAPER request before losing its reply; no
bookmaker, real-money execution, Windows UIA or physical NVDA is involved.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "src/autosport/windows_web/app.js"
HTML = ROOT / "src/autosport/windows_web/index.html"


def test_uncertain_dispatch_has_keyboard_ack_and_no_blind_retry_advice() -> None:
    html = HTML.read_text(encoding="utf-8")
    app = APP.read_text(encoding="utf-8")
    assert html.count('id="dispatch-uncertain-ack"') == 1
    assert 'aria-describedby="error-status" hidden' in html
    assert 'id="error-status" role="alert" aria-live="assertive" aria-atomic="true" tabindex="-1"' in html
    assert 'dispatchUncertainAck.addEventListener("click"' in app
    assert "if (unconfirmedDispatchResult)" in app
    assert "не повторюйте команду всліпу" in app
    assert "Перевірте стан і повторіть дію" not in app
    assert 'globalAnnouncement: false' in (ROOT / "src/autosport/windows_web/emergency_stop.js").read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("node") is None, reason="node unavailable")
def test_accepted_paper_effect_lost_response_is_sticky_until_explicit_ack() -> None:
    script = r'''
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.argv[1], "utf8");
function fragment(a, b) {
  const i = source.indexOf(a), j = source.indexOf(b, i + a.length);
  assert.ok(i >= 0 && j > i, "missing exact packaged JavaScript markers");
  return source.slice(i, j);
}
const code = fragment("  function projectLiveState(", "  function projectEmergencyStopState(") +
             fragment("  async function dispatch(", "  function renderState(");
const events = {};
const statusNode = { textContent:"", hidden:false };
const errorNode = { textContent:"", hidden:true, tabIndex:-1 };
const dispatchUncertainAck = { hidden:true, addEventListener:(event, fn) => { events[event] = fn; } };
const calls = [], focused = [];
let loseReply = true, id = 0, backendEffects = 0;
const sandbox = {
  statusNode, errorNode, dispatchUncertainAck, module:{exports:{}},
  globalThis:{pywebview:{api:{async dispatch(command) {
    calls.push(command);
    backendEffects++;
    if (loseReply) throw Error("secret-transport-failure");
    return {status:"completed", message:"Готово.", focus_id:"app-status"};
  }}}},
  requestId:() => "request-" + (++id),
  invalidateStateProjection:() => {},
  setTextIfChanged:(node, value) => { node.textContent = String(value); },
  setHiddenIfChanged:(node, value) => { node.hidden = value; },
  announce:(message, failure) => {
    if (failure) { errorNode.hidden = false; errorNode.textContent = message; }
    else { errorNode.hidden = true; errorNode.textContent = ""; statusNode.textContent = message; }
  },
  focusOperatorTarget:(node) => { focused.push(node); return true; },
  focusResult:() => {},
  clearErrorStatusWithFocusHandoff:() => { errorNode.hidden = true; errorNode.textContent = ""; },
  refreshState:async () => {},
};
vm.runInNewContext("let unconfirmedDispatchResult = false;\n" + code +
  "\nmodule.exports = {dispatch, projectLiveState};", sandbox);
(async () => {
  const {dispatch, projectLiveState} = sandbox.module.exports;
  const first = await dispatch("replay.run", {dataset_path:"fixture"});
  assert.equal(first, null);
  assert.equal(calls.length, 1);
  assert.equal(backendEffects, 1);
  assert.equal(calls[0].request_id, "request-1");
  assert.equal(errorNode.hidden, false);
  assert.equal(dispatchUncertainAck.hidden, false);
  assert.equal(focused.at(-1), errorNode);
  assert.match(errorNode.textContent, /не повторюйте команду всліпу/i);
  assert.doesNotMatch(errorNode.textContent, /secret-transport/i);
  projectLiveState("Тиха зміна стану.", "");
  assert.equal(errorNode.hidden, false, "background state poll erased UNKNOWN");
  loseReply = false;
  await dispatch("surface.select", {surface_key:"settings"});
  assert.equal(errorNode.hidden, false, "unrelated success erased UNKNOWN");
  assert.equal(dispatchUncertainAck.hidden, false);
  await dispatch("emergency_stop.activate", {}, {
    globalAnnouncement:false, resultFocus:false, postRefresh:false
  });
  assert.equal(errorNode.hidden, false, "STOP presentation stole alert");
  assert.equal(focused.at(-1), errorNode, "STOP stole focus from existing warning");
  events.click();
  assert.equal(errorNode.hidden, true);
  assert.equal(dispatchUncertainAck.hidden, true);
  assert.equal(focused.at(-1), statusNode);
  assert.match(statusNode.textContent, /залишається непідтвердженою/i);
  assert.equal(calls.length, 3);
})().catch(error => { console.error(error); process.exitCode=1; });
''';
    result = subprocess.run(
        [shutil.which("node"), "-e", script, str(APP)],
        text=True, capture_output=True, timeout=15, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
