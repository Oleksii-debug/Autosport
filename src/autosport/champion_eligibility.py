from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping

from .drift_control import DriftControlError, DriftMonitor, DriftState
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
    effective_sample_size: int | None
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
            "degraded_streak",
            "recovery_streak",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ChampionEligibilityError(f"{name} must be a non-negative integer")
        if self.minimum_samples == 0 or self.minimum_effective_sample_size == 0:
            raise ChampionEligibilityError("minimum evidence thresholds must be positive")
        if self.effective_sample_size is not None and (
            isinstance(self.effective_sample_size, bool)
            or not isinstance(self.effective_sample_size, int)
            or self.effective_sample_size <= 0
        ):
            raise ChampionEligibilityError(
                "effective_sample_size must be a positive integer when present"
            )
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
        degraded_streak: int | None = None,
        recovery_streak: int | None = None,
        admissible_actions: tuple[str, ...] = ("WAIT",),
        research_trigger_id: str | None = None,
        reason: str = "",
    ) -> "ChampionEligibilityDecision":
        if not isinstance(registry, ScientificRegistry):
            raise TypeError("registry must be ScientificRegistry")

        ids = _tuple_text(finding_ids, "finding_ids")
        for threshold_name, threshold_value in (
            ("minimum_samples", minimum_samples),
            ("minimum_effective_sample_size", minimum_effective_sample_size),
        ):
            if (
                isinstance(threshold_value, bool)
                or not isinstance(threshold_value, int)
                or threshold_value <= 0
            ):
                raise ChampionEligibilityError(
                    f"{threshold_name} must be a positive integer"
                )
        evaluated_cutoff = _instant(evaluated_at, "evaluated_at")
        requested_scope = {
            "sport": _text(sport, "sport"),
            "league": _text(league, "league"),
            "regime": _text(regime, "regime"),
        }

        def _scope_from_authority(
            finding_payload: Mapping[str, Any],
            observation_payload: Mapping[str, Any],
        ) -> tuple[str, str, str] | None:
            for payload in (finding_payload, observation_payload):
                if all(type(payload.get(key)) is str for key in ("sport", "league", "regime")):
                    return (
                        payload["sport"],
                        payload["league"],
                        payload["regime"],
                    )
                if all(type(payload.get(key)) is str for key in ("sport_id", "league_id", "regime")):
                    return (
                        payload["sport_id"],
                        payload["league_id"],
                        payload["regime"],
                    )
            return None

        drift_monitor = DriftMonitor(registry)
        entries: list[RegistryEntry] = []
        counts: list[int] = []
        effective_sample_sizes: list[int | None] = []
        windows: list[tuple[datetime, datetime, str, DriftState, tuple[str, str, str] | None]] = []

        for finding_id in ids:
            try:
                entry, reference, observation = drift_monitor.require_canonical_finding(
                    finding_id,
                    as_of=evaluated_at,
                )
            except (DriftControlError, TypeError, ValueError) as exc:
                raise ChampionEligibilityError(
                    f"DriftFinding:{finding_id} canonical provenance is invalid"
                ) from exc

            finding = entry.payload
            if finding.get("strategy_version_id") != strategy_version_id:
                raise ChampionEligibilityError("drift finding strategy version scope mismatch")
            if finding.get("model_version_id") != model_version_id:
                raise ChampionEligibilityError("drift finding model scope mismatch")

            try:
                state = DriftState(finding.get("state"))
            except (TypeError, ValueError) as exc:
                raise ChampionEligibilityError("drift finding state is invalid") from exc

            reference_id = finding.get("reference_id")
            if type(reference_id) is not str:
                raise ChampionEligibilityError("drift finding reference identity is missing")

            baseline_sample_count = reference.payload.get("sample_count")
            if (
                isinstance(baseline_sample_count, bool)
                or not isinstance(baseline_sample_count, int)
                or baseline_sample_count <= 0
            ):
                raise ChampionEligibilityError("drift reference sample_count is invalid")
            baseline_effective_sample_size = reference.payload.get("effective_sample_size")
            if baseline_effective_sample_size is not None and (
                isinstance(baseline_effective_sample_size, bool)
                or not isinstance(baseline_effective_sample_size, int)
                or baseline_effective_sample_size <= 0
                or baseline_effective_sample_size > baseline_sample_count
            ):
                raise ChampionEligibilityError(
                    "drift reference effective_sample_size is invalid"
                )

            observation_id = finding.get("observation_id")
            if type(observation_id) is not str:
                raise ChampionEligibilityError("drift finding observation identity is missing")

            sample_count = observation.payload.get("sample_count")
            if isinstance(sample_count, bool) or not isinstance(sample_count, int) or sample_count <= 0:
                raise ChampionEligibilityError("drift observation sample_count is invalid")
            effective_sample_size = observation.payload.get("effective_sample_size")
            if effective_sample_size is not None and (
                isinstance(effective_sample_size, bool)
                or not isinstance(effective_sample_size, int)
                or effective_sample_size <= 0
                or effective_sample_size > sample_count
            ):
                raise ChampionEligibilityError(
                    "drift observation effective_sample_size is invalid"
                )

            observed_start = _instant(observation.payload.get("window_start"), "DriftObservation.window_start")
            observed_end = _instant(observation.payload.get("window_end"), "DriftObservation.window_end")
            if observed_end < observed_start:
                raise ChampionEligibilityError("DriftObservation window is inverted")

            scope = _scope_from_authority(finding, observation.payload)
            entries.append(entry)
            counts.extend((baseline_sample_count, sample_count))
            effective_sample_sizes.extend(
                (baseline_effective_sample_size, effective_sample_size)
            )
            windows.append((observed_start, observed_end, observation_id, state, scope))

        ordered = sorted(windows, key=lambda item: (item[0], item[1], item[2]))
        if len({item[2] for item in ordered}) != len(ordered):
            raise ChampionEligibilityError("drift findings must reference distinct observation windows")
        for previous, current in zip(ordered, ordered[1:]):
            if current[0] < previous[1]:
                raise ChampionEligibilityError(
                    "drift finding windows overlap and cannot establish repeated evidence"
                )

        tail_state = ordered[-1][3]
        derived_streak = 0
        for _start, _end, _observation_id, state, _scope in reversed(ordered):
            if state is not tail_state:
                break
            derived_streak += 1

        expected_degraded_streak = (
            derived_streak if tail_state is DriftState.DRIFT_DETECTED else 0
        )
        expected_recovery_streak = (
            derived_streak if tail_state is DriftState.NO_DRIFT else 0
        )
        if degraded_streak is not None and degraded_streak != expected_degraded_streak:
            raise ChampionEligibilityError(
                "degraded_streak must match causal finding windows"
            )
        if recovery_streak is not None and recovery_streak != expected_recovery_streak:
            raise ChampionEligibilityError(
                "recovery_streak must match causal finding windows"
            )

        # Scope is activation authority, not presentation metadata.  The current
        # DriftFinding/DriftObservation schema does not require sport/league/regime,
        # so an absent authoritative scope must fail closed.
        authoritative_scopes = [item[4] for item in ordered]
        if any(scope is None for scope in authoritative_scopes):
            scoped = None
        else:
            scoped = authoritative_scopes[0]
            if any(scope != scoped for scope in authoritative_scopes[1:]):
                raise ChampionEligibilityError("drift evidence scope changes across windows")

        canonical_window_start = min(item[0] for item in ordered).isoformat().replace("+00:00", "Z")
        canonical_window_end = max(item[1] for item in ordered).isoformat().replace("+00:00", "Z")
        if _ts(window_start, "window_start") != canonical_window_start:
            raise ChampionEligibilityError("window_start does not match causal evidence")
        if _ts(window_end, "window_end") != canonical_window_end:
            raise ChampionEligibilityError("window_end does not match causal evidence")

        canonical_effective_sample_size = (
            None
            if any(value is None for value in effective_sample_sizes)
            else min(value for value in effective_sample_sizes if value is not None)
        )

        if scoped is None:
            status = ChampionEligibilityStatus.WAIT_MORE_EVIDENCE
            status_reason = "authoritative_sport_league_regime_scope_is_missing"
        elif tuple(requested_scope.values()) != scoped:
            raise ChampionEligibilityError("drift evidence scope does not match champion scope")
        elif any(count < minimum_samples for count in counts) or (
            canonical_effective_sample_size is None
            or canonical_effective_sample_size < minimum_effective_sample_size
        ):
            status = ChampionEligibilityStatus.WAIT_MORE_EVIDENCE
            status_reason = "insufficient_or_under_supported_drift_evidence"
        elif tail_state is DriftState.DRIFT_DETECTED:
            if derived_streak < 2:
                status = ChampionEligibilityStatus.WAIT_MORE_EVIDENCE
                status_reason = "single_or_non_sustained_degradation_window"
            elif research_trigger_id is not None:
                if not research_trigger_id.startswith("champion-drift:"):
                    raise ChampionEligibilityError(
                        "research_trigger_id is not a canonical champion-drift trigger"
                    )
                status = ChampionEligibilityStatus.RESEARCH_REQUIRED
                status_reason = "sustained_scoped_degradation_requires_governed_research"
            else:
                status = ChampionEligibilityStatus.SHADOW_DEACTIVATED
                status_reason = "sustained_scoped_degradation"
        elif tail_state is DriftState.NO_DRIFT:
            if derived_streak < 2:
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
            effective_sample_size=canonical_effective_sample_size,
            degraded_streak=expected_degraded_streak,
            recovery_streak=expected_recovery_streak,
            admissible_actions=admissible_actions,
            research_trigger_id=research_trigger_id,
            reason=reason or status_reason,
        )


