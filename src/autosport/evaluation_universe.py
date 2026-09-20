from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Iterable, Mapping

from .integrity import atomic_write_json
from .workspace_lock import WorkspaceEconomicLock

SCHEMA = "autosport.evaluation_universe"
SCHEMA_VERSION = 1
_HEX = frozenset("0123456789abcdef")


class EvaluationUniverseError(ValueError):
    """Invalid or conflicting frozen evaluation-universe evidence."""


class EvaluationUniverseIntegrityError(EvaluationUniverseError):
    """Durable evaluation-universe evidence failed integrity checks."""


class SlotState(StrEnum):
    CANDIDATE = "CANDIDATE"
    NO_EVENT = "NO_EVENT"
    NO_QUOTE = "NO_QUOTE"
    SOURCE_OUTAGE = "SOURCE_OUTAGE"
    NO_CANDIDATE = "NO_CANDIDATE"
    WAIT_ZERO = "WAIT_ZERO"


class FunnelStage(StrEnum):
    OBSERVED_SLOT = "OBSERVED_SLOT"
    DETECTED = "DETECTED"
    ELIGIBLE = "ELIGIBLE"
    EXECUTION_MODEL_ELIGIBLE = "EXECUTION_MODEL_ELIGIBLE"
    ATTEMPTED = "ATTEMPTED"
    ACCEPTED = "ACCEPTED"
    PARTIAL = "PARTIAL"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"
    RECONCILED = "RECONCILED"
    SETTLED = "SETTLED"
    VOID = "VOID"
    PENDING = "PENDING"
    MISSING = "MISSING"


class AttritionReason(StrEnum):
    NO_EVENT = "NO_EVENT"
    NO_QUOTE = "NO_QUOTE"
    SOURCE_OUTAGE = "SOURCE_OUTAGE"
    NO_CANDIDATE = "NO_CANDIDATE"
    WAIT_ZERO = "WAIT_ZERO"
    INVALID_EVIDENCE = "INVALID_EVIDENCE"
    INCOMPLETE_EVIDENCE = "INCOMPLETE_EVIDENCE"
    EXPIRED = "EXPIRED"
    STALE_QUOTE = "STALE_QUOTE"
    RISK_REJECTED = "RISK_REJECTED"
    THEORETICAL_ONLY = "THEORETICAL_ONLY"
    UNSUPPORTED_MARKET = "UNSUPPORTED_MARKET"
    LIMIT_REJECTED = "LIMIT_REJECTED"
    BUDGET_REJECTED = "BUDGET_REJECTED"
    EXECUTION_MODEL_REJECTED = "EXECUTION_MODEL_REJECTED"


_ZERO_REASON = {
    SlotState.NO_EVENT: AttritionReason.NO_EVENT,
    SlotState.NO_QUOTE: AttritionReason.NO_QUOTE,
    SlotState.SOURCE_OUTAGE: AttritionReason.SOURCE_OUTAGE,
    SlotState.NO_CANDIDATE: AttritionReason.NO_CANDIDATE,
    SlotState.WAIT_ZERO: AttritionReason.WAIT_ZERO,
}
_PRE_EXEC = {
    FunnelStage.OBSERVED_SLOT,
    FunnelStage.DETECTED,
    FunnelStage.ELIGIBLE,
    FunnelStage.EXECUTION_MODEL_ELIGIBLE,
}
_ATTEMPT_OUTCOMES = {
    FunnelStage.ACCEPTED,
    FunnelStage.PARTIAL,
    FunnelStage.REJECTED,
    FunnelStage.UNKNOWN,
}
_TERMINAL = {
    FunnelStage.SETTLED,
    FunnelStage.VOID,
    FunnelStage.PENDING,
    FunnelStage.MISSING,
}
_POST_EXEC = {FunnelStage.ATTEMPTED, FunnelStage.RECONCILED} | _ATTEMPT_OUTCOMES | _TERMINAL


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise EvaluationUniverseError(f"{field} must be a non-empty canonical string")
    value.encode("utf-8")
    return value


def _optional_text(value: object, field: str) -> str | None:
    return None if value is None else _text(value, field)


def _sha(value: object, field: str) -> str:
    text = _text(value, field).lower()
    if len(text) != 64 or any(ch not in _HEX for ch in text):
        raise EvaluationUniverseError(f"{field} must be canonical SHA-256 hex")
    return text


