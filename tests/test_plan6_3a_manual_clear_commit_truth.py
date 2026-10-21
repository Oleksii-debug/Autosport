"""Plan 6 §3А: do not destroy the manual calculation draft on failed/unknown clear.

This is a source-exact client test, not a Windows/NVDA acceptance claim.
The backend owns the manual.clear result and app.js owns the operator's draft.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest


SOURCE = (
    Path(__file__).resolve().parents[1]
    / "src" / "autosport" / "windows_web" / "app.js"
)
START = '  byId(335).addEventListener("click", async () => {'
END = "\n  });"


def _manual_clear_handler() -> str:
    source = SOURCE.read_text(encoding="utf-8")
    if source.count(START) != 1:
        raise AssertionError("manual.clear must have exactly one operator handler")
    after = source.split(START, 1)[1]
    if END not in after:
        raise AssertionError("manual.clear handler is not bounded")
    return START + after.split(END, 1)[0] + END


class ManualClearCommitTruthTest(unittest.TestCase):
    def test_handler_commits_only_confirmed_clear_and_preserves_input_races(self) -> None:
        handler = _manual_clear_handler()
        self.assertIn('await dispatch("manual.clear")', handler)
        self.assertIn('result.status === "completed"', handler)
        self.assertIn("input.value === pendingDraft", handler)
        self.assertNotIn("input.focus()", handler)

    def test_real_shipped_javascript_handler_behaviour(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js unavailable; semantic source test still runs")
        script = r"""
"use strict";
const assert = require("node:assert/strict");
const handlerSource = """ + json.dumps(_manual_clear_handler(), ensure_ascii=False) + r""";
const register = new Function("byId", "dispatch", handlerSource);
async function scenario(label, result, concurrentlyEdited, expected) {
  const field = {value: "ORIGINAL DRAFT", focus() {throw Error("forced focus");}};
  let clickHandler, commandCount = 0;
  const byId = (id) => id === 335
    ? {addEventListener(kind, cb) {
        assert.equal(kind, "click");
        assert.equal(clickHandler, undefined);
        clickHandler = cb;
      }}
    : id === 332 ? field : (() => {throw Error("unknown input id");})();
  const dispatch = async (action) => {
    assert.equal(action, "manual.clear");
    commandCount += 1;
    if (concurrentlyEdited) field.value = "NEWER DRAFT";
    return result;
  };
  register(byId, dispatch);
  await clickHandler();
  assert.equal(commandCount, 1, label);
  assert.equal(field.value, expected, label);
}
(async () => {
  await scenario("rejected", {status:"rejected"}, false, "ORIGINAL DRAFT");
  await scenario("bridge outcome unknown", null, false, "ORIGINAL DRAFT");
  await scenario("explicit unknown", {status:"unknown"}, false, "ORIGINAL DRAFT");
  await scenario("confirmed", {status:"completed"}, false, "");
  await scenario("late success after edit", {status:"completed"}, true, "NEWER DRAFT");
  await scenario("rejected after edit", {status:"rejected"}, true, "NEWER DRAFT");
  console.log("6/6 source-exact manual.clear operator behaviours PASS");
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
        run = subprocess.run(
            [node, "-e", script], text=True, capture_output=True,
            check=False, timeout=20,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("6/6 source-exact", run.stdout)


if __name__ == "__main__":
    unittest.main()
