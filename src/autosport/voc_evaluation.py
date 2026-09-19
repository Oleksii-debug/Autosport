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

from .integrity import atomic_write_json
from .workspace_lock import WorkspaceEconomicLock

_SCHEMA = "autosport.voc_evaluation"
_VERSION = 1
_ZERO = Decimal("0")
_ONE = Decimal("1")


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
    baseline_candidate_id: str
    baseline_backend_id: str
    baseline_model_id: str
    baseline_config_sha256: str
    challenger_candidate_id: str
    challenger_backend_id: str
    challenger_model_id: str
    challenger_config_sha256: str
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
            "decision_evidence_sha256",
            "baseline_output_sha256",
            "challenger_output_sha256",
            "outcome_evidence_sha256",
            "scoring_rule_sha256",
            "research_protocol_sha256",
            "multiple_comparison_control_sha256",
        ):
            _sha256(name, getattr(self, name))
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
            "baseline_candidate_id": self.baseline_candidate_id,
            "baseline_backend_id": self.baseline_backend_id,
            "baseline_model_id": self.baseline_model_id,
            "baseline_config_sha256": self.baseline_config_sha256,
            "challenger_candidate_id": self.challenger_candidate_id,
            "challenger_backend_id": self.challenger_backend_id,
            "challenger_model_id": self.challenger_model_id,
            "challenger_config_sha256": self.challenger_config_sha256,
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
                baseline_candidate_id=raw["baseline_candidate_id"],
                baseline_backend_id=raw["baseline_backend_id"],
                baseline_model_id=raw["baseline_model_id"],
                baseline_config_sha256=raw["baseline_config_sha256"],
                challenger_candidate_id=raw["challenger_candidate_id"],
                challenger_backend_id=raw["challenger_backend_id"],
                challenger_model_id=raw["challenger_model_id"],
                challenger_config_sha256=raw["challenger_config_sha256"],
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