def _optional_sha(value: object, field: str) -> str | None:
    return None if value is None else _sha(value, field)


def _instant(value: object, field: str) -> datetime:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvaluationUniverseError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvaluationUniverseError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _ts(value: object, field: str) -> str:
    return _instant(value, field).isoformat().replace("+00:00", "Z")


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _clusters(values: object) -> tuple[str, ...]:
    if not isinstance(values, tuple) or not values:
        raise EvaluationUniverseError("dependence_cluster_keys must be a non-empty tuple")
    items = tuple(_text(value, "dependence_cluster_key") for value in values)
    if items != tuple(sorted(items)) or len(items) != len(set(items)):
        raise EvaluationUniverseError("dependence_cluster_keys must be sorted and unique")
    return items


@dataclass(frozen=True, slots=True)
class EvaluationRow:
    row_key: str
    campaign_id: str
    research_protocol_id: str
    protocol_sha256: str
    universe_id: str
    slot_state: SlotState
    decision_stage: FunnelStage
    attrition_reason: AttritionReason | None
    sport: str
    provider_id: str
    source_id: str
    event_id: str | None
    market_id: str | None
    selection_id: str | None
    source_at: str
    received_at: str
    committed_at: str
    detection_at: str | None
    decision_at: str | None
    quote_set_sha256: str | None
    freshness_policy_sha256: str
    strategy_version_id: str
    model_version_id: str | None
    config_sha256: str
    portfolio_before_id: str
    economic_goal_id: str
    risk_policy_id: str
    terminal_space_proof_id: str | None
    settlement_proof_id: str | None
    execution_model_id: str | None
    cost_contract_sha256: str
    outcome_reveal_not_before: str | None
    dependence_cluster_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        for field in (
            "row_key", "campaign_id", "research_protocol_id", "universe_id", "sport",
            "provider_id", "source_id", "strategy_version_id", "portfolio_before_id",
            "economic_goal_id", "risk_policy_id",
        ):
            _text(getattr(self, field), field)
        for field in ("protocol_sha256", "freshness_policy_sha256", "config_sha256", "cost_contract_sha256"):
            object.__setattr__(self, field, _sha(getattr(self, field), field))
        for field in (
            "event_id", "market_id", "selection_id", "model_version_id",
            "terminal_space_proof_id", "settlement_proof_id", "execution_model_id",
        ):
            _optional_text(getattr(self, field), field)
        object.__setattr__(self, "quote_set_sha256", _optional_sha(self.quote_set_sha256, "quote_set_sha256"))
        object.__setattr__(self, "dependence_cluster_keys", _clusters(self.dependence_cluster_keys))
        if not isinstance(self.slot_state, SlotState):
            raise EvaluationUniverseError("slot_state must use SlotState")
        if self.decision_stage not in _PRE_EXEC:
            raise EvaluationUniverseError("decision_stage must be a pre-execution FunnelStage")
        if self.attrition_reason is not None and not isinstance(self.attrition_reason, AttritionReason):
            raise EvaluationUniverseError("attrition_reason must use AttritionReason")

        source = _instant(self.source_at, "source_at")
        received = _instant(self.received_at, "received_at")
        committed = _instant(self.committed_at, "committed_at")
        if not source <= received <= committed:
            raise EvaluationUniverseError("source_at <= received_at <= committed_at is required")
        detection = None if self.detection_at is None else _instant(self.detection_at, "detection_at")
        decision = None if self.decision_at is None else _instant(self.decision_at, "decision_at")
        if detection is not None and detection < committed:
            raise EvaluationUniverseError("detection_at cannot precede committed evidence")
        if decision is not None and (detection is None or decision < detection):
            raise EvaluationUniverseError("decision_at requires detection_at and cannot precede it")
        if self.outcome_reveal_not_before is not None:
            reveal = _instant(self.outcome_reveal_not_before, "outcome_reveal_not_before")
            if decision is not None and reveal < decision:
                raise EvaluationUniverseError("outcome reveal cannot precede the decision")

        if self.slot_state is SlotState.CANDIDATE:
            for field in ("event_id", "market_id", "selection_id"):
                if getattr(self, field) is None:
                    raise EvaluationUniverseError(f"candidate row requires {field}")
            if self.quote_set_sha256 is None or detection is None:
                raise EvaluationUniverseError("candidate row requires quote_set_sha256 and detection_at")
            if self.decision_stage is FunnelStage.OBSERVED_SLOT:
                raise EvaluationUniverseError("candidate row must reach at least DETECTED")
            if self.decision_stage in {FunnelStage.ELIGIBLE, FunnelStage.EXECUTION_MODEL_ELIGIBLE} and decision is None:
                raise EvaluationUniverseError("eligible candidate row requires decision_at")
            if self.decision_stage is FunnelStage.EXECUTION_MODEL_ELIGIBLE:
                if self.execution_model_id is None:
                    raise EvaluationUniverseError("execution-model eligible row requires execution_model_id")
                if self.attrition_reason is not None:
                    raise EvaluationUniverseError("execution-model eligible row cannot carry attrition_reason")
            elif self.attrition_reason is None:
                raise EvaluationUniverseError("candidate row that stops before execution-model eligibility requires attrition_reason")
        else:
            if self.decision_stage is not FunnelStage.OBSERVED_SLOT:
                raise EvaluationUniverseError("zero/coverage row must remain at OBSERVED_SLOT")
            expected = _ZERO_REASON[self.slot_state]
            if self.attrition_reason is not expected:
                raise EvaluationUniverseError(f"{self.slot_state.value} requires {expected.value} reason")
            if self.event_id is None and self.slot_state not in {SlotState.NO_EVENT, SlotState.SOURCE_OUTAGE}:
                raise EvaluationUniverseError(f"{self.slot_state.value} requires event_id")

    @property
    def row_id(self) -> str:
        return _digest(self.to_payload())

    def to_payload(self) -> dict[str, object]:
        return {
            "row_key": self.row_key,
            "campaign_id": self.campaign_id,
            "research_protocol_id": self.research_protocol_id,
            "protocol_sha256": self.protocol_sha256,
            "universe_id": self.universe_id,
            "slot_state": self.slot_state.value,
            "decision_stage": self.decision_stage.value,
            "attrition_reason": None if self.attrition_reason is None else self.attrition_reason.value,
            "sport": self.sport,
            "provider_id": self.provider_id,
            "source_id": self.source_id,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "source_at": _ts(self.source_at, "source_at"),
            "received_at": _ts(self.received_at, "received_at"),
            "committed_at": _ts(self.committed_at, "committed_at"),
            "detection_at": None if self.detection_at is None else _ts(self.detection_at, "detection_at"),
            "decision_at": None if self.decision_at is None else _ts(self.decision_at, "decision_at"),
            "quote_set_sha256": self.quote_set_sha256,
            "freshness_policy_sha256": self.freshness_policy_sha256,
            "strategy_version_id": self.strategy_version_id,
            "model_version_id": self.model_version_id,
            "config_sha256": self.config_sha256,
            "portfolio_before_id": self.portfolio_before_id,
            "economic_goal_id": self.economic_goal_id,
            "risk_policy_id": self.risk_policy_id,
            "terminal_space_proof_id": self.terminal_space_proof_id,
            "settlement_proof_id": self.settlement_proof_id,
            "execution_model_id": self.execution_model_id,
            "cost_contract_sha256": self.cost_contract_sha256,
            "outcome_reveal_not_before": None if self.outcome_reveal_not_before is None else _ts(self.outcome_reveal_not_before, "outcome_reveal_not_before"),
            "dependence_cluster_keys": list(self.dependence_cluster_keys),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "EvaluationRow":
        try:
            values = dict(payload)
            values["slot_state"] = SlotState(values["slot_state"])
            values["decision_stage"] = FunnelStage(values["decision_stage"])
            values["attrition_reason"] = None if values.get("attrition_reason") is None else AttritionReason(values["attrition_reason"])
            values["dependence_cluster_keys"] = tuple(values["dependence_cluster_keys"])
            return cls(**values)
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, EvaluationUniverseError):
                raise
            raise EvaluationUniverseIntegrityError("invalid evaluation row payload") from exc


