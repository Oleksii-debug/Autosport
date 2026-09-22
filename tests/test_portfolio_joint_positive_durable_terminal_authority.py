from __future__ import annotations

import importlib.util
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from autosport.opportunity import StrategyClass
from autosport.paper import PaperBook
from autosport.portfolio_plan import PortfolioAction, VerifiedTerminalEconomics
from autosport.scenario_search import ScenarioGroup, ScenarioOutcome


def _load_portfolio_harness():
    path = Path(__file__).with_name("test_portfolio_plan.py")
    spec = importlib.util.spec_from_file_location(
        "_autosport_portfolio_plan_harness_for_durable_authority",
        path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load portfolio-plan compatibility harness")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_HARNESS = _load_portfolio_harness()


class JointPositiveDurableTerminalAuthorityTests(unittest.TestCase):
    def test_caller_terminal_economics_cannot_replace_verified_authority(self) -> None:
        harness = _HARNESS.PortfolioPlanTests
        goal = harness._goal()
        authority = harness._betfair_authority()

        base_intents = tuple(
            harness._intent(
                goal,
                suffix=f"durable-authority-{selection_id}",
                strategy_class=StrategyClass.PREDICTIVE_EDGE,
                signal=Decimal("0.03"),
                odds=Decimal("3"),
                sport="table_tennis",
                event_id="event-betfair-1",
                market_id="1.23456789",
                selection_id=selection_id,
                source_id="betfair_exchange_historical",
            )
            for selection_id in ("101", "202")
        )
        groups = (
            ScenarioGroup(
                "durable-authority-joint-state",
                tuple(
                    ScenarioOutcome(
                        quote_key=intent.risk_context.legs[0].quote_key
                    )
                    for intent in base_intents
                ),
            ),
        )
        intents = harness._bind_terminal_state(base_intents, groups)
        book = PaperBook("1000")
        edge = tuple(
            sorted(
                (
                    intents[0].candidate_sha256,
                    intents[1].candidate_sha256,
                )
            )
        )
        graph = harness._graph(book, intents, dependency_edges=(edge,))
        witness = harness._terminal_witness(book, intents, graph, groups)

        plan = _HARNESS._IMPL.build_portfolio_plan(
            book,
            intents,
            harness._policy(goal),
            harness.DECISION_TS,
            dependency_graph=graph,
            terminal_state_evidence=witness,
            market_outcome_authorities=(authority,),
        )

        self.assertEqual(plan.action, PortfolioAction.STAKE_VECTOR)
        self.assertIsNotNone(plan.terminal_economics)
        legitimate = plan.terminal_economics
        assert legitimate is not None
        self.assertTrue(legitimate.outcome_space_exhaustive)
        self.assertEqual(
            legitimate.outcome_authority_sha256s,
            (authority.authority_sha256,),
        )

        forged = VerifiedTerminalEconomics(
            completeness_evidence=legitimate.completeness_evidence,
            report_mode=legitimate.report_mode,
            total_states=legitimate.total_states,
            worst_terminal_profit=legitimate.worst_terminal_profit,
            best_terminal_profit=legitimate.best_terminal_profit,
            worst_proven=legitimate.worst_proven,
            best_proven=legitimate.best_proven,
            outcome_space_exhaustive=False,
            outcome_space_exact=False,
            outcome_authority_sha256s=(),
        )
        self.assertEqual(
            forged.completeness_evidence,
            legitimate.completeness_evidence,
        )
        self.assertEqual(forged.outcome_authority_sha256s, ())
        self.assertFalse(forged.outcome_space_exhaustive)

        with self.assertRaises(ValueError):
            replace(plan, terminal_economics=forged)


if __name__ == "__main__":
    unittest.main()
