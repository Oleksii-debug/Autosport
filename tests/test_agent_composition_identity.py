from __future__ import annotations

import unittest
from unittest.mock import patch

import autosport.strategies as strategies
from autosport.agents import (
    AgentContext,
    AgentOrchestrator,
    MarketMirrorAgent,
    agent_composition_sha256,
)
from autosport.paper import PaperBook
from autosport.strategies import available_strategies, build_strategy_agents


class _NamedAgent:
    name = "duplicate-agent"

    def on_market_event(self, event, context) -> None:
        del event, context


class AgentCompositionIdentityTests(unittest.TestCase):
    def test_composition_hash_is_deterministic_and_order_sensitive(self):
        names = ("market-mirror", "paper-baseline")

        first = agent_composition_sha256(names)
        second = agent_composition_sha256(list(names))
        reversed_hash = agent_composition_sha256(tuple(reversed(names)))

        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        self.assertNotEqual(first, reversed_hash)

    def test_orchestrator_rejects_duplicate_agent_identity(self):
        context = AgentContext(PaperBook("100"))

        with self.assertRaisesRegex(ValueError, "duplicate agent names"):
            AgentOrchestrator([_NamedAgent(), _NamedAgent()], context)

    def test_strategy_factory_drift_fails_closed(self):
        spec, original_factory = strategies._STRATEGIES["baseline-v1"]
        self.assertIsNotNone(original_factory)

        with patch.dict(
            strategies._STRATEGIES,
            {"baseline-v1": (spec, lambda _plan: [MarketMirrorAgent()])},
        ):
            with self.assertRaisesRegex(RuntimeError, "does not match canonical StrategySpec"):
                build_strategy_agents("baseline-v1")

    def test_strategy_spec_hash_matches_actual_ordered_runtime(self):
        for spec in available_strategies():
            self.assertEqual(
                spec.agent_composition_sha256,
                agent_composition_sha256(spec.agent_names),
            )
            if spec.requires_research_plan:
                continue
            agents = build_strategy_agents(spec.strategy_id)
            self.assertEqual(tuple(agent.name for agent in agents), spec.agent_names)
            self.assertEqual(
                agent_composition_sha256(agent.name for agent in agents),
                spec.agent_composition_sha256,
            )


if __name__ == "__main__":
    unittest.main()