@dataclass(frozen=True, slots=True)
class EvaluationUniverse:
    universe_id: str
    campaign_id: str
    research_protocol_id: str
    protocol_sha256: str
    frozen_at: str
    rows: tuple[EvaluationRow, ...]

    def __post_init__(self) -> None:
        for field in ("universe_id", "campaign_id", "research_protocol_id"):
            _text(getattr(self, field), field)
        object.__setattr__(self, "protocol_sha256", _sha(self.protocol_sha256, "protocol_sha256"))
        frozen = _instant(self.frozen_at, "frozen_at")
        if not isinstance(self.rows, tuple) or not self.rows:
            raise EvaluationUniverseError("rows must be a non-empty tuple")
        by_key: dict[str, EvaluationRow] = {}
        by_id: dict[str, EvaluationRow] = {}
        for row in self.rows:
            if not isinstance(row, EvaluationRow):
                raise EvaluationUniverseError("rows must contain EvaluationRow")
            if (row.universe_id, row.campaign_id, row.research_protocol_id, row.protocol_sha256) != (
                self.universe_id, self.campaign_id, self.research_protocol_id, self.protocol_sha256
            ):
                raise EvaluationUniverseError("every row must bind the exact universe/campaign/protocol identity")
            if _instant(row.committed_at, "committed_at") > frozen:
                raise EvaluationUniverseError("membership evidence committed after frozen_at")
            if row.decision_at is not None and _instant(row.decision_at, "decision_at") > frozen:
                raise EvaluationUniverseError("decision-time membership cannot be frozen before its decision")
            prior = by_key.get(row.row_key)
            if prior is not None and prior.row_id != row.row_id:
                raise EvaluationUniverseError("conflicting immutable row_key in frozen universe")
            by_key[row.row_key] = row
            by_id[row.row_id] = row
        object.__setattr__(self, "rows", tuple(sorted(by_id.values(), key=lambda item: (item.row_key, item.row_id))))

    @property
    def row_ids(self) -> tuple[str, ...]:
        return tuple(sorted(row.row_id for row in self.rows))

    @property
    def membership_sha256(self) -> str:
        return _digest({
            "universe_id": self.universe_id,
            "campaign_id": self.campaign_id,
            "research_protocol_id": self.research_protocol_id,
            "protocol_sha256": self.protocol_sha256,
            "row_ids": list(self.row_ids),
        })

    @property
    def universe_sha256(self) -> str:
        return _digest(self.to_payload(include_digest=False))

    def to_payload(self, *, include_digest: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "universe_id": self.universe_id,
            "campaign_id": self.campaign_id,
            "research_protocol_id": self.research_protocol_id,
            "protocol_sha256": self.protocol_sha256,
            "frozen_at": _ts(self.frozen_at, "frozen_at"),
            "membership_sha256": self.membership_sha256,
            "rows": [{"row_id": row.row_id, **row.to_payload()} for row in self.rows],
        }
        if include_digest:
            payload["universe_sha256"] = self.universe_sha256
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "EvaluationUniverse":
        try:
            if payload["schema"] != SCHEMA or payload["schema_version"] != SCHEMA_VERSION:
                raise EvaluationUniverseIntegrityError("unsupported evaluation-universe schema")
            raw_rows = payload["rows"]
            if not isinstance(raw_rows, list):
                raise EvaluationUniverseIntegrityError("rows must be a list")
            rows = []
            for raw in raw_rows:
                if not isinstance(raw, Mapping):
                    raise EvaluationUniverseIntegrityError("row must be an object")
                values = dict(raw)
                claimed = values.pop("row_id", None)
                row = EvaluationRow.from_payload(values)
                if claimed != row.row_id:
                    raise EvaluationUniverseIntegrityError("row_id does not bind row payload")
                rows.append(row)
            universe = cls(
                universe_id=payload["universe_id"], campaign_id=payload["campaign_id"],
                research_protocol_id=payload["research_protocol_id"], protocol_sha256=payload["protocol_sha256"],
                frozen_at=payload["frozen_at"], rows=tuple(rows),
            )
            if payload.get("membership_sha256") != universe.membership_sha256:
                raise EvaluationUniverseIntegrityError("membership_sha256 does not bind frozen rows")
            if payload.get("universe_sha256") != universe.universe_sha256:
                raise EvaluationUniverseIntegrityError("universe_sha256 does not bind universe payload")
            return universe
        except KeyError as exc:
            raise EvaluationUniverseIntegrityError("evaluation universe payload is incomplete") from exc