def _reject_later_contradictory_drift(
    registry: ScientificRegistry,
    decision: ChampionEligibilityDecision,
    *,
    as_of: str,
) -> None:
    """Reject an old positive lease after newer causal drift in the same exact scope.

    The decision's own finding set remains immutable historical evidence. Activation
    additionally inspects canonical findings that are causally visible at as_of so
    a later degraded window cannot be hidden simply by replaying the older decision.
    This is invalidation only: later NO_DRIFT evidence never extends valid_until.
    """

    decision_scope = (
        decision.sport,
        decision.league,
        decision.regime,
    )
    evidence_end = _instant(decision.window_end, "decision.window_end")
    monitor = DriftMonitor(registry)

    for candidate in registry.causal_records("DriftFinding", as_of=as_of):
        if candidate.record_id in decision.finding_ids:
            continue
        payload = candidate.payload
        if (
            payload.get("strategy_version_id") != decision.strategy_version_id
            or payload.get("model_version_id") != decision.model_version_id
        ):
            continue
        try:
            finding, _reference, observation = monitor.require_canonical_finding(
                candidate.record_id,
                as_of=as_of,
            )
            state = DriftState(finding.payload.get("state"))
            observation_end = _instant(
                observation.payload.get("window_end"),
                "later DriftObservation.window_end",
            )
            scope_values = tuple(
                _text(observation.payload.get(name), f"later DriftObservation.{name}")
                for name in ("sport", "league", "regime")
            )
        except (DriftControlError, ChampionEligibilityError, TypeError, ValueError):
            # Only independently canonical drift evidence may invalidate activation.
            # A caller-authored/malformed registry record is not deployment authority.
            continue

        if scope_values != decision_scope:
            continue
        if observation_end <= evidence_end:
            continue
        if state is DriftState.DRIFT_DETECTED:
            raise ChampionEligibilityError(
                "later canonical drift invalidates champion eligibility; "
                "explicit re-authorization is required"
            )


