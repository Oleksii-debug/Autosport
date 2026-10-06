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

    @staticmethod
    def _record_dict() -> dict[str, object]:
        return {
            "replay_run_id": "run-causal",
            "agent": "agent-a",
            "observed_ts": "2026-01-01T00:00:00+00:00",
            "action": "OBSERVE",
            "payload": {"feature": "serve-form"},
            "context_hash": "context-a",
            "decision_id": "decision-causal",
            "recorded_at": "2026-01-01T00:00:01+00:00",
        }

    def test_constructor_rejects_nested_future_result_fields(self):
        for key in ("result", "winner", "outcome"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, "future-result fields"):
                    self._record({"nested": [{key: "selection-a"}]})

    def test_payload_is_snapshotted_and_structurally_immutable(self):
        original = {"nested": [{"feature": "serve-form"}]}
        record = self._record(original)

        original["winner"] = "selection-b"
        original["nested"][0]["result"] = "selection-b"

        self.assertNotIn("winner", record.payload)
        self.assertNotIn("result", record.payload["nested"][0])
        self.assertNotIsInstance(record.payload, dict)
        self.assertNotIsInstance(record.payload["nested"][0], dict)
        self.assertIsInstance(record.payload["nested"], tuple)
        with self.assertRaises(TypeError):
            dict.__setitem__(record.payload, "winner", "selection-b")
        with self.assertRaises(TypeError):
            dict.__setitem__(record.payload["nested"][0], "result", "selection-b")
        with self.assertRaises(TypeError):
            list.append(record.payload["nested"], {"result": "selection-b"})
        self.assertNotIn("winner", record.payload)
        self.assertNotIn("result", record.payload["nested"][0])

    def test_to_dict_is_detached_and_append_persists_frozen_snapshot(self):
        record = self._record({"nested": [{"feature": "serve-form"}]})
        exported = record.to_dict()
        self.assertIsInstance(exported["payload"]["nested"], list)
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

    def test_caller_tuple_remains_unsupported_for_json_ledger(self):
        record = self._record({"tuple_value": ("not-json-list",)})
        self.assertIsInstance(record.payload["tuple_value"], tuple)
        self.assertNotIsInstance(record.payload["tuple_value"], list)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            with self.assertRaisesRegex(DecisionLedgerIntegrityError, "unsupported type tuple"):
                JsonlDecisionLedger(path).append(record)

    def test_verifier_rejects_self_consistently_hashed_future_result_payload(self):
        forged_record = {
            "replay_run_id": "run-causal",
            "agent": "agent-a",
            "observed_ts": "2026-01-01T00:00:00+00:00",
            "action": "OBSERVE",
            "payload": {"nested": [{"outcome": "selection-a"}]},
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

    def test_verifier_rejects_lone_surrogate_strings_before_hashing(self):
        cases = (
            ("payload-value", lambda record: record["payload"].update({"note": "\ud800"})),
            ("payload-key", lambda record: record["payload"].update({"\ud800": "value"})),
            ("required-agent", lambda record: record.update({"agent": "\ud800"})),
        )
        for label, mutate in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                record = self._record_dict()
                mutate(record)
                envelope = {"sha256": "0" * 64, "record": record}
                path = Path(tmp) / "decisions.jsonl"
                path.write_bytes(
                    (json.dumps(envelope, ensure_ascii=True, sort_keys=True) + "\n").encode("utf-8")
                )

                with self.assertRaisesRegex(DecisionLedgerIntegrityError, "valid UTF-8"):
                    JsonlDecisionLedger(path).verified_snapshot()

    def test_append_rejects_lone_surrogate_without_publishing_partial_line(self):
        record = self._record({"note": "\ud800"})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            ledger = JsonlDecisionLedger(path)

            with self.assertRaisesRegex(DecisionLedgerIntegrityError, "valid UTF-8"):
                ledger.append(record)

            self.assertFalse(path.exists())

    def test_valid_cyrillic_json_round_trips_through_durable_ledger(self):
        record = self._record({"нотатка": "дані", "nested": [{"ключ": "значення"}]})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            ledger = JsonlDecisionLedger(path)

            ledger.append(record)
            snapshot = ledger.verified_snapshot()
            envelope = json.loads(snapshot.payload.decode("utf-8"))

            self.assertEqual(snapshot.record_count, 1)
            self.assertEqual(envelope["record"]["payload"]["нотатка"], "дані")
            self.assertEqual(
                envelope["record"]["payload"]["nested"][0]["ключ"],
                "значення",
            )


if __name__ == "__main__":
    unittest.main()
