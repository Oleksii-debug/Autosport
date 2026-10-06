import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.dataset import load_dataset
from autosport.research_strategy import RESEARCH_STRATEGY_ID, ResearchStrategyPlan
from autosport.session import AutosportSession


class PackagedResearchDemoTests(unittest.TestCase):
    def test_static_demo_plan_runs_against_static_demo_dataset(self):
        root = Path(__file__).resolve().parents[1] / "examples" / "tt_demo"
        dataset = load_dataset(root)
        plan = ResearchStrategyPlan.from_path(root / "research_plan.json")

        plan.preflight(dataset.load_market_events())

        with tempfile.TemporaryDirectory() as temp:
            session = AutosportSession(
                temp,
                "1000",
                strategy_id=RESEARCH_STRATEGY_ID,
                research_plan=plan,
            )
            try:
                result = session.run_dataset(dataset)
            finally:
                session.close()

        self.assertEqual(result.replay.event_count, 4)
        self.assertEqual(len(result.settled_ticket_ids), 1)
        self.assertEqual(result.balance, Decimal("990"))
        self.assertEqual(result.evaluation.net_profit, Decimal("-10"))


if __name__ == "__main__":
    unittest.main()