def _rederive_decision(
    registry: ScientificRegistry,
    decision: ChampionEligibilityDecision,
) -> ChampionEligibilityDecision:
    """Re-resolve one decision from canonical drift evidence before authority use."""

    try:
        rederived = ChampionEligibilityDecision.from_findings(
            registry,
            canonical_strategy_id=decision.canonical_strategy_id,
            strategy_version_id=decision.strategy_version_id,
            model_version_id=decision.model_version_id,
            environment_sha256=decision.environment_sha256,
            protocol_id=decision.protocol_id,
            config_sha256=decision.config_sha256,
            sport=decision.sport,
            league=decision.league,
            regime=decision.regime,
            finding_ids=decision.finding_ids,
            window_start=decision.window_start,
            window_end=decision.window_end,
            evaluated_at=decision.evaluated_at,
            valid_until=decision.valid_until,
            minimum_samples=decision.minimum_samples,
            minimum_effective_sample_size=decision.minimum_effective_sample_size,
            degraded_streak=decision.degraded_streak,
            recovery_streak=decision.recovery_streak,
            admissible_actions=decision.admissible_actions,
            research_trigger_id=decision.research_trigger_id,
            reason=decision.reason,
        )
    except (ChampionEligibilityError, TypeError, ValueError) as exc:
        raise ChampionEligibilityError(
            "champion eligibility canonical re-derivation failed"
        ) from exc

    if rederived.to_payload() != decision.to_payload():
        raise ChampionEligibilityError(
            "champion eligibility decision does not match canonical derivation"
        )
    return rederived


def persist_eligibility_decision(
    registry: ScientificRegistry,
    decision: ChampionEligibilityDecision,
) -> str:
    if not isinstance(registry, ScientificRegistry):
        raise TypeError("registry must be ScientificRegistry")
    if type(decision) is not ChampionEligibilityDecision:
        raise TypeError("decision must be exact ChampionEligibilityDecision")
    canonical = _rederive_decision(registry, decision)
    registry.append(canonical)
    return canonical.decision_id


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
    if type(decision) is not ChampionEligibilityDecision:
        raise TypeError("decision must be exact ChampionEligibilityDecision")
    _rederive_decision(registry, decision)
    if decision.status is not ChampionEligibilityStatus.ELIGIBLE:
        raise ChampionEligibilityError(f"champion eligibility is {decision.status.value}")
    cutoff = _instant(as_of, "as_of")
    if _instant(decision.available_at, "decision.available_at") > cutoff:
        raise ChampionEligibilityError("eligibility decision is future evidence")
    if _instant(decision.valid_until, "valid_until") < cutoff:
        raise ChampionEligibilityError("eligibility decision is expired")
    if decision.canonical_strategy_id != _text(canonical_strategy_id, "canonical_strategy_id"):
        raise ChampionEligibilityError("eligibility strategy mismatch")
    if decision.strategy_version_id != _text(expected_strategy_version_id, "expected_strategy_version_id"):
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
    expected_entry = {
        "record_type": entry.record_type,
        "record_id": entry.record_id,
        "available_at": _ts(entry.available_at, "entry.available_at"),
        "payload": decision.to_payload(),
    }
    if entry.record_sha256 != _digest(expected_entry):
        raise ChampionEligibilityError("eligibility decision record identity mismatch")
    _reject_later_contradictory_drift(
        registry,
        decision,
        as_of=as_of,
    )
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
