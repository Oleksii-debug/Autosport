"""Durable paired shadow evidence for realized value-of-computation feedback."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

from .decision_ledger import DecisionLedgerIntegrityError, JsonlDecisionLedger
from .integrity import atomic_write_json
from .market_outcomes import MarketSettlementOutcomeAuthority
from .scientific_registry import ScientificRegistry
from .workspace_lock import WorkspaceEconomicLock

_SCHEMA = "autosport.voc_evaluation"
_VERSION = 2
_ZERO = Decimal("0")
_ONE = Decimal("1")
VOC_CURRENT_CONTEXT_ACTION = "VOC_ROUTE_CONTEXT"
VOC_CURRENT_CONTEXT_PAYLOAD_KEY = "voc_current_context"
_VOC_CURRENT_CONTEXT_FIELDS = frozenset(
    {
        "request_id",
        "task_class",
        "sport_id",
        "league_id",
        "regime_id",
        "urgency_id",
        "contradiction_state",
    }
)


class VOCEvaluationError(ValueError):
    """Raised when paired value-of-computation evidence is invalid or tampered."""


class VOCEvaluationProvenance(StrEnum):
    MEASURED_SHADOW = "MEASURED_SHADOW"
    SIMULATED = "SIMULATED"


def _text(name: str, value: object) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise VOCEvaluationError(f"{name} must be a non-empty canonical string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise VOCEvaluationError(f"{name} must be valid UTF-8") from exc
    return value


def _sha256(name: str, value: object) -> str:
    text = _text(name, value)
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise VOCEvaluationError(f"{name} must be SHA-256 hex")
    return text


def _instant(name: str, value: object) -> datetime:
    text = _text(name, value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise VOCEvaluationError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise VOCEvaluationError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _time(name: str, value: object) -> str:
    return _instant(name, value).isoformat().replace("+00:00", "Z")


def _decimal(name: str, value: object) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise VOCEvaluationError(f"{name} must be a finite Decimal")
    return value


def _nonnegative(name: str, value: object) -> Decimal:
    result = _decimal(name, value)
    if result < _ZERO:
        raise VOCEvaluationError(f"{name} must be non-negative")
    return result


def _fraction(name: str, value: object) -> Decimal:
    result = _decimal(name, value)
    if result <= _ZERO or result > _ONE:
        raise VOCEvaluationError(f"{name} must be in (0, 1]")
    return result


def _canonical_digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def resolve_voc_decision_context(
    decision_ledger: JsonlDecisionLedger,
    *,
    context_sha256: str,
    as_of: str,
) -> dict[str, str]:
    """Resolve one pre-request VOC context from the verified DecisionLedger."""
    if not isinstance(decision_ledger, JsonlDecisionLedger):
        raise TypeError("decision_ledger must be JsonlDecisionLedger")
    expected_digest = _sha256("context_sha256", context_sha256)
    cutoff = _instant("as_of", as_of)
    try:
        records = decision_ledger.verified_records()
    except DecisionLedgerIntegrityError as exc:
        raise VOCEvaluationError(
            "canonical current VOC DecisionLedger verification failed"
        ) from exc
    matches = [
        record
        for record in records
        if _canonical_digest(record.to_dict()) == expected_digest
    ]
    if not matches:
        raise VOCEvaluationError("canonical current VOC decision context is missing")
    if len(matches) != 1:
        raise VOCEvaluationError("canonical current VOC decision context is ambiguous")
    record = matches[0]
    if record.action != VOC_CURRENT_CONTEXT_ACTION:
        raise VOCEvaluationError("canonical current VOC decision context action is invalid")
    if _instant("recorded_at", record.recorded_at) > cutoff:
        raise VOCEvaluationError(
            "canonical current VOC decision context was recorded after the causal boundary"
        )
    if _instant("observed_ts", record.observed_ts) > cutoff:
        raise VOCEvaluationError(
            "canonical current VOC decision context observation is from the future"
        )
    payload = record.payload
    if not isinstance(payload, Mapping):
        raise VOCEvaluationError("canonical current VOC decision payload is invalid")
    context = payload.get(VOC_CURRENT_CONTEXT_PAYLOAD_KEY)
    if not isinstance(context, Mapping) or set(context) != _VOC_CURRENT_CONTEXT_FIELDS:
        raise VOCEvaluationError("canonical current VOC decision context schema is invalid")
    resolved: dict[str, str] = {}
    for field in sorted(_VOC_CURRENT_CONTEXT_FIELDS):
        resolved[field] = _text(
            f"canonical current VOC context {field}", context.get(field)
        )
    return resolved


@dataclass(frozen=True, slots=True)
class PairedVOCEvaluation:
    """One post-outcome paired baseline/challenger evaluation.

    The same decision_evidence_sha256 is shared by both candidates. The record is
    intentionally outcome-bearing and therefore can only become causally usable
    after outcome_revealed_at. It carries scientific-protocol identities but does
    not itself grant promotion or financial authority.
    """

    evaluation_id: str
    task_class: str
    sport_id: str
    league_id: str
    regime_id: str
    urgency_id: str
    contradiction_state: str
    baseline_candidate_id: str
    baseline_backend_id: str
    baseline_model_id: str
    baseline_config_sha256: str
    challenger_candidate_id: str
    challenger_backend_id: str
    challenger_model_id: str
    challenger_config_sha256: str
    decision_context_sha256: str
    decision_evidence_sha256: str
    baseline_output_sha256: str
    challenger_output_sha256: str
    baseline_action: str
    challenger_action: str
    baseline_abstained: bool
    challenger_abstained: bool
    decision_at: str
    decision_deadline: str
    baseline_completed_at: str
    challenger_completed_at: str
    outcome_evidence_sha256: str
    outcome_revealed_at: str
    evaluated_at: str
    scoring_rule_id: str
    scoring_rule_sha256: str
    research_protocol_id: str
    research_protocol_sha256: str
    holdout_access_id: str
    multiple_comparison_control_sha256: str
    baseline_utility: Decimal
    challenger_utility: Decimal
    compute_cost_penalty: Decimal
    latency_opportunity_cost_penalty: Decimal
    measured_compute_cost: Decimal
    paired_sample_count: int
    effective_sample_size: int
    support_fraction: Decimal
    incremental_value_interval_low: Decimal
    incremental_value_interval_high: Decimal
    provenance: VOCEvaluationProvenance = VOCEvaluationProvenance.MEASURED_SHADOW

    def __post_init__(self) -> None:
        for name in (
            "evaluation_id",
            "task_class",
            "sport_id",
            "league_id",
            "regime_id",
            "urgency_id",
            "contradiction_state",
            "baseline_candidate_id",
            "baseline_backend_id",
            "baseline_model_id",
            "challenger_candidate_id",
            "challenger_backend_id",
            "challenger_model_id",
            "baseline_action",
            "challenger_action",
            "scoring_rule_id",
            "research_protocol_id",
            "holdout_access_id",
        ):
            _text(name, getattr(self, name))
        for name in (
            "baseline_config_sha256",
            "challenger_config_sha256",
            "decision_context_sha256",
            "decision_evidence_sha256",
            "baseline_output_sha256",
            "challenger_output_sha256",
            "outcome_evidence_sha256",
            "scoring_rule_sha256",
            "research_protocol_sha256",
            "multiple_comparison_control_sha256",
        ):
            _sha256(name, getattr(self, name))
        if self.decision_context_sha256 == self.decision_evidence_sha256:
            raise VOCEvaluationError(
                "pre-request decision context must differ from post-output VOC binding"
            )
        if self.baseline_candidate_id == self.challenger_candidate_id:
            raise VOCEvaluationError("paired VOC candidates must be distinct")
        if type(self.baseline_abstained) is not bool or type(self.challenger_abstained) is not bool:
            raise VOCEvaluationError("abstention fields must be bool")
        if not isinstance(self.provenance, VOCEvaluationProvenance):
            raise VOCEvaluationError("provenance must be VOCEvaluationProvenance")

        decision_at = _instant("decision_at", self.decision_at)
        deadline = _instant("decision_deadline", self.decision_deadline)
        baseline_completed = _instant("baseline_completed_at", self.baseline_completed_at)
        challenger_completed = _instant("challenger_completed_at", self.challenger_completed_at)
        outcome_revealed = _instant("outcome_revealed_at", self.outcome_revealed_at)
        evaluated = _instant("evaluated_at", self.evaluated_at)
        if deadline <= decision_at:
            raise VOCEvaluationError("decision_deadline must be after decision_at")
        if baseline_completed < decision_at or challenger_completed < decision_at:
            raise VOCEvaluationError("candidate output cannot complete before decision evidence")
        if outcome_revealed < max(baseline_completed, challenger_completed):
            raise VOCEvaluationError("outcome cannot be revealed before paired candidate outputs")
        if evaluated < outcome_revealed:
            raise VOCEvaluationError("evaluation cannot predate outcome reveal")

        _decimal("baseline_utility", self.baseline_utility)
        _decimal("challenger_utility", self.challenger_utility)
        _nonnegative("compute_cost_penalty", self.compute_cost_penalty)
        _nonnegative(
            "latency_opportunity_cost_penalty",
            self.latency_opportunity_cost_penalty,
        )
        _nonnegative("measured_compute_cost", self.measured_compute_cost)
        if isinstance(self.paired_sample_count, bool) or not isinstance(self.paired_sample_count, int) or self.paired_sample_count < 1:
            raise VOCEvaluationError("paired_sample_count must be a positive integer")
        if isinstance(self.effective_sample_size, bool) or not isinstance(self.effective_sample_size, int) or self.effective_sample_size < 1:
            raise VOCEvaluationError("effective_sample_size must be a positive integer")
        if self.effective_sample_size > self.paired_sample_count:
            raise VOCEvaluationError("effective_sample_size cannot exceed paired_sample_count")
        _fraction("support_fraction", self.support_fraction)
        low = _decimal("incremental_value_interval_low", self.incremental_value_interval_low)
        high = _decimal("incremental_value_interval_high", self.incremental_value_interval_high)
        if low > high:
            raise VOCEvaluationError("incremental value interval low exceeds high")
        if not low <= self.net_value <= high:
            raise VOCEvaluationError("incremental value interval must contain exact net value")

        unchanged_decision = (
            self.baseline_action == self.challenger_action
            and self.baseline_abstained == self.challenger_abstained
        )
        if unchanged_decision and self.challenger_utility > self.baseline_utility:
            raise VOCEvaluationError(
                "unchanged decision cannot claim positive utility from extra computation"
            )

    @property
    def net_value(self) -> Decimal:
        return (
            self.challenger_utility
            - self.baseline_utility
            - self.compute_cost_penalty
            - self.latency_opportunity_cost_penalty
        )

    @property
    def deadline_missed(self) -> bool:
        deadline = _instant("decision_deadline", self.decision_deadline)
        return (
            _instant("baseline_completed_at", self.baseline_completed_at) > deadline
            or _instant("challenger_completed_at", self.challenger_completed_at) > deadline
        )

    @property
    def action_changed(self) -> bool:
        return (
            self.baseline_action != self.challenger_action
            or self.baseline_abstained != self.challenger_abstained
        )

    def unsigned_payload(self) -> dict[str, Any]:
        return {
            "evaluation_id": self.evaluation_id,
            "task_class": self.task_class,
            "sport_id": self.sport_id,
            "league_id": self.league_id,
            "regime_id": self.regime_id,
            "urgency_id": self.urgency_id,
            "contradiction_state": self.contradiction_state,
            "baseline_candidate_id": self.baseline_candidate_id,
            "baseline_backend_id": self.baseline_backend_id,
            "baseline_model_id": self.baseline_model_id,
            "baseline_config_sha256": self.baseline_config_sha256,
            "challenger_candidate_id": self.challenger_candidate_id,
            "challenger_backend_id": self.challenger_backend_id,
            "challenger_model_id": self.challenger_model_id,
            "challenger_config_sha256": self.challenger_config_sha256,
            "decision_context_sha256": self.decision_context_sha256,
            "decision_evidence_sha256": self.decision_evidence_sha256,
            "baseline_output_sha256": self.baseline_output_sha256,
            "challenger_output_sha256": self.challenger_output_sha256,
            "baseline_action": self.baseline_action,
            "challenger_action": self.challenger_action,
            "baseline_abstained": self.baseline_abstained,
            "challenger_abstained": self.challenger_abstained,
            "decision_at": _time("decision_at", self.decision_at),
            "decision_deadline": _time("decision_deadline", self.decision_deadline),
            "baseline_completed_at": _time("baseline_completed_at", self.baseline_completed_at),
            "challenger_completed_at": _time("challenger_completed_at", self.challenger_completed_at),
            "outcome_evidence_sha256": self.outcome_evidence_sha256,
            "outcome_revealed_at": _time("outcome_revealed_at", self.outcome_revealed_at),
            "evaluated_at": _time("evaluated_at", self.evaluated_at),
            "scoring_rule_id": self.scoring_rule_id,
            "scoring_rule_sha256": self.scoring_rule_sha256,
            "research_protocol_id": self.research_protocol_id,
            "research_protocol_sha256": self.research_protocol_sha256,
            "holdout_access_id": self.holdout_access_id,
            "multiple_comparison_control_sha256": self.multiple_comparison_control_sha256,
            "baseline_utility": str(self.baseline_utility),
            "challenger_utility": str(self.challenger_utility),
            "compute_cost_penalty": str(self.compute_cost_penalty),
            "latency_opportunity_cost_penalty": str(self.latency_opportunity_cost_penalty),
            "measured_compute_cost": str(self.measured_compute_cost),
            "paired_sample_count": self.paired_sample_count,
            "effective_sample_size": self.effective_sample_size,
            "support_fraction": str(self.support_fraction),
            "incremental_value_interval_low": str(self.incremental_value_interval_low),
            "incremental_value_interval_high": str(self.incremental_value_interval_high),
            "provenance": self.provenance.value,
        }

    @property
    def record_type(self) -> str:
        return "PairedVOCEvaluation"

    @property
    def record_id(self) -> str:
        return self.evaluation_id

    @property
    def available_at(self) -> str:
        return self.evaluated_at

    def to_payload(self) -> dict[str, Any]:
        return self.payload()

    @property
    def evaluation_sha256(self) -> str:
        return _canonical_digest(self.unsigned_payload())

    def payload(self) -> dict[str, Any]:
        return {**self.unsigned_payload(), "evaluation_sha256": self.evaluation_sha256}

    def routing_ineligibility_reason(
        self,
        *,
        as_of: str,
        minimum_effective_sample_size: int,
    ) -> str | None:
        if isinstance(minimum_effective_sample_size, bool) or not isinstance(minimum_effective_sample_size, int) or minimum_effective_sample_size < 1:
            raise VOCEvaluationError("minimum_effective_sample_size must be positive")
        now = _instant("as_of", as_of)
        if self.provenance is not VOCEvaluationProvenance.MEASURED_SHADOW:
            return "VOC evaluation is not measured shadow evidence"
        if _instant("evaluated_at", self.evaluated_at) > now:
            return "VOC evaluation is not causally available"
        if self.deadline_missed:
            return "challenger compute missed the frozen decision deadline"
        if self.effective_sample_size < minimum_effective_sample_size:
            return "VOC evaluation effective sample size is insufficient"
        if self.incremental_value_interval_low <= _ZERO:
            return "VOC uncertainty interval does not establish positive incremental value"
        if self.net_value <= _ZERO:
            return "measured value of computation is non-positive"
        if not self.action_changed:
            return "extra computation did not change action or abstention"
        return None

    @classmethod
    def from_payload(cls, raw: Mapping[str, Any]) -> "PairedVOCEvaluation":
        try:
            expected_sha256 = raw["evaluation_sha256"]
            value = cls(
                evaluation_id=raw["evaluation_id"],
                task_class=raw["task_class"],
                sport_id=raw["sport_id"],
                league_id=raw["league_id"],
                regime_id=raw["regime_id"],
                urgency_id=raw["urgency_id"],
                contradiction_state=raw["contradiction_state"],
                baseline_candidate_id=raw["baseline_candidate_id"],
                baseline_backend_id=raw["baseline_backend_id"],
                baseline_model_id=raw["baseline_model_id"],
                baseline_config_sha256=raw["baseline_config_sha256"],
                challenger_candidate_id=raw["challenger_candidate_id"],
                challenger_backend_id=raw["challenger_backend_id"],
                challenger_model_id=raw["challenger_model_id"],
                challenger_config_sha256=raw["challenger_config_sha256"],
                decision_context_sha256=raw["decision_context_sha256"],
                decision_evidence_sha256=raw["decision_evidence_sha256"],
                baseline_output_sha256=raw["baseline_output_sha256"],
                challenger_output_sha256=raw["challenger_output_sha256"],
                baseline_action=raw["baseline_action"],
                challenger_action=raw["challenger_action"],
                baseline_abstained=raw["baseline_abstained"],
                challenger_abstained=raw["challenger_abstained"],
                decision_at=raw["decision_at"],
                decision_deadline=raw["decision_deadline"],
                baseline_completed_at=raw["baseline_completed_at"],
                challenger_completed_at=raw["challenger_completed_at"],
                outcome_evidence_sha256=raw["outcome_evidence_sha256"],
                outcome_revealed_at=raw["outcome_revealed_at"],
                evaluated_at=raw["evaluated_at"],
                scoring_rule_id=raw["scoring_rule_id"],
                scoring_rule_sha256=raw["scoring_rule_sha256"],
                research_protocol_id=raw["research_protocol_id"],
                research_protocol_sha256=raw["research_protocol_sha256"],
                holdout_access_id=raw["holdout_access_id"],
                multiple_comparison_control_sha256=raw["multiple_comparison_control_sha256"],
                baseline_utility=Decimal(raw["baseline_utility"]),
                challenger_utility=Decimal(raw["challenger_utility"]),
                compute_cost_penalty=Decimal(raw["compute_cost_penalty"]),
                latency_opportunity_cost_penalty=Decimal(raw["latency_opportunity_cost_penalty"]),
                measured_compute_cost=Decimal(raw["measured_compute_cost"]),
                paired_sample_count=raw["paired_sample_count"],
                effective_sample_size=raw["effective_sample_size"],
                support_fraction=Decimal(raw["support_fraction"]),
                incremental_value_interval_low=Decimal(raw["incremental_value_interval_low"]),
                incremental_value_interval_high=Decimal(raw["incremental_value_interval_high"]),
                provenance=VOCEvaluationProvenance(raw["provenance"]),
            )
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            if isinstance(exc, VOCEvaluationError):
                raise
            raise VOCEvaluationError("invalid paired VOC evaluation payload") from exc
        _sha256("evaluation_sha256", expected_sha256)
        if value.evaluation_sha256 != expected_sha256:
            raise VOCEvaluationError("paired VOC evaluation SHA-256 mismatch")
        return value


@dataclass(frozen=True, slots=True)
class OutcomeDerivedVOCScore:
    """Outcome-derived VOC arithmetic resolved independently of routed evidence.

    This artifact is deliberately separate from ScientificRegistry records. Its
    authority implementation must compute or resolve the values from immutable
    causal outcome observations; a PairedVOCEvaluation cannot mint its own score.
    """

    evaluation_id: str
    available_at: str
    outcome_evidence_sha256: str
    scoring_rule_sha256: str
    research_protocol_sha256: str
    holdout_access_id: str
    multiple_comparison_control_sha256: str
    baseline_utility: Decimal
    challenger_utility: Decimal
    compute_cost_penalty: Decimal
    latency_opportunity_cost_penalty: Decimal
    measured_compute_cost: Decimal
    paired_sample_count: int
    effective_sample_size: int
    support_fraction: Decimal
    incremental_value_interval_low: Decimal
    incremental_value_interval_high: Decimal
    source_artifact_sha256: str

    def __post_init__(self) -> None:
        _text("evaluation_id", self.evaluation_id)
        _time("available_at", self.available_at)
        for name in (
            "outcome_evidence_sha256",
            "scoring_rule_sha256",
            "research_protocol_sha256",
            "multiple_comparison_control_sha256",
            "source_artifact_sha256",
        ):
            _sha256(name, getattr(self, name))
        _text("holdout_access_id", self.holdout_access_id)
        _decimal("baseline_utility", self.baseline_utility)
        _decimal("challenger_utility", self.challenger_utility)
        _nonnegative("compute_cost_penalty", self.compute_cost_penalty)
        _nonnegative(
            "latency_opportunity_cost_penalty",
            self.latency_opportunity_cost_penalty,
        )
        _nonnegative("measured_compute_cost", self.measured_compute_cost)
        if (
            isinstance(self.paired_sample_count, bool)
            or not isinstance(self.paired_sample_count, int)
            or self.paired_sample_count < 1
        ):
            raise VOCEvaluationError("paired_sample_count must be positive")
        if (
            isinstance(self.effective_sample_size, bool)
            or not isinstance(self.effective_sample_size, int)
            or self.effective_sample_size < 1
            or self.effective_sample_size > self.paired_sample_count
        ):
            raise VOCEvaluationError(
                "effective_sample_size must be positive and no larger than paired_sample_count"
            )
        _fraction("support_fraction", self.support_fraction)
        low = _decimal(
            "incremental_value_interval_low",
            self.incremental_value_interval_low,
        )
        high = _decimal(
            "incremental_value_interval_high",
            self.incremental_value_interval_high,
        )
        if low > high:
            raise VOCEvaluationError(
                "incremental value interval low must not exceed high"
            )

    @property
    def net_value(self) -> Decimal:
        return (
            self.challenger_utility
            - self.baseline_utility
            - self.compute_cost_penalty
            - self.latency_opportunity_cost_penalty
        )

    def payload(self) -> dict[str, Any]:
        return {
            "schema": "autosport.outcome_derived_voc_score",
            "schema_version": 1,
            "evaluation_id": self.evaluation_id,
            "available_at": _time("available_at", self.available_at),
            "outcome_evidence_sha256": self.outcome_evidence_sha256,
            "scoring_rule_sha256": self.scoring_rule_sha256,
            "research_protocol_sha256": self.research_protocol_sha256,
            "holdout_access_id": self.holdout_access_id,
            "multiple_comparison_control_sha256": self.multiple_comparison_control_sha256,
            "baseline_utility": str(self.baseline_utility),
            "challenger_utility": str(self.challenger_utility),
            "compute_cost_penalty": str(self.compute_cost_penalty),
            "latency_opportunity_cost_penalty": str(
                self.latency_opportunity_cost_penalty
            ),
            "measured_compute_cost": str(self.measured_compute_cost),
            "paired_sample_count": self.paired_sample_count,
            "effective_sample_size": self.effective_sample_size,
            "support_fraction": str(self.support_fraction),
            "incremental_value_interval_low": str(
                self.incremental_value_interval_low
            ),
            "incremental_value_interval_high": str(
                self.incremental_value_interval_high
            ),
            "net_value": str(self.net_value),
            "source_artifact_sha256": self.source_artifact_sha256,
        }

    @property
    def score_sha256(self) -> str:
        return _canonical_digest(self.payload())


@runtime_checkable
class OutcomeDerivedVOCScoreAuthority(Protocol):
    """Resolve a score from canonical outcome observations, never routed values.

    Implementations receive only evaluation identity plus a causal cutoff. They MUST
    NOT accept a PairedVOCEvaluation as an input or derive the score from one.
    """

    def resolve(
        self,
        evaluation_id: str,
        *,
        as_of: str,
    ) -> OutcomeDerivedVOCScore | None: ...


@runtime_checkable
class VOCCanonicalAuthorityResolver(Protocol):
    """Resolve a VOC record from canonical decision/outcome/protocol authorities.

    Implementations MUST resolve identity from durable canonical sources rather than
    trusting the caller-supplied PairedVOCEvaluation. Returning the supplied object
    unchanged is intentionally not an application-level authority implementation.
    """

    def resolve(
        self,
        evaluation: PairedVOCEvaluation,
        *,
        as_of: str,
    ) -> PairedVOCEvaluation | None: ...

    def resolve_decision_context(
        self,
        context_sha256: str,
        *,
        as_of: str,
    ) -> Mapping[str, str] | None: ...


class CanonicalVOCAuthorityResolver:
    """Resolve VOC only from durable canonical decision/protocol/outcome authorities."""

    def __init__(
        self,
        decision_ledger: JsonlDecisionLedger,
        scientific_registry: ScientificRegistry,
        outcome_authority: MarketSettlementOutcomeAuthority,
        outcome_score_authority: OutcomeDerivedVOCScoreAuthority,
    ) -> None:
        if not isinstance(decision_ledger, JsonlDecisionLedger):
            raise TypeError("decision_ledger must be JsonlDecisionLedger")
        if not isinstance(scientific_registry, ScientificRegistry):
            raise TypeError("scientific_registry must be ScientificRegistry")
        if not isinstance(outcome_authority, MarketSettlementOutcomeAuthority):
            raise TypeError("outcome_authority must be MarketSettlementOutcomeAuthority")
        if not isinstance(outcome_score_authority, OutcomeDerivedVOCScoreAuthority):
            raise TypeError(
                "outcome_score_authority must implement OutcomeDerivedVOCScoreAuthority"
            )
        self.decision_ledger = decision_ledger
        self.scientific_registry = scientific_registry
        self.outcome_authority = outcome_authority
        self.outcome_score_authority = outcome_score_authority

    @staticmethod
    def _decision_digest(record: object) -> str:
        try:
            payload = record.to_dict()
        except AttributeError as exc:
            raise VOCEvaluationError("canonical decision record is invalid") from exc
        return _canonical_digest(payload)

    def resolve_decision_context(
        self,
        context_sha256: str,
        *,
        as_of: str,
    ) -> Mapping[str, str] | None:
        return resolve_voc_decision_context(
            self.decision_ledger,
            context_sha256=context_sha256,
            as_of=as_of,
        )

    def _require_decision(self, evaluation: PairedVOCEvaluation) -> None:
        try:
            records = self.decision_ledger.verified_records()
        except DecisionLedgerIntegrityError as exc:
            raise VOCEvaluationError("canonical DecisionLedger verification failed") from exc
        context = self.resolve_decision_context(
            evaluation.decision_context_sha256,
            as_of=evaluation.decision_at,
        )
        if context is None:
            raise VOCEvaluationError("canonical source VOC decision context is missing")
        expected_context = {
            "task_class": evaluation.task_class,
            "sport_id": evaluation.sport_id,
            "league_id": evaluation.league_id,
            "regime_id": evaluation.regime_id,
            "urgency_id": evaluation.urgency_id,
            "contradiction_state": evaluation.contradiction_state,
        }
        if any(context.get(key) != value for key, value in expected_context.items()):
            raise VOCEvaluationError(
                "canonical source VOC decision context does not match paired evaluation"
            )
        expected_binding = {
            "decision_context_sha256": evaluation.decision_context_sha256,
            "baseline_candidate_id": evaluation.baseline_candidate_id,
            "baseline_backend_id": evaluation.baseline_backend_id,
            "baseline_model_id": evaluation.baseline_model_id,
            "baseline_config_sha256": evaluation.baseline_config_sha256,
            "baseline_output_sha256": evaluation.baseline_output_sha256,
            "baseline_action": evaluation.baseline_action,
            "baseline_abstained": evaluation.baseline_abstained,
            "challenger_candidate_id": evaluation.challenger_candidate_id,
            "challenger_backend_id": evaluation.challenger_backend_id,
            "challenger_model_id": evaluation.challenger_model_id,
            "challenger_config_sha256": evaluation.challenger_config_sha256,
            "challenger_output_sha256": evaluation.challenger_output_sha256,
            "challenger_action": evaluation.challenger_action,
            "challenger_abstained": evaluation.challenger_abstained,
            "sport_id": evaluation.sport_id,
            "league_id": evaluation.league_id,
            "regime_id": evaluation.regime_id,
            "urgency_id": evaluation.urgency_id,
            "contradiction_state": evaluation.contradiction_state,
        }
        for record in records:
            if self._decision_digest(record) != evaluation.decision_evidence_sha256:
                continue
            if _instant("observed_ts", record.observed_ts) > _instant("decision_at", evaluation.decision_at):
                raise VOCEvaluationError("canonical decision evidence postdates paired decision")
            recorded_at = _instant("recorded_at", record.recorded_at)
            outputs_completed_at = max(
                _instant("baseline_completed_at", evaluation.baseline_completed_at),
                _instant("challenger_completed_at", evaluation.challenger_completed_at),
            )
            if recorded_at < outputs_completed_at:
                raise VOCEvaluationError(
                    "canonical decision VOC binding predates paired candidate outputs"
                )
            if recorded_at >= _instant(
                "outcome_revealed_at", evaluation.outcome_revealed_at
            ):
                raise VOCEvaluationError(
                    "canonical decision VOC binding was not durably recorded before outcome reveal"
                )
            payload = record.payload
            if not isinstance(payload, Mapping):
                raise VOCEvaluationError("canonical decision payload is invalid")
            binding = payload.get("voc_binding")
            if not isinstance(binding, Mapping):
                raise VOCEvaluationError("canonical decision VOC binding is missing")
            if binding != expected_binding:
                raise VOCEvaluationError("canonical decision VOC binding does not match paired evaluation")
            return
        raise VOCEvaluationError("canonical DecisionLedger decision evidence is missing")

    def _require_protocol(self, evaluation: PairedVOCEvaluation) -> None:
        decision_at = _instant("decision_at", evaluation.decision_at)
        entry = self.scientific_registry.get(
            "ResearchProtocol", evaluation.research_protocol_id
        )
        if entry is None:
            raise VOCEvaluationError("canonical ResearchProtocol is missing")
        payload = entry.payload
        if payload.get("research_protocol_id") != evaluation.research_protocol_id:
            raise VOCEvaluationError("canonical ResearchProtocol identity mismatch")
        if payload.get("protocol_sha256") != evaluation.research_protocol_sha256:
            raise VOCEvaluationError("canonical ResearchProtocol digest mismatch")
        if _instant("available_at", entry.available_at) > decision_at:
            raise VOCEvaluationError("canonical ResearchProtocol is not available at decision time")

        binding = payload.get("binding")
        if type(binding) is not dict:
            raise VOCEvaluationError("canonical ResearchProtocol binding is missing")
        evaluation_design = binding.get("evaluation_design")
        if not isinstance(evaluation_design, str) or not evaluation_design.strip():
            raise VOCEvaluationError("canonical ResearchProtocol evaluation design is missing")
        try:
            design = json.loads(evaluation_design)
        except json.JSONDecodeError as exc:
            raise VOCEvaluationError("canonical VOC evaluation design must be canonical JSON") from exc
        if type(design) is not dict:
            raise VOCEvaluationError("canonical VOC evaluation design must be an object")
        if design.get("task_class") != evaluation.task_class:
            raise VOCEvaluationError(
                "canonical ResearchProtocol task-class binding does not match paired evaluation"
            )

        scope = design.get("scope")
        if type(scope) is not dict:
            raise VOCEvaluationError("canonical VOC evaluation scope is missing")
        expected_scope = {
            "sport_id": evaluation.sport_id,
            "league_id": evaluation.league_id,
            "regime_id": evaluation.regime_id,
            "urgency_id": evaluation.urgency_id,
            "contradiction_state": evaluation.contradiction_state,
        }
        if scope != expected_scope:
            raise VOCEvaluationError("canonical ResearchProtocol scope does not match paired evaluation")

        outcome_identity = design.get("outcome_identity")
        if type(outcome_identity) is not dict:
            raise VOCEvaluationError("canonical VOC outcome identity is missing")
        for field in ("event_id", "market_id", "source_id", "market_type"):
            value = outcome_identity.get(field)
            if type(value) is not str or not value.strip():
                raise VOCEvaluationError(f"canonical VOC outcome identity field {field} is missing")

        multiple_comparison_control = binding.get("multiple_comparison_control")
        if not isinstance(multiple_comparison_control, str) or not multiple_comparison_control.strip():
            raise VOCEvaluationError("canonical multiple-comparison control is missing")
        control_digest = hashlib.sha256(
            multiple_comparison_control.encode("utf-8")
        ).hexdigest()
        if control_digest != evaluation.multiple_comparison_control_sha256:
            raise VOCEvaluationError("canonical multiple-comparison control digest mismatch")

        scoring_rule = design.get("scoring_rule")
        if type(scoring_rule) is not dict:
            raise VOCEvaluationError("canonical VOC scoring rule is missing")
        if scoring_rule.get("id") != evaluation.scoring_rule_id:
            raise VOCEvaluationError("canonical scoring rule identity mismatch")
        scoring_payload = scoring_rule.get("payload")
        if type(scoring_payload) is not dict:
            raise VOCEvaluationError("canonical scoring rule payload is missing")
        if hashlib.sha256(
            json.dumps(
                scoring_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest() != evaluation.scoring_rule_sha256:
            raise VOCEvaluationError("canonical scoring rule digest mismatch")

        snapshot = None
        for candidate in self.scientific_registry.causal_records(
            "DatasetSnapshot",
            as_of=evaluation.evaluated_at,
        ):
            if candidate.payload.get("manifest_sha256") == payload.get("dataset_manifest_sha256"):
                if snapshot is not None:
                    raise VOCEvaluationError("canonical dataset snapshot for VOC protocol is ambiguous")
                snapshot = candidate
        if snapshot is None:
            raise VOCEvaluationError("canonical DatasetSnapshot for VOC protocol is missing")
        confirmation_trial_family_id = design.get("confirmation_trial_family_id")
        if type(confirmation_trial_family_id) is not str or not confirmation_trial_family_id.strip():
            raise VOCEvaluationError("canonical confirmation-trial family is missing")
        expected_holdout = _canonical_digest(
            {
                "schema_version": 1,
                "research_protocol_id": evaluation.research_protocol_id,
                "dataset_manifest_sha256": payload.get("dataset_manifest_sha256"),
                "source_identity": snapshot.payload.get("source_identity"),
                "license_identity": snapshot.payload.get("license_identity"),
                "confirmation_trial_family_id": confirmation_trial_family_id,
            }
        )
        if expected_holdout != evaluation.holdout_access_id:
            raise VOCEvaluationError("canonical holdout identity mismatch")

    def _require_outcome(self, evaluation: PairedVOCEvaluation) -> None:
        try:
            decision_at = _instant("decision_at", evaluation.decision_at)
            self.outcome_authority.assert_available_as_of(decision_at)
        except (TypeError, ValueError) as exc:
            raise VOCEvaluationError("canonical MarketSettlementOutcomeAuthority is not causally available") from exc
        identity = self.outcome_authority.identity
        if identity.sport != evaluation.sport_id:
            raise VOCEvaluationError("canonical outcome authority sport scope mismatch")
        if self.outcome_authority.authority_sha256 != evaluation.outcome_evidence_sha256:
            raise VOCEvaluationError("canonical outcome authority digest mismatch")
        protocol_entry = self.scientific_registry.get(
            "ResearchProtocol", evaluation.research_protocol_id
        )
        if protocol_entry is None:
            raise VOCEvaluationError("canonical ResearchProtocol is missing for outcome binding")
        binding = protocol_entry.payload.get("binding")
        design_text = None if not isinstance(binding, dict) else binding.get("evaluation_design")
        if not isinstance(design_text, str):
            raise VOCEvaluationError("canonical VOC outcome identity binding is missing")
        try:
            design = json.loads(design_text)
        except json.JSONDecodeError as exc:
            raise VOCEvaluationError("canonical VOC evaluation design is not valid JSON") from exc
        outcome_identity = design.get("outcome_identity") if isinstance(design, dict) else None
        if type(outcome_identity) is not dict:
            raise VOCEvaluationError("canonical VOC outcome identity binding is missing")
        expected_identity = {
            "event_id": identity.event_id,
            "market_id": identity.market_id,
            "source_id": identity.source_id,
            "market_type": identity.market_type.value,
            "sport_id": evaluation.sport_id,
            "league_id": evaluation.league_id,
            "regime_id": evaluation.regime_id,
        }
        for field, expected in expected_identity.items():
            if outcome_identity.get(field) != expected:
                raise VOCEvaluationError(
                    "canonical outcome authority identity does not match research scope"
                )

    def _require_outcome_score(
        self,
        evaluation: PairedVOCEvaluation,
    ) -> OutcomeDerivedVOCScore:
        """Resolve arithmetic from an authority that cannot echo routed values."""

        score = self.outcome_score_authority.resolve(
            evaluation.evaluation_id,
            as_of=evaluation.evaluated_at,
        )
        if score is None:
            raise VOCEvaluationError(
                "canonical outcome-derived VOC score is missing"
            )
        if not isinstance(score, OutcomeDerivedVOCScore):
            raise VOCEvaluationError(
                "canonical outcome-derived VOC score is invalid"
            )
        available_at = _instant("score.available_at", score.available_at)
        if available_at < _instant(
            "outcome_revealed_at",
            evaluation.outcome_revealed_at,
        ):
            raise VOCEvaluationError(
                "canonical outcome-derived VOC score predates outcome reveal"
            )
        if available_at > _instant("evaluated_at", evaluation.evaluated_at):
            raise VOCEvaluationError(
                "canonical outcome-derived VOC score is not causally available"
            )
        expected_identity = (
            evaluation.evaluation_id,
            evaluation.outcome_evidence_sha256,
            evaluation.scoring_rule_sha256,
            evaluation.research_protocol_sha256,
            evaluation.holdout_access_id,
            evaluation.multiple_comparison_control_sha256,
        )
        actual_identity = (
            score.evaluation_id,
            score.outcome_evidence_sha256,
            score.scoring_rule_sha256,
            score.research_protocol_sha256,
            score.holdout_access_id,
            score.multiple_comparison_control_sha256,
        )
        if actual_identity != expected_identity:
            raise VOCEvaluationError(
                "canonical outcome-derived VOC score identity mismatch"
            )
        expected_values = (
            evaluation.baseline_utility,
            evaluation.challenger_utility,
            evaluation.compute_cost_penalty,
            evaluation.latency_opportunity_cost_penalty,
            evaluation.measured_compute_cost,
            evaluation.paired_sample_count,
            evaluation.effective_sample_size,
            evaluation.support_fraction,
            evaluation.incremental_value_interval_low,
            evaluation.incremental_value_interval_high,
            evaluation.net_value,
        )
        actual_values = (
            score.baseline_utility,
            score.challenger_utility,
            score.compute_cost_penalty,
            score.latency_opportunity_cost_penalty,
            score.measured_compute_cost,
            score.paired_sample_count,
            score.effective_sample_size,
            score.support_fraction,
            score.incremental_value_interval_low,
            score.incremental_value_interval_high,
            score.net_value,
        )
        if actual_values != expected_values:
            raise VOCEvaluationError(
                "canonical outcome-derived VOC score does not match routed evaluation"
            )
        return score

    def _require_scientific_statistics(
        self,
        evaluation: PairedVOCEvaluation,
    ) -> None:
        """Fail closed unless outcome authority and #367 memory bind VOC statistics."""

        score = self._require_outcome_score(evaluation)

        protocol_entry = self.scientific_registry.get(
            "ResearchProtocol",
            evaluation.research_protocol_id,
        )
        if protocol_entry is None:
            raise VOCEvaluationError(
                "canonical ResearchProtocol is missing for VOC statistics"
            )
        protocol_payload = protocol_entry.payload
        binding = protocol_payload.get("binding")
        if type(binding) is not dict:
            raise VOCEvaluationError(
                "canonical ResearchProtocol binding is missing for VOC statistics"
            )
        design_text = binding.get("evaluation_design")
        if not isinstance(design_text, str):
            raise VOCEvaluationError(
                "canonical VOC statistics design is missing"
            )
        try:
            design = json.loads(design_text)
        except json.JSONDecodeError as exc:
            raise VOCEvaluationError(
                "canonical VOC statistics design is not valid JSON"
            ) from exc
        if type(design) is not dict:
            raise VOCEvaluationError(
                "canonical VOC statistics design must be an object"
            )

        estimand = design.get("estimand")
        if estimand != "incremental_net_voc":
            raise VOCEvaluationError(
                "canonical VOC statistics estimand is not incremental_net_voc"
            )
        cohort_id = design.get("cohort_id")
        if type(cohort_id) is not str or not cohort_id.strip():
            raise VOCEvaluationError("canonical VOC statistics cohort is missing")
        confirmation_trial_family_id = design.get(
            "confirmation_trial_family_id"
        )
        if (
            type(confirmation_trial_family_id) is not str
            or not confirmation_trial_family_id.strip()
        ):
            raise VOCEvaluationError(
                "canonical VOC confirmation-trial family is missing"
            )
        evaluator_source_sha256 = design.get("evaluator_source_sha256")
        try:
            _sha256(
                "canonical VOC evaluator_source_sha256",
                evaluator_source_sha256,
            )
        except VOCEvaluationError as exc:
            raise VOCEvaluationError(
                "canonical VOC evaluator source identity is missing"
            ) from exc
        uncertainty_method = binding.get("uncertainty_method")
        if type(uncertainty_method) is not str or not uncertainty_method.strip():
            raise VOCEvaluationError(
                "canonical VOC uncertainty method is missing"
            )

        matches = []
        for entry in self.scientific_registry.causal_records(
            "PromotionEvidence",
            as_of=evaluation.evaluated_at,
        ):
            payload = entry.payload
            if (
                payload.get("research_protocol_id")
                == evaluation.research_protocol_id
                and payload.get("holdout_access_id")
                == evaluation.holdout_access_id
                and payload.get("multiple_comparison_control_sha256")
                == evaluation.multiple_comparison_control_sha256
                and payload.get("confirmation_trial_family_id")
                == confirmation_trial_family_id
                and payload.get("estimand") == estimand
                and payload.get("cohort_id") == cohort_id
                and payload.get("uncertainty_method") == uncertainty_method
            ):
                matches.append(entry)
        if not matches:
            raise VOCEvaluationError(
                "canonical scientific VOC statistics evidence is missing"
            )
        if len(matches) != 1:
            raise VOCEvaluationError(
                "canonical scientific VOC statistics evidence is ambiguous"
            )

        evidence = matches[0].payload
        if (
            evidence.get("validity") != "ELIGIBLE"
            or evidence.get("guardrails_passed") is not True
            or evidence.get("holdout_consumed") is not True
        ):
            raise VOCEvaluationError(
                "canonical scientific VOC statistics evidence is not eligible"
            )
        if evidence.get("effective_sample_size") != evaluation.effective_sample_size:
            raise VOCEvaluationError(
                "canonical VOC effective sample size mismatch"
            )
        minimum_ess = evidence.get("minimum_effective_sample_size")
        if (
            isinstance(minimum_ess, bool)
            or not isinstance(minimum_ess, int)
            or evaluation.effective_sample_size < minimum_ess
        ):
            raise VOCEvaluationError(
                "canonical VOC minimum effective sample size is not satisfied"
            )
        try:
            interval_low = Decimal(evidence["effect_interval_low"])
            interval_high = Decimal(evidence["effect_interval_high"])
            practical_improvement = Decimal(evidence["practical_improvement"])
        except (KeyError, InvalidOperation, TypeError) as exc:
            raise VOCEvaluationError(
                "canonical scientific VOC statistics are invalid"
            ) from exc
        if (
            interval_low != evaluation.incremental_value_interval_low
            or interval_high != evaluation.incremental_value_interval_high
            or practical_improvement != evaluation.net_value
        ):
            raise VOCEvaluationError(
                "canonical scientific VOC statistics do not match routed evaluation"
            )

        bundle_id = evidence.get("evaluation_bundle_id")
        if type(bundle_id) is not str or not bundle_id.strip():
            raise VOCEvaluationError(
                "canonical VOC EvaluationBundle identity is missing"
            )
        bundle_entry = self.scientific_registry.get(
            "EvaluationBundle",
            bundle_id,
        )
        if bundle_entry is None:
            raise VOCEvaluationError("canonical VOC EvaluationBundle is missing")
        bundle = bundle_entry.payload
        if (
            bundle.get("bundle_sha256")
            != evidence.get("evaluation_bundle_sha256")
            or bundle.get("protocol_sha256")
            != evaluation.research_protocol_sha256
            or bundle.get("evaluator_source_sha256")
            != evaluator_source_sha256
            or bundle.get("dataset_snapshot_id")
            != evidence.get("dataset_snapshot_id")
        ):
            raise VOCEvaluationError(
                "canonical VOC EvaluationBundle authority mismatch"
            )
        if bundle.get("effective_sample_size") != evaluation.effective_sample_size:
            raise VOCEvaluationError(
                "canonical VOC EvaluationBundle ESS mismatch"
            )
        try:
            bundle_low = Decimal(bundle["effect_interval_low"])
            bundle_high = Decimal(bundle["effect_interval_high"])
            bundle_improvement = Decimal(bundle["practical_improvement"])
        except (KeyError, InvalidOperation, TypeError) as exc:
            raise VOCEvaluationError(
                "canonical VOC EvaluationBundle statistics are invalid"
            ) from exc
        if (
            bundle_low != evaluation.incremental_value_interval_low
            or bundle_high != evaluation.incremental_value_interval_high
            or bundle_improvement != evaluation.net_value
        ):
            raise VOCEvaluationError(
                "canonical VOC EvaluationBundle statistics mismatch"
            )

        snapshot_id = evidence.get("dataset_snapshot_id")
        if type(snapshot_id) is not str or not snapshot_id.strip():
            raise VOCEvaluationError(
                "canonical VOC DatasetSnapshot identity is missing"
            )
        snapshot = self.scientific_registry.get(
            "DatasetSnapshot",
            snapshot_id,
        )
        if snapshot is None:
            raise VOCEvaluationError(
                "canonical VOC DatasetSnapshot is missing"
            )
        if (
            snapshot.payload.get("manifest_sha256")
            != protocol_payload.get("dataset_manifest_sha256")
        ):
            raise VOCEvaluationError(
                "canonical VOC DatasetSnapshot does not match protocol"
            )

        artifact_hashes = bundle.get("artifact_hashes")
        if (
            type(artifact_hashes) is not list
            or score.score_sha256 not in artifact_hashes
        ):
            raise VOCEvaluationError(
                "canonical outcome-derived VOC score artifact is missing"
            )

    def _require_registry_result(
        self,
        evaluation: PairedVOCEvaluation,
        *,
        as_of: str,
    ) -> PairedVOCEvaluation:
        entry = self.scientific_registry.get(
            "PairedVOCEvaluation",
            evaluation.evaluation_id,
        )
        if entry is None:
            raise VOCEvaluationError(
                "canonical PairedVOCEvaluation result is missing"
            )
        available_at = _instant("available_at", entry.available_at)
        reveal_at = _instant("outcome_revealed_at", evaluation.outcome_revealed_at)
        if available_at < reveal_at:
            raise VOCEvaluationError(
                "canonical PairedVOCEvaluation result predates outcome reveal"
            )
        if available_at > _instant("as_of", as_of):
            raise VOCEvaluationError(
                "canonical PairedVOCEvaluation result is not causally available"
            )
        if entry.payload != evaluation.payload():
            raise VOCEvaluationError(
                "canonical PairedVOCEvaluation result differs from routed evaluation"
            )
        return PairedVOCEvaluation.from_payload(entry.payload)

    def resolve(
        self,
        evaluation: PairedVOCEvaluation,
        *,
        as_of: str,
    ) -> PairedVOCEvaluation | None:
        if not isinstance(evaluation, PairedVOCEvaluation):
            raise TypeError("evaluation must be PairedVOCEvaluation")
        _instant("as_of", as_of)
        canonical = self._require_registry_result(evaluation, as_of=as_of)
        self._require_decision(canonical)
        self._require_protocol(canonical)
        self._require_outcome(canonical)
        self._require_scientific_statistics(canonical)
        return canonical


