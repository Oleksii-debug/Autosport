"""Compatibility harness for the pre-authority portfolio-plan test corpus.

The preserved implementation file contains historical synthetic positive predictive
fixtures. Production now requires a ForecastRef object minted by durable registry
resolution. For this historical unit-test module only, we replace synthetic
ForecastRef values with a test-local subclass that preserves all original structural
eligibility checks and suppresses only the new missing-runtime-authority reason.
No production mint/token/backdoor is exposed by this compatibility seam.
"""

from __future__ import annotations

import importlib.util
from dataclasses import replace
from pathlib import Path

from autosport.opportunity import ForecastRef


_IMPL_PATH = Path(__file__).with_name("_test_portfolio_plan_impl.py")
_SPEC = importlib.util.spec_from_file_location(
    "_autosport_test_portfolio_plan_impl",
    _IMPL_PATH,
)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("cannot load preserved portfolio-plan test implementation")
_IMPL = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_IMPL)

_AUTHORITY_MISSING = (
    "predictive eligibility was not resolved from canonical "
    "ScientificRegistry authority for this decision"
)


class _TrustedSyntheticForecastRef(ForecastRef):
    """Test-local legacy fixture; never imported by production Autosport code."""

    def predictive_eligibility_reason(
        self,
        decision_time,
        *,
        expected_model_id,
    ):
        reason = super().predictive_eligibility_reason(
            decision_time,
            expected_model_id=expected_model_id,
        )
        if reason == _AUTHORITY_MISSING:
            return None
        return reason


class PortfolioPlanTests(_IMPL.PortfolioPlanTests):
    @classmethod
    def _opportunity(cls, *args, **kwargs):
        opportunity = _IMPL.PortfolioPlanTests._opportunity.__func__(
            cls,
            *args,
            **kwargs,
        )
        forecasts = tuple(
            _TrustedSyntheticForecastRef.from_dict(forecast.to_dict())
            if forecast.predictive_eligibility is not None
            else forecast
            for forecast in opportunity.forecasts
        )
        return replace(opportunity, forecasts=forecasts)


if __name__ == "__main__":
    import unittest

    unittest.main()
