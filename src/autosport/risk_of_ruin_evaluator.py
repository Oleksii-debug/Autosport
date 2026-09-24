"""Fixed-N risk-of-ruin estimator with a fail-closed product-issuance boundary.

The pure estimator derives a conservative probability upper bound from raw
bankroll-path observations and never accepts a caller-supplied final upper bound.
Raw requests remain assertion-only: this module does not currently have a
product-owned observation/dataset/independence resolver, so a caller-constructed
request cannot be durably promoted to product authority.

The only estimator currently qualified here is a one-sided exact
Clopper-Pearson bound under a pre-registered fixed-N independent Bernoulli-trial
contract. Legacy durable journal bytes remain parseable only as quarantined
audit history; both positive issuance and positive re-resolution stay closed
until canonical upstream input authority is composed.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import (
    Decimal,
    DivisionByZero,
    InvalidOperation,
    MAX_EMAX,
    MIN_EMIN,
    Overflow,
    ROUND_CEILING,
    ROUND_HALF_EVEN,
    localcontext,
)
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

from .integrity import atomic_write_json
from .monotonic_workspace_authority import (
    MonotonicWorkspaceAuthority,
    MonotonicWorkspaceAuthorityError,
)
from .workspace_lock import WorkspaceEconomicLock


_SCHEMA = "autosport.risk-of-ruin-product-evaluator.v1"
_JOURNAL_SCHEMA = "autosport.risk-of-ruin-product-evaluator-journal.v1"
_METHOD_ID = "clopper-pearson-one-sided-fixed-n-iid-v1"
_STOPPING_RULE = "fixed_n_preregistered_v1"
_INDEPENDENCE_CONTRACT = "distinct_preregistered_independent_units_v1"
_PRODUCER_IDENTITY = f"autosport:{_METHOD_ID}"
_AUTHORITY_DOMAIN = "risk-of-ruin-product-evaluator-v1"
_AUTHORITY_KEY = "issued-results-v1"
_JOURNAL_NAME = "risk-of-ruin-evaluator-v1.json"
_HEX = frozenset("0123456789abcdef")
_MAX_FIXED_POINT_MATERIALIZATION_LENGTH = 512
# Operational implementation support budget, not a statistical max-N or
# sample-adequacy rule. The current exact CP implementation performs 240
# high-precision bisection evaluations with O(k) recurrence work per step.
_MAX_SUPPORTED_FIXED_N_OBSERVATIONS = 10_000
_MAX_SUPPORTED_EVALUATED_STAKES = 10_000
_UNSUPPORTED_RESOURCE_DOMAIN = "UNSUPPORTED_RESOURCE_DOMAIN"
_CP_BASE_WORKING_PRECISION = 70
_CP_INPUT_SCALE_GUARD_DIGITS = 16
_CP_FINAL_PRECISION = 50


class RiskTargetKind(StrEnum):
    SINGLE = "single"
    VECTOR = "vector"


class RiskEvidenceClass(StrEnum):
    SYNTHETIC = "synthetic"
    HISTORICAL = "historical"
    PAPER = "paper"
    FORWARD_PAPER = "forward_paper"


class RiskOfRuinEvaluationError(ValueError):
    """The request is malformed, outside resource support, or statistically unqualified."""


class RiskOfRuinIssuanceError(RuntimeError):
    """Durable product issuance or re-resolution failed closed."""


def _text(value: object, name: str, *, max_length: int = 512) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise RiskOfRuinEvaluationError(f"{name} must be non-empty canonical text")
    if len(value) > max_length or "\x00" in value or any(ord(ch) < 32 for ch in value):
        raise RiskOfRuinEvaluationError(f"{name} contains unsupported characters")
    value.encode("utf-8", errors="strict")
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(ch not in _HEX for ch in text):
        raise RiskOfRuinEvaluationError(f"{name} must be a canonical SHA-256 digest")
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RiskOfRuinEvaluationError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RiskOfRuinEvaluationError(f"{name} must include a timezone offset")
    return parsed.astimezone(timezone.utc)


def _decimal(value: object, name: str) -> Decimal:
    # Decimal is a caller-facing authority boundary.  Decimal subclasses may
    # override is_finite(), as_tuple() and __format__(), so accepting them would
    # let virtual methods bypass the pre-materialization resource fence or make
    # canonical text/hash output depend on mutable caller state.
    if type(value) is not Decimal or not value.is_finite():
        raise RiskOfRuinEvaluationError(f"{name} must be a finite exact Decimal")
    return value


def _probability(value: object, name: str) -> Decimal:
    result = _decimal(value, name)
    if result <= 0 or result >= 1:
        raise RiskOfRuinEvaluationError(f"{name} must be strictly between 0 and 1")
    return result


def _require_supported_fixed_n_work_domain(independent_units: int) -> None:
    if independent_units > _MAX_SUPPORTED_FIXED_N_OBSERVATIONS:
        raise RiskOfRuinEvaluationError(
            f"{_UNSUPPORTED_RESOURCE_DOMAIN}: fixed-N computation exceeds "
            "the current implementation work budget; this is not a "
            "statistical validity or sample-adequacy judgment"
        )


def _fixed_point_materialization_length(value: Decimal) -> int:
    """Return format(value, "f") size without materializing that string."""

    value = _decimal(value, "decimal")
    if value.is_zero():
        return 1
    sign, digits, exponent = value.as_tuple()
    sign_length = 1 if sign else 0
    digit_count = len(digits)
    if exponent >= 0:
        return sign_length + digit_count + exponent
    if digit_count + exponent > 0:
        return sign_length + digit_count + 1
    return sign_length + 2 - exponent


def _decimal_text(value: Decimal) -> str:
    value = _decimal(value, "decimal")
    if value.is_zero():
        return "0"
    if (
        _fixed_point_materialization_length(value)
        > _MAX_FIXED_POINT_MATERIALIZATION_LENGTH
    ):
        raise RiskOfRuinEvaluationError(
            "decimal fixed-point representation exceeds supported canonical size"
        )
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _clopper_pearson_working_precision(confidence: Decimal) -> int:
    """Bound CP arithmetic precision to the accepted canonical Decimal domain."""

    text = _decimal_text(confidence)
    _, separator, fractional = text.partition(".")
    decimal_places = len(fractional) if separator else 0
    # Near 0/1, subtraction in alpha=1-confidence and q=1-p must preserve
    # the input's decimal scale *plus* the bisection working digits.  A fixed
    # 70-digit context loses that tail for legal values such as 1E-69.
    return (
        _CP_BASE_WORKING_PRECISION
        + decimal_places
        + _CP_INPUT_SCALE_GUARD_DIGITS
    )


def _decimal_from_payload(value: object, name: str) -> Decimal:
    text = _text(value, name)
    if "e" in text.lower():
        raise RiskOfRuinEvaluationError(
            f"{name} must use canonical fixed-point decimal text"
        )
    try:
        parsed = Decimal(text)
    except Exception as exc:
        raise RiskOfRuinEvaluationError(
            f"{name} must use canonical fixed-point decimal text"
        ) from exc
    if not parsed.is_finite() or _decimal_text(parsed) != text:
        raise RiskOfRuinEvaluationError(
            f"{name} must use canonical fixed-point decimal text"
        )
    return parsed


def _decimal_tuple_from_payload(value: object, name: str) -> tuple[Decimal, ...]:
    if type(value) is not list or not value:
        raise RiskOfRuinEvaluationError(f"{name} must be a non-empty JSON array")
    return tuple(
        _decimal_from_payload(item, f"{name} item")
        for item in value
    )


def _int_from_payload(
    value: object,
    name: str,
    *,
    minimum: int = 0,
) -> int:
    if type(value) is not int or value < minimum:
        raise RiskOfRuinEvaluationError(
            f"{name} must be a canonical integer >= {minimum}"
        )
    return value


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _payload_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _atomic_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _file_sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def evaluator_source_sha256() -> str:
    """Return the exact current evaluator source digest for scientific lineage."""
    try:
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    except OSError as exc:
        raise RiskOfRuinIssuanceError(
            "risk-of-ruin evaluator source bytes are not readable"
        ) from exc


@dataclass(frozen=True, slots=True)
class RiskPathObservation:
    """One pre-registered independent bankroll path observation."""

    independent_unit_id: str
    dependence_group_id: str
    minimum_equity: Decimal
    outcome_available_at: str
    source_evidence_sha256: str

    def __post_init__(self) -> None:
        _text(self.independent_unit_id, "independent_unit_id")
        _text(self.dependence_group_id, "dependence_group_id")
        _decimal_text(self.minimum_equity)
        _instant(self.outcome_available_at, "outcome_available_at")
        _sha256(self.source_evidence_sha256, "source_evidence_sha256")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "independent_unit_id": self.independent_unit_id,
            "dependence_group_id": self.dependence_group_id,
            "minimum_equity": _decimal_text(self.minimum_equity),
            "outcome_available_at": _instant(
                self.outcome_available_at, "outcome_available_at"
            ).isoformat(),
            "source_evidence_sha256": self.source_evidence_sha256.lower(),
        }


@dataclass(frozen=True, slots=True)
class RiskOfRuinEvaluationRequest:
    """Frozen evaluator inputs. There is intentionally no upper_bound field."""

    target_kind: RiskTargetKind
    bankroll_id: str
    currency: str
    base_portfolio_sha256: str
    capital_state_sha256: str
    target_sha256: str
    evaluated_stakes: tuple[Decimal, ...]
    research_protocol_sha256: str
    reproducibility_bundle_sha256: str
    dataset_snapshot_id: str
    dataset_manifest_sha256: str
    causal_cutoff: str
    evaluated_at: str
    confidence_level: Decimal
    ruin_threshold: Decimal
    planned_independent_units: int
    evidence_class: RiskEvidenceClass
    observations: tuple[RiskPathObservation, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.target_kind, RiskTargetKind):
            raise RiskOfRuinEvaluationError("target_kind must be RiskTargetKind")
        if not isinstance(self.evidence_class, RiskEvidenceClass):
            raise RiskOfRuinEvaluationError("evidence_class must be RiskEvidenceClass")
        _text(self.bankroll_id, "bankroll_id")
        currency = _text(self.currency, "currency")
        if (
            len(currency) != 3
            or not currency.isascii()
            or not currency.isalpha()
            or currency != currency.upper()
        ):
            raise RiskOfRuinEvaluationError(
                "currency must be a three-letter uppercase ASCII code"
            )
        for name in (
            "base_portfolio_sha256",
            "capital_state_sha256",
            "target_sha256",
            "research_protocol_sha256",
            "reproducibility_bundle_sha256",
            "dataset_manifest_sha256",
        ):
            _sha256(getattr(self, name), name)
        _text(self.dataset_snapshot_id, "dataset_snapshot_id")
        cutoff = _instant(self.causal_cutoff, "causal_cutoff")
        evaluated = _instant(self.evaluated_at, "evaluated_at")
        if cutoff > evaluated:
            raise RiskOfRuinEvaluationError(
                "causal_cutoff must not be after evaluated_at"
            )
        _probability(self.confidence_level, "confidence_level")
        # Keep the public request and direct estimator on the same bounded
        # canonical Decimal domain before any precision is allocated from scale.
        _decimal_text(self.confidence_level)
        _decimal_text(self.ruin_threshold)
        if (
            isinstance(self.planned_independent_units, bool)
            or not isinstance(self.planned_independent_units, int)
            or self.planned_independent_units <= 0
        ):
            raise RiskOfRuinEvaluationError(
                "planned_independent_units must be a positive integer"
            )
        _require_supported_fixed_n_work_domain(
            self.planned_independent_units
        )
        if type(self.evaluated_stakes) is not tuple or not self.evaluated_stakes:
            raise RiskOfRuinEvaluationError(
                "evaluated_stakes must be a non-empty tuple"
            )
        if len(self.evaluated_stakes) > _MAX_SUPPORTED_EVALUATED_STAKES:
            raise RiskOfRuinEvaluationError(
                f"{_UNSUPPORTED_RESOURCE_DOMAIN}: evaluated stake vector "
                "exceeds the current implementation work budget; this is not "
                "a statistical validity or sample-adequacy judgment"
            )
        for stake in self.evaluated_stakes:
            if _decimal(stake, "evaluated_stake") <= 0:
                raise RiskOfRuinEvaluationError(
                    "evaluated stakes must be positive exact Decimals"
                )
            _decimal_text(stake)
        if self.target_kind is RiskTargetKind.SINGLE and len(self.evaluated_stakes) != 1:
            raise RiskOfRuinEvaluationError(
                "single target requires exactly one evaluated stake"
            )
        if type(self.observations) is not tuple or not self.observations:
            raise RiskOfRuinEvaluationError("observations must be a non-empty tuple")
        if len(self.observations) > _MAX_SUPPORTED_FIXED_N_OBSERVATIONS:
            raise RiskOfRuinEvaluationError(
                f"{_UNSUPPORTED_RESOURCE_DOMAIN}: observation cohort exceeds "
                "the current implementation work budget; this is not a "
                "statistical validity or sample-adequacy judgment"
            )
        if any(type(item) is not RiskPathObservation for item in self.observations):
            raise RiskOfRuinEvaluationError(
                "observations must contain exact RiskPathObservation values"
            )
        if len(self.observations) != self.planned_independent_units:
            raise RiskOfRuinEvaluationError(
                "fixed-N evaluation requires exactly the pre-registered unit count"
            )
        unit_ids = tuple(item.independent_unit_id for item in self.observations)
        groups = tuple(item.dependence_group_id for item in self.observations)
        evidence_ids = tuple(
            item.source_evidence_sha256.lower() for item in self.observations
        )
        if len(unit_ids) != len(set(unit_ids)):
            raise RiskOfRuinEvaluationError(
                "independent_unit_id values must be unique"
            )
        if len(groups) != len(set(groups)):
            raise RiskOfRuinEvaluationError(
                "fixed-N IID evaluation cannot reuse a dependence group"
            )
        if len(evidence_ids) != len(set(evidence_ids)):
            raise RiskOfRuinEvaluationError(
                "fixed-N IID evaluation requires unique source evidence identity"
            )
        for item in self.observations:
            if _instant(item.outcome_available_at, "outcome_available_at") > cutoff:
                raise RiskOfRuinEvaluationError(
                    "all consumed outcomes must be causally available by causal_cutoff"
                )

    @property
    def request_sha256(self) -> str:
        return _payload_sha256(self.canonical_payload())

    @property
    def observation_manifest_sha256(self) -> str:
        payload = {
            "observations": [
                item.canonical_payload()
                for item in sorted(
                    self.observations, key=lambda item: item.independent_unit_id
                )
            ]
        }
        return _payload_sha256(payload)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema": _SCHEMA,
            "request_version": 1,
            "target_kind": self.target_kind.value,
            "bankroll_id": self.bankroll_id,
            "currency": self.currency,
            "base_portfolio_sha256": self.base_portfolio_sha256.lower(),
            "capital_state_sha256": self.capital_state_sha256.lower(),
            "target_sha256": self.target_sha256.lower(),
            "evaluated_stakes": [
                _decimal_text(value) for value in self.evaluated_stakes
            ],
            "research_protocol_sha256": self.research_protocol_sha256.lower(),
            "reproducibility_bundle_sha256": self.reproducibility_bundle_sha256.lower(),
            "dataset_snapshot_id": self.dataset_snapshot_id,
            "dataset_manifest_sha256": self.dataset_manifest_sha256.lower(),
            "causal_cutoff": _instant(
                self.causal_cutoff, "causal_cutoff"
            ).isoformat(),
            "evaluated_at": _instant(self.evaluated_at, "evaluated_at").isoformat(),
            "confidence_level": _decimal_text(self.confidence_level),
            "ruin_threshold": _decimal_text(self.ruin_threshold),
            "planned_independent_units": self.planned_independent_units,
            "evidence_class": self.evidence_class.value,
            "stopping_rule": _STOPPING_RULE,
            "independence_contract": _INDEPENDENCE_CONTRACT,
            "observations": [
                item.canonical_payload()
                for item in sorted(
                    self.observations, key=lambda item: item.independent_unit_id
                )
            ],
        }


def _binomial_cdf(k: int, n: int, p: Decimal) -> Decimal:
    if k < 0:
        return Decimal(0)
    if k >= n:
        return Decimal(1)
    if p <= 0:
        return Decimal(1)
    if p >= 1:
        return Decimal(0)
    q = Decimal(1) - p
    term = q ** n
    total = term
    ratio = p / q
    for i in range(1, k + 1):
        term *= Decimal(n - i + 1) / Decimal(i)
        term *= ratio
        total += term
    if total < 0:
        return Decimal(0)
    if total > 1:
        return Decimal(1)
    return total


def clopper_pearson_upper_bound(
    *,
    ruin_count: int,
    independent_units: int,
    confidence_level: Decimal,
) -> Decimal:
    """One-sided exact binomial upper confidence bound.

    The result is valid only for the fixed-N independent Bernoulli contract
    enforced by RiskOfRuinEvaluationRequest.
    """

    if (
        isinstance(independent_units, bool)
        or not isinstance(independent_units, int)
        or independent_units <= 0
    ):
        raise RiskOfRuinEvaluationError(
            "independent_units must be a positive integer"
        )
    if (
        isinstance(ruin_count, bool)
        or not isinstance(ruin_count, int)
        or ruin_count < 0
        or ruin_count > independent_units
    ):
        raise RiskOfRuinEvaluationError(
            "ruin_count must be an integer inside [0, independent_units]"
        )
    confidence = _probability(confidence_level, "confidence_level")
    working_precision = _clopper_pearson_working_precision(confidence)
    if ruin_count == independent_units:
        return Decimal(1)
    _require_supported_fixed_n_work_domain(independent_units)

    with localcontext() as context:
        # This is a scientific arithmetic boundary, not an ambient process-context
        # boundary.  A caller may legitimately change Decimal precision, rounding,
        # exponent limits or traps elsewhere in the process; none of those settings
        # may move an exact confidence endpoint inward.
        context.prec = working_precision
        context.rounding = ROUND_HALF_EVEN
        context.Emin = MIN_EMIN
        context.Emax = MAX_EMAX
        context.capitals = 1
        context.clamp = 0
        for signal in tuple(context.traps):
            context.traps[signal] = False
        context.traps[InvalidOperation] = True
        context.traps[DivisionByZero] = True
        context.traps[Overflow] = True
        context.clear_flags()

        alpha = Decimal(1) - confidence
        low = Decimal(0)
        high = Decimal(1)
        for _ in range(240):
            middle = (low + high) / Decimal(2)
            cdf = _binomial_cdf(ruin_count, independent_units, middle)
            if cdf > alpha:
                low = middle
            else:
                high = middle
        # The bisection invariant keeps high on the conservative side.
        # Final public precision must therefore round outward, never back through
        # the mathematical endpoint.
        context.prec = _CP_FINAL_PRECISION
        context.rounding = ROUND_CEILING
        return +high


@dataclass(frozen=True, slots=True)
class IssuedRiskOfRuinResult:
    workspace_instance_id: str
    result_id: str
    request_sha256: str
    target_kind: RiskTargetKind
    bankroll_id: str
    currency: str
    base_portfolio_sha256: str
    capital_state_sha256: str
    target_sha256: str
    evaluated_stakes: tuple[Decimal, ...]
    research_protocol_sha256: str
    reproducibility_bundle_sha256: str
    dataset_snapshot_id: str
    dataset_manifest_sha256: str
    observation_manifest_sha256: str
    causal_cutoff: str
    evaluated_at: str
    issued_at: str
    evidence_class: RiskEvidenceClass
    method_id: str
    evaluator_source_sha256: str
    stopping_rule: str
    independence_contract: str
    confidence_level: Decimal
    ruin_threshold: Decimal
    independent_units: int
    ruin_count: int
    upper_bound: Decimal

    @property
    def producer_identity(self) -> str:
        return _PRODUCER_IDENTITY

    @property
    def real_money_execution_authority(self) -> bool:
        return False

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema": _SCHEMA,
            "result_version": 1,
            "workspace_instance_id": self.workspace_instance_id,
            "result_id": self.result_id,
            "request_sha256": self.request_sha256,
            "target_kind": self.target_kind.value,
            "bankroll_id": self.bankroll_id,
            "currency": self.currency,
            "base_portfolio_sha256": self.base_portfolio_sha256,
            "capital_state_sha256": self.capital_state_sha256,
            "target_sha256": self.target_sha256,
            "evaluated_stakes": [_decimal_text(x) for x in self.evaluated_stakes],
            "research_protocol_sha256": self.research_protocol_sha256,
            "reproducibility_bundle_sha256": self.reproducibility_bundle_sha256,
            "dataset_snapshot_id": self.dataset_snapshot_id,
            "dataset_manifest_sha256": self.dataset_manifest_sha256,
            "observation_manifest_sha256": self.observation_manifest_sha256,
            "causal_cutoff": self.causal_cutoff,
            "evaluated_at": self.evaluated_at,
            "issued_at": self.issued_at,
            "evidence_class": self.evidence_class.value,
            "method_id": self.method_id,
            "evaluator_source_sha256": self.evaluator_source_sha256,
            "stopping_rule": self.stopping_rule,
            "independence_contract": self.independence_contract,
            "confidence_level": _decimal_text(self.confidence_level),
            "ruin_threshold": _decimal_text(self.ruin_threshold),
            "independent_units": self.independent_units,
            "ruin_count": self.ruin_count,
            "upper_bound": _decimal_text(self.upper_bound),
            "producer_identity": self.producer_identity,
            "real_money_execution_authority": False,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "IssuedRiskOfRuinResult":
        expected_fields = {
            "schema",
            "result_version",
            "workspace_instance_id",
            "result_id",
            "request_sha256",
            "target_kind",
            "bankroll_id",
            "currency",
            "base_portfolio_sha256",
            "capital_state_sha256",
            "target_sha256",
            "evaluated_stakes",
            "research_protocol_sha256",
            "reproducibility_bundle_sha256",
            "dataset_snapshot_id",
            "dataset_manifest_sha256",
            "observation_manifest_sha256",
            "causal_cutoff",
            "evaluated_at",
            "issued_at",
            "evidence_class",
            "method_id",
            "evaluator_source_sha256",
            "stopping_rule",
            "independence_contract",
            "confidence_level",
            "ruin_threshold",
            "independent_units",
            "ruin_count",
            "upper_bound",
            "producer_identity",
            "real_money_execution_authority",
        }
        if set(payload) != expected_fields:
            raise RiskOfRuinIssuanceError("risk-of-ruin result fields mismatch")
        result_version = payload.get("result_version")
        if (
            payload.get("schema") != _SCHEMA
            or type(result_version) is not int
            or result_version != 1
        ):
            raise RiskOfRuinIssuanceError("unsupported risk-of-ruin result schema")
        try:
            result = cls(
                workspace_instance_id=_text(
                    payload["workspace_instance_id"], "workspace_instance_id"
                ),
                result_id=_sha256(payload["result_id"], "result_id"),
                request_sha256=_sha256(payload["request_sha256"], "request_sha256"),
                target_kind=RiskTargetKind(payload["target_kind"]),
                bankroll_id=_text(payload["bankroll_id"], "bankroll_id"),
                currency=_text(payload["currency"], "currency"),
                base_portfolio_sha256=_sha256(
                    payload["base_portfolio_sha256"], "base_portfolio_sha256"
                ),
                capital_state_sha256=_sha256(
                    payload["capital_state_sha256"], "capital_state_sha256"
                ),
                target_sha256=_sha256(payload["target_sha256"], "target_sha256"),
                evaluated_stakes=_decimal_tuple_from_payload(
                    payload["evaluated_stakes"], "evaluated_stakes"
                ),
                research_protocol_sha256=_sha256(
                    payload["research_protocol_sha256"], "research_protocol_sha256"
                ),
                reproducibility_bundle_sha256=_sha256(
                    payload["reproducibility_bundle_sha256"],
                    "reproducibility_bundle_sha256",
                ),
                dataset_snapshot_id=_text(
                    payload["dataset_snapshot_id"], "dataset_snapshot_id"
                ),
                dataset_manifest_sha256=_sha256(
                    payload["dataset_manifest_sha256"], "dataset_manifest_sha256"
                ),
                observation_manifest_sha256=_sha256(
                    payload["observation_manifest_sha256"],
                    "observation_manifest_sha256",
                ),
                causal_cutoff=_instant(
                    payload["causal_cutoff"], "causal_cutoff"
                ).isoformat(),
                evaluated_at=_instant(
                    payload["evaluated_at"], "evaluated_at"
                ).isoformat(),
                issued_at=_instant(payload["issued_at"], "issued_at").isoformat(),
                evidence_class=RiskEvidenceClass(payload["evidence_class"]),
                method_id=_text(payload["method_id"], "method_id"),
                evaluator_source_sha256=_sha256(
                    payload["evaluator_source_sha256"], "evaluator_source_sha256"
                ),
                stopping_rule=_text(payload["stopping_rule"], "stopping_rule"),
                independence_contract=_text(
                    payload["independence_contract"], "independence_contract"
                ),
                confidence_level=_decimal_from_payload(
                    payload["confidence_level"], "confidence_level"
                ),
                ruin_threshold=_decimal_from_payload(
                    payload["ruin_threshold"], "ruin_threshold"
                ),
                independent_units=_int_from_payload(
                    payload["independent_units"], "independent_units", minimum=1
                ),
                ruin_count=_int_from_payload(
                    payload["ruin_count"], "ruin_count", minimum=0
                ),
                upper_bound=_decimal_from_payload(
                    payload["upper_bound"], "upper_bound"
                ),
            )
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise RiskOfRuinIssuanceError(
                "invalid durable risk-of-ruin result payload"
            ) from exc
        if payload.get("producer_identity") != _PRODUCER_IDENTITY:
            raise RiskOfRuinIssuanceError("risk-of-ruin producer identity mismatch")
        if payload.get("real_money_execution_authority") is not False:
            raise RiskOfRuinIssuanceError(
                "risk-of-ruin result cannot carry real-money authority"
            )
        if result.method_id != _METHOD_ID:
            raise RiskOfRuinIssuanceError("risk-of-ruin method identity mismatch")
        if result.stopping_rule != _STOPPING_RULE:
            raise RiskOfRuinIssuanceError("risk-of-ruin stopping rule mismatch")
        if result.independence_contract != _INDEPENDENCE_CONTRACT:
            raise RiskOfRuinIssuanceError(
                "risk-of-ruin independence contract mismatch"
            )
        if result.ruin_count < 0 or result.ruin_count > result.independent_units:
            raise RiskOfRuinIssuanceError("risk-of-ruin count is invalid")
        expected = clopper_pearson_upper_bound(
            ruin_count=result.ruin_count,
            independent_units=result.independent_units,
            confidence_level=result.confidence_level,
        )
        if result.upper_bound != expected:
            raise RiskOfRuinIssuanceError(
                "durable risk-of-ruin upper bound does not rederive exactly"
            )
        material = dict(result.canonical_payload())
        material.pop("result_id")
        expected_id = _payload_sha256(material)
        if result.result_id != expected_id:
            raise RiskOfRuinIssuanceError("risk-of-ruin result_id mismatch")
        return result


def evaluate_risk_of_ruin(
    request: RiskOfRuinEvaluationRequest,
    *,
    workspace_instance_id: str,
    issued_at: str,
    source_sha256: str,
) -> IssuedRiskOfRuinResult:
    """Derive the exact result from raw path observations; no final bound input exists."""

    if type(request) is not RiskOfRuinEvaluationRequest:
        raise TypeError("request must be exact RiskOfRuinEvaluationRequest")
    workspace_instance_id = _text(workspace_instance_id, "workspace_instance_id")
    issued = _instant(issued_at, "issued_at")
    evaluated = _instant(request.evaluated_at, "evaluated_at")
    if issued < evaluated:
        raise RiskOfRuinEvaluationError(
            "product issuance cannot precede evaluation completion"
        )
    source_sha256 = _sha256(source_sha256, "evaluator_source_sha256")
    ordered = tuple(
        sorted(request.observations, key=lambda item: item.independent_unit_id)
    )
    ruin_count = sum(
        item.minimum_equity <= request.ruin_threshold for item in ordered
    )
    upper = clopper_pearson_upper_bound(
        ruin_count=ruin_count,
        independent_units=len(ordered),
        confidence_level=request.confidence_level,
    )
    common = {
        "workspace_instance_id": workspace_instance_id,
        "request_sha256": request.request_sha256,
        "target_kind": request.target_kind,
        "bankroll_id": request.bankroll_id,
        "currency": request.currency,
        "base_portfolio_sha256": request.base_portfolio_sha256.lower(),
        "capital_state_sha256": request.capital_state_sha256.lower(),
        "target_sha256": request.target_sha256.lower(),
        "evaluated_stakes": request.evaluated_stakes,
        "research_protocol_sha256": request.research_protocol_sha256.lower(),
        "reproducibility_bundle_sha256": request.reproducibility_bundle_sha256.lower(),
        "dataset_snapshot_id": request.dataset_snapshot_id,
        "dataset_manifest_sha256": request.dataset_manifest_sha256.lower(),
        "observation_manifest_sha256": request.observation_manifest_sha256,
        "causal_cutoff": _instant(request.causal_cutoff, "causal_cutoff").isoformat(),
        "evaluated_at": evaluated.isoformat(),
        "issued_at": issued.isoformat(),
        "evidence_class": request.evidence_class,
        "method_id": _METHOD_ID,
        "evaluator_source_sha256": source_sha256,
        "stopping_rule": _STOPPING_RULE,
        "independence_contract": _INDEPENDENCE_CONTRACT,
        "confidence_level": request.confidence_level,
        "ruin_threshold": request.ruin_threshold,
        "independent_units": len(ordered),
        "ruin_count": ruin_count,
        "upper_bound": upper,
    }
    provisional = IssuedRiskOfRuinResult(result_id="0" * 64, **common)
    material = dict(provisional.canonical_payload())
    material.pop("result_id")
    result_id = _payload_sha256(material)
    return IssuedRiskOfRuinResult(result_id=result_id, **common)


def _record_payload(
    result: IssuedRiskOfRuinResult,
    *,
    previous_record_sha256: str | None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "result": result.canonical_payload(),
        "previous_record_sha256": previous_record_sha256,
        "authority_tx_id": f"risk-eval-{result.result_id}",
        "semantic_binding_sha256": _payload_sha256(
            {
                "schema": _SCHEMA,
                "result_id": result.result_id,
                "request_sha256": result.request_sha256,
                "workspace_instance_id": result.workspace_instance_id,
            }
        ),
    }
    payload["record_sha256"] = _payload_sha256(payload)
    return payload


def _validate_journal(
    state: object,
    *,
    workspace_instance_id: str,
) -> tuple[dict[str, object], ...]:
    if type(state) is not dict:
        raise RiskOfRuinIssuanceError("risk-of-ruin journal must be an object")
    if set(state) != {
        "schema",
        "schema_version",
        "workspace_instance_id",
        "records",
    }:
        raise RiskOfRuinIssuanceError("risk-of-ruin journal fields mismatch")
    schema_version = state.get("schema_version")
    if (
        state.get("schema") != _JOURNAL_SCHEMA
        or type(schema_version) is not int
        or schema_version != 1
        or state.get("workspace_instance_id") != workspace_instance_id
    ):
        raise RiskOfRuinIssuanceError("risk-of-ruin journal identity mismatch")
    records = state.get("records")
    if type(records) is not list:
        raise RiskOfRuinIssuanceError("risk-of-ruin journal records must be a list")
    previous: str | None = None
    seen_results: set[str] = set()
    seen_requests: set[str] = set()
    validated: list[dict[str, object]] = []
    for raw in records:
        if type(raw) is not dict:
            raise RiskOfRuinIssuanceError("risk-of-ruin journal record is invalid")
        if set(raw) != {
            "result",
            "previous_record_sha256",
            "authority_tx_id",
            "semantic_binding_sha256",
            "record_sha256",
        }:
            raise RiskOfRuinIssuanceError("risk-of-ruin journal record fields mismatch")
        if raw["previous_record_sha256"] != previous:
            raise RiskOfRuinIssuanceError("risk-of-ruin journal chain is broken")
        expected = dict(raw)
        record_sha = expected.pop("record_sha256")
        if _sha256(record_sha, "record_sha256") != _payload_sha256(expected):
            raise RiskOfRuinIssuanceError("risk-of-ruin journal record digest mismatch")
        result = IssuedRiskOfRuinResult.from_payload(raw["result"])
        if result.workspace_instance_id != workspace_instance_id:
            raise RiskOfRuinIssuanceError(
                "risk-of-ruin result belongs to another workspace"
            )
        expected_tx = f"risk-eval-{result.result_id}"
        if raw["authority_tx_id"] != expected_tx:
            raise RiskOfRuinIssuanceError("risk-of-ruin authority transaction mismatch")
        expected_binding = _payload_sha256(
            {
                "schema": _SCHEMA,
                "result_id": result.result_id,
                "request_sha256": result.request_sha256,
                "workspace_instance_id": result.workspace_instance_id,
            }
        )
        if raw["semantic_binding_sha256"] != expected_binding:
            raise RiskOfRuinIssuanceError("risk-of-ruin semantic binding mismatch")
        if result.result_id in seen_results or result.request_sha256 in seen_requests:
            raise RiskOfRuinIssuanceError(
                "risk-of-ruin journal contains duplicate result/request identity"
            )
        seen_results.add(result.result_id)
        seen_requests.add(result.request_sha256)
        previous = record_sha
        validated.append(raw)
    return tuple(validated)


class ProductRiskOfRuinEvaluator:
    """Estimator with fail-closed positive issuance and durable-read quarantine.

    The legacy journal remains parseable as non-authoritative audit history, but
    no durable record is product-issued while the canonical positive producer is
    missing. Both issuance and positive re-resolution therefore remain closed
    until product-owned observation/provenance and IID/dependence authority exists.
    """

    def __init__(
        self,
        *,
        workspace: str | Path,
        authority_root: str | Path | None = None,
    ) -> None:
        path = Path(workspace).expanduser()
        if not path.is_absolute():
            raise RiskOfRuinIssuanceError("workspace must be an absolute path")
        path.mkdir(parents=True, exist_ok=True)
        self.workspace = path
        self.journal_path = path / _JOURNAL_NAME
        try:
            self.authority = MonotonicWorkspaceAuthority(
                workspace=path,
                domain=_AUTHORITY_DOMAIN,
                key=_AUTHORITY_KEY,
                authority_root=authority_root,
            )
        except MonotonicWorkspaceAuthorityError as exc:
            raise RiskOfRuinIssuanceError(
                "risk-of-ruin monotonic authority is unavailable"
            ) from exc

    def _empty_state(self) -> dict[str, object]:
        return {
            "schema": _JOURNAL_SCHEMA,
            "schema_version": 1,
            "workspace_instance_id": self.authority.workspace_instance_id,
            "records": [],
        }

    def _read_state_under_lock(self) -> tuple[dict[str, object], tuple[dict[str, object], ...]]:
        observed = _file_sha256(self.journal_path)
        if observed is None:
            state = self._empty_state()
            records: tuple[dict[str, object], ...] = ()
        else:
            try:
                state = json.loads(self.journal_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise RiskOfRuinIssuanceError(
                    "risk-of-ruin journal is unreadable"
                ) from exc
            records = _validate_journal(
                state,
                workspace_instance_id=self.authority.workspace_instance_id,
            )
        tx_id = None
        binding = None
        if records:
            tx_id = records[-1]["authority_tx_id"]
            binding = records[-1]["semantic_binding_sha256"]
        try:
            recovery = self.authority.recover(
                observed_state_sha256=observed,
                tx_id=tx_id,
                semantic_binding_sha256=binding,
            )
        except MonotonicWorkspaceAuthorityError as exc:
            raise RiskOfRuinIssuanceError(
                "risk-of-ruin journal is stale, copied, deleted or unproven"
            ) from exc
        if recovery.committed_state_sha256 != observed:
            if observed is not None or recovery.committed_state_sha256 is not None:
                raise RiskOfRuinIssuanceError(
                    "risk-of-ruin journal/authority state mismatch"
                )
        return state, records

    def issue(
        self,
        request: RiskOfRuinEvaluationRequest,
    ) -> IssuedRiskOfRuinResult:
        """Refuse to promote caller-owned input assertions to product authority.

        RiskOfRuinEvaluationRequest remains useful as the pure estimator input
        contract. It is not, by itself, evidence that Autosport observed the paths,
        froze the dataset before outcomes, or established the declared
        independence/dependence structure. Until those facts can be re-resolved
        from a canonical product-owned producer, durable issuance remains closed.
        """
        if type(request) is not RiskOfRuinEvaluationRequest:
            raise TypeError("request must be exact RiskOfRuinEvaluationRequest")
        raise RiskOfRuinIssuanceError(
            "product-issued risk-of-ruin requires canonical product-owned "
            "observation, dataset/provenance and independence authority; "
            "caller-constructed evaluation requests are assertion-only"
        )

    def resolve(self, result_id: str) -> IssuedRiskOfRuinResult:
        _sha256(result_id, "result_id")
        raise RiskOfRuinIssuanceError(
            "product-issued risk-of-ruin resolution is unavailable; "
            "pre-authority journal records are quarantined as audit history"
        )

    def verify(self, result: IssuedRiskOfRuinResult) -> bool:
        if type(result) is not IssuedRiskOfRuinResult:
            return False
        try:
            resolved = self.resolve(result.result_id)
        except (RiskOfRuinEvaluationError, RiskOfRuinIssuanceError):
            return False
        return resolved.canonical_payload() == result.canonical_payload()


__all__ = [
    "IssuedRiskOfRuinResult",
    "ProductRiskOfRuinEvaluator",
    "RiskEvidenceClass",
    "RiskOfRuinEvaluationError",
    "RiskOfRuinEvaluationRequest",
    "RiskOfRuinIssuanceError",
    "RiskPathObservation",
    "RiskTargetKind",
    "clopper_pearson_upper_bound",
    "evaluate_risk_of_ruin",
    "evaluator_source_sha256",
]
