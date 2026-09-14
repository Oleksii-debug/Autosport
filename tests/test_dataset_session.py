import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.dataset import load_dataset
from autosport.decision_ledger import (
    DecisionLedgerIntegrityError,
    DecisionRecord,
    JsonlDecisionLedger,
)
from autosport.integrity import sha256_file
from autosport.session import AutosportSession


class DatasetSessionTests(unittest.TestCase):
    def test_dataset_hashes_and_end_to_end_session(self):
        dataset = load_dataset(Path("examples/tt_demo"))
        self.assertEqual(dataset.sport, "table_tennis")
        with tempfile.TemporaryDirectory() as tmp:
            session = AutosportSession(tmp, "10000")
            result = session.run_dataset(dataset)
            self.assertEqual(result.replay.event_count, 4)
            self.assertEqual(len(result.settled_ticket_ids), 1)
            self.assertEqual(result.balance, Decimal("10031.00"))
            self.assertEqual(result.evaluation.net_profit, Decimal("31.00"))
            summary = json.loads(Path(result.result_path).read_text(encoding="utf-8"))
            self.assertEqual(summary["schema_version"], 2)
            self.assertEqual(summary["strategy_id"], "baseline-v1")
            self.assertEqual(
                summary["strategy_runtime"]["agent_names"],
                ["market-mirror", "paper-baseline"],
            )
            self.assertTrue(summary["strategy_runtime"]["opens_paper_tickets"])
            self.assertEqual(summary["paper_book_sha256"], sha256_file(session.book_path))
            self.assertFalse(summary["real_money_execution"])
            session.close()
            restored = AutosportSession(tmp, "1")
            self.assertEqual(restored.book.balance, Decimal("10031.00"))
            restored.close()

    def test_unknown_strategy_id_fails_before_opening_runtime_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            with self.assertRaisesRegex(ValueError, "unknown strategy_id"):
                AutosportSession(workspace, strategy_id="research-v99")
            self.assertTrue(workspace.exists())
            self.assertFalse((workspace / "market.db").exists())
            self.assertFalse((workspace / "paper_book.json").exists())

    def test_observe_only_strategy_is_a_real_no_action_control(self):
        dataset = load_dataset(Path("examples/tt_demo"))
        with tempfile.TemporaryDirectory() as tmp:
            session = AutosportSession(tmp, "10000", strategy_id="observe-only-v1")
            result = session.run_dataset(dataset)
            self.assertEqual(result.replay.event_count, 4)
            self.assertEqual(result.settled_ticket_ids, ())
            self.assertEqual(result.balance, Decimal("10000"))
            self.assertEqual(result.evaluation.net_profit, Decimal("0"))
            summary = json.loads(Path(result.result_path).read_text(encoding="utf-8"))
            self.assertEqual(summary["strategy_id"], "observe-only-v1")
            self.assertEqual(summary["strategy_runtime"]["agent_names"], ["market-mirror"])
            self.assertFalse(summary["strategy_runtime"]["opens_paper_tickets"])
            session.close()

    def test_tampered_market_dataset_fails_closed(self):
        source = Path("examples/tt_demo")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            for name in ("manifest.json", "market.jsonl", "results.json"):
                (target / name).write_bytes((source / name).read_bytes())
            with (target / "market.jsonl").open("a", encoding="utf-8") as handle:
                handle.write("{}\n")
            with self.assertRaisesRegex(ValueError, "market dataset hash mismatch"):
                load_dataset(target)

    def test_corrupt_decision_ledger_fails_before_new_economic_base(self):
        dataset = load_dataset(Path("examples/tt_demo"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = JsonlDecisionLedger(root / "decisions.jsonl")
            ledger.append(
                DecisionRecord(
                    "prior-run",
                    "agent",
                    "2026-01-01T00:00:00+00:00",
                    "OBSERVE",
                    {"x": 1},
                    "ctx",
                )
            )
            envelope = json.loads(ledger.path.read_text(encoding="utf-8"))
            envelope["record"]["payload"]["x"] = 2
            ledger.path.write_text(
                json.dumps(envelope, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            session = AutosportSession(root, "10000")
            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "SHA-256 mismatch at line 1",
            ):
                session.run_dataset(dataset)
            session.close()

            self.assertFalse((root / "paper_book.json").exists())
            registry = json.loads((root / "run_registry.json").read_text(encoding="utf-8"))
            self.assertEqual(registry["runs"], {})


if __name__ == "__main__":
    unittest.main()
