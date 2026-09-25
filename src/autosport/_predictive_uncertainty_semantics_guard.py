from __future__ import annotations

"""Fail closed on known-invalid ForecastRecord uncertainty semantics.

``ForecastRecord.uncertainty`` predates the durable predictive admission authority and
is used by some research producers for descriptive quantities. Positive allocation,
however, currently interprets that scalar as an absolute probability radius.

This compatibility guard closes two concrete unsafe cases without rewriting the legacy
wire schema inside the participant-strength lineage:

* the historical omitted/default ``uncertainty=0`` value must not mean mathematical
  certainty when no semantics were declared; and
* any producer that explicitly declares a non-probability uncertainty meaning (for
  example participant-strength descriptive rating radius) cannot reach positive
  predictive authority.

Legacy non-zero forecasts that predate semantic tagging remain readable/resolvable so
this bounded convergence repair does not silently invalidate older evidence. They are
not proof that the broader typed-uncertainty migration is complete; new/updated
producers should declare ``absolute_probability_radius_v1`` only when that quantity is
actually derived by their frozen scientific method.
"""

from decimal import Decimal

from .forecasting import ForecastRecord
from . import predictive_authority as _authority
from . import predictive_qualification as _qualification


_ABSOLUTE_PROBABILITY_RADIUS = "absolute_probability_radius_v1"
_ORIGINAL_AUTHORITY_RESOLVE = _authority.resolve_predictive_eligibility
_ORIGINAL_QUALIFICATION_RESOLVE = _qualification.resolve_predictive_eligibility


def _require_supported_uncertainty_semantics(forecast: object) -> None:
    # Let the already-installed exact-type/public fences own malformed or
    # polymorphic ForecastRecord diagnostics. This guard owns only semantic truth
    # for exact canonical records.
    if type(forecast) is not ForecastRecord:
        return
    semantics = forecast.provenance.get("uncertainty_semantics")
    if semantics is None:
        if forecast.uncertainty == Decimal("0"):
            raise _qualification.PredictiveQualificationError(
                "forecast default-zero uncertainty lacks probability-radius semantics"
            )
        return
    if semantics != _ABSOLUTE_PROBABILITY_RADIUS:
        raise _qualification.PredictiveQualificationError(
            "forecast uncertainty semantics are not absolute_probability_radius_v1"
        )


def _guarded_authority_resolve(
    registry,
    forecast,
    *,
    decision_time: str,
    policy,
    qualification,
):
    _require_supported_uncertainty_semantics(forecast)
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
    _require_supported_uncertainty_semantics(forecast)
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
