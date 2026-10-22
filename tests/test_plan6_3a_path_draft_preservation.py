"""Plan 6 §3А: polling and rejected file-path selection preserve operator drafts.

Tests the helper extracted verbatim from the shipped WebView2 JavaScript.
This is a source/Node regression, not physical Windows or NVDA acceptance.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest

APP = Path(__file__).resolve().parents[1] / "src/autosport/windows_web/app.js"
HELPER_START = "  const pathDraftEdits = new Map();"
HELPER_END = "  // A provider/source choice is a pending operator draft"


def _source() -> str:
    return APP.read_text(encoding="utf-8")


def _helper() -> str:
    source = _source()
    assert source.count(HELPER_START) == 1
    assert source.count(HELPER_END) == 1
    return HELPER_START + source.split(HELPER_START, 1)[1].split(HELPER_END, 1)[0]


class WebViewPathDraftTruthTest(unittest.TestCase):
    def test_canonical_render_and_submit_handlers_use_draft_guard(self) -> None:
        source = _source()
        for identifier in ("dataset-path", "research-plan-path"):
            self.assertIn(f'renderCanonicalPathUnlessDraft("{identifier}",', source)
            self.assertIn(f'finalizeConfirmedPathSelection("{identifier}", editEpoch, result)', source)
        self.assertEqual(source.count('const editEpoch = pathDraftEdits.get("dataset-path");'), 1)
        self.assertEqual(source.count('const editEpoch = pathDraftEdits.get("research-plan-path");'), 1)
        self.assertIn('await dispatch("dataset.select", { path });', source)
        self.assertIn('await dispatch("research_plan.select", { path });', source)
        # Do not import unrelated provider-source DOM listeners into the isolated
        # path-draft helper; preserve their real production reachability.
        self.assertNotIn('byId("product-source-select")', _helper())
        self.assertIn('byId("product-source-select").addEventListener("change"', source)
        self.assertIn('result.status === "completed"', _helper())
        self.assertIn('pathDraftEdits.get(id) === editEpoch', _helper())
        self.assertIn('dataset_validation_pending', _helper())
        self.assertIn('dataset_success_generation <= pending.afterGeneration', _helper())
        backend = (APP.parent.parent / 'windows_webview_shell.py').read_text(encoding='utf-8')
        self.assertIn('self._dataset_success_generation += 1', backend)
        self.assertIn('result["dataset_validation_pending"]', backend)
        self.assertIn('"dataset_success_generation": self._dataset_success_generation', backend)

    def test_source_exact_javascript_preserves_dirty_and_racing_input(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js unavailable; source wiring test still runs")
        script = r"""
"use strict";
const assert = require("node:assert/strict");
const helperSource = """ + json.dumps(_helper(), ensure_ascii=False) + r""";

function harness() {
  const elements = {};
  for (const id of ["dataset-path", "research-plan-path"]) {
    elements[id] = {
      value: "",
      handlers: {},
      addEventListener(type, handler) { this.handlers[type] = handler; },
      edit(text) { this.value = text; this.handlers.input(); },
    };
  }
  const renderValue = (node, value) => { node.value = String(value); };
  const api = new Function(
    "byId", "setValueUnlessFocused",
    helperSource + "\nreturn {pathDraftEdits, unsavedPathDrafts, "
      + "renderCanonicalPathUnlessDraft, finalizeConfirmedPathSelection, confirmValidatedDatasetSelection};"
  )(id => elements[id], renderValue);
  return {elements, ...api};
}

let count = 0;
for (const id of ["dataset-path", "research-plan-path"]) {
  const t = harness(), input = t.elements[id];
  t.renderCanonicalPathUnlessDraft(id, "PERSISTED");
  assert.equal(input.value, "PERSISTED"); count++;
  input.edit("DRAFT");
  const epoch = t.pathDraftEdits.get(id);
  t.renderCanonicalPathUnlessDraft(id, "PERSISTED");
  assert.equal(input.value, "DRAFT", "poll after Tab must keep draft"); count++;
  t.finalizeConfirmedPathSelection(id, epoch, {status:"rejected"});
  t.renderCanonicalPathUnlessDraft(id, "PERSISTED");
  assert.equal(input.value, "DRAFT", "reject must keep draft"); count++;
  t.finalizeConfirmedPathSelection(id, epoch, null);
  t.renderCanonicalPathUnlessDraft(id, "PERSISTED");
  assert.equal(input.value, "DRAFT", "lost bridge reply must keep draft"); count++;
  t.finalizeConfirmedPathSelection(id, epoch, {status:"unknown"});
  t.renderCanonicalPathUnlessDraft(id, "PERSISTED");
  assert.equal(input.value, "DRAFT", "unknown must keep draft"); count++;
  input.edit("NEWER DRAFT");
  t.finalizeConfirmedPathSelection(id, epoch, {status:"completed"});
  t.renderCanonicalPathUnlessDraft(id, "PERSISTED");
  assert.equal(input.value, "NEWER DRAFT", "late success must keep later edit"); count++;
  if (id === "dataset-path") {
    const currentEpoch = t.pathDraftEdits.get(id);
    t.finalizeConfirmedPathSelection(id, currentEpoch, {status:"completed"});
    t.renderCanonicalPathUnlessDraft(id, "OLD");
    assert.equal(input.value, "NEWER DRAFT", "queued action cannot clear draft"); count++;
    t.finalizeConfirmedPathSelection(id, currentEpoch, {
      status:"completed",
      dataset_validation_pending:{path:"CANONICAL", after_generation:6}
    });
    t.renderCanonicalPathUnlessDraft(id, "OLD");
    assert.equal(input.value, "NEWER DRAFT", "pending worker cannot clear draft"); count++;
    t.confirmValidatedDatasetSelection({dataset_path:"CANONICAL", dataset_success_generation:6});
    t.renderCanonicalPathUnlessDraft(id, "OLD");
    assert.equal(input.value, "NEWER DRAFT", "same generation cannot clear draft"); count++;
    t.confirmValidatedDatasetSelection({dataset_path:"WRONG", dataset_success_generation:7});
    t.renderCanonicalPathUnlessDraft(id, "OLD");
    assert.equal(input.value, "NEWER DRAFT", "wrong path cannot clear draft"); count++;
    t.confirmValidatedDatasetSelection({dataset_path:"CANONICAL", dataset_success_generation:7});
    t.renderCanonicalPathUnlessDraft(id, "CANONICAL");
    assert.equal(input.value, "CANONICAL", "successful exact worker clears draft"); count++;
  } else {
    t.finalizeConfirmedPathSelection(id, t.pathDraftEdits.get(id), {status:"completed"});
    t.renderCanonicalPathUnlessDraft(id, "CANONICAL");
    assert.equal(input.value, "CANONICAL", "synchronous research plan restores readback"); count++;
  }
}
const separate = harness();
separate.elements["dataset-path"].edit("DATA DRAFT");
separate.renderCanonicalPathUnlessDraft("research-plan-path", "PLAN SAVED");
separate.renderCanonicalPathUnlessDraft("dataset-path", "DATA SAVED");
assert.equal(separate.elements["dataset-path"].value, "DATA DRAFT");
assert.equal(separate.elements["research-plan-path"].value, "PLAN SAVED");
count++;
console.log(count + "/" + count + " source-exact path draft cases PASS");
"""
        result = subprocess.run(
            [node, "-e", script], capture_output=True, text=True,
            check=False, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("19/19 source-exact", result.stdout)


if __name__ == "__main__":
    unittest.main()
