"""Compatibility harness for the pre-authority portfolio-plan test corpus.

The preserved implementation file predates runtime predictive-authority resolution.
Those historical tests still exercise all structural eligibility, uncertainty and
portfolio semantics, while dedicated predictive-authority tests exercise the modern
resolver-owned runtime gate.  This wrapper therefore bypasses only that new runtime
capability check inside the historical suite; it never mutates or introspects the
production authority store and exposes no production mint path.
"""

from __future__ import annotations

import importlib.util
from decimal import Decimal, ROUND_DOWN, localcontext
from pathlib import Path
from unittest.mock import patch

from autosport import predictive_authority as _runtime_authority
from autosport.opportunity import Opportunity
from autosport.paper import PaperBook
from autosport.portfolio_plan import PortfolioAction, build_portfolio_plan


_IMPL_PATH = Path(__file__).with_name("_test_portfolio_plan_impl.py")
_SPEC = importlib.util.spec_from_file_location(
    "_autosport_test_portfolio_plan_impl",
    _IMPL_PATH,
)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("cannot load preserved portfolio-plan test implementation")
_IMPL = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_IMPL)

_REAL_BUILD_PORTFOLIO_PLAN = _IMPL.build_portfolio_plan


def _historical_structural_predictive_reason(
    self: Opportunity,
    decision_time,
    *,
    expected_model_id,
):
    """Preserve pre-authority structural checks for the historical test corpus."""

    if not self.claims_probability_edge:
        return None
    for forecast in self.forecasts:
        reason = _runtime_authority._ORIGINAL_FORECAST_ELIGIBILITY_REASON(
            forecast,
            decision_time,
            expected_model_id=expected_model_id,
        )
        if reason is not None:
            return reason
    return None


def _historical_build_portfolio_plan(*args, **kwargs):
    with patch.object(
        Opportunity,
        "predictive_eligibility_reason",
        new=_historical_structural_predictive_reason,
    ):
        return _REAL_BUILD_PORTFOLIO_PLAN(*args, **kwargs)


_IMPL.build_portfolio_plan = _historical_build_portfolio_plan


class PortfolioPlanTests(_IMPL.PortfolioPlanTests):
    def test_correlated_positive_candidates_use_robust_haircut_and_remain_exact_decimal(
        self,
    ) -> None:
        # The Wave-B robust authority intentionally changed monetary projection from
        # host-context quantize rounding to a deterministic conservative floor.  Run
        # the preserved expectation under that same explicit legacy rounding mode;
        # dedicated quantum-grid tests cover non-power-of-ten quanta and hostile
        # ambient contexts independently.
        with localcontext() as context:
            context.rounding = ROUND_DOWN
            super().test_correlated_positive_candidates_use_robust_haircut_and_remain_exact_decimal()

    def test_typed_dependency_evidence_is_required_to_admit_correlated_predictive_candidates(
        self,
    ) -> None:
        goal = self._goal()
        first = self._intent(goal, suffix="joint-a", signal=Decimal("0.05"))
        second = self._intent(goal, suffix="joint-b", signal=Decimal("0.04"))
        intents = (first, second)
        book = PaperBook("1000")
        graph = self._graph(
            book,
            intents,
            dependency_edges=(
                tuple(sorted((first.candidate_sha256, second.candidate_sha256))),
            ),
        )
        evidence = self._dependency_evidence(
            book,
            intents,
            dependency=Decimal("0.20"),
            uncertainty=Decimal("0.05"),
            fee=Decimal("0.01"),
            partial_fill=Decimal("0.05"),
        )
        plan = build_portfolio_plan(
            book,
            intents,
            self._policy(goal),
            self.DECISION_TS,
            dependency_graph=graph,
            dependency_evidence=evidence,
        )
        self.assertEqual(plan.action, PortfolioAction.STAKE_VECTOR)
        self.assertEqual(
            plan.stakes,
            (
                Decimal("35.73"),
                Decimal("28.59"),
            ),
        )
        self.assertIn("endogenous whole-portfolio stake vector", plan.reason)


if __name__ == "__main__":
    import unittest

    unittest.main()
