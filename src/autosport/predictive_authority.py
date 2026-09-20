from __future__ import annotations

"""Canonical runtime bridge from durable predictive science to stake admission.

The scientific resolver owns eligibility truth.  This module binds that resolved
truth to the exact forecast and exact portfolio decision instant, then installs a
fail-closed guard on ``ForecastRef``.  Serialized/caller-constructed eligibility
objects remain audit data only: after restart they must be re-resolved from the
``ScientificRegistry`` before they can authorize a positive predictive action.
"""

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from .forecasting import ForecastRecord, parse_iso_timestamp
from .opportunity import ForecastRef, PredictiveEligibilityEvidence, QuoteRef
from .scientific_registry import ScientificRegistry
from . import predictive_qualification as _qualification


_ORIGINAL_FORECAST_ELIGIBILITY_REASON = ForecastRef.predictive_eligibility_reason
_ORIGINAL_RESOLVE_PREDICTIVE_ELIGIBILITY = (
    _qualification.resolve_predictive_eligibility
)
_RESOLVED_AUTHORITIES: set[str] = set()


def _utc_instant(value: object) -> str:
    if isinstance(value, str):
        parsed = parse_iso_timestamp(value)
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise TypeError("predictive authority time must be ISO text or datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("predictive authority time must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _authority_digest(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _authority_payload(
    *,
    forecast_hash: str,
    quote_key: str,
    probability: object,
    input_cutoff_ts: str,
    market_snapshot_hash: str | None,
    model_id: str | None,
    model_version: str | None,
    strategy_version: str | None,
    uncertainty: object,
    evidence: PredictiveEligibilityEvidence,
    decision_time: object,
) -> dict[str, object]:
    return {
        "schema": "autosport.resolved_predictive_runtime_authority",
        "schema_version": 1,
        "forecast_hash": forecast_hash,
        "quote_key": quote_key,
        "probability": str(probability),
        "input_cutoff_ts": _utc_instant(input_cutoff_ts),
        "market_snapshot_hash": market_snapshot_hash,
        "model_id": model_id,
        "model_version": model_version,
        "strategy_version": strategy_version,
        "uncertainty": None if uncertainty is None else str(uncertainty),
        "eligibility": evidence.to_dict(),
        "decision_time": _utc_instant(decision_time),
    }


def _authority_key_from_ref(
    forecast: ForecastRef,
    decision_time: object,
) -> str | None:
    evidence = forecast.predictive_eligibility
    if evidence is None:
        return None
    return _authority_digest(
        _authority_payload(
            forecast_hash=forecast.forecast_hash,
            quote_key=forecast.quote_key,
            probability=forecast.probability,
            input_cutoff_ts=forecast.input_cutoff_ts,
            market_snapshot_hash=forecast.market_snapshot_hash,
            model_id=forecast.model_id,
            model_version=forecast.model_version,
            strategy_version=forecast.strategy_version,
            uncertainty=forecast.uncertainty,
            evidence=evidence,
            decision_time=decision_time,
        )
    )


def _authority_key_from_record(
    forecast: ForecastRecord,
    evidence: PredictiveEligibilityEvidence,
    decision_time: object,
) -> str:
    return _authority_digest(
        _authority_payload(
            forecast_hash=forecast.canonical_hash,
            quote_key=forecast.quote_key,
            probability=forecast.probability,
            input_cutoff_ts=forecast.input_cutoff_ts,
            market_snapshot_hash=forecast.market_snapshot_hash,
            model_id=forecast.model_id,
            model_version=forecast.model_version,
            strategy_version=forecast.strategy_version,
            uncertainty=forecast.uncertainty,
            evidence=evidence,
            decision_time=decision_time,
        )
    )


class _PromotionBundleDigestRegistry(ScientificRegistry):
    """Read-only resolver view matching PromotionDecision's bundle-hash contract.

    ``ScientificRegistry.record_promotion`` canonically stores the EvaluationBundle's
    declared ``bundle_sha256`` in PromotionDecision.  The pre-existing #597 resolver
    compared that value with the registry-envelope digest instead, making every real
    promoted lineage fail its positive path.  This view fixes only that comparison;
    the returned resolver result is rebound to the real immutable registry-record
    digest before it leaves this module.
    """

    def __init__(self, delegate: ScientificRegistry) -> None:
        self._delegate = delegate

    def get(self, record_type: str, record_id: str):  # type: ignore[override]
        entry = self._delegate.get(record_type, record_id)
        if entry is None or record_type != "EvaluationBundle":
            return entry
        declared = entry.payload.get("bundle_sha256")
        if not isinstance(declared, str):
            return entry
        return replace(entry, record_sha256=declared)

    def champion_strategy(self, *args: Any, **kwargs: Any):  # type: ignore[override]
        return self._delegate.champion_strategy(*args, **kwargs)

    def causal_records(self, *args: Any, **kwargs: Any):  # type: ignore[override]
        return self._delegate.causal_records(*args, **kwargs)


def resolve_predictive_eligibility(
    registry: ScientificRegistry,
    forecast: ForecastRecord,
    *,
    decision_time: str,
    policy: _qualification.PredictiveAdmissionPolicy,
    qualification: _qualification.ForecastCalibrationQualification,
) -> _qualification.ResolvedPredictiveEligibility:
    """Resolve eligibility from immutable registry truth with exact bundle identity."""

    if not isinstance(registry, ScientificRegistry):
        raise _qualification.PredictiveQualificationError(
            "registry must be ScientificRegistry"
        )
    resolved = _ORIGINAL_RESOLVE_PREDICTIVE_ELIGIBILITY(
        _PromotionBundleDigestRegistry(registry),
        forecast,
        decision_time=decision_time,
        policy=policy,
        qualification=qualification,
    )
    bundle = registry.get("EvaluationBundle", resolved.evaluation_bundle_id)
    if bundle is None:
        raise _qualification.PredictiveQualificationError(
            "promotion EvaluationBundle disappeared during resolution"
        )
    return replace(resolved, evaluation_bundle_sha256=bundle.record_sha256)


def resolve_forecast_predictive_authority(
    registry: ScientificRegistry,
    forecast: ForecastRecord,
    *,
    decision_time: str,
    policy: _qualification.PredictiveAdmissionPolicy,
    qualification: _qualification.ForecastCalibrationQualification,
) -> PredictiveEligibilityEvidence:
    """Mint one non-durable admission capability from durable scientific truth.

    The capability is valid only for the exact decision instant used for resolution.
    Its serialized fields are audit evidence, not a transferable authority token.
    """

    resolved = resolve_predictive_eligibility(
        registry,
        forecast,
        decision_time=decision_time,
        policy=policy,
        qualification=qualification,
    )
    evidence = PredictiveEligibilityEvidence(
        evaluation_id=resolved.evaluation_bundle_id,
        evaluation_sha256=resolved.evaluation_bundle_sha256,
        protocol_sha256=resolved.protocol_sha256,
        admission_policy_sha256=resolved.policy_sha256,
        model_id=forecast.model_id,
        model_version=forecast.model_version,
        strategy_version=forecast.strategy_version,
        uncertainty_kind="absolute_probability_radius_v1",
        sample_size=resolved.effective_sample_size,
        minimum_sample_size=resolved.minimum_effective_sample_size,
        maximum_uncertainty=resolved.maximum_uncertainty,
        as_of=resolved.resolved_at,
        valid_until=resolved.resolved_at,
    )
    _RESOLVED_AUTHORITIES.add(
        _authority_key_from_record(forecast, evidence, decision_time)
    )
    return evidence


def resolve_authoritative_forecast_ref(
    registry: ScientificRegistry,
    forecast: ForecastRecord,
    quote: QuoteRef,
    *,
    decision_time: str,
    policy: _qualification.PredictiveAdmissionPolicy,
    qualification: _qualification.ForecastCalibrationQualification,
) -> ForecastRef:
    """Resolve durable eligibility and bind it to the exact quote/forecast snapshot."""

    evidence = resolve_forecast_predictive_authority(
        registry,
        forecast,
        decision_time=decision_time,
        policy=policy,
        qualification=qualification,
    )
    return ForecastRef.from_forecast(
        forecast,
        quote,
        predictive_eligibility=evidence,
    )


def _authoritative_predictive_eligibility_reason(
    self: ForecastRef,
    decision_time: object,
    *,
    expected_model_id: str | None,
) -> str | None:
    reason = _ORIGINAL_FORECAST_ELIGIBILITY_REASON(
        self,
        decision_time,
        expected_model_id=expected_model_id,
    )
    if reason is not None:
        return reason
    key = _authority_key_from_ref(self, decision_time)
    if key is None or key not in _RESOLVED_AUTHORITIES:
        return (
            "predictive eligibility was not resolved from canonical "
            "ScientificRegistry authority for this decision"
        )
    return None


# Install the guard before any normal Autosport submodule import can consume these
# APIs.  The package initializer imports this module exactly once per interpreter.
ForecastRef.predictive_eligibility_reason = _authoritative_predictive_eligibility_reason  # type: ignore[method-assign]
_qualification.resolve_predictive_eligibility = resolve_predictive_eligibility
