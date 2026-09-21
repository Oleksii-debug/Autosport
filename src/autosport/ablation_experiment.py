"""Typed, non-authoritative ablation experiment contracts.

This module identifies controlled intervention sets. It does not promote a
strategy, decompose realized reward, authorize execution, or infer that a
multi-factor contrast is additive. Promotion/economic authorities remain in
their existing modules.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import Iterable


_HEX = frozenset("0123456789abcdef")


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be canonical non-empty text")
    value.encode("utf-8", errors="strict")
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(character not in _HEX for character in text):
        raise ValueError(f"{name} must be lowercase SHA-256")
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc)


def _instant_text(value: object, name: str) -> str:
    return _instant(value, name).isoformat().replace("+00:00", "Z")


def _decimal(value: object, name: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise ValueError(f"{name} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    return value


def _decimal_text(value: Decimal) -> str:
    value = _decimal(value, "decimal")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


class AblationMode(StrEnum):
    ONE_FACTOR = "ONE_FACTOR"
    FACTORIAL = "FACTORIAL"


class AblationFactor(StrEnum):
    DATA = "DATA"
    MODEL = "MODEL"
    THRESHOLD_SELECTION = "THRESHOLD_SELECTION"
    SIZING = "SIZING"
    EXECUTION = "EXECUTION"


_FACTOR_ORDER = (
    AblationFactor.DATA,
    AblationFactor.MODEL,
    AblationFactor.THRESHOLD_SELECTION,
    AblationFactor.SIZING,
    AblationFactor.EXECUTION,
)
_FACTOR_INDEX = {factor: index for index, factor in enumerate(_FACTOR_ORDER)}


def _factor_tuple(
    values: object,
    name: str,
    *,
    allow_empty: bool,
) -> tuple[AblationFactor, ...]:
    if type(values) is not tuple:
        raise ValueError(f"{name} must be a tuple")
    if not values and not allow_empty:
        raise ValueError(f"{name} must not be empty")
    if not all(isinstance(value, AblationFactor) for value in values):
        raise ValueError(f"{name} must contain AblationFactor values")
    canonical = tuple(sorted(values, key=_FACTOR_INDEX.__getitem__))
    if values != canonical or len(values) != len(set(values)):
        raise ValueError(f"{name} must be canonical, sorted and unique")
    return values


def _factor_set_sort_key(values: tuple[AblationFactor, ...]) -> tuple[int, ...]:
    return tuple(_FACTOR_INDEX[value] for value in values)


@dataclass(frozen=True, slots=True)
class FactorIdentitySet:
    """Exact immutable identity of each causal system factor in one run cell."""

    data_sha256: str
    model_sha256: str
    threshold_selection_sha256: str
    sizing_sha256: str
    execution_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "data_sha256",
            "model_sha256",
            "threshold_selection_sha256",
            "sizing_sha256",
            "execution_sha256",
        ):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))

    def identity(self, factor: AblationFactor) -> str:
        if factor is AblationFactor.DATA:
            return self.data_sha256
        if factor is AblationFactor.MODEL:
            return self.model_sha256
        if factor is AblationFactor.THRESHOLD_SELECTION:
            return self.threshold_selection_sha256
        if factor is AblationFactor.SIZING:
            return self.sizing_sha256
        if factor is AblationFactor.EXECUTION:
            return self.execution_sha256
        raise ValueError("unsupported ablation factor")

    def changed_factors(self, other: "FactorIdentitySet") -> tuple[AblationFactor, ...]:
        if not isinstance(other, FactorIdentitySet):
            raise TypeError("other must be FactorIdentitySet")
        return tuple(
            factor
            for factor in _FACTOR_ORDER
            if self.identity(factor) != other.identity(factor)
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "data_sha256": self.data_sha256,
            "model_sha256": self.model_sha256,
            "threshold_selection_sha256": self.threshold_selection_sha256,
            "sizing_sha256": self.sizing_sha256,
            "execution_sha256": self.execution_sha256,
        }


@dataclass(frozen=True, slots=True)
class AblationCellSpec:
    cell_id: str
    factors: FactorIdentitySet
    declared_changed_factors: tuple[AblationFactor, ...]

    def __post_init__(self) -> None:
        _text(self.cell_id, "cell_id")
        if not isinstance(self.factors, FactorIdentitySet):
            raise ValueError("factors must be FactorIdentitySet")
        _factor_tuple(
            self.declared_changed_factors,
            "declared_changed_factors",
            allow_empty=True,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "cell_id": self.cell_id,
            "factors": self.factors.to_dict(),
            "declared_changed_factors": [
                factor.value for factor in self.declared_changed_factors
            ],
        }

    @property
    def spec_sha256(self) -> str:
        return _digest(self.to_dict())


@dataclass(frozen=True, slots=True)
class AblationProtocol:
    """Frozen intervention matrix adjacent to the existing scientific protocol."""

    protocol_id: str
    mode: AblationMode
    scientific_protocol_sha256: str
    case_population_sha256: str
    causal_cutoff: str
    primary_metric: str
    baseline: AblationCellSpec
    interventions: tuple[AblationCellSpec, ...]
    declared_factor_sets: tuple[tuple[AblationFactor, ...], ...]

    def __post_init__(self) -> None:
        _text(self.protocol_id, "protocol_id")
        if not isinstance(self.mode, AblationMode):
            raise ValueError("mode must be AblationMode")
        object.__setattr__(
            self,
            "scientific_protocol_sha256",
            _sha256(self.scientific_protocol_sha256, "scientific_protocol_sha256"),
        )
        object.__setattr__(
            self,
            "case_population_sha256",
            _sha256(self.case_population_sha256, "case_population_sha256"),
        )
        object.__setattr__(
            self,
            "causal_cutoff",
            _instant_text(self.causal_cutoff, "causal_cutoff"),
        )
        _text(self.primary_metric, "primary_metric")
        if not isinstance(self.baseline, AblationCellSpec):
            raise ValueError("baseline must be AblationCellSpec")
        if self.baseline.declared_changed_factors:
            raise ValueError("baseline cannot declare changed factors")
        if type(self.interventions) is not tuple or not self.interventions:
            raise ValueError("interventions must be a non-empty tuple")
        if not all(isinstance(cell, AblationCellSpec) for cell in self.interventions):
            raise ValueError("interventions must contain AblationCellSpec values")

        cell_ids = (self.baseline.cell_id, *(cell.cell_id for cell in self.interventions))
        if len(cell_ids) != len(set(cell_ids)):
            raise ValueError("ablation cell_id values must be unique")

        if type(self.declared_factor_sets) is not tuple or not self.declared_factor_sets:
            raise ValueError("declared_factor_sets must be a non-empty tuple")
        checked_sets = tuple(
            _factor_tuple(values, "declared_factor_set", allow_empty=False)
            for values in self.declared_factor_sets
        )
        if len(checked_sets) != len(set(checked_sets)):
            raise ValueError("declared_factor_sets must be unique")
        if checked_sets != tuple(sorted(checked_sets, key=_factor_set_sort_key)):
            raise ValueError("declared_factor_sets must be canonically ordered")

        actual_sets: list[tuple[AblationFactor, ...]] = []
        for cell in self.interventions:
            actual = self.baseline.factors.changed_factors(cell.factors)
            if not actual:
                raise ValueError("intervention cell must change at least one factor")
            if cell.declared_changed_factors != actual:
                raise ValueError(
                    "declared changed factors do not match exact factor identity delta"
                )
            if self.mode is AblationMode.ONE_FACTOR and len(actual) != 1:
                raise ValueError("ONE_FACTOR intervention must change exactly one factor")
            actual_sets.append(actual)

        if tuple(actual_sets) != checked_sets:
            raise ValueError(
                "intervention cells must exactly match preregistered declared_factor_sets"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "kind": "autosport-ablation-protocol-v1",
            "protocol_id": self.protocol_id,
            "mode": self.mode.value,
            "scientific_protocol_sha256": self.scientific_protocol_sha256,
            "case_population_sha256": self.case_population_sha256,
            "causal_cutoff": self.causal_cutoff,
            "primary_metric": self.primary_metric,
            "baseline": self.baseline.to_dict(),
            "interventions": [cell.to_dict() for cell in self.interventions],
            "declared_factor_sets": [
                [factor.value for factor in factor_set]
                for factor_set in self.declared_factor_sets
            ],
        }

    @property
    def protocol_sha256(self) -> str:
        return _digest(self.to_dict())


@dataclass(frozen=True, slots=True)
class AblationCellObservation:
    """One exact run result bound to one exact frozen ablation protocol."""

    cell_id: str
    protocol_sha256: str
    cell_spec_sha256: str
    run_evidence_sha256: str
    metric_value: Decimal
    evidence_available_at: str

    def __post_init__(self) -> None:
        _text(self.cell_id, "cell_id")
        object.__setattr__(
            self,
            "protocol_sha256",
            _sha256(self.protocol_sha256, "protocol_sha256"),
        )
        object.__setattr__(
            self,
            "cell_spec_sha256",
            _sha256(self.cell_spec_sha256, "cell_spec_sha256"),
        )
        object.__setattr__(
            self,
            "run_evidence_sha256",
            _sha256(self.run_evidence_sha256, "run_evidence_sha256"),
        )
        _decimal(self.metric_value, "metric_value")
        object.__setattr__(
            self,
            "evidence_available_at",
            _instant_text(self.evidence_available_at, "evidence_available_at"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "cell_id": self.cell_id,
            "protocol_sha256": self.protocol_sha256,
            "cell_spec_sha256": self.cell_spec_sha256,
            "run_evidence_sha256": self.run_evidence_sha256,
            "metric_value": _decimal_text(self.metric_value),
            "evidence_available_at": self.evidence_available_at,
        }


@dataclass(frozen=True, slots=True)
class AblationContrast:
    cell_id: str
    identified_factor_set: tuple[AblationFactor, ...]
    baseline_metric: Decimal
    intervention_metric: Decimal
    delta: Decimal
    run_evidence_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "cell_id": self.cell_id,
            "identified_factor_set": [
                factor.value for factor in self.identified_factor_set
            ],
            "baseline_metric": _decimal_text(self.baseline_metric),
            "intervention_metric": _decimal_text(self.intervention_metric),
            "delta": _decimal_text(self.delta),
            "run_evidence_sha256": self.run_evidence_sha256,
            "additive_per_factor_contribution_claim": False,
        }


@dataclass(frozen=True, slots=True)
class AblationReport:
    protocol_sha256: str
    scientific_protocol_sha256: str
    case_population_sha256: str
    mode: AblationMode
    causal_cutoff: str
    primary_metric: str
    evaluated_at: str
    baseline_run_evidence_sha256: str
    contrasts: tuple[AblationContrast, ...]

    def to_dict(self) -> dict[str, object]:
        payload = {
            "schema_version": 1,
            "kind": "autosport-ablation-report-v1",
            "protocol_sha256": self.protocol_sha256,
            "scientific_protocol_sha256": self.scientific_protocol_sha256,
            "case_population_sha256": self.case_population_sha256,
            "mode": self.mode.value,
            "causal_cutoff": self.causal_cutoff,
            "primary_metric": self.primary_metric,
            "evaluated_at": self.evaluated_at,
            "baseline_run_evidence_sha256": self.baseline_run_evidence_sha256,
            "contrasts": [contrast.to_dict() for contrast in self.contrasts],
            "truth": {
                "recommendation_only": True,
                "external_metric_evidence_must_be_resolved": True,
                "factor_contributions_additive": False,
                "active_strategy_mutation": False,
                "real_money_execution": False,
                "human_tested": False,
                "nvda_verified": False,
                "whole_product_complete": False,
            },
        }
        payload["report_sha256"] = _digest(payload)
        return payload


def _exact_difference(left: Decimal, right: Decimal) -> Decimal:
    """Subtract without inheriting a low ambient Decimal precision."""

    left = _decimal(left, "left")
    right = _decimal(right, "right")
    left_digits = len(left.as_tuple().digits)
    right_digits = len(right.as_tuple().digits)
    exponent_span = abs(left.as_tuple().exponent - right.as_tuple().exponent)
    with localcontext() as context:
        context.prec = max(left_digits, right_digits) + exponent_span + 8
        result = left - right
    if not result.is_finite():
        raise ValueError("ablation metric delta must be finite")
    return result


def evaluate_ablation(
    protocol: AblationProtocol,
    observations: Iterable[AblationCellObservation],
    *,
    evaluated_at: str,
) -> AblationReport:
    """Evaluate exact preregistered contrasts under their frozen protocol only."""

    if not isinstance(protocol, AblationProtocol):
        raise TypeError("protocol must be AblationProtocol")
    evaluated_text = _instant_text(evaluated_at, "evaluated_at")
    evaluated = _instant(evaluated_text, "evaluated_at")
    if evaluated < _instant(protocol.causal_cutoff, "causal_cutoff"):
        raise ValueError("evaluation cannot precede causal cutoff")

    values = tuple(observations)
    if not values or not all(
        isinstance(value, AblationCellObservation) for value in values
    ):
        raise ValueError("observations must contain AblationCellObservation values")

    by_cell: dict[str, AblationCellObservation] = {}
    evidence_ids: set[str] = set()
    for value in values:
        if value.cell_id in by_cell:
            raise ValueError("ablation observations contain duplicate cell_id")
        if value.run_evidence_sha256 in evidence_ids:
            raise ValueError("ablation run evidence cannot be reused across cells")
        if _instant(value.evidence_available_at, "evidence_available_at") > evaluated:
            raise ValueError("ablation evidence was not available at evaluation time")
        by_cell[value.cell_id] = value
        evidence_ids.add(value.run_evidence_sha256)

    cells = (protocol.baseline, *protocol.interventions)
    expected_ids = {cell.cell_id for cell in cells}
    if set(by_cell) != expected_ids:
        missing = sorted(expected_ids - set(by_cell))
        extra = sorted(set(by_cell) - expected_ids)
        raise ValueError(
            "ablation observation matrix is incomplete or unexpected: "
            f"missing={missing} extra={extra}"
        )

    expected_protocol_sha256 = protocol.protocol_sha256
    for cell in cells:
        observation = by_cell[cell.cell_id]
        if observation.protocol_sha256 != expected_protocol_sha256:
            raise ValueError(
                f"ablation observation protocol mismatch: {cell.cell_id}"
            )
        if observation.cell_spec_sha256 != cell.spec_sha256:
            raise ValueError(
                f"ablation observation cell specification mismatch: {cell.cell_id}"
            )

    baseline_observation = by_cell[protocol.baseline.cell_id]
    contrasts = tuple(
        AblationContrast(
            cell_id=cell.cell_id,
            identified_factor_set=cell.declared_changed_factors,
            baseline_metric=baseline_observation.metric_value,
            intervention_metric=by_cell[cell.cell_id].metric_value,
            delta=_exact_difference(
                by_cell[cell.cell_id].metric_value,
                baseline_observation.metric_value,
            ),
            run_evidence_sha256=by_cell[cell.cell_id].run_evidence_sha256,
        )
        for cell in protocol.interventions
    )
    return AblationReport(
        protocol_sha256=expected_protocol_sha256,
        scientific_protocol_sha256=protocol.scientific_protocol_sha256,
        case_population_sha256=protocol.case_population_sha256,
        mode=protocol.mode,
        causal_cutoff=protocol.causal_cutoff,
        primary_metric=protocol.primary_metric,
        evaluated_at=evaluated_text,
        baseline_run_evidence_sha256=baseline_observation.run_evidence_sha256,
        contrasts=contrasts,
    )


__all__ = [
    "AblationCellObservation",
    "AblationCellSpec",
    "AblationContrast",
    "AblationFactor",
    "AblationMode",
    "AblationProtocol",
    "AblationReport",
    "FactorIdentitySet",
    "evaluate_ablation",
]
