import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from autosport.dataset import load_dataset
from autosport.domain import TicketStatus
from autosport.session import AutosportSession


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_research_dataset(root: Path) -> Path:
    market = root / "market.jsonl"
    results = root / "results.json"
    events = [
        {
            "event_id": "tt-research-1",
            "market_id": "winner",
            "selection_id": "alice",
            "decimal_odds": "1.80",
            "observed_ts": "2026-09-13T10:00:00+00:00",
            "source_id": "typed-fixture",
            "sequence": 1,
            "market_type": "winner",
            "metadata": {},
        },
        {
            "event_id": "tt-research-1",
            "market_id": "winner",
            "selection_id": "bob",
            "decimal_odds": "2.10",
            "observed_ts": "2026-09-13T10:00:01+00:00",
            "source_id": "typed-fixture",
            "sequence": 2,
            "market_type": "winner",
            "metadata": {
                "research_signal": {
                    "signal_id": "fixture-research-1",
                    "probability": "0.55",
                    "uncertainty": "0.10",
                    "model_id": "fixture-model",
                    "model_version": "1.0.0",
                    "model_training_cutoff_ts": "2026-09-13T09:00:00+00:00",
                    "stake": "10",
                }
            },
        },
    ]
    market.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )
    results.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "quote_outcomes": {
                    "tt-research-1|winner|alice": "loss",
                    "tt-research-1|winner|bob": "win",
                },
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 1,
        "name": "typed research strategy fixture",
        "sport": "table_tennis",
        "market_file": market.name,
        "results_file": results.name,
        "market_sha256": _sha256(market),
        "results_sha256": _sha256(results),
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return root


class ResearchDatasetStrategyTests(unittest.TestCase):
    def test_research_v1_runs_through_transactional_persistent_product_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset_root = root / "dataset"
            dataset_root.mkdir()
            workspace = root / "workspace"
            dataset = load_dataset(_write_research_dataset(dataset_root))

            session = AutosportSession(workspace, "1000", strategy_id="research-v1")
            try:
                result = session.run_dataset(dataset)
            finally:
                session.close()

            self.assertEqual(result.replay.event_count, 2)
            self.assertEqual(len(result.settled_ticket_ids), 1)
            persisted = AutosportSession(workspace, "1000", strategy_id="research-v1")
            try:
                self.assertEqual(len(persisted.book.tickets), 1)
                ticket = next(iter(persisted.book.tickets.values()))
                self.assertEqual(ticket.status, TicketStatus.WON)
                self.assertEqual(ticket.stake, 10)
            finally:
                persisted.close()

            ledger_lines = (workspace / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(ledger_lines), 1)
            envelope = json.loads(ledger_lines[0])
            record = envelope["record"]
            self.assertEqual(record["agent"], "research-decision-pipeline")
            self.assertEqual(record["action"], "OPEN_PAPER_RESEARCH_TICKET")
            self.assertFalse(record["payload"]["real_money_execution"])
            self.assertEqual(record["payload"]["forecasts"][0]["strategy_version"], "research-v1")

            summary = json.loads(Path(result.result_path).read_text(encoding="utf-8"))
            self.assertEqual(summary["strategy_id"], "research-v1")
            self.assertFalse(summary["real_money_execution"])

    def test_unknown_dataset_strategy_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "unsupported dataset strategy"):
                AutosportSession(Path(tmp) / "workspace", strategy_id="not-a-strategy")

    def test_research_v1_requires_complete_current_scenario_surface(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset_root = root / "dataset"
            dataset_root.mkdir()
            _write_research_dataset(dataset_root)
            market = dataset_root / "market.jsonl"
            lines = market.read_text(encoding="utf-8").splitlines()
            market.write_text(lines[1] + "\n", encoding="utf-8")
            manifest_path = dataset_root / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["market_sha256"] = _sha256(market)
            manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
            dataset = load_dataset(dataset_root)

            session = AutosportSession(root / "workspace", "1000", strategy_id="research-v1")
            try:
                with self.assertRaisesRegex(ValueError, "at least two current outcomes"):
                    session.run_dataset(dataset)
                self.assertEqual(len(session.book.tickets), 0)
            finally:
                session.close()

            self.assertFalse((root / "workspace" / "decisions.jsonl").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
