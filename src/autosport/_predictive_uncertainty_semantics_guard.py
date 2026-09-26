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


def _install_uncertainty_semantics_guard() -> None:
    """Install one closure-sealed semantic fence over the canonical resolvers.

    The first revision exposed the semantic checker as a module-global callable.
    Rebinding that helper to a no-op could therefore disable the new truth boundary
    while leaving the public resolver object in place. Capture every authority-bearing
    dependency in this installation closure instead; no second predictive resolver or
    registry is introduced.
    """

    absolute_probability_radius = "absolute_probability_radius_v1"
    zero = Decimal("0")
    forecast_type = ForecastRecord
    error_type = _qualification.PredictiveQualificationError
    original_authority_resolve = _authority.resolve_predictive_eligibility
    original_qualification_resolve = _qualification.resolve_predictive_eligibility

    def require_supported_uncertainty_semantics(forecast: object) -> None:
        # Let the already-installed exact-type/public fences own malformed or
        # polymorphic ForecastRecord diagnostics. This guard owns only semantic truth
        # for exact canonical records.
        if type(forecast) is not forecast_type:
            return
        semantics = forecast.provenance.get("uncertainty_semantics")
        if semantics is None:
            if forecast.uncertainty == zero:
                raise error_type(
                    "forecast default-zero uncertainty lacks probability-radius semantics"
                )
            return
        if semantics != absolute_probability_radius:
            raise error_type(
                "forecast uncertainty semantics are not absolute_probability_radius_v1"
            )

    def guarded_authority_resolve(
        registry,
        forecast,
        *,
        decision_time: str,
        policy,
        qualification,
    ):
        require_supported_uncertainty_semantics(forecast)
        return original_authority_resolve(
            registry,
            forecast,
            decision_time=decision_time,
            policy=policy,
            qualification=qualification,
        )

    def guarded_qualification_resolve(
        registry,
        forecast,
        *,
        decision_time: str,
        policy,
        qualification,
    ):
        require_supported_uncertainty_semantics(forecast)
        return original_qualification_resolve(
            registry,
            forecast,
            decision_time=decision_time,
            policy=policy,
            qualification=qualification,
        )

    guarded_authority_resolve._autosport_uncertainty_semantics_guard = True
    guarded_qualification_resolve._autosport_uncertainty_semantics_guard = True
    _authority.resolve_predictive_eligibility = guarded_authority_resolve
    _qualification.resolve_predictive_eligibility = guarded_qualification_resolve


_install_uncertainty_semantics_guard()
del _install_uncertainty_semantics_guard
