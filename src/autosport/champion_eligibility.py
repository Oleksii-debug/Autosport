from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping

from .drift_control import DriftState
from .research_supervisor import ResearchSupervisor, ResearchTrigger
from .scientific_registry import RegistryEntry, ScientificRegistry


SCHEMA_VERSION = 1


class ChampionEligibilityError(RuntimeError):
    """Champion eligibility evidence is missing, stale, incompatible, or corrupted."""


class ChampionEligibilityStatus(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    WAIT_MORE_EVIDENCE = "WAIT_MORE_EVIDENCE"
    SHADOW_DEACTIVATED = "SHADOW_DEACTIVATED"
    RESEARCH_REQUIRED = "RESEARCH_REQUIRED"


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ChampionEligibilityError(f"{name} must be canonical non-empty text")
    value.encode("utf-8")
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise ChampionEligibilityError(f"{name} must be lowercase SHA-256")
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ChampionEligibilityError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ChampionEligibilityError(f"{name} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _ts(value: object, name: str) -> str:
    return _instant(value, name).isoformat().replace("+00:00", "Z")


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _tuple_text(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, tuple) or not value:
        raise ChampionEligibilityError(f"{name} must be a non-empty tuple")
    items = tuple(_text(item, f"{name} item") for item in value)
    if len(items) != len(set(items)):
        raise ChampionEligibilityError(f"{name} must not contain duplicates")
    return items


@dataclass(frozen=True, slots=True)
class ChampionEligibilityDecision:
    status: ChampionEligibilityStatus
    canonical_strategy_id: str
    strategy_version_id: str
    model_version_id: str
    environment_sha256: str
    protocol_id: str
    config_sha256: str
    sport: str
    league: str
    regime: str
    finding_ids: tuple[str, ...]
    finding_record_sha256s: tuple[str, ...]
    window_start: str
    window_end: str
    evaluated_at: str
    valid_until: str
    minimum_samples: int
    minimum_effective_sample_size: int
    effective_sample_size: int
    degraded_streak: int
    recovery_streak: int
    admissible_actions: tuple[str, ...]
    research_trigger_id: str | None = None
    reason: str = ""
    decision_id: str = ""

    def __post_init__(self) -> None:
        _text(self.canonical_strategy_id, "canonical_strategy_id")
        _text(self.strategy_version_id, "strategy_version_id")
        _text(self.model_version_id, "model_version_id")
        _sha(self.environment_sha256, "environment_sha256")
        _text(self.protocol_id, "protocol_id")
        _sha(self.config_sha256, "config_sha256")
        for name in ("sport", "league", "regime"):
            _text(getattr(self, name), name)
        ids = _tuple_text(self.finding_ids, "finding_ids")
        hashes = _tuple_text(self.finding_record_sha256s, "finding_record_sha256s")
        if len(ids) != len(hashes):
            raise ChampionEligibilityError("finding identity/hash lengths differ")
        for value in hashes:
            _sha(value, "finding_record_sha256")
        start = _instant(self.window_start, "window_start")
        end = _instant(self.window_end, "window_end")
        evaluated = _instant(self.evaluated_at, "evaluated_at")
        valid = _instant(self.valid_until, "valid_until")
        if end < start:
            raise ChampionEligibilityError("window_end cannot precede window_start")
        if evaluated < end:
            raise ChampionEligibilityError("evaluated_at must cover the evidence window")
        if valid < evaluated:
            raise ChampionEligibilityError("valid_until cannot precede evaluated_at")
        for name in (
            "minimum_samples",
            "minimum_effective_sample_size",
            "effective_sample_size",
            "degraded_streak",
            "recovery_streak",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ChampionEligibilityError(f"{name} must be a non-negative integer")
        if self.minimum_samples == 0 or self.minimum_effective_sample_size == 0:
            raise ChampionEligibilityError("minimum evidence thresholds must be positive")
        if self.effective_sample_size > sum(1 for _ in ids) * max(self.minimum_effective_sample_size, self.effective_sample_size):
            raise ChampionEligibilityError("effective_sample_size is malformed")
        actions = _tuple_text(self.admissible_actions, "admissible_actions")
        if self.research_trigger_id is not None:
            _text(self.research_trigger_id, "research_trigger_id")
        if type(self.reason) is not str:
            raise ChampionEligibilityError("reason must be text")
        expected = _digest(self._payload_without_id())
        if self.decision_id and self.decision_id != expected:
            raise ChampionEligibilityError("decision_id does not match canonical evidence")
        object.__setattr__(self, "decision_id", expected)

    def _payload_without_id(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": self.status.value,
            "canonical_strategy_id": self.canonical_strategy_id,
            "strategy_version_id": self.strategy_version_id,
            "model_version_id": self.model_version_id,
            "environment_sha256": self.environment_sha256.lower(),
            "protocol_id": self.protocol_id,
            "config_sha256": self.config_sha256.lower(),
            "sport": self.sport,
            "league": self.league,
            "regime": self.regime,
            "finding_ids": list(self.finding_ids),
            "finding_record_sha256s": [value.lower() for value in self.finding_record_sha256s],
            "window_start": _ts(self.window_start, "window_start"),
            "window_end": _ts(self.window_end, "window_end"),
            "evaluated_at": _ts(self.evaluated_at, "evaluated_at"),
            "valid_until": _ts(self.valid_until, "valid_until"),
            "minimum_samples": self.minimum_samples,
            "minimum_effective_sample_size": self.minimum_effective_sample_size,
            "effective_sample_size": self.effective_sample_size,
            "degraded_streak": self.degraded_streak,
            "recovery_streak": self.recovery_streak,
            "admissible_actions": list(self.admissible_actions),
            "research_trigger_id": self.research_trigger_id,
            "reason": self.reason,
        }

    @property
    def record_type(self) -> str:
        return "ChampionEligibilityDecision"

    @property
    def record_id(self) -> str:
        return self.decision_id

    @property
    def available_at(self) -> str:
        return _ts(self.evaluated_at, "evaluated_at")

    def to_payload(self) -> dict[str, Any]:
        return {"decision_id": self.decision_id, **self._payload_without_id()}

    @classmethod
    def from_findings(
        cls,
        registry: ScientificRegistry,
        *,
        canonical_strategy_id: str,
        strategy_version_id: str,
        model_version_id: str,
        environment_sha256: str,
        protocol_id: str,
        config_sha256: str,
        sport: str,
        league: str,
        regime: str,
        finding_ids: tuple[str, ...],
        window_start: str,
        window_end: str,
        evaluated_at: str,
        valid_until: str,
        minimum_samples: int,
        minimum_effective_sample_size: int,
        degraded_streak: int,
        recovery_streak: int,
        admissible_actions: tuple[str, ...],
        research_trigger_id: str | None = None,
        reason: str = "",
    ) -> "ChampionEligibilityDecision":
        if not isinstance(registry, ScientificRegistry):
            raise TypeError("registry must be ScientificRegistry")
        ids = _tuple_text(finding_ids, "finding_ids")
        evaluated_cutoff = _instant(evaluated_at, "evaluated_at")
        entries: list[RegistryEntry] = []
        counts: list[int] = []
        states: list[DriftState] = []
        for finding_id in ids:
            entry = registry.get("DriftFinding", finding_id)
            if entry is None:
                raise ChampionEligibilityError(f"missing DriftFinding:{finding_id}")
            if _instant(entry.available_at, "DriftFinding.available_at") > evaluated_cutoff:
                raise ChampionEligibilityError(f"DriftFinding:{finding_id} is future evidence")
            finding = entry.payload
            if finding.get("strategy_version_id") != canonical_strategy_id:
                raise ChampionEligibilityError("drift finding strategy scope mismatch")
            if finding.get("model_version_id") != model_version_id:
                raise ChampionEligibilityError("drift finding model scope mismatch")
            try:
                state = DriftState(finding.get("state"))
            except (TypeError, ValueError) as exc:
                raise ChampionEligibilityError("drift finding state is invalid") from exc
            observation_id = finding.get("observation_id")
            if type(observation_id) is not str:
                raise ChampionEligibilityError("drift finding observation identity is missing")
            observation = registry.get("DriftObservation", observation_id)
            if observation is None:
                raise ChampionEligibilityError("drift observation is missing")
            sample_count = observation.payload.get("sample_count")
            if isinstance(sample_count, bool) or not isinstance(sample_count, int) or sample_count <= 0:
                raise ChampionEligibilityError("drift observation sample_count is invalid")
            entries.append(entry)
            counts.append(sample_count)
            states.append(state)

        effective_sample_size = min(counts)
        if effective_sample_size < minimum_effective_sample_size or any(
            count < minimum_samples for count in counts
        ):
            status = ChampionEligibilityStatus.WAIT_MORE_EVIDENCE
            status_reason = "insufficient_or_under_supported_drift_evidence"
        elif all(state is DriftState.DRIFT_DETECTED for state in states):
            if degraded_streak < 2:
                status = ChampionEligibilityStatus.WAIT_MORE_EVIDENCE
                status_reason = "single_or_non_sustained_degradation_window"
            elif research_trigger_id is not None:
                status = ChampionEligibilityStatus.RESEARCH_REQUIRED
                status_reason = "sustained_scoped_degradation_requires_governed_research"
            else:
                status = ChampionEligibilityStatus.SHADOW_DEACTIVATED
                status_reason = "sustained_scoped_degradation"
        elif all(state is DriftState.NO_DRIFT for state in states):
            if recovery_streak > 0 and recovery_streak < 2:
                status = ChampionEligibilityStatus.WAIT_MORE_EVIDENCE
                status_reason = "recovery_requires_repeated_fresh_evidence"
            else:
                status = ChampionEligibilityStatus.ELIGIBLE
                status_reason = "current_scoped_evidence_within_drift_bounds"
        else:
            status = ChampionEligibilityStatus.WAIT_MORE_EVIDENCE
            status_reason = "mixed_drift_states_require_further_evidence"

        return cls(
            status=status,
            canonical_strategy_id=canonical_strategy_id,
            strategy_version_id=strategy_version_id,
            model_version_id=model_version_id,
            environment_sha256=environment_sha256,
            protocol_id=protocol_id,
            config_sha256=config_sha256,
            sport=sport,
            league=league,
            regime=regime,
            finding_ids=ids,
            finding_record_sha256s=tuple(entry.record_sha256 for entry in entries),
            window_start=window_start,
            window_end=window_end,
            evaluated_at=evaluated_at,
            valid_until=valid_until,
            minimum_samples=minimum_samples,
            minimum_effective_sample_size=minimum_effective_sample_size,
            effective_sample_size=effective_sample_size,
            degraded_streak=degraded_streak,
            recovery_streak=recovery_streak,
            admissible_actions=admissible_actions,
            research_trigger_id=research_trigger_id,
            reason=reason or status_reason,
        )


def persist_eligibility_decision(
    registry: ScientificRegistry,
    decision: ChampionEligibilityDecision,
) -> str:
    if not isinstance(registry, ScientificRegistry):
        raise TypeError("registry must be ScientificRegistry")
    if not isinstance(decision, ChampionEligibilityDecision):
        raise TypeError("decision must be ChampionEligibilityDecision")
    registry.append(decision)
    return decision.decision_id


def bind_research_trigger(
    supervisor: ResearchSupervisor,
    decision: ChampionEligibilityDecision,
    *,
    question_id: str,
    requested_at: str,
    budget_units: int,
) -> str:
    """Bind one deterministic trigger; ResearchSupervisor makes exact redelivery idempotent."""
    if not isinstance(supervisor, ResearchSupervisor):
        raise TypeError("supervisor must be ResearchSupervisor")
    if not isinstance(decision, ChampionEligibilityDecision):
        raise TypeError("decision must be ChampionEligibilityDecision")
    trigger = ResearchTrigger(
        trigger_id=f"champion-drift:{decision.decision_id}",
        question_id=question_id,
        requested_at=requested_at,
        budget_units=budget_units,
    )
    snapshot = supervisor.accept_trigger(
        trigger,
        exclusive_trigger_prefix="champion-drift:",
    )
    return snapshot.run_id


def validate_activation_eligibility(
    registry: ScientificRegistry,
    decision: ChampionEligibilityDecision,
    *,
    as_of: str,
    canonical_strategy_id: str,
    expected_strategy_version_id: str,
    expected_model_version_id: str,
    expected_environment_sha256: str,
    expected_protocol_id: str,
    expected_config_sha256: str,
    admissible_actions: frozenset[str],
) -> None:
    """Pure fail-closed gate for champion activation; runtime actions may only narrow."""
    if not isinstance(registry, ScientificRegistry):
        raise TypeError("registry must be ScientificRegistry")
    if not isinstance(decision, ChampionEligibilityDecision):
        raise TypeError("decision must be ChampionEligibilityDecision")
    if decision.status is not ChampionEligibilityStatus.ELIGIBLE:
        raise ChampionEligibilityError(f"champion eligibility is {decision.status.value}")
    cutoff = _instant(as_of, "as_of")
    if _instant(decision.available_at, "decision.available_at") > cutoff:
        raise ChampionEligibilityError("eligibility decision is future evidence")
    if _instant(decision.valid_until, "valid_until") < cutoff:
        raise ChampionEligibilityError("eligibility decision is expired")
    if decision.canonical_strategy_id != _text(canonical_strategy_id, "canonical_strategy_id"):
        raise ChampionEligibilityError("eligibility strategy mismatch")
    if decision.canonical_strategy_id != _text(expected_strategy_version_id, "expected_strategy_version_id"):
        raise ChampionEligibilityError("eligibility champion identity mismatch")
    if decision.model_version_id != _text(expected_model_version_id, "expected_model_version_id"):
        raise ChampionEligibilityError("eligibility model identity mismatch")
    if decision.environment_sha256 != _sha(expected_environment_sha256, "expected_environment_sha256"):
        raise ChampionEligibilityError("eligibility environment mismatch")
    if decision.protocol_id != _text(expected_protocol_id, "expected_protocol_id"):
        raise ChampionEligibilityError("eligibility protocol mismatch")
    if decision.config_sha256 != _sha(expected_config_sha256, "expected_config_sha256"):
        raise ChampionEligibilityError("eligibility config mismatch")
    entry = registry.get(decision.record_type, decision.record_id)
    if entry is None:
        raise ChampionEligibilityError("eligibility decision is not durably registered")
    if entry.record_sha256 != _digest(decision.to_payload()):
        raise ChampionEligibilityError("eligibility decision record identity mismatch")
    if type(admissible_actions) is not frozenset or not admissible_actions:
        raise ChampionEligibilityError("admissible_actions must be a non-empty frozenset")
    if not frozenset(admissible_actions).issubset(frozenset(decision.admissible_actions)):
        raise ChampionEligibilityError("activation actions may only be narrowed")
    for finding_id, finding_sha in zip(decision.finding_ids, decision.finding_record_sha256s):
        finding = registry.get("DriftFinding", finding_id)
        if finding is None or finding.record_sha256 != finding_sha:
            raise ChampionEligibilityError("eligibility drift finding binding is stale or tampered")
        if _instant(finding.available_at, "DriftFinding.available_at") > cutoff:
            raise ChampionEligibilityError("eligibility references future drift evidence")
