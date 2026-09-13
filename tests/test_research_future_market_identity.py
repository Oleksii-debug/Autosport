from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path

from autosport.dataset import load_dataset
from autosport.research_strategy import ResearchStrategyPlan


class ResearchFutureMarketIdentityTests(unittest.TestCase):
    @staticmethod
    def _plan_with_extra_outcome(quote_key: str) -> ResearchStrategyPlan:
        raw = json.loads(
            Path("examples/tt_demo/research_plan.json").read_text(encoding="utf-8")
        )
        outcomes = raw["decisions"][0]["scenario_groups"][0]["outcomes"]
        outcomes[1]["probability"] = "0.50"
        outcomes.append({"quote_key": quote_key, "probability": "0.10"})
        return ResearchStrategyPlan.from_dict(raw)

    def test_synthetic_selection_under_future_replay_market_fails_preflight(self) -> None:
        dataset = load_dataset(Path("examples/tt_demo"))
        events = dataset.load_market_events()
        future = replace(
            events[-1],
            event_id="tt-future-market-1",
            market_id="winner",
            selection_id="future-observed-selection",
            observed_ts="2026-09-12T10:00:02+00:00",
            sequence=999,
        )
        plan = self._plan_with_extra_outcome(
            "tt-future-market-1|winner|synthetic-selection"
        )

        with self.assertRaisesRegex(
            ValueError,
            "research scenario market identity first appears after decision",
        ):
            plan.preflight([*events, future])

    def test_fully_unobserved_abstract_market_namespace_remains_allowed(self) -> None:
        dataset = load_dataset(Path("examples/tt_demo"))
        events = dataset.load_market_events()
        plan = self._plan_with_extra_outcome(
            "synthetic-complement-event|synthetic-market|synthetic-selection"
        )

        plan.preflight(events)


if __name__ == "__main__":
    unittest.main()
