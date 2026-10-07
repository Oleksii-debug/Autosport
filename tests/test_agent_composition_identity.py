from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import patch

import autosport.strategies as strategies
from autosport.agents import (
    AgentContext,
    AgentOrchestrator,
    MarketMirrorAgent,
    _LatestQuotesView,
    agent_composition_sha256,
)
from autosport.domain import MarketEvent
from autosport.paper import PaperBook
from autosport.strategies import available_strategies, build_strategy_agents


class _NamedAgent:
    name = "duplicate-agent"

    def on_market_event(self, event, context) -> None:
        del event, context


class _EventRenamingAgent:
    name = "event-stable-agent"

    def on_market_event(self, event, context) -> None:
        del event, context
        self.name = "event-renamed-after-dispatch"


class _FinalizeRenamingAgent:
    name = "finalize-stable-agent"

    def on_market_event(self, event, context) -> None:
        del event, context

    def finalize_replay(self, context) -> None:
        del context
        self.name = "finalize-renamed-after-callback"


class _LaterAgent:
    name = "later-stable-agent"

    def __init__(self) -> None:
        self.event_calls = 0
        self.finalize_calls = 0

    def on_market_event(self, event, context) -> None:
        del event, context
        self.event_calls += 1

    def finalize_replay(self, context) -> None:
        del context
        self.finalize_calls += 1


class _PeerEventRenamingAgent:
    name = "peer-event-mutator"

    def __init__(self, target: _LaterAgent) -> None:
        self.target = target

    def on_market_event(self, event, context) -> None:
        del event, context
        self.target.name = "later-renamed-before-turn"


class _PeerFinalizeRenamingAgent:
    name = "peer-finalize-mutator"

    def __init__(self, target: _LaterAgent) -> None:
        self.target = target

    def on_market_event(self, event, context) -> None:
        del event, context

    def finalize_replay(self, context) -> None:
        del context
        self.target.name = "later-renamed-before-finalize"


class AgentCompositionIdentityTests(unittest.TestCase):
    def test_composition_hash_is_deterministic_and_order_sensitive(self):
        names = ("market-mirror", "paper-baseline")

        first = agent_composition_sha256(names)
        second = agent_composition_sha256(list(names))
        reversed_hash = agent_composition_sha256(tuple(reversed(names)))

        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        self.assertNotEqual(first, reversed_hash)

    def test_agent_composition_rejects_identity_subclass_before_dispatch(self):
        dispatch_calls = []

        class HostileAgentName(str):
            def strip(self, *args, **kwargs):
                dispatch_calls.append("strip")
                raise AssertionError(
                    "hostile agent identity stripped before exact-type admission"
                )

            def __hash__(self):
                dispatch_calls.append("hash")
                raise AssertionError(
                    "hostile agent identity hashed before exact-type admission"
                )

        with self.assertRaisesRegex(
            ValueError,
            "agent names must be non-empty canonical strings",
        ):
            agent_composition_sha256((HostileAgentName("market-mirror"),))

        self.assertEqual(dispatch_calls, [])

    def test_latest_quotes_rejects_identity_subclasses_before_hash_dispatch(self):
        dispatch_calls = []

        class HostileIdentity(str):
            def __hash__(self):
                dispatch_calls.append("hash")
                raise AssertionError("latest quote identity hashed before exact admission")

            def strip(self, *args, **kwargs):
                dispatch_calls.append("strip")
                raise AssertionError("latest quote identity stripped before exact admission")

        view = _LatestQuotesView(())

        with self.assertRaises(KeyError):
            _ = view[HostileIdentity("quote")]
        self.assertEqual(dispatch_calls, [])

        with self.assertRaisesRegex(ValueError, "latest_quotes source_id"):
            _ = view[(HostileIdentity("source"), "quote")]
        self.assertEqual(dispatch_calls, [])

        with self.assertRaisesRegex(ValueError, "latest_quotes quote_key"):
            _ = view[("source", HostileIdentity("quote"))]
        self.assertEqual(dispatch_calls, [])

        event = MarketEvent(
            event_id="event",
            market_id="market",
            selection_id="selection",
            decimal_odds=Decimal("2.0"),
            observed_ts="2026-10-07T00:00:00+00:00",
            source_id="source",
            sequence=1,
            ingest_ts="2026-10-07T00:00:01+00:00",
        )
        object.__setattr__(event, "source_id", HostileIdentity("source"))

        with self.assertRaisesRegex(ValueError, "latest_quotes source_id"):
            _LatestQuotesView((event,))
        self.assertEqual(dispatch_calls, [])

    def test_orchestrator_rejects_duplicate_agent_identity(self):
        context = AgentContext(PaperBook("100"))

        with self.assertRaisesRegex(ValueError, "duplicate agent names"):
            AgentOrchestrator([_NamedAgent(), _NamedAgent()], context)

    def test_orchestrator_exposes_immutable_bound_agent_tuple(self):
        context = AgentContext(PaperBook("100"))
        orchestrator = AgentOrchestrator([MarketMirrorAgent()], context)

        self.assertIsInstance(orchestrator.agents, tuple)
        self.assertEqual(orchestrator.agent_names, ("market-mirror",))
        with self.assertRaises(AttributeError):
            orchestrator.agents = ()

    def test_orchestrator_fails_closed_if_agent_identity_drifts_after_binding(self):
        context = AgentContext(PaperBook("100"))
        agent = _NamedAgent()
        orchestrator = AgentOrchestrator([agent], context)
        bound_hash = orchestrator.agent_composition_sha256

        agent.name = "renamed-after-binding"

        with self.assertRaisesRegex(RuntimeError, "changed after provenance binding"):
            _ = orchestrator.agent_names
        with self.assertRaisesRegex(RuntimeError, "changed after provenance binding"):
            _ = orchestrator.agent_composition_sha256
        self.assertEqual(len(bound_hash), 64)

    def test_orchestrator_rejects_identity_drift_during_market_callback(self):
        context = AgentContext(PaperBook("100"))
        orchestrator = AgentOrchestrator([_EventRenamingAgent()], context)

        with self.assertRaisesRegex(RuntimeError, "changed after provenance binding"):
            orchestrator.on_market_event(object())

    def test_orchestrator_rejects_identity_drift_during_finalize_callback(self):
        context = AgentContext(PaperBook("100"))
        orchestrator = AgentOrchestrator([_FinalizeRenamingAgent()], context)

        with self.assertRaisesRegex(RuntimeError, "changed after provenance binding"):
            orchestrator.finalize_replay()

    def test_peer_identity_drift_stops_before_later_market_callback(self):
        context = AgentContext(PaperBook("100"))
        later = _LaterAgent()
        orchestrator = AgentOrchestrator(
            [_PeerEventRenamingAgent(later), later],
            context,
        )

        with self.assertRaisesRegex(RuntimeError, "changed after provenance binding"):
            orchestrator.on_market_event(object())

        self.assertEqual(later.event_calls, 0)

    def test_peer_identity_drift_stops_before_later_finalize_callback(self):
        context = AgentContext(PaperBook("100"))
        later = _LaterAgent()
        orchestrator = AgentOrchestrator(
            [_PeerFinalizeRenamingAgent(later), later],
            context,
        )

        with self.assertRaisesRegex(RuntimeError, "changed after provenance binding"):
            orchestrator.finalize_replay()

        self.assertEqual(later.finalize_calls, 0)

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
