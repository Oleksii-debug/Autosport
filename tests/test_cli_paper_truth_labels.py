from __future__ import annotations

import io
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from contextlib import redirect_stdout

from autosport.cli import run_dataset, run_demo, run_replay


class CliPaperTruthLabelTests(unittest.TestCase):
    def test_demo_labels_sample_fixture_and_preserves_metrics(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(run_demo(), 0)

        text = output.getvalue()
        self.assertIn(
            "mode=demo paper_only=true real_money_execution=false "
            "profitability_claim=false sample_fixture=true",
            text,
        )
        self.assertIn("run=demo-run", text)
        self.assertIn("balance=", text)
        self.assertIn("tickets=", text)

    def test_replay_labels_paper_execution_without_hiding_existing_metrics(self) -> None:
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

        text = output.getvalue()
        self.assertIn(
            "mode=replay paper_only=true real_money_execution=false "
            "profitability_claim=false sample_fixture=false",
            text,
        )
        self.assertIn("dataset_hash=dataset-hash", text)
        self.assertIn("virtual_balance=10000", text)
        self.assertIn("scenario_mode=empty", text)

    def test_dataset_labels_paper_execution_without_hiding_net_profit(self) -> None:
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

        text = output.getvalue()
        self.assertIn(
            "mode=dataset paper_only=true real_money_execution=false "
            "profitability_claim=false sample_fixture=false",
            text,
        )
        self.assertIn("balance=10025", text)
        self.assertIn("net_profit=25", text)
        self.assertIn("settled=1", text)
        session.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
