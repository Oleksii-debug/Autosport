"""Plan 6 3A: invalidate financial owner preview on lost WebView authority.

Executes source-exact functions from the packaged operator surface.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest

APP = Path(__file__).resolve().parents[1] / "src/autosport/windows_web/app.js"


def exact_fragment(source: str, start: str, end: str) -> str:
    if source.count(start) != 1 or source.count(end) != 1:
        raise AssertionError("expected exactly one canonical source fragment")
    return start + source.split(start, 1)[1].split(end, 1)[0]


class OwnerPreviewSnapshotInvalidationTest(unittest.TestCase):
    def test_old_review_cannot_survive_snapshot_failure(self):
        source = APP.read_text(encoding="utf-8")
        review = exact_fragment(
            source, "  function invalidateOwnerReview() {",
            "  function requestId() {",
        )
        freeze = exact_fragment(
            source, "  function freezeUnverifiedOperatorControls() {",
            "  async function apiState() {",
        )
        self.assertIn("    invalidateOwnerReview();", freeze)
        self.assertIn("ownerReviewEpoch += 1;", review)
        self.assertIn("ownerReviewFresh = false;", review)
        self.assertIn('byId("owner-confirm-checkbox").checked = false;', review)
        self.assertNotIn('"emergency-stop-action"', freeze)
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js unavailable; static assertions passed")
        script = r"""
"use strict";
const assert = require("node:assert/strict");
const [review, freeze] = """ + json.dumps([review, freeze], ensure_ascii=False) + r""";
const controls = new Map();
const byId = id => {
  const key = String(id);
  if (!controls.has(key)) controls.set(key, {
    disabled:false, checked:false, options:[], textContent:""
  });
  return controls.get(key);
};
byId("owner-confirm-checkbox").checked = true;
const program = [
  "let ownerReviewEpoch = 2;",
  "let ownerReviewFresh = true;",
  "let latestState = {owner:{can_initialize:true}};",
  "function setDisabledWithFocusFallback(node, disabled) {",
  "  node.disabled = Boolean(disabled);",
  "}",
  "function syncOwnerConfirmationAvailability(canInitialize) {",
  '  const checkbox = byId("owner-confirm-checkbox");',
  "  setDisabledWithFocusFallback(checkbox, !canInitialize || !ownerReviewFresh);",
  '  setDisabledWithFocusFallback(byId("owner-confirm"),',
  "    checkbox.disabled || checkbox.checked !== true);",
  "}",
  review, freeze,
  "return {freezeUnverifiedOperatorControls, markOwnerReviewFresh,",
  " state: () => ({epoch:ownerReviewEpoch,fresh:ownerReviewFresh,latestState,",
  ' checked:byId("owner-confirm-checkbox").checked})};'
].join("\n");
const h = new Function("byId", "document", program)(
  byId, {querySelectorAll(){return [];}});
let n = 0;
assert.equal(h.state().fresh, true); n++;
assert.equal(h.state().checked, true); n++;
h.freezeUnverifiedOperatorControls();
assert.equal(h.state().latestState, null); n++;
assert.equal(h.state().epoch, 3); n++;
assert.equal(h.state().fresh, false); n++;
assert.equal(h.state().checked, false); n++;
assert.equal(byId("owner-confirm").disabled, true); n++;
assert.equal(byId("owner-confirm-checkbox").disabled, true); n++;
assert.equal(h.markOwnerReviewFresh(2), false); n++;
assert.equal(h.state().fresh, false); n++;
h.freezeUnverifiedOperatorControls();
assert.equal(h.state().epoch, 4); n++;
assert.equal(h.markOwnerReviewFresh(3), false); n++;
console.log(n + "/" + n + " owner-review invalidation assertions PASS");
"""
        result = subprocess.run(
            [node, "-e", script],
            capture_output=True, text=True, timeout=20, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("12/12 owner-review invalidation", result.stdout)


if __name__ == "__main__":
    unittest.main()
