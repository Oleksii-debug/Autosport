from __future__ import annotations

"""Canonical runtime bridge from durable predictive science to stake admission.

The scientific resolver owns eligibility truth.  This module binds a pre-forecast,
immutable admission-policy commitment plus the durable scientific lineage to the
exact forecast and decision instant.  Serialized/caller-constructed eligibility is
audit data only: positive predictive allocation requires the exact ``ForecastRef``
object returned by a successful durable resolution in this process.
"""

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable

from .forecasting import ForecastRecord, parse_iso_timestamp
from .opportunity import ForecastRef, PredictiveEligibilityEvidence, QuoteRef
from .scientific_registry import ScientificRegistry
from . import predictive_qualification as _qualification


_ORIGINAL_FORECAST_ELIGIBILITY_REASON = ForecastRef.predictive_eligibility_reason
_ORIGINAL_RESOLVE_PREDICTIVE_ELIGIBILITY = (
    _qualification.resolve_predictive_eligibility
)
_AUTHORITY_MISSING_REASON = (
    "predictive eligibility was not resolved from canonical "
    "ScientificRegistry authority for this decision"
)
_MAX_RUNTIME_AUTHORITIES = 10_000


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
    forecast: ForecastRef,
    decision_time: object,
) -> dict[str, object] | None:
    evidence = forecast.predictive_eligibility
    if evidence is None:
        return None
    return {
        "schema": "autosport.resolved_predictive_runtime_authority",
        "schema_version": 2,
        "forecast_hash": forecast.forecast_hash,
        "quote_key": forecast.quote_key,
        "probability": str(forecast.probability),
        "input_cutoff_ts": _utc_instant(forecast.input_cutoff_ts),
        "market_snapshot_hash": forecast.market_snapshot_hash,
        "quote_market_event_hash": forecast.quote_market_event_hash,
        "model_id": forecast.model_id,
        "model_version": forecast.model_version,
        "strategy_version": forecast.strategy_version,
        "uncertainty": (
            None if forecast.uncertainty is None else str(forecast.uncertainty)
        ),
        "eligibility": evidence.to_dict(),
        "decision_time": _utc_instant(decision_time),
    }


def _runtime_fingerprint(
    forecast: ForecastRef,
    decision_time: object,
) -> str | None:
    payload = _authority_payload(forecast, decision_time)
    return None if payload is None else _authority_digest(payload)


def _policy_commitment_from_protocol(
    registry: ScientificRegistry,
    *,
    qualification: _qualification.ForecastCalibrationQualification,
    forecast: ForecastRecord,
    policy: _qualification.PredictiveAdmissionPolicy,
) -> None:
    """Require the exact admission policy to have been frozen before the forecast.

    ``ResearchProtocol.binding.promotion_rule`` is already an immutable, registry-
    hashed preregistration object.  We extend that existing authority rather than
    inventing a second policy registry: a predictive protocol must commit the exact
    policy id/digest in that canonical JSON before forecast generation.
    """

    entry = registry.get("ResearchProtocol", qualification.research_protocol_id)
    if entry is None:
        raise _qualification.PredictiveQualificationError(
            "predictive admission policy has no durable ResearchProtocol authority"
        )
    payload = entry.payload
    binding = payload.get("binding")
    if not isinstance(binding, dict):
        raise _qualification.PredictiveQualificationError(
            "ResearchProtocol binding is not canonical"
        )
    rule_text = binding.get("promotion_rule")
    if not isinstance(rule_text, str):
        raise _qualification.PredictiveQualificationError(
            "ResearchProtocol lacks frozen promotion rule"
        )
    try:
        rule = json.loads(rule_text)
    except json.JSONDecodeError as exc:
        raise _qualification.PredictiveQualificationError(
            "ResearchProtocol promotion rule is not canonical JSON"
        ) from exc
    if not isinstance(rule, dict) or rule.get("kind") != "autosport-promotion-rule-v1":
        raise _qualification.PredictiveQualificationError(
            "ResearchProtocol promotion rule kind is unsupported"
        )
    committed_id = rule.get("predictive_admission_policy_id")
    committed_sha = rule.get("predictive_admission_policy_sha256")
    if committed_id != policy.policy_id or committed_sha != policy.sha256:
        raise _qualification.PredictiveQualificationError(
            "predictive admission policy was not precommitted by ResearchProtocol"
        )
    try:
        protocol_available = parse_iso_timestamp(str(payload.get("available_at")))
        protocol_frozen = parse_iso_timestamp(str(binding.get("frozen_at_utc")))
        generated_at = parse_iso_timestamp(forecast.generated_at)
        policy_frozen = parse_iso_timestamp(policy.frozen_at)
    except (TypeError, ValueError) as exc:
        raise _qualification.PredictiveQualificationError(
            "predictive admission policy commitment timestamps are invalid"
        ) from exc
    if protocol_available > generated_at or protocol_frozen > generated_at:
        raise _qualification.PredictiveQualificationError(
            "predictive admission policy commitment was unavailable at forecast time"
        )
    if policy_frozen > protocol_available:
        raise _qualification.PredictiveQualificationError(
            "predictive admission policy claims freeze after durable protocol authority"
        )


