from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path

from autosport.dataset import load_dataset
from autosport.research_strategy import ResearchStrategyPlan


class ResearchFutureScenarioIdentityTests(unittest.TestCase):
    @staticmethod
    def _plan_with_extra_outcome(quote_key: str) -> ResearchStrategyPlan:
        raw = json.loads(
            Path("examples/tt_demo/research_plan.json").read_text(encoding="utf-8")
        )
        outcomes = raw["decisions"][0]["scenario_groups"][0]["outcomes"]
        outcomes[1]["probability"] = "0.50"
        outcomes.append({"quote_key": quote_key, "probability": "0.10"})
        return ResearchStrategyPlan.from_dict(raw)

    def test_exact_scenario_identity_first_seen_after_decision_fails_preflight(self) -> None:
        dataset = load_dataset(Path("examples/tt_demo"))
        events = dataset.load_market_events()
        future = replace(
            events[-1],
            event_id="tt-future-1",
            selection_id="future-player",
            observed_ts="2026-09-12T10:00:02+00:00",
            sequence=999,
        )
        plan = self._plan_with_extra_outcome(future.quote_key)

        with self.assertRaisesRegex(
            ValueError,
            "research scenario outcome identity first appears after decision",
        ):
            plan.preflight([*events, future])

    def test_future_market_identity_with_unseen_selection_fails_preflight(self) -> None:
        dataset = load_dataset(Path("examples/tt_demo"))
        events = dataset.load_market_events()
        future = replace(
            events[-1],
            event_id="tt-future-market-1",
            selection_id="future-player",
            observed_ts="2026-09-12T10:00:02+00:00",
            sequence=999,
        )
        plan = self._plan_with_extra_outcome(
            f"{future.event_id}|{future.market_id}|synthetic-selection"
        )

        with self.assertRaisesRegex(
            ValueError,
            "research scenario market identity first appears after decision",
        ):
            plan.preflight([*events, future])

    def test_future_market_identity_with_delimiter_in_event_id_fails_preflight(self) -> None:
        dataset = load_dataset(Path("examples/tt_demo"))
        events = dataset.load_market_events()
        future = replace(
            events[-1],
            event_id="tt|future-market-2",
            selection_id="future-player",
            observed_ts="2026-09-12T10:00:02+00:00",
            sequence=1000,
        )
        plan = self._plan_with_extra_outcome(
            f"{future.event_id}|{future.market_id}|synthetic-selection"
        )

        with self.assertRaisesRegex(
            ValueError,
            "research scenario market identity first appears after decision",
        ):
            plan.preflight([*events, future])

    def test_future_event_identity_with_unseen_market_fails_preflight(self) -> None:
        dataset = load_dataset(Path("examples/tt_demo"))
        events = dataset.load_market_events()
        future = replace(
            events[-1],
            event_id="tt-future-event-prefix",
            market_id="future-observed-market",
            selection_id="future-player",
            observed_ts="2026-09-12T10:00:02+00:00",
            sequence=1001,
        )
        plan = self._plan_with_extra_outcome(
            f"{future.event_id}|synthetic-market|synthetic-selection"
        )

        with self.assertRaisesRegex(
            ValueError,
            "research scenario event identity first appears after decision",
        ):
            plan.preflight([*events, future])

    def test_never_observed_synthetic_placeholder_remains_allowed(self) -> None:
        dataset = load_dataset(Path("examples/tt_demo"))
        events = dataset.load_market_events()
        plan = self._plan_with_extra_outcome(
            "synthetic-complement-event|winner|synthetic-selection"
        )

        plan.preflight(events)


if __name__ == "__main__":
    unittest.main()
