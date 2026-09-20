from __future__ import annotations

"""Supported-API type fence for positive predictive allocation authority.

The predictive resolver consumes values through public Python methods/properties.  A
subclass can therefore present forged registry records or a committed digest while
changing the runtime fields consumed by the resolver.  Positive authority is stricter
than ordinary structural compatibility: the outer mint boundary accepts only the
canonical concrete authority/value types.  Internal resolver adapters remain behind
this fence and may continue to use implementation-only subclasses.
"""

from typing import Any

from . import predictive_authority as _authority
from . import predictive_qualification as _qualification
from .forecasting import ForecastRecord
from .opportunity import QuoteRef
from .scientific_registry import ScientificRegistry


_ORIGINAL_RESOLVE_PREDICTIVE_ELIGIBILITY = _authority.resolve_predictive_eligibility
_ORIGINAL_RESOLVE_AUTHORITATIVE_FORECAST_REF = (
    _authority.resolve_authoritative_forecast_ref
)


def _require_exact_type(value: object, expected: type[Any], label: str) -> None:
    if type(value) is not expected:
        raise _qualification.PredictiveQualificationError(
            f"{label} must be exact canonical {expected.__name__}"
        )


def resolve_predictive_eligibility(
    registry: ScientificRegistry,
    forecast: ForecastRecord,
    *,
    decision_time: str,
    policy: _qualification.PredictiveAdmissionPolicy,
    qualification: _qualification.ForecastCalibrationQualification,
) -> _qualification.ResolvedPredictiveEligibility:
    """Reject caller-polymorphic authority before any public resolver dispatch."""

    _require_exact_type(registry, ScientificRegistry, "registry")
    _require_exact_type(forecast, ForecastRecord, "forecast")
    _require_exact_type(policy, _qualification.PredictiveAdmissionPolicy, "policy")
    _require_exact_type(
        qualification,
        _qualification.ForecastCalibrationQualification,
        "qualification",
    )
    return _ORIGINAL_RESOLVE_PREDICTIVE_ELIGIBILITY(
        registry,
        forecast,
        decision_time=decision_time,
        policy=policy,
        qualification=qualification,
    )


def resolve_authoritative_forecast_ref(
    registry: ScientificRegistry,
    forecast: ForecastRecord,
    quote: QuoteRef,
    *,
    decision_time: str,
    policy: _qualification.PredictiveAdmissionPolicy,
    qualification: _qualification.ForecastCalibrationQualification,
):
    """Fence every caller-owned value before the sole positive ForecastRef mint."""

    _require_exact_type(registry, ScientificRegistry, "registry")
    _require_exact_type(forecast, ForecastRecord, "forecast")
    _require_exact_type(quote, QuoteRef, "quote")
    _require_exact_type(policy, _qualification.PredictiveAdmissionPolicy, "policy")
    _require_exact_type(
        qualification,
        _qualification.ForecastCalibrationQualification,
        "qualification",
    )
    return _ORIGINAL_RESOLVE_AUTHORITATIVE_FORECAST_REF(
        registry,
        forecast,
        quote,
        decision_time=decision_time,
        policy=policy,
        qualification=qualification,
    )


# Package import installs this fence after predictive_authority has installed its
# durable resolver/runtime bridge.  Its internal _PromotionBundleDigestRegistry is
# consequently created only after the exact external ScientificRegistry check above.
_authority.resolve_predictive_eligibility = resolve_predictive_eligibility
_authority.resolve_authoritative_forecast_ref = resolve_authoritative_forecast_ref
_qualification.resolve_predictive_eligibility = resolve_predictive_eligibility
