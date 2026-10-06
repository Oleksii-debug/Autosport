from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.dataset import load_dataset
from autosport.domain import TicketLeg
from autosport.evaluation import evaluate
from autosport.paper import PaperBook
from autosport.session import AutosportSession


class EvaluationEconomicTruthTests(unittest.TestCase):
    def test_open_exposure_is_explicit_in_economic_identity(self):
        book = PaperBook("100")
        leg = TicketLeg("event-1", "winner", "player-a", Decimal("2.0"))
        book.open_ticket([leg], "10", reason="paper-only open exposure")

        summary = evaluate(book)

        self.assertEqual(summary.initial_bankroll, Decimal("100"))
        self.assertEqual(summary.final_balance, Decimal("90"))
        self.assertEqual(summary.committed_stake, Decimal("10"))
        self.assertEqual(summary.settled_stake, Decimal("0"))
        self.assertEqual(summary.net_profit, Decimal("0"))
        self.assertEqual(summary.roi, Decimal("0"))
        self.assertEqual(
            summary.net_profit,
            summary.final_balance + summary.committed_stake - summary.initial_bankroll,
        )

    def test_settled_loss_has_zero_committed_stake_and_preserves_identity(self):
        book = PaperBook("100")
        leg = TicketLeg("event-1", "winner", "player-a", Decimal("2.0"))
        ticket = book.open_ticket([leg], "10", reason="paper-only settled exposure")
        book.settle(ticket.ticket_id, set())

        summary = evaluate(book)

        self.assertEqual(summary.final_balance, Decimal("90"))
        self.assertEqual(summary.committed_stake, Decimal("0"))
        self.assertEqual(summary.settled_stake, Decimal("10"))
        self.assertEqual(summary.net_profit, Decimal("-10"))
        self.assertEqual(summary.roi, Decimal("-1"))
        self.assertEqual(
            summary.net_profit,
            summary.final_balance + summary.committed_stake - summary.initial_bankroll,
        )

    def test_canonical_run_summary_serializes_committed_stake(self):
        dataset = load_dataset(Path(__file__).resolve().parents[1] / "examples" / "tt_demo")
        with tempfile.TemporaryDirectory() as temp:
            session = AutosportSession(temp, "10000")
            try:
                result = session.run_dataset(dataset)
            finally:
                session.close()

            payload = json.loads(Path(result.result_path).read_text(encoding="utf-8"))

        evaluation = payload["evaluation"]
        self.assertEqual(Decimal(evaluation["committed_stake"]), result.evaluation.committed_stake)
        self.assertEqual(
            Decimal(evaluation["net_profit"]),
            Decimal(evaluation["final_balance"])
            + Decimal(evaluation["committed_stake"])
            - Decimal(evaluation["initial_bankroll"]),
        )
        self.assertFalse(payload["real_money_execution"])


if __name__ == "__main__":
    unittest.main()
