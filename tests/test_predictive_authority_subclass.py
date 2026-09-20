from __future__ import annotations

import importlib.util
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from autosport.opportunity import ForecastRef, Opportunity
from autosport.paper import PaperBook
from autosport.portfolio_plan import PortfolioAction, build_portfolio_plan


_IMPL_PATH = Path(__file__).with_name("_test_portfolio_plan_impl.py")
_SPEC = importlib.util.spec_from_file_location(
    "_autosport_predictive_subclass_portfolio_helpers",
    _IMPL_PATH,
)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("cannot load portfolio-plan test helpers")
_IMPL = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_IMPL)


class _ForgedForecastRef(ForecastRef):
    """Caller-defined override that used to bypass runtime authority."""

    def predictive_eligibility_reason(
        self,
        decision_time,
        *,
        expected_model_id,
    ):
        return None


class _ForgedOpportunity(Opportunity):
    """Caller-owned polymorphism must not control stake admission or haircut."""

    def predictive_eligibility_reason(
        self,
        decision_time,
        *,
        expected_model_id,
    ):
        return None

    @property
    def predictive_uncertainty_haircut(self) -> Decimal:
        return Decimal("0")


def test_forecast_ref_subclass_cannot_mint_predictive_portfolio_authority() -> None:
    helpers = _IMPL.PortfolioPlanTests
    goal = helpers._goal()
    intent = helpers._intent(goal, suffix="subclass-bypass", signal=Decimal("0.05"))
    original = intent.opportunity.forecasts[0]
    forged = _ForgedForecastRef.from_dict(original.to_dict())
    intent = replace(
        intent,
        opportunity=replace(intent.opportunity, forecasts=(forged,)),
    )
    book = PaperBook("1000")

    plan = build_portfolio_plan(
        book,
        (intent,),
        helpers._policy(goal),
        helpers.DECISION_TS,
        dependency_graph=helpers._graph(book, (intent,)),
    )

    assert plan.action is PortfolioAction.WAIT
    assert plan.stakes == (Decimal("0"),)
    assert "not resolved from canonical ScientificRegistry authority" in plan.reason


def test_opportunity_subclass_cannot_override_predictive_authority_or_haircut() -> None:
    helpers = _IMPL.PortfolioPlanTests
    goal = helpers._goal()
    intent = helpers._intent(
        goal,
        suffix="opportunity-subclass-bypass",
        signal=Decimal("0.05"),
    )
    original = intent.opportunity
    forged = _ForgedOpportunity(
        strategy_class=original.strategy_class,
        decision=original.decision,
        quotes=original.quotes,
        claims_probability_edge=original.claims_probability_edge,
        forecasts=original.forecasts,
        evidence_refs=original.evidence_refs,
    )
    intent = replace(intent, opportunity=forged)
    book = PaperBook("1000")

    plan = build_portfolio_plan(
        book,
        (intent,),
        helpers._policy(goal),
        helpers.DECISION_TS,
        dependency_graph=helpers._graph(book, (intent,)),
    )

    assert plan.action is PortfolioAction.WAIT
    assert plan.stakes == (Decimal("0"),)
    assert "exact canonical Opportunity authority" in plan.reason
