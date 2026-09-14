import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from autosport.decision_ledger import (
    DecisionLedgerIntegrityError,
    DecisionRecord,
    JsonlDecisionLedger,
)


class DecisionLedgerFutureKeyIntegrityTests(unittest.TestCase):
    @staticmethod
    def _record(payload: dict[str, object]) -> DecisionRecord:
        return DecisionRecord(
            replay_run_id="run-causal",
            agent="agent-a",
            observed_ts="2026-01-01T00:00:00+00:00",
            action="OBSERVE",
            payload=payload,
            context_hash="context-a",
            decision_id="decision-causal",
            recorded_at="2026-01-01T00:00:01+00:00",
        )

    def test_constructor_rejects_nested_future_result_fields(self):
        with self.assertRaisesRegex(ValueError, "future-result fields"):
            self._record({"nested": [{"winner": "selection-a"}]})

    def test_payload_is_snapshotted_and_normal_mutation_is_blocked(self):
        original = {"nested": [{"feature": "serve-form"}]}
        record = self._record(original)

        original["winner"] = "selection-b"
        original["nested"][0]["result"] = "selection-b"

        self.assertNotIn("winner", record.payload)
        self.assertNotIn("result", record.payload["nested"][0])
        with self.assertRaisesRegex(TypeError, "DecisionRecord payload is immutable"):
            record.payload["winner"] = "selection-b"
        with self.assertRaisesRegex(TypeError, "DecisionRecord payload is immutable"):
            record.payload["nested"].append({"result": "selection-b"})
        with self.assertRaisesRegex(TypeError, "DecisionRecord payload is immutable"):
            record.payload["nested"][0]["result"] = "selection-b"

    def test_to_dict_is_detached_and_append_persists_frozen_snapshot(self):
        record = self._record({"nested": [{"feature": "serve-form"}]})
        exported = record.to_dict()
        exported["payload"]["winner"] = "selection-b"
        exported["payload"]["nested"][0]["result"] = "selection-b"

        self.assertNotIn("winner", record.payload)
        self.assertNotIn("result", record.payload["nested"][0])

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            ledger = JsonlDecisionLedger(path)
            digest = ledger.append(record)
            envelope = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(ledger.verify_integrity(), 1)
            self.assertEqual(envelope["sha256"], digest)
            self.assertNotIn("winner", envelope["record"]["payload"])
            self.assertNotIn("result", envelope["record"]["payload"]["nested"][0])

    def test_verifier_rejects_self_consistently_hashed_future_result_payload(self):
        forged_record = {
            "replay_run_id": "run-causal",
            "agent": "agent-a",
            "observed_ts": "2026-01-01T00:00:00+00:00",
            "action": "OBSERVE",
            "payload": {"nested": [{"result": "selection-a"}]},
            "context_hash": "context-a",
            "decision_id": "forged-decision",
            "recorded_at": "2026-01-01T00:00:01+00:00",
        }
        canonical = json.dumps(
            forged_record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        envelope = {
            "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "record": forged_record,
        }

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            path.write_text(json.dumps(envelope, sort_keys=True) + "\n", encoding="utf-8")
            ledger = JsonlDecisionLedger(path)

            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "future-result fields at line 1",
            ):
                ledger.verify_integrity()


if __name__ == "__main__":
    unittest.main()
