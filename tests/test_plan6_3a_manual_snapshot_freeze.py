"""Plan 6 3A: fail closed manual dispatch on an invalid WebView state.

Execute source-exact client fragments in Node; a successful new canonical
snapshot restores the calculator. These are source tests, not Windows/NVDA.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "src/autosport/windows_web/app.js"


def fragment(source: str, start: str, end: str) -> str:
    if source.count(start) != 1 or source.count(end) != 1:
        raise AssertionError("expected one canonical WebView source fragment")
    return start + source.split(start, 1)[1].split(end, 1)[0]


class ManualSnapshotAdmissionTest(unittest.TestCase):
    def test_unverified_state_disables_both_manual_backend_actions(self):
        source = SOURCE.read_text(encoding="utf-8")
        freeze = fragment(
            source,
            "  function freezeUnverifiedOperatorControls() {",
            "  async function apiState() {",
        )
        recovery = fragment(
            source,
            "    const operations = byId(331);",
            "    const productSource = state.product_source || {};",
        )
        self.assertIn("328, 333, 335,", freeze)
        self.assertIn("setDisabledWithFocusFallback(byId(333), false)", recovery)
        self.assertIn("setDisabledWithFocusFallback(byId(335), false)", recovery)
        self.assertEqual(source.count('dispatch("manual.calculate"'), 1)
        self.assertEqual(source.count('dispatch("manual.clear"'), 1)

        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js unavailable; static source assertions passed")
        script = r"""
"use strict";
const assert = require("node:assert/strict");
const [freeze, recovery] = """ + json.dumps([freeze, recovery], ensure_ascii=False) + r""";
const nodes = new Map();
const byId = id => {
  const key = String(id);
  if (!nodes.has(key)) nodes.set(key, {
    disabled: false, options: [], value: "", textContent: "",
  });
  return nodes.get(key);
};
let transitions = [];
const setDisabledWithFocusFallback = (node, disabled) => {
  node.disabled = Boolean(disabled);
  transitions.push([node, node.disabled]);
};
const document = {querySelectorAll() {return [];}};
const freezeControl = new Function(
  "byId", "setDisabledWithFocusFallback", "document", "latestState",
  freeze + "\nreturn freezeUnverifiedOperatorControls;"
)(byId, setDisabledWithFocusFallback, document, {manual:{operations:[]}});
const restore = new Function(
  "byId", "state", "setSelectOptions", "setTextIfChanged",
  "setValueIfChanged", "setDisabledWithFocusFallback", recovery
);
const restoreCanonical = () => restore(
  byId, {manual:{operations:[], status:"Готово",result:""}},
  () => {}, () => {}, () => {}, setDisabledWithFocusFallback
);
assert.equal(byId(333).disabled, false);
assert.equal(byId(335).disabled, false);
freezeControl();
assert.equal(byId(333).disabled, true, "manual calculate must fail closed");
assert.equal(byId(335).disabled, true, "manual clear must fail closed");
restoreCanonical();
assert.equal(byId(333).disabled, false, "confirmed snapshot restores calculate");
assert.equal(byId(335).disabled, false, "confirmed snapshot restores clear");
freezeControl();
assert.equal(byId(333).disabled, true, "second failure freezes calculate again");
assert.equal(byId(335).disabled, true, "second failure freezes clear again");
assert.equal(transitions.filter(([node]) => node === byId(333)).length, 3);
assert.equal(transitions.filter(([node]) => node === byId(335)).length, 3);
console.log("8/8 manual snapshot fail-closed/recovery assertions PASS");
"""
        proc = subprocess.run(
            [node, "-e", script],
            text=True, capture_output=True, timeout=20, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("8/8 manual snapshot", proc.stdout)


if __name__ == "__main__":
    unittest.main()
