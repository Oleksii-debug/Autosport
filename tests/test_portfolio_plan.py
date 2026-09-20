"""Compatibility harness for the pre-authority portfolio-plan test corpus.

The preserved implementation file contains historical synthetic positive predictive
fixtures.  Production now requires a runtime capability minted by the durable
ScientificRegistry resolver, so this subclass explicitly marks only those legacy
unit-test fixtures as already resolved.  New authority tests exercise the real
registry path and prove that ordinary caller-constructed witnesses fail closed.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from autosport import predictive_authority as _predictive_authority
from autosport.forecasting import parse_iso_timestamp


_IMPL_PATH = Path(__file__).with_name("_test_portfolio_plan_impl.py")
_SPEC = importlib.util.spec_from_file_location(
    "_autosport_test_portfolio_plan_impl",
    _IMPL_PATH,
)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("cannot load preserved portfolio-plan test implementation")
_IMPL = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_IMPL)


class PortfolioPlanTests(_IMPL.PortfolioPlanTests):
    @classmethod
    def _opportunity(cls, *args, **kwargs):
        opportunity = _IMPL.PortfolioPlanTests._opportunity.__func__(
            cls,
            *args,
            **kwargs,
        )
        decision_time = parse_iso_timestamp(cls.DECISION_TS)
        for forecast in opportunity.forecasts:
            if forecast.predictive_eligibility is None:
                continue
            key = _predictive_authority._authority_key_from_ref(
                forecast,
                decision_time,
            )
            if key is not None:
                _predictive_authority._RESOLVED_AUTHORITIES.add(key)
        return opportunity


if __name__ == "__main__":
    import unittest

    unittest.main()