@dataclass(frozen=True, slots=True)
class FunnelEvent:
    row_id: str
    stage: FunnelStage
    event_at: str
    execution_model_id: str | None = None
    execution_attempt_id: str | None = None
    execution_reality_sha256: str | None = None
    settlement_proof_id: str | None = None
    reason_code: str | None = None
    correction_of: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "row_id", _sha(self.row_id, "row_id"))
        if self.stage not in _POST_EXEC:
            raise EvaluationUniverseError("funnel event must be a post-freeze stage")
        _instant(self.event_at, "event_at")
        for field in ("execution_model_id", "execution_attempt_id", "settlement_proof_id", "reason_code"):
            _optional_text(getattr(self, field), field)
        object.__setattr__(self, "execution_reality_sha256", _optional_sha(self.execution_reality_sha256, "execution_reality_sha256"))
        object.__setattr__(self, "correction_of", _optional_sha(self.correction_of, "correction_of"))
        if self.stage is FunnelStage.ATTEMPTED and (self.execution_model_id is None or self.execution_attempt_id is None):
            raise EvaluationUniverseError("ATTEMPTED requires execution model and attempt identity")
        if self.stage in _ATTEMPT_OUTCOMES and (self.execution_attempt_id is None or self.execution_reality_sha256 is None):
            raise EvaluationUniverseError("attempt outcome requires attempt identity and execution reality digest")
        if self.stage in _TERMINAL and self.settlement_proof_id is None:
            raise EvaluationUniverseError("terminal outcome requires settlement_proof_id")
        if self.correction_of is not None and self.stage not in _TERMINAL:
            raise EvaluationUniverseError("only terminal outcome evidence may correct an earlier event")

    @property
    def event_id(self) -> str:
        return _digest(self.to_payload())

    def to_payload(self) -> dict[str, object]:
        return {
            "row_id": self.row_id,
            "stage": self.stage.value,
            "event_at": _ts(self.event_at, "event_at"),
            "execution_model_id": self.execution_model_id,
            "execution_attempt_id": self.execution_attempt_id,
            "execution_reality_sha256": self.execution_reality_sha256,
            "settlement_proof_id": self.settlement_proof_id,
            "reason_code": self.reason_code,
            "correction_of": self.correction_of,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "FunnelEvent":
        try:
            values = dict(payload)
            values["stage"] = FunnelStage(values["stage"])
            return cls(**values)
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, EvaluationUniverseError):
                raise
            raise EvaluationUniverseIntegrityError("invalid funnel event payload") from exc


