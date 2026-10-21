"""Plan 6 §3А: no source selected until a canonical backend save confirms it.

Executes verbatim WebView2 client fragments in Node to test draft/rejected/
UNKNOWN/concurrent outcomes. Fixture-only; no bookmaker or Windows/NVDA claim.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "src/autosport/windows_web/app.js"


def _fragment(start: str, end: str) -> str:
    source = SOURCE.read_text(encoding="utf-8")
    if source.count(start) != 1 or source.count(end) != 1:
        raise AssertionError("expected one canonical source selector fragment")
    return source.split(start, 1)[1].split(end, 1)[0]


SETUP = "  const sourceSelectionDraft ="
RENDER = "    const productSource = state.product_source || {};"
SAVE = '  byId("product-source-save").addEventListener("click", async () => {'


class SourceSelectionAuthorityTest(unittest.TestCase):
    def test_client_uses_single_canonical_configure_action_and_truthful_empty_state(self):
        source = SOURCE.read_text(encoding="utf-8")
        setup = SETUP + _fragment(SETUP, "  function hasFocusedReadback")
        render = RENDER + _fragment(RENDER, '    setValueIfChanged(\n      byId("product-source-status")')
        save = SAVE + _fragment(SAVE, '  byId("product-runtime-start").addEventListener')
        self.assertIn('byId("product-source-select").addEventListener("change"', setup)
        self.assertIn('!sourceSelectionDraft.dirty', render)
        self.assertIn('productSource.selected_id || ""', render)
        self.assertIn('await dispatch("product_source.configure"', save)
        self.assertIn('result.status === "rejected"', save)
        self.assertIn('result.status === "completed"', save)
        self.assertIn('sourceSelectionDraft.epoch !== editEpoch', save)
        self.assertEqual(source.count('dispatch("product_source.configure"'), 1)

    def test_source_exact_browser_choices(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js unavailable; static source assertion still runs")
        fragments = [
            SETUP + _fragment(SETUP, "  function hasFocusedReadback"),
            RENDER + _fragment(RENDER, '    setValueIfChanged(\n      byId("product-source-status")'),
            SAVE + _fragment(SAVE, '  byId("product-runtime-start").addEventListener'),
        ]
        script = r"""
"use strict";
const assert = require("node:assert/strict");
const [setup, render, save] = """ + json.dumps(fragments, ensure_ascii=False) + r""";
let checks = 0;
async function scenario(label, previous, outcome, editDuringRequest) {
  const control = {
    value: "", handlers: {},
    addEventListener(kind, listener) {this.handlers[kind] = listener;},
    choose(id) {this.value = id; this.handlers.change();}
  };
  const button = {
    addEventListener(kind, listener) {
      assert.equal(kind, "click");
      this.handler = listener;
    }
  };
  const byId = id => id === "product-source-select" ? control
    : id === "product-source-save" ? button : null;
  const draft = new Function(
    "byId", setup + "\nreturn sourceSelectionDraft;"
  )(byId);
  const state = {product_source: {
    selected_id: previous,
    choices:[{id:"fixture-a",label:"A"},{id:"fixture-b",label:"B"}]
  }};
  const renderFragment = new Function(
    "state","byId","setSelectOptions","setValueUnlessFocused","sourceSelectionDraft",
    render
  );
  const paint = () => renderFragment(
    state, byId,
    (node, options) => {if (!node.value) node.value = options[0].id;},
    (node, value) => {node.value = String(value);},
    draft
  );
  paint();
  assert.equal(control.value, previous, label + " unconfigured/confirmed initial truth");
  checks++;
  control.choose("fixture-a");
  const dispatch = async (action, payload) => {
    assert.equal(action, "product_source.configure");
    assert.equal(payload.source_id, "fixture-a");
    if (outcome === "completed") state.product_source.selected_id = "fixture-a";
    if (editDuringRequest) control.choose("fixture-b");
    paint();
    return outcome === "no-response" ? null : {status:outcome};
  };
  const refreshState = async () => paint();
  new Function(
    "byId","sourceSelectionDraft","dispatch","latestState","refreshState",
    save
  )(byId,draft,dispatch,state,refreshState);
  await button.handler();
  const expected = editDuringRequest ? "fixture-b"
    : outcome === "completed" ? "fixture-a"
    : outcome === "rejected" ? previous : "fixture-a";
  assert.equal(control.value, expected, label);
  checks++;
  const shouldRemainDraft = editDuringRequest
    || outcome === "no-response" || outcome === "unknown";
  assert.equal(draft.dirty, shouldRemainDraft, label);
  checks++;
}
(async () => {
  await scenario("unconfigured rejected", "", "rejected", false);
  await scenario("configured rejected", "fixture-b", "rejected", false);
  await scenario("unconfigured completed", "", "completed", false);
  await scenario("bridge lost", "", "no-response", false);
  await scenario("explicit unknown", "", "unknown", false);
  await scenario("newer edit outruns prior save", "fixture-b", "completed", true);
  console.log(checks + "/" + checks + " source-exact source-selection cases PASS");
})().catch(error => {console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(
            [node, "-e", script], capture_output=True, text=True,
            check=False, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("18/18 source-exact", result.stdout)


if __name__ == "__main__":
    unittest.main()
