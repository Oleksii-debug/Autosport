"""Compatibility harness for the pre-authority portfolio-plan test corpus.

The preserved implementation file contains historical synthetic positive predictive
fixtures. Production now requires an exact ForecastRef object whose identity was
minted by durable registry resolution. Historical portfolio tests keep using their
synthetic durable payloads, but this wrapper authorizes only exact base-class fixture
objects by reaching into the resolver closure from test code. No production mint or
transferable token API is exposed, and subclass polymorphism is not used as authority.
"""

from __future__ import annotations

import importlib.util
from dataclasses import replace
from pathlib import Path

from autosport import predictive_authority as _runtime_authority
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


def _test_only_authority_store() -> dict[int, tuple[ForecastRef, str]]:
    resolver = _runtime_authority.resolve_authoritative_forecast_ref
    closure = resolver.__closure__ or ()
    by_name = {
        name: cell.cell_contents
        for name, cell in zip(resolver.__code__.co_freevars, closure, strict=True)
    }
    store = by_name.get("authorized")
    if not isinstance(store, dict):
        raise RuntimeError("predictive resolver authority closure is unavailable")
    return store


def _authorize_historical_fixture(
    forecast: ForecastRef,
    decision_time: str,
) -> ForecastRef:
    if type(forecast) is not ForecastRef:
        raise RuntimeError("historical fixture authority requires exact ForecastRef")
    fingerprint = _runtime_authority._runtime_fingerprint(forecast, decision_time)
    if fingerprint is None:
        raise RuntimeError("historical predictive fixture lacks canonical fingerprint")
    _test_only_authority_store()[id(forecast)] = (forecast, fingerprint)
    return forecast


class PortfolioPlanTests(_IMPL.PortfolioPlanTests):
    @classmethod
    def _opportunity(cls, *args, **kwargs):
        opportunity = _IMPL.PortfolioPlanTests._opportunity.__func__(
            cls,
            *args,
            **kwargs,
        )
        forecasts = tuple(
            _authorize_historical_fixture(
                ForecastRef.from_dict(forecast.to_dict()),
                cls.DECISION_TS,
            )
            if forecast.predictive_eligibility is not None
            else forecast
            for forecast in opportunity.forecasts
        )
        return replace(opportunity, forecasts=forecasts)


if __name__ == "__main__":
    import unittest

    unittest.main()