class VOCEvaluationStore:
    """Restart-safe immutable memory for positive, null, negative and harmful VOC results."""

    def __init__(
        self,
        path: str | Path,
        *,
        canonical_authority_resolver: VOCCanonicalAuthorityResolver | None = None,
    ) -> None:
        if canonical_authority_resolver is not None and not isinstance(
            canonical_authority_resolver, VOCCanonicalAuthorityResolver,
        ):
            raise TypeError(
                "canonical_authority_resolver must implement VOCCanonicalAuthorityResolver"
            )
        self.path = Path(path)
        self._canonical_authority_resolver = canonical_authority_resolver
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write([])
        self._load()

    def _body(self, evaluations: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "schema": _SCHEMA,
            "version": _VERSION,
            "evaluations": evaluations,
        }

    def _write(self, evaluations: list[dict[str, Any]]) -> None:
        body = self._body(evaluations)
        atomic_write_json(
            self.path,
            {**body, "state_sha256": _canonical_digest(body)},
        )

    def _load(self) -> dict[str, PairedVOCEvaluation]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VOCEvaluationError("VOC evaluation store is unreadable") from exc
        if type(raw) is not dict or set(raw) != {
            "schema",
            "version",
            "evaluations",
            "state_sha256",
        }:
            raise VOCEvaluationError("VOC evaluation store schema is invalid")
        if raw["schema"] != _SCHEMA or raw["version"] != _VERSION:
            raise VOCEvaluationError("VOC evaluation store version mismatch")
        evaluations = raw["evaluations"]
        if type(evaluations) is not list:
            raise VOCEvaluationError("VOC evaluations must be a list")
        body = self._body(evaluations)
        if _sha256("state_sha256", raw["state_sha256"]) != _canonical_digest(body):
            raise VOCEvaluationError("VOC evaluation store state SHA-256 mismatch")
        loaded: dict[str, PairedVOCEvaluation] = {}
        for item in evaluations:
            if not isinstance(item, Mapping):
                raise VOCEvaluationError("VOC evaluation record must be an object")
            value = PairedVOCEvaluation.from_payload(item)
            if value.evaluation_id in loaded:
                raise VOCEvaluationError("duplicate VOC evaluation identity")
            loaded[value.evaluation_id] = value
        return loaded

    def record(self, evaluation: PairedVOCEvaluation) -> str:
        """Persist immutable evidence; persistence alone grants no CLOUD authority."""
        if not isinstance(evaluation, PairedVOCEvaluation):
            raise TypeError("evaluation must be PairedVOCEvaluation")
        with WorkspaceEconomicLock(self.path.parent):
            loaded = self._load()
            existing = loaded.get(evaluation.evaluation_id)
            if existing is not None:
                if existing.payload() == evaluation.payload():
                    return existing.evaluation_sha256
                raise VOCEvaluationError("conflicting immutable VOC evaluation identity")
            values = list(loaded.values())
            values.append(evaluation)
            values.sort(
                key=lambda item: (
                    _instant("evaluated_at", item.evaluated_at),
                    item.evaluation_id,
                )
            )
            self._write([item.payload() for item in values])
        self._load()
        return evaluation.evaluation_sha256

    def get(self, evaluation_id: str) -> PairedVOCEvaluation | None:
        wanted = _text("evaluation_id", evaluation_id)
        return self._load().get(wanted)

    def require_decision_context(
        self,
        context_sha256: str,
        *,
        as_of: str,
    ) -> dict[str, str]:
        expected = _sha256("context_sha256", context_sha256)
        resolver = self._canonical_authority_resolver
        if resolver is None:
            raise VOCEvaluationError("missing canonical VOC authority resolver")
        resolved = resolver.resolve_decision_context(expected, as_of=as_of)
        if resolved is None:
            raise VOCEvaluationError("canonical current VOC decision context is missing")
        if not isinstance(resolved, Mapping) or set(resolved) != _VOC_CURRENT_CONTEXT_FIELDS:
            raise VOCEvaluationError("canonical current VOC decision context schema is invalid")
        return {
            field: _text(f"canonical current VOC context {field}", resolved.get(field))
            for field in sorted(_VOC_CURRENT_CONTEXT_FIELDS)
        }

    def require(
        self,
        evaluation_id: str,
        *,
        evaluation_sha256: str,
        as_of: str,
    ) -> PairedVOCEvaluation:
        expected = _sha256("evaluation_sha256", evaluation_sha256)
        value = self.get(evaluation_id)
        if value is None:
            raise VOCEvaluationError("VOC evaluation is missing")
        if value.evaluation_sha256 != expected:
            raise VOCEvaluationError("VOC evaluation identity/digest mismatch")
        if _instant("evaluated_at", value.evaluated_at) > _instant("as_of", as_of):
            raise VOCEvaluationError("VOC evaluation is not causally available")
        resolver = self._canonical_authority_resolver
        if resolver is None:
            raise VOCEvaluationError("missing canonical VOC authority resolver")
        resolved = resolver.resolve(value, as_of=as_of)
        if resolved is None:
            raise VOCEvaluationError("canonical VOC authority could not resolve evaluation")
        if not isinstance(resolved, PairedVOCEvaluation):
            raise VOCEvaluationError("canonical VOC authority returned invalid evaluation")
        if resolved.payload() != value.payload():
            raise VOCEvaluationError("canonical VOC authority differs from routed evaluation")
        return resolved

    def values(self) -> tuple[PairedVOCEvaluation, ...]:
        loaded = self._load()
        return tuple(
            sorted(
                loaded.values(),
                key=lambda item: (
                    _instant("evaluated_at", item.evaluated_at),
                    item.evaluation_id,
                ),
            )
        )
