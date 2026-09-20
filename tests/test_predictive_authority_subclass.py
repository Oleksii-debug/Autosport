from __future__ import annotations

import importlib.util
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from autosport.opportunity import ForecastRef
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
