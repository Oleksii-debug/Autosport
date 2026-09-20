from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
import hashlib
import json
import re
from typing import Any, Sequence

from .campaign_cost_evidence import (
    CostBasis,
    CostClass,
    CostSourceRef,
    CostTreatment,
    CostTruth,
    CostUnit,
    REQUIRED_COST_CLASSES,
)
from .opportunity import PortfolioPlan


SCHEMA_VERSION = 1
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class ProspectiveApplicableCostError(ValueError):
    """Raised when prospective applicable-cost evidence is malformed."""


class ProspectiveCostCompleteness(StrEnum):
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"


def _utc(value: datetime, field: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ProspectiveApplicableCostError(f"{field} must be timezone-aware")
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise ProspectiveApplicableCostError(f"{field} must be UTC")


def _sha256(value: str, field: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ProspectiveApplicableCostError(
            f"{field} must be a canonical lowercase SHA-256 digest"
        )


def _finite_nonnegative(value: Decimal | None, field: str) -> None:
    if value is None or not isinstance(value, Decimal) or not value.is_finite():
        raise ProspectiveApplicableCostError(f"{field} must be a finite Decimal")
    if value < 0 or (value.is_zero() and value.is_signed()):
        raise ProspectiveApplicableCostError(f"{field} must be non-negative")


def _datetime_text(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _digest(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class CostApplicabilityEvidence:
    """Product-owned authority saying whether one cost class applies to one plan."""

    plan_id: str
    cost_class: CostClass
    applicable: bool
    basis: CostBasis
    source: CostSourceRef
    observed_at: datetime
    available_at: datetime
    valid_until: datetime

    def __post_init__(self) -> None:
        _sha256(self.plan_id, "plan_id")
        if type(self.applicable) is not bool:
            raise ProspectiveApplicableCostError("applicable must be a bool")
        if not isinstance(self.cost_class, CostClass):
            raise ProspectiveApplicableCostError("cost_class must be CostClass")
        if self.basis is not CostBasis.AUTHORITATIVE_DECLARATION:
            raise ProspectiveApplicableCostError(
                "applicability requires AUTHORITATIVE_DECLARATION basis"
            )
        if not isinstance(self.source, CostSourceRef):
            raise ProspectiveApplicableCostError("source must be CostSourceRef")
        _utc(self.observed_at, "observed_at")
        _utc(self.available_at, "available_at")
        _utc(self.valid_until, "valid_until")
        if self.observed_at > self.available_at:
            raise ProspectiveApplicableCostError(
                "observed_at cannot be later than available_at"
            )
        if self.available_at > self.valid_until:
            raise ProspectiveApplicableCostError(
                "available_at cannot be later than valid_until"
            )

    @property
    def evidence_id(self) -> str:
        return _digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "autosport.prospective_cost_applicability",
            "schema_version": SCHEMA_VERSION,
            "plan_id": self.plan_id,
            "cost_class": self.cost_class.value,
            "applicable": self.applicable,
            "basis": self.basis.value,
            "source": self.source.to_dict(),
            "observed_at": _datetime_text(self.observed_at),
            "available_at": _datetime_text(self.available_at),
            "valid_until": _datetime_text(self.valid_until),
        }


@dataclass(frozen=True, slots=True)
class ProspectiveCostEvidence:
    """Exact prospective monetary evidence for one applicable plan cost class."""

    plan_id: str
    cost_class: CostClass
    truth: CostTruth
    basis: CostBasis
    treatment: CostTreatment
    source: CostSourceRef
    unit: CostUnit
    currency: str | None
    amount: Decimal
    observed_at: datetime
    available_at: datetime
    valid_until: datetime

    def __post_init__(self) -> None:
        _sha256(self.plan_id, "plan_id")
        if not isinstance(self.cost_class, CostClass):
            raise ProspectiveApplicableCostError("cost_class must be CostClass")
        if self.truth not in {CostTruth.KNOWN_ZERO, CostTruth.KNOWN_AMOUNT}:
            raise ProspectiveApplicableCostError(
                "prospective cost evidence must be KNOWN_ZERO or KNOWN_AMOUNT"
            )
        if not isinstance(self.basis, CostBasis):
            raise ProspectiveApplicableCostError("basis must be CostBasis")
        if not isinstance(self.treatment, CostTreatment):
            raise ProspectiveApplicableCostError("treatment must be CostTreatment")
        if not isinstance(self.source, CostSourceRef):
            raise ProspectiveApplicableCostError("source must be CostSourceRef")
        if self.unit is not CostUnit.MONEY:
            raise ProspectiveApplicableCostError(
                "prospective live proof requires monetary cost evidence"
            )
        if self.currency is None or _CURRENCY_RE.fullmatch(self.currency) is None:
            raise ProspectiveApplicableCostError(
                "monetary prospective cost requires an uppercase three-letter currency"
            )
        _finite_nonnegative(self.amount, "amount")
        if self.truth is CostTruth.KNOWN_ZERO and self.amount != Decimal("0"):
            raise ProspectiveApplicableCostError(
                "KNOWN_ZERO prospective cost requires amount=0"
            )
        if self.truth is CostTruth.KNOWN_AMOUNT and self.amount == Decimal("0"):
            raise ProspectiveApplicableCostError(
                "KNOWN_AMOUNT prospective cost must be positive; use KNOWN_ZERO"
            )
        _utc(self.observed_at, "observed_at")
        _utc(self.available_at, "available_at")
        _utc(self.valid_until, "valid_until")
        if self.observed_at > self.available_at:
            raise ProspectiveApplicableCostError(
                "observed_at cannot be later than available_at"
            )
        if self.available_at > self.valid_until:
            raise ProspectiveApplicableCostError(
                "available_at cannot be later than valid_until"
            )

    @property
    def evidence_id(self) -> str:
        return _digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "autosport.prospective_cost_evidence",
            "schema_version": SCHEMA_VERSION,
            "plan_id": self.plan_id,
            "cost_class": self.cost_class.value,
            "truth": self.truth.value,
            "basis": self.basis.value,
            "treatment": self.treatment.value,
            "source": self.source.to_dict(),
            "unit": self.unit.value,
            "currency": self.currency,
            "amount": str(self.amount),
            "observed_at": _datetime_text(self.observed_at),
            "available_at": _datetime_text(self.available_at),
            "valid_until": _datetime_text(self.valid_until),
        }


_RESOLUTION_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False)
class ProspectiveApplicableCostResolution:
    """Resolver-sealed result; callers cannot pass a completeness verdict directly."""

    plan_id: str
    opportunity_set_id: str
    decision_at: datetime
    currency: str
    applicability_evidence_ids: tuple[str, ...]
    cost_evidence_ids: tuple[str, ...]
    total_subtractable_amount: Decimal | None
    completeness: ProspectiveCostCompleteness
    incomplete_reasons: tuple[str, ...]

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise ProspectiveApplicableCostError(
            "ProspectiveApplicableCostResolution is created only by "
            "resolve_prospective_applicable_costs"
        )

    @classmethod
    def _from_resolver(
        cls,
        *,
        plan_id: str,
        opportunity_set_id: str,
        decision_at: datetime,
        currency: str,
        applicability_evidence_ids: tuple[str, ...],
        cost_evidence_ids: tuple[str, ...],
        total_subtractable_amount: Decimal | None,
        completeness: ProspectiveCostCompleteness,
        incomplete_reasons: tuple[str, ...],
        _token: object,
    ) -> "ProspectiveApplicableCostResolution":
        if _token is not _RESOLUTION_TOKEN:
            raise ProspectiveApplicableCostError("invalid resolution authority token")
        item = object.__new__(cls)
        object.__setattr__(item, "plan_id", plan_id)
        object.__setattr__(item, "opportunity_set_id", opportunity_set_id)
        object.__setattr__(item, "decision_at", decision_at)
        object.__setattr__(item, "currency", currency)
        object.__setattr__(
            item, "applicability_evidence_ids", applicability_evidence_ids
        )
        object.__setattr__(item, "cost_evidence_ids", cost_evidence_ids)
        object.__setattr__(
            item, "total_subtractable_amount", total_subtractable_amount
        )
        object.__setattr__(item, "completeness", completeness)
        object.__setattr__(item, "incomplete_reasons", incomplete_reasons)
        item._validate()
        return item

    def _validate(self) -> None:
        _sha256(self.plan_id, "plan_id")
        _sha256(self.opportunity_set_id, "opportunity_set_id")
        _utc(self.decision_at, "decision_at")
        if _CURRENCY_RE.fullmatch(self.currency) is None:
            raise ProspectiveApplicableCostError(
                "currency must be an uppercase three-letter currency"
            )
        if tuple(sorted(self.applicability_evidence_ids)) != self.applicability_evidence_ids:
            raise ProspectiveApplicableCostError(
                "applicability_evidence_ids must be sorted"
            )
        if len(set(self.applicability_evidence_ids)) != len(
            self.applicability_evidence_ids
        ):
            raise ProspectiveApplicableCostError(
                "applicability_evidence_ids must be unique"
            )
        if tuple(sorted(self.cost_evidence_ids)) != self.cost_evidence_ids:
            raise ProspectiveApplicableCostError("cost_evidence_ids must be sorted")
        if len(set(self.cost_evidence_ids)) != len(self.cost_evidence_ids):
            raise ProspectiveApplicableCostError("cost_evidence_ids must be unique")
        if tuple(sorted(self.incomplete_reasons)) != self.incomplete_reasons:
            raise ProspectiveApplicableCostError(
                "incomplete_reasons must be sorted"
            )
        if self.completeness is ProspectiveCostCompleteness.COMPLETE:
            _finite_nonnegative(
                self.total_subtractable_amount,
                "total_subtractable_amount",
            )
            if self.incomplete_reasons:
                raise ProspectiveApplicableCostError(
                    "complete resolution cannot carry incomplete reasons"
                )
        else:
            if self.total_subtractable_amount is not None:
                raise ProspectiveApplicableCostError(
                    "incomplete resolution cannot expose a trusted total"
                )

    @property
    def proof_id(self) -> str:
        return _digest(self.to_dict(include_proof_id=False))

    def to_dict(self, *, include_proof_id: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": "autosport.prospective_applicable_cost_resolution",
            "schema_version": SCHEMA_VERSION,
            "plan_id": self.plan_id,
            "opportunity_set_id": self.opportunity_set_id,
            "decision_at": _datetime_text(self.decision_at),
            "currency": self.currency,
            "applicability_evidence_ids": list(self.applicability_evidence_ids),
            "cost_evidence_ids": list(self.cost_evidence_ids),
            "total_subtractable_amount": (
                None
                if self.total_subtractable_amount is None
                else str(self.total_subtractable_amount)
            ),
            "completeness": self.completeness.value,
            "incomplete_reasons": list(self.incomplete_reasons),
        }
        if include_proof_id:
            payload["proof_id"] = _digest(payload)
        return payload


def resolve_prospective_applicable_costs(
    *,
    plan: PortfolioPlan,
    decision_at: datetime,
    currency: str,
    applicability: Sequence[CostApplicabilityEvidence],
    costs: Sequence[ProspectiveCostEvidence],
) -> ProspectiveApplicableCostResolution:
    """Resolve exact decision-time cost coverage without accepting caller completeness."""

    if not isinstance(plan, PortfolioPlan):
        raise ProspectiveApplicableCostError("plan must be a canonical PortfolioPlan")
    _utc(decision_at, "decision_at")
    if not isinstance(currency, str) or _CURRENCY_RE.fullmatch(currency) is None:
        raise ProspectiveApplicableCostError(
            "currency must be an uppercase three-letter currency"
        )

    plan_id = plan.plan_id
    reasons: set[str] = set()
    applicability_by_class: dict[CostClass, list[CostApplicabilityEvidence]] = {}
    costs_by_class: dict[CostClass, list[ProspectiveCostEvidence]] = {}

    for item in applicability:
        if not isinstance(item, CostApplicabilityEvidence):
            raise ProspectiveApplicableCostError(
                "applicability must contain CostApplicabilityEvidence values"
            )
        applicability_by_class.setdefault(item.cost_class, []).append(item)
        if item.plan_id != plan_id:
            reasons.add(f"applicability-plan-mismatch:{item.cost_class.value}")

    for item in costs:
        if not isinstance(item, ProspectiveCostEvidence):
            raise ProspectiveApplicableCostError(
                "costs must contain ProspectiveCostEvidence values"
            )
        costs_by_class.setdefault(item.cost_class, []).append(item)
        if item.plan_id != plan_id:
            reasons.add(f"cost-plan-mismatch:{item.cost_class.value}")

    subtractable_total = Decimal("0")
    applicability_ids: list[str] = []
    cost_ids: list[str] = []

    for cost_class in REQUIRED_COST_CLASSES:
        applicability_items = applicability_by_class.get(cost_class, [])
        if len(applicability_items) == 0:
            reasons.add(f"missing-applicability:{cost_class.value}")
            continue
        if len(applicability_items) != 1:
            reasons.add(f"conflicting-applicability:{cost_class.value}")
            applicability_ids.extend(item.evidence_id for item in applicability_items)
            continue

        applicability_item = applicability_items[0]
        applicability_ids.append(applicability_item.evidence_id)
        if not (
            applicability_item.observed_at <= decision_at
            and applicability_item.available_at <= decision_at
            and decision_at <= applicability_item.valid_until
        ):
            reasons.add(f"stale-or-future-applicability:{cost_class.value}")
            continue

        class_costs = costs_by_class.get(cost_class, [])
        if not applicability_item.applicable:
            if class_costs:
                reasons.add(f"cost-for-not-applicable:{cost_class.value}")
                cost_ids.extend(item.evidence_id for item in class_costs)
            continue

        if len(class_costs) == 0:
            reasons.add(f"missing-cost:{cost_class.value}")
            continue
        if len(class_costs) != 1:
            reasons.add(f"conflicting-cost:{cost_class.value}")
            cost_ids.extend(item.evidence_id for item in class_costs)
            continue

        cost = class_costs[0]
        cost_ids.append(cost.evidence_id)
        if cost.plan_id != plan_id:
            continue
        if not (
            cost.observed_at <= decision_at
            and cost.available_at <= decision_at
            and decision_at <= cost.valid_until
        ):
            reasons.add(f"stale-or-future-cost:{cost_class.value}")
            continue
        if cost.currency != currency:
            reasons.add(f"currency-mismatch:{cost_class.value}")
            continue
        if cost.basis is not CostBasis.AUTHORITATIVE_DECLARATION:
            reasons.add(f"non-authoritative-cost-basis:{cost_class.value}")
            continue
        if cost.treatment is CostTreatment.INFORMATIONAL:
            reasons.add(f"non-economic-treatment:{cost_class.value}")
            continue
        if cost.treatment is CostTreatment.SUBTRACT_FROM_GROSS:
            subtractable_total += cost.amount
        elif cost.treatment is CostTreatment.EMBEDDED_IN_GROSS:
            pass
        else:  # defensive against future enum expansion
            reasons.add(f"unsupported-treatment:{cost_class.value}")

    unexpected_applicability = set(applicability_by_class) - set(REQUIRED_COST_CLASSES)
    unexpected_costs = set(costs_by_class) - set(REQUIRED_COST_CLASSES)
    for cost_class in unexpected_applicability:
        reasons.add(f"unexpected-applicability:{cost_class.value}")
    for cost_class in unexpected_costs:
        reasons.add(f"unexpected-cost:{cost_class.value}")

    incomplete_reasons = tuple(sorted(reasons))
    completeness = (
        ProspectiveCostCompleteness.COMPLETE
        if not incomplete_reasons
        else ProspectiveCostCompleteness.INCOMPLETE
    )
    return ProspectiveApplicableCostResolution._from_resolver(
        plan_id=plan_id,
        opportunity_set_id=plan.opportunity_set.opportunity_set_id,
        decision_at=decision_at,
        currency=currency,
        applicability_evidence_ids=tuple(sorted(set(applicability_ids))),
        cost_evidence_ids=tuple(sorted(set(cost_ids))),
        total_subtractable_amount=(
            subtractable_total
            if completeness is ProspectiveCostCompleteness.COMPLETE
            else None
        ),
        completeness=completeness,
        incomplete_reasons=incomplete_reasons,
        _token=_RESOLUTION_TOKEN,
    )
