import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.dataset import load_dataset
from autosport.domain import MarketEvent, MarketType
from autosport.research_strategy import RESEARCH_STRATEGY_ID, ResearchStrategyPlan
from autosport.run_registry import RunRegistry
from autosport.session import AutosportSession


class ResearchScenarioReplayBindingTests(unittest.TestCase):
    @staticmethod
    def _plan_dict() -> dict:
        return json.loads(
            Path("examples/tt_demo/research_plan.json").read_text(encoding="utf-8")
        )

    def test_fabricated_scenario_outcome_fails_before_economic_mutation(self) -> None:
        raw = self._plan_dict()
        outcomes = raw["decisions"][0]["scenario_groups"][0]["outcomes"]
        outcomes[0]["probability"] = "0.20"
        outcomes.append(
            {
                "quote_key": "tt-demo-1|winner|fabricated",
                "probability": "0.20",
            }
        )
        plan = ResearchStrategyPlan.from_dict(raw)
        dataset = load_dataset(Path("examples/tt_demo"))

        with tempfile.TemporaryDirectory() as tmp:
            session = AutosportSession(
                tmp,
                "1000",
                strategy_id=RESEARCH_STRATEGY_ID,
                research_plan=plan,
            )
            try:
                with self.assertRaisesRegex(
                    ValueError,
                    "research scenario outcome absent from replay state",
                ):
                    session.run_dataset(dataset)
                self.assertEqual(
                    RunRegistry(Path(tmp) / "run_registry.json").in_progress(),
                    (),
                )
                self.assertFalse((Path(tmp) / "paper_book.json").exists())
                self.assertFalse((Path(tmp) / "decisions.jsonl").exists())
            finally:
                session.close()

    def test_incomplete_scenario_group_cannot_claim_complete_replay_market(self) -> None:
        plan = ResearchStrategyPlan.from_dict(self._plan_dict())
        dataset = load_dataset(Path("examples/tt_demo"))
        events = dataset.load_market_events()
        events.append(
            MarketEvent(
                event_id="tt-demo-1",
                market_id="winner",
                selection_id="player-c",
                decimal_odds=Decimal("5.00"),
                observed_ts="2026-09-12T10:00:00+00:00",
                source_id="fixture",
                sequence=99,
                market_type=MarketType.WINNER,
                status="open",
            )
        )

        with self.assertRaisesRegex(
            ValueError,
            "research scenario group is not complete for replay market",
        ):
            plan.preflight(events)


if __name__ == "__main__":
    unittest.main()
