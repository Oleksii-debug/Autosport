from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from autosport.evaluation_bundle import WalkForwardBundle, evaluate_walk_forward_bundle
from test_forecast_origin_binding import _bundle_raw, _write_bundle, _write_dataset, _write_origin


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inject_self_consistent_future_truth(root: Path) -> None:
    ledger = root / "workspace" / "decisions.jsonl"
    lines = ledger.read_text(encoding="utf-8").splitlines()
    envelope = json.loads(lines[0])
    record = envelope["record"]
    record["payload"]["nested_future_truth"] = {"outcome": "selection-a"}
    canonical = json.dumps(
        record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    envelope["sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    lines[0] = json.dumps(envelope, ensure_ascii=False, sort_keys=True)
    ledger.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    summary_path = root / "workspace" / "run-run-1.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["decision_ledger_sha256"] = _sha256(ledger)
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")


class ForecastOriginFutureKeyIntegrityTests(unittest.TestCase):
    def test_public_origin_verifier_rejects_self_consistently_rehashed_future_truth(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset = _write_dataset(root / "dataset")
            raw = _bundle_raw(dataset)
            _write_origin(root, dataset, raw)
            _inject_self_consistent_future_truth(root)

            with self.assertRaisesRegex(
                ValueError,
                "semantic integrity validation",
            ):
                evaluate_walk_forward_bundle(
                    WalkForwardBundle.from_path(_write_bundle(root, raw))
                )


if __name__ == "__main__":
    unittest.main()
