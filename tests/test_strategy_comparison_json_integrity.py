from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.run_transaction import RunTransaction
from autosport.strategy_comparison import load_strategy_run_summary, main


class StrategyComparisonJsonIntegrityTests(unittest.TestCase):
    @staticmethod
    def _valid_summary() -> dict[str, object]:
        return {
            "schema_version": 2,
            "transaction_schema_version": RunTransaction.SCHEMA_VERSION,
            "transaction_run_id": "run-1",
            "run_id": "run-1",
            "paper_book_sha256": "5" * 64,
            "decision_ledger_sha256": "6" * 64,
            "dataset_name": "licensed-table-tennis-slice",
            "sport": "table_tennis",
            "dataset_schema_version": 2,
            "historical_import_identity": "3" * 64,
            "market_sha256": "1" * 64,
            "sealed_results_sha256": "2" * 64,
            "replay_dataset_hash": "4" * 64,
            "event_count": 100,
            "strategy_id": "baseline-v1",
            "strategy_runtime": {
                "strategy_id": "baseline-v1",
                "canonical_strategy_id": "baseline-v1",
                "label": "baseline-v1",
                "agent_names": ["market-mirror", "paper-baseline"],
                "opens_paper_tickets": True,
                "research_plan_sha256": None,
            },
            "balance": "10000",
            "evaluation": {
                "initial_bankroll": "10000",
                "final_balance": "10000",
                "committed_stake": "0",
                "settled_stake": "500",
                "net_profit": "0",
                "roi": "0",
                "won": 3,
                "lost": 2,
                "void": 0,
            },
            "real_money_execution": False,
        }

    @staticmethod
    def _write_raw(root: Path, name: str, raw: str) -> Path:
        path = root / name
        path.write_text(raw, encoding="utf-8")
        return path

    def test_loader_rejects_duplicate_nested_agent_names_even_when_last_value_is_canonical(self) -> None:
        payload = json.dumps(self._valid_summary(), separators=(",", ":"))
        needle = '"agent_names":["market-mirror","paper-baseline"]'
        raw = payload.replace(
            needle,
            '"agent_names":["wrong-agent"],' + needle,
            1,
        )
        self.assertNotEqual(raw, payload)

        with tempfile.TemporaryDirectory() as temp:
            path = self._write_raw(Path(temp), "duplicate-agent-names.json", raw)
            with self.assertRaisesRegex(ValueError, "duplicate JSON object key: agent_names"):
                load_strategy_run_summary(path)

    def test_loader_rejects_duplicate_canonical_strategy_id_even_when_last_value_is_valid(self) -> None:
        payload = json.dumps(self._valid_summary(), separators=(",", ":"))
        needle = '"canonical_strategy_id":"baseline-v1"'
        raw = payload.replace(
            needle,
            '"canonical_strategy_id":"observe-only-v1",' + needle,
            1,
        )
        self.assertNotEqual(raw, payload)

        with tempfile.TemporaryDirectory() as temp:
            path = self._write_raw(Path(temp), "duplicate-canonical-id.json", raw)
            with self.assertRaisesRegex(ValueError, "duplicate JSON object key: canonical_strategy_id"):
                load_strategy_run_summary(path)

    def test_loader_rejects_duplicate_top_level_strategy_runtime(self) -> None:
        payload = json.dumps(self._valid_summary(), separators=(",", ":"))
        needle = '"strategy_runtime":'
        raw = payload.replace(
            needle,
            '"strategy_runtime":{"agent_names":["wrong-agent"]},' + needle,
            1,
        )
        self.assertNotEqual(raw, payload)

        with tempfile.TemporaryDirectory() as temp:
            path = self._write_raw(Path(temp), "duplicate-runtime.json", raw)
            with self.assertRaisesRegex(ValueError, "duplicate JSON object key: strategy_runtime"):
                load_strategy_run_summary(path)

    def test_loader_rejects_nonstandard_json_constant_before_provenance_validation(self) -> None:
        payload = json.dumps(self._valid_summary(), separators=(",", ":"))
        raw = payload[:-1] + ',"ambiguous_numeric_evidence":NaN}'

        with tempfile.TemporaryDirectory() as temp:
            path = self._write_raw(Path(temp), "nan-summary.json", raw)
            with self.assertRaisesRegex(ValueError, "non-standard JSON constant: NaN"):
                load_strategy_run_summary(path)

    def test_cli_normalizes_json_recursion_exhaustion_to_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = self._write_raw(root, "deep-summary.json", "{}")
            output = root / "comparison.json"
            with patch(
                "autosport.strategy_comparison.json.loads",
                side_effect=RecursionError("maximum recursion depth exceeded while decoding JSON"),
            ):
                rc = main([str(path), "--output", str(output)])

            self.assertEqual(rc, 2)
            self.assertFalse(output.exists())

    def test_loader_requires_exact_integer_run_summary_schema_version(self) -> None:
        for invalid in (2.0, "2", True):
            with self.subTest(invalid=invalid):
                payload = self._valid_summary()
                payload["schema_version"] = invalid
                with tempfile.TemporaryDirectory() as temp:
                    path = self._write_raw(
                        Path(temp),
                        "invalid-run-summary-schema.json",
                        json.dumps(payload, separators=(",", ":")),
                    )
                    with self.assertRaisesRegex(ValueError, "run summary schema_version must be 2"):
                        load_strategy_run_summary(path)

    def test_loader_requires_exact_integer_transaction_schema_version(self) -> None:
        for invalid in (float(RunTransaction.SCHEMA_VERSION), True, str(RunTransaction.SCHEMA_VERSION)):
            with self.subTest(invalid=invalid):
                payload = self._valid_summary()
                payload["transaction_schema_version"] = invalid
                with tempfile.TemporaryDirectory() as temp:
                    path = self._write_raw(
                        Path(temp),
                        "invalid-transaction-schema.json",
                        json.dumps(payload, separators=(",", ":")),
                    )
                    with self.assertRaisesRegex(ValueError, "canonical transaction schema"):
                        load_strategy_run_summary(path)


if __name__ == "__main__":
    unittest.main()