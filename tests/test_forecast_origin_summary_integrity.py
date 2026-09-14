from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from autosport.forecast_origin import _load_summary


class ForecastOriginSummaryIntegrityTests(unittest.TestCase):
    def _dataset(self):
        return SimpleNamespace(
            import_identity={"source": "licensed-fixture", "version": 1},
            market_sha256="a" * 64,
            results_sha256="b" * 64,
        )

    def _summary(self) -> dict:
        dataset = self._dataset()
        return {
            "schema_version": 2,
            "transaction_schema_version": 1,
            "transaction_run_id": "run-1",
            "run_id": "run-1",
            "dataset_schema_version": 2,
            "historical_import_identity": dataset.import_identity,
            "market_sha256": dataset.market_sha256,
            "sealed_results_sha256": dataset.results_sha256,
            "decision_ledger_sha256": "c" * 64,
            "strategy_runtime": {"canonical_strategy_id": "research-replay-v1"},
            "real_money_execution": False,
        }

    def _load_payload(self, payload: bytes) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run-summary.json"
            path.write_bytes(payload)
            return _load_summary(path, self._dataset())

    def test_canonical_summary_remains_accepted(self) -> None:
        raw = self._summary()

        loaded = self._load_payload(json.dumps(raw, sort_keys=True).encode("utf-8"))

        self.assertEqual(loaded["run_id"], "run-1")
        self.assertEqual(loaded["decision_ledger_sha256"], "c" * 64)

    def test_duplicate_run_id_is_rejected_before_authority_selection(self) -> None:
        raw = self._summary()
        body = json.dumps(raw, sort_keys=True)
        marker = '"run_id": "run-1"'
        self.assertIn(marker, body)
        body = body.replace(marker, marker + ', "run_id": "run-forged"', 1)

        with self.assertRaisesRegex(ValueError, "invalid canonical UTF-8 JSON"):
            self._load_payload(body.encode("utf-8"))

    def test_duplicate_decision_ledger_sha_is_rejected_before_prefix_selection(self) -> None:
        raw = self._summary()
        body = json.dumps(raw, sort_keys=True)
        marker = f'"decision_ledger_sha256": "{"c" * 64}"'
        self.assertIn(marker, body)
        body = body.replace(
            marker,
            marker + f', "decision_ledger_sha256": "{"d" * 64}"',
            1,
        )

        with self.assertRaisesRegex(ValueError, "invalid canonical UTF-8 JSON"):
            self._load_payload(body.encode("utf-8"))

    def test_schema_versions_do_not_accept_boolean_or_float_coercion(self) -> None:
        cases = (
            ("schema_version", 2.0, "schema_version 2"),
            ("transaction_schema_version", True, "transaction precommit evidence"),
            ("dataset_schema_version", 2.0, "governed dataset schema v2"),
        )
        for field, value, message in cases:
            with self.subTest(field=field, value=value):
                raw = self._summary()
                raw[field] = value
                payload = json.dumps(raw, sort_keys=True).encode("utf-8")
                with self.assertRaisesRegex(ValueError, message):
                    self._load_payload(payload)

    def test_run_id_must_already_be_canonical_without_trimming(self) -> None:
        raw = self._summary()
        raw["run_id"] = " run-1 "
        raw["transaction_run_id"] = " run-1 "

        with self.assertRaisesRegex(ValueError, "canonical non-empty text"):
            self._load_payload(json.dumps(raw, sort_keys=True).encode("utf-8"))

    def test_decision_ledger_sha_must_already_be_lowercase(self) -> None:
        raw = self._summary()
        raw["decision_ledger_sha256"] = "C" * 64

        with self.assertRaisesRegex(ValueError, "canonical lowercase SHA-256"):
            self._load_payload(json.dumps(raw, sort_keys=True).encode("utf-8"))

    def test_non_standard_json_constant_is_rejected_even_in_unused_field(self) -> None:
        raw = self._summary()
        raw["unused_numeric_evidence"] = float("nan")
        payload = json.dumps(raw, sort_keys=True).encode("utf-8")
        self.assertIn(b"NaN", payload)

        with self.assertRaisesRegex(ValueError, "invalid canonical UTF-8 JSON"):
            self._load_payload(payload)

    def test_non_utf8_summary_bytes_are_rejected(self) -> None:
        payload = json.dumps(self._summary(), sort_keys=True).encode("utf-8") + b"\xff"

        with self.assertRaisesRegex(ValueError, "invalid canonical UTF-8 JSON"):
            self._load_payload(payload)


if __name__ == "__main__":
    unittest.main()
