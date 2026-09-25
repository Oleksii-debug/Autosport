from __future__ import annotations

"""Fail closed when ForecastRecord uncertainty lacks probability-radius semantics.

``ForecastRecord.uncertainty`` predates the durable predictive admission authority and
is also used by research producers for descriptive quantities.  Positive allocation,
however, interprets that scalar as an absolute probability radius.  A bare number —
including the legacy default ``0`` — therefore cannot carry predictive authority.

This composition guard preserves the existing ForecastRecord wire schema while
requiring an explicit, canonical semantic declaration before the scientific resolver
may compare the value with a frozen ``maximum_uncertainty`` policy.  Research/audit
forecasts with other uncertainty meanings remain valid records but are ineligible for
positive predictive allocation.
"""

from .forecasting import ForecastRecord
from . import predictive_authority as _authority
from . import predictive_qualification as _qualification


_ABSOLUTE_PROBABILITY_RADIUS = "absolute_probability_radius_v1"
_ORIGINAL_AUTHORITY_RESOLVE = _authority.resolve_predictive_eligibility
_ORIGINAL_QUALIFICATION_RESOLVE = _qualification.resolve_predictive_eligibility


def _require_probability_radius_semantics(forecast: object) -> None:
    # Let the already-installed exact-type/public fences own malformed or
    # polymorphic ForecastRecord diagnostics.  This guard owns only semantic truth
    # for exact canonical records.
    if type(forecast) is not ForecastRecord:
        return
    semantics = forecast.provenance.get("uncertainty_semantics")
    if semantics != _ABSOLUTE_PROBABILITY_RADIUS:
        raise _qualification.PredictiveQualificationError(
            "forecast uncertainty is not declared as absolute_probability_radius_v1"
        )


def _guarded_authority_resolve(
    registry,
    forecast,
    *,
    decision_time: str,
    policy,
    qualification,
):
    _require_probability_radius_semantics(forecast)
    return _ORIGINAL_AUTHORITY_RESOLVE(
        registry,
        forecast,
        decision_time=decision_time,
        policy=policy,
        qualification=qualification,
    )


def _guarded_qualification_resolve(
    registry,
    forecast,
    *,
    decision_time: str,
    policy,
    qualification,
):
    _require_probability_radius_semantics(forecast)
    return _ORIGINAL_QUALIFICATION_RESOLVE(
        registry,
        forecast,
        decision_time=decision_time,
        policy=policy,
        qualification=qualification,
    )


_guarded_authority_resolve._autosport_uncertainty_semantics_guard = True
_guarded_qualification_resolve._autosport_uncertainty_semantics_guard = True
_authority.resolve_predictive_eligibility = _guarded_authority_resolve
_qualification.resolve_predictive_eligibility = _guarded_qualification_resolve
