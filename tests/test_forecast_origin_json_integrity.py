import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from autosport.forecast_origin import _load_summary, _validated_ledger_prefixes


def _dataset_identity():
    return SimpleNamespace(
        import_identity="1" * 64,
        market_sha256="2" * 64,
        results_sha256="3" * 64,
    )


def _valid_summary(dataset):
    return {
        "schema_version": 2,
        "transaction_schema_version": 1,
        "run_id": "run-1",
        "transaction_run_id": "run-1",
        "real_money_execution": False,
        "dataset_schema_version": 2,
        "historical_import_identity": dataset.import_identity,
        "market_sha256": dataset.market_sha256,
        "sealed_results_sha256": dataset.results_sha256,
        "strategy_runtime": {"canonical_strategy_id": "research-replay-v1"},
        "decision_ledger_sha256": "4" * 64,
    }


def _canonical_record_sha(record):
    canonical = json.dumps(
        record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ForecastOriginJsonIntegrityTests(unittest.TestCase):
    def test_valid_run_summary_remains_loadable(self):
        dataset = _dataset_identity()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "summary.json"
            path.write_text(json.dumps(_valid_summary(dataset)), encoding="utf-8")
            loaded = _load_summary(path, dataset)
            self.assertEqual(loaded["run_id"], "run-1")
            self.assertEqual(loaded["decision_ledger_sha256"], "4" * 64)

    def test_run_summary_rejects_duplicate_json_key(self):
        dataset = _dataset_identity()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "summary.json"
            text = json.dumps(_valid_summary(dataset))
            text = text.replace(
                '"run_id": "run-1"',
                '"run_id": "run-1", "run_id": "run-1"',
                1,
            )
            path.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate JSON object key: run_id"):
                _load_summary(path, dataset)

    def test_run_summary_rejects_nonstandard_json_constant(self):
        dataset = _dataset_identity()
        raw = _valid_summary(dataset)
        raw["unused"] = float("nan")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "summary.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "non-standard JSON constant: NaN"):
                _load_summary(path, dataset)

    def test_run_summary_schema_fields_require_exact_integer_types(self):
        dataset = _dataset_identity()
        cases = (
            ("schema_version", 2.0, "schema_version 2"),
            (
                "transaction_schema_version",
                True,
                "canonical transaction precommit evidence",
            ),
            (
                "dataset_schema_version",
                2.0,
                "governed dataset schema v2",
            ),
        )
        for field, value, message in cases:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                raw = _valid_summary(dataset)
                raw[field] = value
                path = Path(tmp) / "summary.json"
                path.write_text(json.dumps(raw), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, message):
                    _load_summary(path, dataset)

    def test_valid_committed_ledger_prefix_remains_loadable(self):
        record = {"x": 1}
        digest = _canonical_record_sha(record)
        line = json.dumps(
            {"record": record, "sha256": digest},
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        expected_prefix = hashlib.sha256(line).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            path.write_bytes(line)
            found = _validated_ledger_prefixes(path, {expected_prefix})
            self.assertIn(expected_prefix, found)
            self.assertEqual(found[expected_prefix][0]["record"], record)

    def test_committed_ledger_prefix_rejects_duplicate_record_key(self):
        record = {"x": 1}
        digest = _canonical_record_sha(record)
        line = (
            '{"record":{"x":1,"x":1},"sha256":"' + digest + '"}\n'
        ).encode("utf-8")
        expected_prefix = hashlib.sha256(line).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            path.write_bytes(line)
            with self.assertRaisesRegex(ValueError, "duplicate JSON object key: x"):
                _validated_ledger_prefixes(path, {expected_prefix})

    def test_committed_ledger_prefix_rejects_nonstandard_constant(self):
        record = {"x": float("nan")}
        digest = _canonical_record_sha(record)
        line = (
            '{"record":{"x":NaN},"sha256":"' + digest + '"}\n'
        ).encode("utf-8")
        expected_prefix = hashlib.sha256(line).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            path.write_bytes(line)
            with self.assertRaisesRegex(ValueError, "non-standard JSON constant: NaN"):
                _validated_ledger_prefixes(path, {expected_prefix})


if __name__ == "__main__":
    unittest.main()