def _advance(previous: FunnelStage, current: FunnelStage) -> bool:
    if previous is FunnelStage.EXECUTION_MODEL_ELIGIBLE:
        return current is FunnelStage.ATTEMPTED
    if previous is FunnelStage.ATTEMPTED:
        return current in _ATTEMPT_OUTCOMES
    if previous in _ATTEMPT_OUTCOMES:
        return current is FunnelStage.RECONCILED
    if previous is FunnelStage.RECONCILED:
        return current in _TERMINAL
    return False


@dataclass(frozen=True, slots=True)
class EvaluationCohort:
    universe_sha256: str
    membership_sha256: str
    row_ids: tuple[str, ...]
    stage_counts: tuple[tuple[str, int], ...]
    attrition_counts: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "universe_sha256", _sha(self.universe_sha256, "universe_sha256"))
        object.__setattr__(self, "membership_sha256", _sha(self.membership_sha256, "membership_sha256"))
        if not isinstance(self.row_ids, tuple) or not self.row_ids:
            raise EvaluationUniverseError("row_ids must be a non-empty tuple")
        for row_id in self.row_ids:
            _sha(row_id, "row_id")
        if self.row_ids != tuple(sorted(self.row_ids)):
            raise EvaluationUniverseError("row_ids must use canonical sorted order")
        for name, values in (("stage_counts", self.stage_counts), ("attrition_counts", self.attrition_counts)):
            if not isinstance(values, tuple) or values != tuple(sorted(values)):
                raise EvaluationUniverseError(f"{name} must use canonical sorted tuple order")
            for key, count in values:
                _text(key, f"{name} key")
                if type(count) is not int or count < 0:
                    raise EvaluationUniverseError(f"{name} counts must be non-negative integers")

    @property
    def sample_count(self) -> int:
        return len(self.row_ids)

    @property
    def cohort_sha256(self) -> str:
        return _digest({
            "universe_sha256": self.universe_sha256,
            "membership_sha256": self.membership_sha256,
            "row_ids": list(self.row_ids),
            "stage_counts": [list(item) for item in self.stage_counts],
            "attrition_counts": [list(item) for item in self.attrition_counts],
        })

    def assert_same_membership(self, other: "EvaluationCohort") -> None:
        if not isinstance(other, EvaluationCohort) or (
            self.universe_sha256, self.membership_sha256, self.row_ids
        ) != (
            getattr(other, "universe_sha256", None), getattr(other, "membership_sha256", None), getattr(other, "row_ids", None)
        ):
            raise EvaluationUniverseError("candidate and baseline do not share the exact frozen universe")


