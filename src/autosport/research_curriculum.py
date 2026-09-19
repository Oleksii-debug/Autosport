"""Governed night/idle research curriculum for Autosport.

This is a thin selection/dispatch seam over the existing ResearchTriggerAdapter and
ResearchSupervisor.  It owns selection evidence and restart state only; it does not
own scheduling, replay, scientific truth, promotion, risk, or provider execution.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Iterable

from .integrity import atomic_write_json
from .research_supervisor import SupervisorStatus
from .research_trigger_adapter import (
    ExternalResearchTrigger,
    ResearchTriggerAdapter,
    ResearchTriggerReceipt,
    ResearchTriggerSource,
)
from .workspace_lock import WorkspaceEconomicLock

SCHEMA: Final = "autosport.research_curriculum"
SCHEMA_VERSION: Final = 1
_HEX: Final = frozenset("0123456789abcdef")


class ResearchCurriculumError(RuntimeError):
    """Curriculum evidence/state is invalid or violates a causal boundary."""


class CurriculumPurpose(StrEnum):
    CURRICULUM = "CURRICULUM"
    CONFIRMATORY = "CONFIRMATORY"


class ReplayProvenance(StrEnum):
    HISTORICAL_OBSERVED = "HISTORICAL_OBSERVED"
    SHADOW_LIVE = "SHADOW_LIVE"
    PAPER_LIVE = "PAPER_LIVE"
    REAL_EXECUTION = "REAL_EXECUTION"
    HISTORICAL_COUNTERFACTUAL_LIMITED = "HISTORICAL_COUNTERFACTUAL_LIMITED"
    SYNTHETIC_WORLD_MODEL = "SYNTHETIC_WORLD_MODEL"


class CurriculumStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"


class CurriculumOutcome(StrEnum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    NULL = "NULL"
    STOPPED = "STOPPED"


_NONCONFIRMATORY_PROVENANCE = frozenset(
    {
        ReplayProvenance.HISTORICAL_COUNTERFACTUAL_LIMITED,
        ReplayProvenance.SYNTHETIC_WORLD_MODEL,
    }
)


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ResearchCurriculumError(f"{name} must be canonical non-empty text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ResearchCurriculumError(f"{name} must be UTF-8") from exc
    return value


def _instant(value: object, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(_text(value, name).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResearchCurriculumError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ResearchCurriculumError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _timestamp(value: object, name: str) -> str:
    return _instant(value, name).isoformat().replace("+00:00", "Z")


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if text != text.lower() or len(text) != 64 or any(c not in _HEX for c in text):
        raise ResearchCurriculumError(f"{name} must be lowercase SHA-256")
    return text


def _decimal(value: object, name: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ResearchCurriculumError(f"{name} must be a finite Decimal")
    if value < 0 or (positive and value == 0):
        raise ResearchCurriculumError(f"{name} is outside its valid range")
    return value


def _strings(value: object, name: str) -> tuple[str, ...]:
    if type(value) is not tuple or not value:
        raise ResearchCurriculumError(f"{name} must be a non-empty tuple")
    normalized = tuple(_text(item, f"{name} member") for item in value)
    if normalized != tuple(sorted(normalized)) or len(normalized) != len(set(normalized)):
        raise ResearchCurriculumError(f"{name} must be sorted and unique")
    return normalized


def _pairs(value: object, name: str) -> tuple[tuple[str, str], ...]:
    if type(value) is not tuple:
        raise ResearchCurriculumError(f"{name} must be a tuple")
    normalized: list[tuple[str, str]] = []
    for item in value:
        if type(item) is not tuple or len(item) != 2:
            raise ResearchCurriculumError(f"{name} entries must be pairs")
        normalized.append((_text(item[0], f"{name} key"), _text(item[1], f"{name} value")))
    if normalized != sorted(normalized):
        raise ResearchCurriculumError(f"{name} must be sorted")
    keys = [key for key, _ in normalized]
    if len(keys) != len(set(keys)):
        raise ResearchCurriculumError(f"{name} keys must be unique")
    return tuple(normalized)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ReplayCandidate:
    question_id: str
    episode_id: str
    environment_id: str
    available_at: str
    provenance: ReplayProvenance
    reasons: tuple[str, ...]
    selector_features: tuple[tuple[str, str], ...]
    priority: int
    expected_learning_value: Decimal
    sampling_probability: Decimal | None = None
    sampling_weight: Decimal | None = None
    outcome_available_at: str | None = None

    def __post_init__(self) -> None:
        _text(self.question_id, "question_id")
        _sha(self.episode_id, "episode_id")
        _sha(self.environment_id, "environment_id")
        _instant(self.available_at, "available_at")
        if not isinstance(self.provenance, ReplayProvenance):
            raise ResearchCurriculumError("provenance must be ReplayProvenance")
        _strings(self.reasons, "reasons")
        _pairs(self.selector_features, "selector_features")
        if isinstance(self.priority, bool) or not isinstance(self.priority, int) or self.priority < 0:
            raise ResearchCurriculumError("priority must be a non-negative integer")
        _decimal(self.expected_learning_value, "expected_learning_value")
        if self.sampling_probability is not None:
            probability = _decimal(self.sampling_probability, "sampling_probability", positive=True)
            if probability > 1:
                raise ResearchCurriculumError("sampling_probability cannot exceed 1")
        if self.sampling_weight is not None:
            _decimal(self.sampling_weight, "sampling_weight", positive=True)
        if self.outcome_available_at is not None:
            _instant(self.outcome_available_at, "outcome_available_at")

    def payload(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "episode_id": self.episode_id,
            "environment_id": self.environment_id,
            "available_at": _timestamp(self.available_at, "available_at"),
            "provenance": self.provenance.value,
            "reasons": list(self.reasons),
            "selector_features": [list(item) for item in self.selector_features],
            "priority": self.priority,
            "expected_learning_value": str(self.expected_learning_value),
            "sampling_probability": None if self.sampling_probability is None else str(self.sampling_probability),
            "sampling_weight": None if self.sampling_weight is None else str(self.sampling_weight),
            "outcome_available_at": (
                None if self.outcome_available_at is None
                else _timestamp(self.outcome_available_at, "outcome_available_at")
            ),
        }

    @property
    def candidate_id(self) -> str:
        return _digest({"schema": SCHEMA, "schema_version": SCHEMA_VERSION, "kind": "ReplayCandidate", **self.payload()})


@dataclass(frozen=True, slots=True)
class CurriculumSelectionRecord:
    selector_policy_version: str
    purpose: CurriculumPurpose
    eligible_candidate_ids: tuple[str, ...]
    eligible_question_ids: tuple[str, ...]
    eligible_episode_ids: tuple[str, ...]
    selected_question_id: str
    selected_candidate_id: str
    selected_episode_id: str
    selected_reasons: tuple[str, ...]
    selected_features: tuple[tuple[str, str], ...]
    selected_provenance: ReplayProvenance
    priority: int
    expected_learning_value: Decimal
    sampling_probability: Decimal | None
    sampling_weight: Decimal | None
    outcome_information_available: bool
    selected_outcome_available_at: str | None
    budget_units: int
    seed: int
    as_of: str
    source_observed_at: str

    def __post_init__(self) -> None:
        _text(self.selector_policy_version, "selector_policy_version")
        if not isinstance(self.purpose, CurriculumPurpose):
            raise ResearchCurriculumError("purpose must be CurriculumPurpose")
        for name in ("eligible_candidate_ids", "eligible_episode_ids"):
            for value in _strings(getattr(self, name), name):
                _sha(value, f"{name} member")
        _strings(self.eligible_question_ids, "eligible_question_ids")
        _text(self.selected_question_id, "selected_question_id")
        _sha(self.selected_candidate_id, "selected_candidate_id")
        _sha(self.selected_episode_id, "selected_episode_id")
        _strings(self.selected_reasons, "selected_reasons")
        _pairs(self.selected_features, "selected_features")
        if not isinstance(self.selected_provenance, ReplayProvenance):
            raise ResearchCurriculumError("selected_provenance must be ReplayProvenance")
        if isinstance(self.priority, bool) or not isinstance(self.priority, int) or self.priority < 0:
            raise ResearchCurriculumError("priority must be a non-negative integer")
        _decimal(self.expected_learning_value, "expected_learning_value")
        if self.sampling_probability is not None:
            probability = _decimal(self.sampling_probability, "sampling_probability", positive=True)
            if probability > 1:
                raise ResearchCurriculumError("sampling_probability cannot exceed 1")
        if self.sampling_weight is not None:
            _decimal(self.sampling_weight, "sampling_weight", positive=True)
        if type(self.outcome_information_available) is not bool:
            raise ResearchCurriculumError("outcome_information_available must be bool")
        if self.selected_outcome_available_at is not None:
            _instant(self.selected_outcome_available_at, "selected_outcome_available_at")
        if isinstance(self.budget_units, bool) or not isinstance(self.budget_units, int) or self.budget_units <= 0:
            raise ResearchCurriculumError("budget_units must be positive")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            raise ResearchCurriculumError("seed must be non-negative")
        if _instant(self.source_observed_at, "source_observed_at") > _instant(self.as_of, "as_of"):
            raise ResearchCurriculumError("source_observed_at cannot follow as_of")

    def payload(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "kind": "CurriculumSelectionRecord",
            "selector_policy_version": self.selector_policy_version,
            "purpose": self.purpose.value,
            "eligible_candidate_ids": list(self.eligible_candidate_ids),
            "eligible_question_ids": list(self.eligible_question_ids),
            "eligible_episode_ids": list(self.eligible_episode_ids),
            "selected_question_id": self.selected_question_id,
            "selected_candidate_id": self.selected_candidate_id,
            "selected_episode_id": self.selected_episode_id,
            "selected_reasons": list(self.selected_reasons),
            "selected_features": [list(item) for item in self.selected_features],
            "selected_provenance": self.selected_provenance.value,
            "priority": self.priority,
            "expected_learning_value": str(self.expected_learning_value),
            "sampling_probability": None if self.sampling_probability is None else str(self.sampling_probability),
            "sampling_weight": None if self.sampling_weight is None else str(self.sampling_weight),
            "outcome_information_available": self.outcome_information_available,
            "selected_outcome_available_at": (
                None
                if self.selected_outcome_available_at is None
                else _timestamp(
                    self.selected_outcome_available_at,
                    "selected_outcome_available_at",
                )
            ),
            "budget_units": self.budget_units,
            "seed": self.seed,
            "as_of": _timestamp(self.as_of, "as_of"),
            "source_observed_at": _timestamp(self.source_observed_at, "source_observed_at"),
        }

    @property
    def selection_id(self) -> str:
        return _digest(self.payload())


@dataclass(frozen=True, slots=True)
class CurriculumDispatchReceipt:
    selection_id: str
    trigger_receipt: ResearchTriggerReceipt

    @property
    def run_id(self) -> str:
        return self.trigger_receipt.run_id


class NightResearchCurriculum:
    """Restart-safe selector whose only dispatch path is ResearchTriggerAdapter."""

    def __init__(self, path: str | Path, trigger_adapter: ResearchTriggerAdapter, *, max_budget_units: int) -> None:
        if not isinstance(trigger_adapter, ResearchTriggerAdapter):
            raise TypeError("trigger_adapter must be ResearchTriggerAdapter")
        if isinstance(max_budget_units, bool) or not isinstance(max_budget_units, int) or max_budget_units <= 0:
            raise ResearchCurriculumError("max_budget_units must be positive")
        self.path = Path(path)
        self.trigger_adapter = trigger_adapter
        self.max_budget_units = max_budget_units
        if self.path.parent.resolve() != trigger_adapter.supervisor.path.parent.resolve():
            raise ResearchCurriculumError("curriculum state must share the research workspace")
        if not self.path.exists():
            self._write(self._pristine())
        self._validate(self._read())

    @classmethod
    def initialize_pristine(cls, path: str | Path, trigger_adapter: ResearchTriggerAdapter, *, max_budget_units: int) -> "NightResearchCurriculum":
        if Path(path).exists():
            raise FileExistsError(path)
        return cls(path, trigger_adapter, max_budget_units=max_budget_units)

    def _pristine(self) -> dict[str, Any]:
        body = {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "state_version": 0,
            "status": CurriculumStatus.ACTIVE.value,
            "max_budget_units": self.max_budget_units,
            "consumed_budget_units": 0,
            "selections": [],
            "dispatches": {},
            "outcomes": {},
            "stop_reason": None,
        }
        return {**body, "state_sha256": _digest(body)}

    def _read(self) -> dict[str, Any]:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ResearchCurriculumError("curriculum state is unreadable") from exc
        if not isinstance(state, dict):
            raise ResearchCurriculumError("curriculum state root must be an object")
        return state

    def _write(self, state: dict[str, Any]) -> None:
        body = {key: value for key, value in state.items() if key != "state_sha256"}
        atomic_write_json(self.path, {**body, "state_sha256": _digest(body)})

    def _validate(self, state: dict[str, Any]) -> None:
        if state.get("schema") != SCHEMA or state.get("schema_version") != SCHEMA_VERSION:
            raise ResearchCurriculumError("curriculum schema mismatch")
        if state.get("max_budget_units") != self.max_budget_units:
            raise ResearchCurriculumError("curriculum budget authority changed across restart")
        body = {key: value for key, value in state.items() if key != "state_sha256"}
        if state.get("state_sha256") != _digest(body):
            raise ResearchCurriculumError("curriculum state digest mismatch")
        try:
            CurriculumStatus(state.get("status"))
        except ValueError as exc:
            raise ResearchCurriculumError("invalid curriculum status") from exc
        consumed = state.get("consumed_budget_units")
        if isinstance(consumed, bool) or not isinstance(consumed, int) or not 0 <= consumed <= self.max_budget_units:
            raise ResearchCurriculumError("invalid consumed_budget_units")
        if not isinstance(state.get("selections"), list) or not isinstance(state.get("dispatches"), dict) or not isinstance(state.get("outcomes"), dict):
            raise ResearchCurriculumError("curriculum state collections are invalid")

    def _locked_state(self) -> dict[str, Any]:
        state = self._read()
        self._validate(state)
        return state

    @property
    def status(self) -> CurriculumStatus:
        return CurriculumStatus(self._read()["status"])

    def _set_status(self, target: CurriculumStatus, *, reason: str | None = None) -> None:
        if target is CurriculumStatus.STOPPED:
            _text(reason, "stop reason")
        elif reason is not None:
            raise ResearchCurriculumError("reason is only valid for STOPPED")
        with WorkspaceEconomicLock(self.path.parent):
            state = self._locked_state()
            if CurriculumStatus(state["status"]) is CurriculumStatus.STOPPED:
                raise ResearchCurriculumError("stopped curriculum cannot change status")
            state["status"] = target.value
            state["stop_reason"] = reason
            state["state_version"] += 1
            self._write(state)

    def pause(self) -> None:
        self._set_status(CurriculumStatus.PAUSED)

    def resume(self) -> None:
        self._set_status(CurriculumStatus.ACTIVE)

    def stop(self, reason: str) -> None:
        self._set_status(CurriculumStatus.STOPPED, reason=reason)

    def _eligible(self, candidates: Iterable[ReplayCandidate], *, purpose: CurriculumPurpose, as_of: str, state: dict[str, Any]) -> tuple[ReplayCandidate, ...]:
        cutoff = _instant(as_of, "as_of")
        prior_curriculum_episodes = {
            raw["selected_episode_id"]
            for raw in state["selections"]
            if raw.get("purpose") == CurriculumPurpose.CURRICULUM.value
        }
        seen: set[str] = set()
        eligible: list[ReplayCandidate] = []
        for candidate in candidates:
            if not isinstance(candidate, ReplayCandidate):
                raise ResearchCurriculumError("candidates must be ReplayCandidate values")
            if candidate.candidate_id in seen:
                raise ResearchCurriculumError("candidate population contains duplicates")
            seen.add(candidate.candidate_id)
            if _instant(candidate.available_at, "candidate.available_at") > cutoff:
                continue
            outcome_visible = (
                candidate.outcome_available_at is not None
                and _instant(candidate.outcome_available_at, "outcome_available_at") <= cutoff
            )
            if purpose is CurriculumPurpose.CONFIRMATORY and (
                outcome_visible
                or candidate.provenance in _NONCONFIRMATORY_PROVENANCE
                or candidate.episode_id in prior_curriculum_episodes
            ):
                continue
            eligible.append(candidate)
        return tuple(eligible)

    def select(self, candidates: Iterable[ReplayCandidate], *, purpose: CurriculumPurpose, selector_policy_version: str, as_of: str, seed: int, budget_units: int) -> CurriculumSelectionRecord:
        if not isinstance(purpose, CurriculumPurpose):
            raise ResearchCurriculumError("purpose must be CurriculumPurpose")
        _text(selector_policy_version, "selector_policy_version")
        as_of = _timestamp(as_of, "as_of")
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise ResearchCurriculumError("seed must be non-negative")
        if isinstance(budget_units, bool) or not isinstance(budget_units, int) or budget_units <= 0:
            raise ResearchCurriculumError("budget_units must be positive")
        with WorkspaceEconomicLock(self.path.parent):
            state = self._locked_state()
            if CurriculumStatus(state["status"]) is not CurriculumStatus.ACTIVE:
                raise ResearchCurriculumError("curriculum is not active")
            eligible = self._eligible(candidates, purpose=purpose, as_of=as_of, state=state)
            if not eligible:
                raise ResearchCurriculumError("no causally eligible replay candidates")
            ranked = sorted(
                eligible,
                key=lambda candidate: (
                    -candidate.priority,
                    -candidate.expected_learning_value,
                    candidate.candidate_id,
                ),
            )
            selected = ranked[0]
            outcome_visible = (
                selected.outcome_available_at is not None
                and _instant(selected.outcome_available_at, "outcome_available_at")
                <= _instant(as_of, "as_of")
            )
            record = CurriculumSelectionRecord(
                selector_policy_version=selector_policy_version,
                purpose=purpose,
                eligible_candidate_ids=tuple(sorted(item.candidate_id for item in eligible)),
                eligible_question_ids=tuple(sorted({item.question_id for item in eligible})),
                eligible_episode_ids=tuple(sorted({item.episode_id for item in eligible})),
                selected_question_id=selected.question_id,
                selected_candidate_id=selected.candidate_id,
                selected_episode_id=selected.episode_id,
                selected_reasons=selected.reasons,
                selected_features=selected.selector_features,
                selected_provenance=selected.provenance,
                priority=selected.priority,
                expected_learning_value=selected.expected_learning_value,
                sampling_probability=selected.sampling_probability,
                sampling_weight=selected.sampling_weight,
                outcome_information_available=outcome_visible,
                selected_outcome_available_at=selected.outcome_available_at,
                budget_units=budget_units,
                seed=seed,
                as_of=as_of,
                source_observed_at=_timestamp(selected.available_at, "available_at"),
            )
            payload = {**record.payload(), "selection_id": record.selection_id}
            prior = next(
                (item for item in state["selections"] if item.get("selection_id") == record.selection_id),
                None,
            )
            if prior is not None and prior != payload:
                raise ResearchCurriculumError("selection identity conflict")
            if prior is None:
                if state["consumed_budget_units"] + budget_units > self.max_budget_units:
                    raise ResearchCurriculumError("curriculum budget exhausted")
                state["selections"].append(payload)
                state["selections"].sort(key=lambda item: item["selection_id"])
                state["state_version"] += 1
                self._write(state)
            return record

    def dispatch(self, record: CurriculumSelectionRecord, *, deadline_at: str | None = None) -> CurriculumDispatchReceipt:
        if not isinstance(record, CurriculumSelectionRecord):
            raise ResearchCurriculumError("record must be CurriculumSelectionRecord")
        registry = self.trigger_adapter.supervisor.scientific_registry
        question = registry.get("ResearchQuestion", record.selected_question_id)
        if question is None:
            raise ResearchCurriculumError("selected ResearchQuestion is missing")
        if _instant(question.available_at, "ResearchQuestion.available_at") > _instant(record.as_of, "as_of"):
            raise ResearchCurriculumError("selected ResearchQuestion was not causally available")
        source_sha = question.payload.get("source_sha256")
        if not isinstance(source_sha, str):
            raise ResearchCurriculumError("ResearchQuestion has no source_sha256")
        event = ExternalResearchTrigger(
            source_kind=ResearchTriggerSource.SCHEDULE,
            source_scope=f"night-curriculum:{record.selector_policy_version}",
            source_event_id=record.selection_id,
            question_id=record.selected_question_id,
            question_record_sha256=question.record_sha256,
            source_evidence_sha256=source_sha,
            source_observed_at=record.source_observed_at,
            requested_at=record.as_of,
            budget_units=record.budget_units,
            deadline_at=deadline_at,
        )
        reservation = {
            "status": "PENDING",
            "selection_id": record.selection_id,
            "source_event_sha256": event.source_event_sha256,
            "budget_units": record.budget_units,
        }
        with WorkspaceEconomicLock(self.path.parent):
            state = self._locked_state()
            prior = state["dispatches"].get(record.selection_id)
            if prior is None:
                if CurriculumStatus(state["status"]) is not CurriculumStatus.ACTIVE:
                    raise ResearchCurriculumError("curriculum is not active")
                if state["consumed_budget_units"] + record.budget_units > self.max_budget_units:
                    raise ResearchCurriculumError("curriculum budget exhausted")
                state["dispatches"][record.selection_id] = reservation
                state["consumed_budget_units"] += record.budget_units
                state["state_version"] += 1
                self._write(state)
            else:
                if (
                    prior.get("selection_id") != record.selection_id
                    or prior.get("source_event_sha256") != event.source_event_sha256
                    or prior.get("budget_units") != record.budget_units
                    or prior.get("status") not in {"PENDING", "ACCEPTED"}
                ):
                    raise ResearchCurriculumError("dispatch identity conflict")

        # Reservation is the linearization point. Pause/STOP after it may prevent
        # later selections, but cannot turn an already-started immutable dispatch
        # into an uncheckpointed supervisor side effect.
        receipt = self.trigger_adapter.accept(event)
        accepted = {
            **reservation,
            "status": "ACCEPTED",
            "receipt_sha256": receipt.receipt_sha256,
            "run_id": receipt.run_id,
            "checkpoint_sha256": receipt.checkpoint_sha256,
        }
        with WorkspaceEconomicLock(self.path.parent):
            state = self._locked_state()
            prior = state["dispatches"].get(record.selection_id)
            if prior == accepted:
                return CurriculumDispatchReceipt(record.selection_id, receipt)
            if prior != reservation:
                raise ResearchCurriculumError("dispatch reservation conflict")
            state["dispatches"][record.selection_id] = accepted
            state["state_version"] += 1
            self._write(state)
        return CurriculumDispatchReceipt(record.selection_id, receipt)

    def select_and_dispatch(self, candidates: Iterable[ReplayCandidate], *, purpose: CurriculumPurpose, selector_policy_version: str, as_of: str, seed: int, budget_units: int, deadline_at: str | None = None) -> CurriculumDispatchReceipt:
        record = self.select(
            candidates,
            purpose=purpose,
            selector_policy_version=selector_policy_version,
            as_of=as_of,
            seed=seed,
            budget_units=budget_units,
        )
        return self.dispatch(record, deadline_at=deadline_at)

    def record_outcome(self, selection_id: str, *, outcome: CurriculumOutcome, at: str, scientific_evidence_id: str | None = None) -> None:
        _sha(selection_id, "selection_id")
        if not isinstance(outcome, CurriculumOutcome):
            raise ResearchCurriculumError("outcome must be CurriculumOutcome")
        at = _timestamp(at, "at")
        if scientific_evidence_id is not None:
            _text(scientific_evidence_id, "scientific_evidence_id")
        with WorkspaceEconomicLock(self.path.parent):
            state = self._locked_state()
            dispatch = state["dispatches"].get(selection_id)
            if dispatch is None or dispatch.get("status") != "ACCEPTED":
                raise ResearchCurriculumError(
                    "selection has no accepted supervisor dispatch"
                )
            snapshot = self.trigger_adapter.supervisor.status(dispatch["run_id"])
            if outcome is CurriculumOutcome.STOPPED:
                if snapshot.status is not SupervisorStatus.STOPPED:
                    raise ResearchCurriculumError("STOPPED outcome requires stopped supervisor run")
            elif snapshot.status is not SupervisorStatus.COMPLETED:
                raise ResearchCurriculumError("scientific outcome requires completed supervisor run")
            payload = {
                "selection_id": selection_id,
                "run_id": dispatch["run_id"],
                "outcome": outcome.value,
                "scientific_evidence_id": scientific_evidence_id,
                "at": at,
                "supervisor_checkpoint_sha256": snapshot.checkpoint_sha256,
            }
            prior = state["outcomes"].get(selection_id)
            if prior is not None and prior != payload:
                raise ResearchCurriculumError("outcome identity conflict")
            if prior is None:
                state["outcomes"][selection_id] = payload
                state["state_version"] += 1
                self._write(state)

    def snapshot(self) -> dict[str, Any]:
        state = self._read()
        self._validate(state)
        return json.loads(_canonical_json(state))
