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
from pathlib import Path
from unittest.mock import patch

from autosport import predictive_authority as _runtime_authority
from autosport.opportunity import Opportunity


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
    pass


if __name__ == "__main__":
    import unittest

    unittest.main()