@dataclass(frozen=True, slots=True)
class EvaluationUniverseLedger:
    universe: EvaluationUniverse
    events: tuple[FunnelEvent, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.universe, EvaluationUniverse) or not isinstance(self.events, tuple):
            raise EvaluationUniverseError("universe/events have invalid type")
        row_map = {row.row_id: row for row in self.universe.rows}
        unique: list[FunnelEvent] = []
        seen: set[str] = set()
        for event in self.events:
            if not isinstance(event, FunnelEvent) or event.row_id not in row_map:
                raise EvaluationUniverseError("funnel event references row outside frozen universe")
            if event.event_id not in seen:
                seen.add(event.event_id)
                unique.append(event)
        object.__setattr__(self, "events", tuple(unique))
        self._validate_event_chains(row_map)

    def _validate_event_chains(self, row_map: Mapping[str, EvaluationRow]) -> None:
        by_id = {event.event_id: event for event in self.events}
        frozen = _instant(self.universe.frozen_at, "frozen_at")
        for row_id, row in row_map.items():
            events = [event for event in self.events if event.row_id == row_id]
            if not events:
                continue
            if row.decision_stage is not FunnelStage.EXECUTION_MODEL_ELIGIBLE:
                raise EvaluationUniverseError("post-freeze funnel events require execution-model eligible membership")
            stage = row.decision_stage
            when = frozen
            attempt_id: str | None = None
            terminal_ids: set[str] = set()
            for event in events:
                event_at = _instant(event.event_at, "event_at")
                if event_at < frozen:
                    raise EvaluationUniverseError("post-freeze funnel event cannot predate frozen_at")
                if event_at < when:
                    raise EvaluationUniverseError("funnel event time cannot move backwards")
                if event.correction_of is not None:
                    corrected = by_id.get(event.correction_of)
                    if corrected is None or corrected.row_id != row_id or corrected.event_id not in terminal_ids:
                        raise EvaluationUniverseError("terminal correction must reference an earlier terminal event for the same row")
                    if event_at < _instant(corrected.event_at, "corrected event_at"):
                        raise EvaluationUniverseError("terminal correction cannot predate corrected evidence")
                    terminal_ids.add(event.event_id)
                    stage, when = event.stage, event_at
                    continue
                if not _advance(stage, event.stage):
                    raise EvaluationUniverseError(f"invalid funnel transition {stage.value} -> {event.stage.value}")
                if event.stage is FunnelStage.ATTEMPTED:
                    if event.execution_model_id != row.execution_model_id:
                        raise EvaluationUniverseError("attempted execution model does not match frozen execution model")
                    attempt_id = event.execution_attempt_id
                elif event.stage in _ATTEMPT_OUTCOMES and event.execution_attempt_id != attempt_id:
                    raise EvaluationUniverseError("attempt outcome must reference the exact attempted execution identity")
                if event.stage in _TERMINAL:
                    if row.outcome_reveal_not_before is None or event_at < _instant(row.outcome_reveal_not_before, "outcome_reveal_not_before"):
                        raise EvaluationUniverseError("terminal outcome cannot attach before authoritative reveal time")
                    terminal_ids.add(event.event_id)
                stage, when = event.stage, event_at

    @property
    def ledger_sha256(self) -> str:
        return _digest(self.to_payload(include_digest=False))

    def append(self, event: FunnelEvent) -> "EvaluationUniverseLedger":
        if any(existing.event_id == event.event_id for existing in self.events):
            return self
        return EvaluationUniverseLedger(self.universe, (*self.events, event))

    def current_stage(self, row_id: str) -> FunnelStage:
        row_id = _sha(row_id, "row_id")
        row = next((row for row in self.universe.rows if row.row_id == row_id), None)
        if row is None:
            raise EvaluationUniverseError("row_id is outside frozen universe")
        stage = row.decision_stage
        for event in self.events:
            if event.row_id == row_id:
                stage = event.stage
        return stage

    def cohort(self) -> EvaluationCohort:
        stages: dict[str, int] = {}
        attrition: dict[str, int] = {}
        for row in self.universe.rows:
            key = self.current_stage(row.row_id).value
            stages[key] = stages.get(key, 0) + 1
            if row.attrition_reason is not None:
                reason = row.attrition_reason.value
                attrition[reason] = attrition.get(reason, 0) + 1
        return EvaluationCohort(
            self.universe.universe_sha256,
            self.universe.membership_sha256,
            self.universe.row_ids,
            tuple(sorted(stages.items())),
            tuple(sorted(attrition.items())),
        )

    def to_payload(self, *, include_digest: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "kind": "evaluation_universe_ledger",
            "universe": self.universe.to_payload(),
            "events": [{"event_id": event.event_id, **event.to_payload()} for event in self.events],
        }
        if include_digest:
            payload["ledger_sha256"] = self.ledger_sha256
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "EvaluationUniverseLedger":
        try:
            if payload["schema"] != SCHEMA or payload["schema_version"] != SCHEMA_VERSION or payload["kind"] != "evaluation_universe_ledger":
                raise EvaluationUniverseIntegrityError("unsupported evaluation-universe ledger schema")
            if not isinstance(payload["universe"], Mapping) or not isinstance(payload["events"], list):
                raise EvaluationUniverseIntegrityError("ledger universe/events have invalid JSON type")
            universe = EvaluationUniverse.from_payload(payload["universe"])
            events = []
            for raw in payload["events"]:
                if not isinstance(raw, Mapping):
                    raise EvaluationUniverseIntegrityError("event must be an object")
                values = dict(raw)
                claimed = values.pop("event_id", None)
                event = FunnelEvent.from_payload(values)
                if claimed != event.event_id:
                    raise EvaluationUniverseIntegrityError("event_id does not bind event payload")
                events.append(event)
            ledger = cls(universe, tuple(events))
            if payload.get("ledger_sha256") != ledger.ledger_sha256:
                raise EvaluationUniverseIntegrityError("ledger_sha256 does not bind durable payload")
            return ledger
        except KeyError as exc:
            raise EvaluationUniverseIntegrityError("evaluation-universe ledger payload is incomplete") from exc


