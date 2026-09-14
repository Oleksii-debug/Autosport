from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autosport.cli import run_dataset, run_demo, run_replay


class CliPaperTruthLabelTests(unittest.TestCase):
    def test_demo_appends_sample_fixture_truth_after_legacy_output(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(run_demo(), 0)

        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("run=demo-run "))
        self.assertIn("balance=", lines[0])
        self.assertIn("tickets=", lines[0])
        self.assertEqual(
            lines[1],
            "mode=demo paper_only=true real_money_execution=false "
            "profitability_claim=false sample_fixture=true",
        )

    def test_replay_appends_truth_without_changing_legacy_output_prefix(self) -> None:
        replay_run = SimpleNamespace(run_id="run-1", dataset_hash="dataset-hash", event_count=0)
        scenario = SimpleNamespace(mode="empty", worst_case=Decimal("0"), best_case=Decimal("0"))
        with (
            patch("autosport.cli.ReplayEngine.from_jsonl") as from_jsonl,
            patch("autosport.cli.PortfolioEngine") as portfolio_engine,
        ):
            from_jsonl.return_value.run.return_value = replay_run
            portfolio_engine.return_value.analyse.return_value = scenario
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(run_replay(Path("ignored.jsonl"), "10000"), 0)

        lines = output.getvalue().splitlines()
        self.assertEqual(
            lines[:5],
            [
                "run_id=run-1",
                "dataset_hash=dataset-hash",
                "events=0",
                "virtual_balance=10000",
                "scenario_mode=empty worst=0 best=0",
            ],
        )
        self.assertEqual(
            lines[5],
            "mode=replay paper_only=true real_money_execution=false profitability_claim=false",
        )
        self.assertNotIn("sample_fixture=", lines[5])

    def test_dataset_appends_truth_without_changing_legacy_output_prefix(self) -> None:
        dataset = SimpleNamespace(import_identity=None)
        result = SimpleNamespace(
            replay=SimpleNamespace(run_id="run-2", event_count=3),
            balance=Decimal("10025"),
            evaluation=SimpleNamespace(net_profit=Decimal("25")),
            settled_ticket_ids=("ticket-1",),
        )
        with (
            patch("autosport.cli.load_dataset", return_value=dataset),
            patch("autosport.cli.AutosportSession") as session_type,
        ):
            session = session_type.return_value
            session.run_dataset.return_value = result
            session.strategy_id = "baseline-v1"
            session.strategy = SimpleNamespace(strategy_id="baseline-v1")
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    run_dataset(Path("dataset"), Path("workspace"), "10000"),
                    0,
                )

        lines = output.getvalue().splitlines()
        self.assertEqual(
            lines[:7],
            [
                "run_id=run-2",
                "strategy_id=baseline-v1",
                "canonical_strategy_id=baseline-v1",
                "events=3",
                "balance=10025",
                "net_profit=25",
                "settled=1",
            ],
        )
        self.assertEqual(
            lines[7],
            "mode=dataset paper_only=true real_money_execution=false profitability_claim=false",
        )
        self.assertNotIn("sample_fixture=", lines[7])
        session.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