class _PromotionBundleDigestRegistry(ScientificRegistry):
    """Read-only resolver view matching PromotionDecision's bundle-hash contract."""

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
    """Resolve eligibility only from precommitted policy + immutable registry truth."""

    if not isinstance(registry, ScientificRegistry):
        raise _qualification.PredictiveQualificationError(
            "registry must be ScientificRegistry"
        )
    _policy_commitment_from_protocol(
        registry,
        qualification=qualification,
        forecast=forecast,
        policy=policy,
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
    """Return durable audit evidence; this value alone grants no runtime authority."""

    resolved = resolve_predictive_eligibility(
        registry,
        forecast,
        decision_time=decision_time,
        policy=policy,
        qualification=qualification,
    )
    return PredictiveEligibilityEvidence(
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


def _install_runtime_authority() -> Callable[..., ForecastRef]:
    """Install an object-identity guard and return the sole durable minting path.

    Runtime authority state lives only in this closure.  There is no module-global
    token set, computable membership key, or standalone mint helper.  The public
    function returned below first performs durable registry resolution and only then
    records the exact immutable ForecastRef object + decision fingerprint.
    """

    authorized: dict[int, tuple[ForecastRef, str]] = {}

    def guarded_reason(
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
        fingerprint = _runtime_fingerprint(self, decision_time)
        entry = authorized.get(id(self))
        if (
            fingerprint is None
            or entry is None
            or entry[0] is not self
            or entry[1] != fingerprint
        ):
            return _AUTHORITY_MISSING_REASON
        return None

    ForecastRef.predictive_eligibility_reason = guarded_reason  # type: ignore[method-assign]

    def resolve_authoritative_forecast_ref(
        registry: ScientificRegistry,
        forecast: ForecastRecord,
        quote: QuoteRef,
        *,
        decision_time: str,
        policy: _qualification.PredictiveAdmissionPolicy,
        qualification: _qualification.ForecastCalibrationQualification,
    ) -> ForecastRef:
        evidence = resolve_forecast_predictive_authority(
            registry,
            forecast,
            decision_time=decision_time,
            policy=policy,
            qualification=qualification,
        )
        reference = ForecastRef.from_forecast(
            forecast,
            quote,
            predictive_eligibility=evidence,
        )
        fingerprint = _runtime_fingerprint(reference, decision_time)
        if fingerprint is None:
            raise _qualification.PredictiveQualificationError(
                "resolved predictive authority lacks eligibility evidence"
            )
        if len(authorized) >= _MAX_RUNTIME_AUTHORITIES:
            authorized.pop(next(iter(authorized)))
        authorized[id(reference)] = (reference, fingerprint)
        return reference

    return resolve_authoritative_forecast_ref


resolve_authoritative_forecast_ref = _install_runtime_authority()
_qualification.resolve_predictive_eligibility = resolve_predictive_eligibility