class EvaluationUniverseStore:
    FILE_NAME = "evaluation-universe.json"

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace)
        self.path = self.workspace / self.FILE_NAME

    def load(self) -> EvaluationUniverseLedger | None:
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise EvaluationUniverseIntegrityError("cannot read durable evaluation-universe ledger") from exc
        if not isinstance(payload, dict):
            raise EvaluationUniverseIntegrityError("durable evaluation-universe ledger must be a JSON object")
        return EvaluationUniverseLedger.from_payload(payload)

    def save(self, ledger: EvaluationUniverseLedger) -> None:
        if not isinstance(ledger, EvaluationUniverseLedger):
            raise EvaluationUniverseError("ledger must be EvaluationUniverseLedger")
        with WorkspaceEconomicLock(self.workspace):
            existing = self.load()
            if existing is not None:
                if existing.universe.universe_sha256 != ledger.universe.universe_sha256:
                    raise EvaluationUniverseIntegrityError("cannot replace frozen universe identity")
                old_ids = tuple(event.event_id for event in existing.events)
                new_ids = tuple(event.event_id for event in ledger.events)
                if len(new_ids) < len(old_ids):
                    raise EvaluationUniverseIntegrityError("durable funnel history cannot shrink")
                if new_ids[: len(old_ids)] != old_ids:
                    raise EvaluationUniverseIntegrityError("durable funnel history is append-only")
                if existing.ledger_sha256 == ledger.ledger_sha256:
                    return
            atomic_write_json(self.path, ledger.to_payload())


def build_frozen_universe(
    *, universe_id: str, campaign_id: str, research_protocol_id: str,
    protocol_sha256: str, frozen_at: str, rows: Iterable[EvaluationRow],
) -> EvaluationUniverse:
    materialized = tuple(rows)
    if not materialized:
        raise EvaluationUniverseError("cannot freeze an empty evaluation universe")
    return EvaluationUniverse(
        universe_id=universe_id, campaign_id=campaign_id,
        research_protocol_id=research_protocol_id, protocol_sha256=protocol_sha256,
        frozen_at=frozen_at, rows=materialized,
    )
