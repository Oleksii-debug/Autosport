"""Deterministic causal distribution-drift measurement evidence.

This module measures empirical distribution shift only. It deliberately does
not classify a model as stable/warning/breach, authorize promotion/deployment,
or grant provider/execution/economic authority.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any, Mapping, Sequence


class ModelDriftEvidenceError(ValueError):
    """Raised when model-drift evidence is non-canonical or causally invalid."""


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ModelDriftEvidenceError(f"{field} must be a non-empty canonical string")
    value.encode("utf-8")
    return value


def _sha256(value: object, field: str) -> str:
    text = _text(value, field).lower()
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise ModelDriftEvidenceError(f"{field} must be canonical SHA-256 hex")
    return text


def _instant_text(value: object, field: str) -> str:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ModelDriftEvidenceError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ModelDriftEvidenceError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _decimal_text(value: object, field: str) -> str:
    if isinstance(value, bool) or type(value) not in (str, int, Decimal):
        raise ModelDriftEvidenceError(
            f"{field} must be an exact decimal string, integer, or Decimal"
        )
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ModelDriftEvidenceError(f"{field} must be a finite decimal") from exc
    if not decimal_value.is_finite():
        raise ModelDriftEvidenceError(f"{field} must be a finite decimal")

    sign, digits, exponent = decimal_value.as_tuple()
    if not digits or all(digit == 0 for digit in digits):
        return "0"

    coefficient = "".join(str(digit) for digit in digits)
    if exponent >= 0:
        text = coefficient + ("0" * exponent)
    else:
        point = len(coefficient) + exponent
        if point > 0:
            text = coefficient[:point] + "." + coefficient[point:]
        else:
            text = "0." + ("0" * (-point)) + coefficient
        text = text.rstrip("0").rstrip(".")
    return "-" + text if sign else text


def _digest(payload: Mapping[str, Any] | Sequence[Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class DriftObservation:
    """One causally available scalar observation used by drift measurement."""

    sample_id: str
    observed_at: str
    available_at: str
    value: str
    evidence_sha256: str

    def __post_init__(self) -> None:
        _text(self.sample_id, "sample_id")
        observed = _instant_text(self.observed_at, "observed_at")
        available = _instant_text(self.available_at, "available_at")
        if _instant(available) < _instant(observed):
            raise ModelDriftEvidenceError("available_at must not precede observed_at")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "available_at", available)
        object.__setattr__(self, "value", _decimal_text(self.value, "value"))
        object.__setattr__(
            self,
            "evidence_sha256",
            _sha256(self.evidence_sha256, "evidence_sha256"),
        )

    @property
    def identity_sha256(self) -> str:
        return _digest(self.to_payload())

    def to_payload(self) -> dict[str, object]:
        return {
            "sample_id": self.sample_id,
            "observed_at": self.observed_at,
            "available_at": self.available_at,
            "value": self.value,
            "evidence_sha256": self.evidence_sha256,
        }


@dataclass(frozen=True, slots=True)
class DriftWindow:
    """A frozen, non-overlapping causal measurement window."""

    window_id: str
    model_id: str
    model_artifact_sha256: str
    metric_key: str
    window_start: str
    window_end: str
    observations: tuple[DriftObservation, ...]

    def __post_init__(self) -> None:
        _text(self.window_id, "window_id")
        _text(self.model_id, "model_id")
        object.__setattr__(
            self,
            "model_artifact_sha256",
            _sha256(self.model_artifact_sha256, "model_artifact_sha256"),
        )
        _text(self.metric_key, "metric_key")
        start = _instant_text(self.window_start, "window_start")
        end = _instant_text(self.window_end, "window_end")
        if _instant(end) <= _instant(start):
            raise ModelDriftEvidenceError("window_end must be later than window_start")
        object.__setattr__(self, "window_start", start)
        object.__setattr__(self, "window_end", end)

        if type(self.observations) is not tuple or not self.observations:
            raise ModelDriftEvidenceError("observations must be a non-empty canonical tuple")
        if any(type(item) is not DriftObservation for item in self.observations):
            raise ModelDriftEvidenceError(
                "observations must contain exact DriftObservation values"
            )

        identities = [item.sample_id for item in self.observations]
        if len(identities) != len(set(identities)):
            raise ModelDriftEvidenceError("sample_id values must be unique within a window")
        evidence_identities = [item.evidence_sha256 for item in self.observations]
        if len(evidence_identities) != len(set(evidence_identities)):
            raise ModelDriftEvidenceError(
                "evidence_sha256 values must be unique within a window"
            )

        for item in self.observations:
            available = _instant(item.available_at)
            if not (_instant(start) <= available < _instant(end)):
                raise ModelDriftEvidenceError(
                    "observation available_at must be inside [window_start, window_end)"
                )

        canonical = tuple(
            sorted(
                self.observations,
                key=lambda item: (item.available_at, item.observed_at, item.sample_id),
            )
        )
        if self.observations != canonical:
            raise ModelDriftEvidenceError(
                "observations must use canonical causal ordering"
            )

    @property
    def sample_count(self) -> int:
        return len(self.observations)

    @property
    def identity_sha256(self) -> str:
        return _digest(self.to_payload())

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "kind": "autosport_model_drift_window",
            "window_id": self.window_id,
            "model_id": self.model_id,
            "model_artifact_sha256": self.model_artifact_sha256,
            "metric_key": self.metric_key,
            "window_basis": "available_at",
            "window_start": self.window_start,
            "window_end": self.window_end,
            "sample_count": self.sample_count,
            "observations": [item.to_payload() for item in self.observations],
        }


@dataclass(frozen=True, slots=True)
class TwoSampleKSEvidence:
    """Exact two-sample empirical CDF distance over two frozen windows."""

    reference_window_sha256: str
    current_window_sha256: str
    model_id: str
    model_artifact_sha256: str
    metric_key: str
    evaluated_at: str
    reference_count: int
    current_count: int
    ks_numerator: int
    ks_denominator: int
    max_difference_at: str | None
    evidence_sha256: str

    SCHEMA_VERSION = 1

    def __post_init__(self) -> None:
        for field in ("reference_window_sha256", "current_window_sha256"):
            object.__setattr__(self, field, _sha256(getattr(self, field), field))
        _text(self.model_id, "model_id")
        object.__setattr__(
            self,
            "model_artifact_sha256",
            _sha256(self.model_artifact_sha256, "model_artifact_sha256"),
        )
        _text(self.metric_key, "metric_key")
        object.__setattr__(
            self, "evaluated_at", _instant_text(self.evaluated_at, "evaluated_at")
        )
        if type(self.reference_count) is not int or self.reference_count <= 0:
            raise ModelDriftEvidenceError("reference_count must be a positive integer")
        if type(self.current_count) is not int or self.current_count <= 0:
            raise ModelDriftEvidenceError("current_count must be a positive integer")
        if type(self.ks_numerator) is not int or self.ks_numerator < 0:
            raise ModelDriftEvidenceError("ks_numerator must be a non-negative integer")
        if type(self.ks_denominator) is not int or self.ks_denominator <= 0:
            raise ModelDriftEvidenceError("ks_denominator must be a positive integer")
        ratio = Fraction(self.ks_numerator, self.ks_denominator)
        if ratio < 0 or ratio > 1:
            raise ModelDriftEvidenceError("KS statistic must be between zero and one")
        if (ratio.numerator, ratio.denominator) != (
            self.ks_numerator,
            self.ks_denominator,
        ):
            raise ModelDriftEvidenceError("KS statistic must use reduced rational form")
        if ratio == 0:
            if self.max_difference_at is not None:
                raise ModelDriftEvidenceError(
                    "zero KS statistic must not claim max_difference_at"
                )
        else:
            object.__setattr__(
                self,
                "max_difference_at",
                _decimal_text(self.max_difference_at, "max_difference_at"),
            )
        object.__setattr__(
            self,
            "evidence_sha256",
            _sha256(self.evidence_sha256, "evidence_sha256"),
        )
        expected = _digest(self.to_payload(include_identity=False))
        if self.evidence_sha256 != expected:
            raise ModelDriftEvidenceError("evidence_sha256 does not match payload")

    @property
    def ks_fraction(self) -> Fraction:
        return Fraction(self.ks_numerator, self.ks_denominator)

    def to_payload(self, *, include_identity: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.SCHEMA_VERSION,
            "kind": "autosport_two_sample_ks_drift_evidence",
            "reference_window_sha256": self.reference_window_sha256,
            "current_window_sha256": self.current_window_sha256,
            "model_id": self.model_id,
            "model_artifact_sha256": self.model_artifact_sha256,
            "metric_key": self.metric_key,
            "window_basis": "available_at",
            "evaluated_at": self.evaluated_at,
            "reference_count": self.reference_count,
            "current_count": self.current_count,
            "ks_statistic": {
                "numerator": self.ks_numerator,
                "denominator": self.ks_denominator,
            },
            "max_difference_at": self.max_difference_at,
            "truth": {
                "metric": "two-sample-kolmogorov-smirnov-empirical-cdf-distance",
                "threshold_applied": False,
                "lifecycle_classification_produced": False,
                "statistical_significance_claimed": False,
                "source_provenance_authorized": False,
                "promotion_authorized": False,
                "deployment_authorized": False,
                "execution_authorized": False,
                "real_money_authorized": False,
            },
        }
        if include_identity:
            payload["evidence_sha256"] = self.evidence_sha256
        return payload


def _ks_statistic(
    reference_values: Sequence[Decimal],
    current_values: Sequence[Decimal],
) -> tuple[Fraction, Decimal | None]:
    points = sorted(set(reference_values) | set(current_values))
    reference_total = len(reference_values)
    current_total = len(current_values)
    reference_seen = 0
    current_seen = 0
    max_difference = Fraction(0, 1)
    max_point: Decimal | None = None

    reference_sorted = sorted(reference_values)
    current_sorted = sorted(current_values)
    r_index = 0
    c_index = 0

    for point in points:
        while r_index < reference_total and reference_sorted[r_index] <= point:
            reference_seen += 1
            r_index += 1
        while c_index < current_total and current_sorted[c_index] <= point:
            current_seen += 1
            c_index += 1
        difference = abs(
            Fraction(reference_seen, reference_total)
            - Fraction(current_seen, current_total)
        )
        if difference > max_difference:
            max_difference = difference
            max_point = point

    return max_difference, max_point


def build_two_sample_ks_evidence(
    reference: DriftWindow,
    current: DriftWindow,
    *,
    evaluated_at: str,
) -> TwoSampleKSEvidence:
    """Measure exact empirical CDF drift without producing a lifecycle verdict."""

    if type(reference) is not DriftWindow or type(current) is not DriftWindow:
        raise ModelDriftEvidenceError("reference/current must be exact DriftWindow values")
    evaluated = _instant_text(evaluated_at, "evaluated_at")
    if reference.window_id == current.window_id:
        raise ModelDriftEvidenceError(
            "reference/current windows must have distinct window_id values"
        )
    if (
        reference.model_id != current.model_id
        or reference.model_artifact_sha256 != current.model_artifact_sha256
        or reference.metric_key != current.metric_key
    ):
        raise ModelDriftEvidenceError(
            "drift windows must bind the same model/artifact/metric identity"
        )
    if _instant(reference.window_end) > _instant(current.window_start):
        raise ModelDriftEvidenceError("reference/current windows must not overlap")
    if _instant(evaluated) < _instant(current.window_end):
        raise ModelDriftEvidenceError(
            "evaluated_at must not precede the complete current window"
        )

    reference_ids = {item.sample_id for item in reference.observations}
    current_ids = {item.sample_id for item in current.observations}
    if reference_ids & current_ids:
        raise ModelDriftEvidenceError(
            "reference/current windows must not reuse sample_id values"
        )
    reference_evidence = {
        item.evidence_sha256 for item in reference.observations
    }
    current_evidence = {item.evidence_sha256 for item in current.observations}
    if reference_evidence & current_evidence:
        raise ModelDriftEvidenceError(
            "reference/current windows must not reuse evidence_sha256 values"
        )

    reference_values = tuple(Decimal(item.value) for item in reference.observations)
    current_values = tuple(Decimal(item.value) for item in current.observations)
    statistic, max_point = _ks_statistic(reference_values, current_values)

    payload: dict[str, object] = {
        "schema_version": TwoSampleKSEvidence.SCHEMA_VERSION,
        "kind": "autosport_two_sample_ks_drift_evidence",
        "reference_window_sha256": reference.identity_sha256,
        "current_window_sha256": current.identity_sha256,
        "model_id": reference.model_id,
        "model_artifact_sha256": reference.model_artifact_sha256,
        "metric_key": reference.metric_key,
        "window_basis": "available_at",
        "evaluated_at": evaluated,
        "reference_count": reference.sample_count,
        "current_count": current.sample_count,
        "ks_statistic": {
            "numerator": statistic.numerator,
            "denominator": statistic.denominator,
        },
        "max_difference_at": (
            None if max_point is None else _decimal_text(max_point, "max_difference_at")
        ),
        "truth": {
            "metric": "two-sample-kolmogorov-smirnov-empirical-cdf-distance",
            "threshold_applied": False,
            "lifecycle_classification_produced": False,
            "statistical_significance_claimed": False,
            "source_provenance_authorized": False,
            "promotion_authorized": False,
            "deployment_authorized": False,
            "execution_authorized": False,
            "real_money_authorized": False,
        },
    }
    return TwoSampleKSEvidence(
        reference_window_sha256=reference.identity_sha256,
        current_window_sha256=current.identity_sha256,
        model_id=reference.model_id,
        model_artifact_sha256=reference.model_artifact_sha256,
        metric_key=reference.metric_key,
        evaluated_at=evaluated,
        reference_count=reference.sample_count,
        current_count=current.sample_count,
        ks_numerator=statistic.numerator,
        ks_denominator=statistic.denominator,
        max_difference_at=payload["max_difference_at"],
        evidence_sha256=_digest(payload),
    )


def verify_two_sample_ks_evidence(
    evidence: TwoSampleKSEvidence,
    reference: DriftWindow,
    current: DriftWindow,
) -> None:
    """Re-derive a measurement against exact windows and fail on any drift."""

    if type(evidence) is not TwoSampleKSEvidence:
        raise ModelDriftEvidenceError("evidence must be exact TwoSampleKSEvidence")
    expected = build_two_sample_ks_evidence(
        reference,
        current,
        evaluated_at=evidence.evaluated_at,
    )
    if evidence != expected:
        raise ModelDriftEvidenceError(
            "KS evidence does not match exact reference/current windows"
        )
