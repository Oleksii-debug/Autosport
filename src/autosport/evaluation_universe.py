from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Iterable, Mapping

from .evaluation_intake import ObservationIntakeLedger, ObservationIntakeSnapshot
from .integrity import atomic_write_json
from .json_integrity import strict_json_loads
from .monotonic_workspace_authority import (
    AuthorityPhase,
    MonotonicWorkspaceAuthority,
    MonotonicWorkspaceAuthorityError,
)
from .paper_execution_reality import (
    PaperAttemptOutcome,
    PaperExecutionLedger,
    PaperExecutionStateError,
    PaperLegAttempt,
)
from .workspace_lock import WorkspaceEconomicLock


SCHEMA = "autosport.evaluation_universe"
SCHEMA_VERSION = 2
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
_PAPER_STAGE = {
    PaperAttemptOutcome.ACCEPTED: FunnelStage.ACCEPTED,
    PaperAttemptOutcome.PARTIAL: FunnelStage.PARTIAL,
    PaperAttemptOutcome.REJECTED: FunnelStage.REJECTED,
    PaperAttemptOutcome.UNKNOWN: FunnelStage.UNKNOWN,
}


def _text(value: object, field_name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise EvaluationUniverseError(f"{field_name} must be non-empty canonical text")
    value.encode("utf-8")
    return value


def _optional_text(value: object, field_name: str) -> str | None:
    return None if value is None else _text(value, field_name)


def _sha(value: object, field_name: str) -> str:
    raw = _text(value, field_name).lower()
    if len(raw) != 64 or any(ch not in _HEX for ch in raw):
        raise EvaluationUniverseError(f"{field_name} must be canonical SHA-256 hex")
    return raw


def _optional_sha(value: object, field_name: str) -> str | None:
    return None if value is None else _sha(value, field_name)


def _instant(value: object, field_name: str) -> datetime:
    raw = _text(value, field_name)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvaluationUniverseError(f"{field_name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvaluationUniverseError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _ts(value: object, field_name: str) -> str:
    return _instant(value, field_name).isoformat().replace("+00:00", "Z")


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
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
    """Immutable pre-outcome membership row in one complete evaluation denominator."""

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
    execution_run_id: str | None
    execution_plan_id: str | None
    execution_action_id: str | None
    decision_quote_id: str | None
    cost_contract_sha256: str
    outcome_reveal_not_before: str | None
    dependence_cluster_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in (
            "row_key",
            "campaign_id",
            "research_protocol_id",
            "universe_id",
            "sport",
            "provider_id",
            "source_id",
            "strategy_version_id",
            "portfolio_before_id",
            "economic_goal_id",
            "risk_policy_id",
        ):
            _text(getattr(self, name), name)
        for name in (
            "protocol_sha256",
            "freshness_policy_sha256",
            "config_sha256",
            "cost_contract_sha256",
        ):
            object.__setattr__(self, name, _sha(getattr(self, name), name))
        for name in (
            "event_id",
            "market_id",
            "selection_id",
            "model_version_id",
            "terminal_space_proof_id",
            "settlement_proof_id",
            "execution_model_id",
            "execution_run_id",
            "execution_plan_id",
            "execution_action_id",
            "decision_quote_id",
        ):
            _optional_text(getattr(self, name), name)
        object.__setattr__(
            self,
            "quote_set_sha256",
            _optional_sha(self.quote_set_sha256, "quote_set_sha256"),
        )
        object.__setattr__(
            self,
            "dependence_cluster_keys",
            _clusters(self.dependence_cluster_keys),
        )
        if not isinstance(self.slot_state, SlotState):
            raise EvaluationUniverseError("slot_state must use SlotState")
        if self.decision_stage not in _PRE_EXEC:
            raise EvaluationUniverseError("decision_stage must be a pre-execution FunnelStage")
        if self.attrition_reason is not None and not isinstance(
            self.attrition_reason, AttritionReason
        ):
            raise EvaluationUniverseError("attrition_reason must use AttritionReason")

        source = _instant(self.source_at, "source_at")
        received = _instant(self.received_at, "received_at")
        committed = _instant(self.committed_at, "committed_at")
        if not source <= received <= committed:
            raise EvaluationUniverseError(
                "source_at <= received_at <= committed_at is required"
            )
        detection = (
            None
            if self.detection_at is None
            else _instant(self.detection_at, "detection_at")
        )
        decision = (
            None if self.decision_at is None else _instant(self.decision_at, "decision_at")
        )
        if detection is not None and detection < committed:
            raise EvaluationUniverseError("detection_at cannot precede committed evidence")
        if decision is not None and (detection is None or decision < detection):
            raise EvaluationUniverseError(
                "decision_at requires detection_at and cannot precede it"
            )
        if self.outcome_reveal_not_before is not None:
            reveal = _instant(
                self.outcome_reveal_not_before,
                "outcome_reveal_not_before",
            )
            if decision is not None and reveal < decision:
                raise EvaluationUniverseError("outcome reveal cannot precede decision")

        execution_refs = (
            self.execution_model_id,
            self.execution_run_id,
            self.execution_plan_id,
            self.execution_action_id,
            self.decision_quote_id,
        )
        if self.slot_state is SlotState.CANDIDATE:
            for name in ("event_id", "market_id", "selection_id"):
                if getattr(self, name) is None:
                    raise EvaluationUniverseError(f"candidate row requires {name}")
            if self.quote_set_sha256 is None or detection is None:
                raise EvaluationUniverseError(
                    "candidate row requires quote_set_sha256 and detection_at"
                )
            if self.decision_stage is FunnelStage.OBSERVED_SLOT:
                raise EvaluationUniverseError("candidate row must reach at least DETECTED")
            if self.decision_stage in {
                FunnelStage.ELIGIBLE,
                FunnelStage.EXECUTION_MODEL_ELIGIBLE,
            } and decision is None:
                raise EvaluationUniverseError("eligible candidate row requires decision_at")
            if self.decision_stage is FunnelStage.EXECUTION_MODEL_ELIGIBLE:
                if any(value is None for value in execution_refs):
                    raise EvaluationUniverseError(
                        "execution-model eligible row requires complete canonical PAPER identity"
                    )
                if self.attrition_reason is not None:
                    raise EvaluationUniverseError(
                        "execution-model eligible row cannot carry attrition_reason"
                    )
            else:
                if self.attrition_reason is None:
                    raise EvaluationUniverseError(
                        "candidate row stopping before execution-model eligibility requires attrition_reason"
                    )
                if any(value is not None for value in execution_refs):
                    raise EvaluationUniverseError(
                        "pre-execution attrition cannot claim PAPER execution identity"
                    )
        else:
            if self.decision_stage is not FunnelStage.OBSERVED_SLOT:
                raise EvaluationUniverseError(
                    "zero/coverage row must remain at OBSERVED_SLOT"
                )
            expected = _ZERO_REASON[self.slot_state]
            if self.attrition_reason is not expected:
                raise EvaluationUniverseError(
                    f"{self.slot_state.value} requires {expected.value} reason"
                )
            if self.event_id is None and self.slot_state not in {
                SlotState.NO_EVENT,
                SlotState.SOURCE_OUTAGE,
            }:
                raise EvaluationUniverseError(
                    f"{self.slot_state.value} requires event_id"
                )
            if any(value is not None for value in execution_refs):
                raise EvaluationUniverseError(
                    "zero/coverage row cannot claim PAPER execution identity"
                )

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
            "attrition_reason": (
                None if self.attrition_reason is None else self.attrition_reason.value
            ),
            "sport": self.sport,
            "provider_id": self.provider_id,
            "source_id": self.source_id,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "source_at": _ts(self.source_at, "source_at"),
            "received_at": _ts(self.received_at, "received_at"),
            "committed_at": _ts(self.committed_at, "committed_at"),
            "detection_at": (
                None
                if self.detection_at is None
                else _ts(self.detection_at, "detection_at")
            ),
            "decision_at": (
                None if self.decision_at is None else _ts(self.decision_at, "decision_at")
            ),
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
            "execution_run_id": self.execution_run_id,
            "execution_plan_id": self.execution_plan_id,
            "execution_action_id": self.execution_action_id,
            "decision_quote_id": self.decision_quote_id,
            "cost_contract_sha256": self.cost_contract_sha256,
            "outcome_reveal_not_before": (
                None
                if self.outcome_reveal_not_before is None
                else _ts(
                    self.outcome_reveal_not_before,
                    "outcome_reveal_not_before",
                )
            ),
            "dependence_cluster_keys": list(self.dependence_cluster_keys),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "EvaluationRow":
        try:
            values = dict(payload)
            values["slot_state"] = SlotState(values["slot_state"])
            values["decision_stage"] = FunnelStage(values["decision_stage"])
            values["attrition_reason"] = (
                None
                if values.get("attrition_reason") is None
                else AttritionReason(values["attrition_reason"])
            )
            values["dependence_cluster_keys"] = tuple(values["dependence_cluster_keys"])
            return cls(**values)
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, EvaluationUniverseError):
                raise
            raise EvaluationUniverseIntegrityError("invalid evaluation row payload") from exc


@dataclass(frozen=True, slots=True, init=False)
class EvaluationUniverse:
    """Frozen membership. Public construction is only through canonical intake resolution."""

    intake_snapshot: ObservationIntakeSnapshot
    universe_id: str
    campaign_id: str
    research_protocol_id: str
    protocol_sha256: str
    frozen_at: str
    rows: tuple[EvaluationRow, ...]

    @classmethod
    def _construct(
        cls,
        *,
        intake_snapshot: ObservationIntakeSnapshot,
        universe_id: str,
        campaign_id: str,
        research_protocol_id: str,
        protocol_sha256: str,
        frozen_at: str,
        rows: tuple[EvaluationRow, ...],
    ) -> "EvaluationUniverse":
        obj = object.__new__(cls)
        for name, value in (
            ("intake_snapshot", intake_snapshot),
            ("universe_id", universe_id),
            ("campaign_id", campaign_id),
            ("research_protocol_id", research_protocol_id),
            ("protocol_sha256", protocol_sha256),
            ("frozen_at", frozen_at),
            ("rows", rows),
        ):
            object.__setattr__(obj, name, value)
        obj._validate()
        return obj

    def _validate(self) -> None:
        if not isinstance(self.intake_snapshot, ObservationIntakeSnapshot):
            raise EvaluationUniverseError("intake_snapshot must use canonical intake type")
        for name in ("universe_id", "campaign_id", "research_protocol_id"):
            _text(getattr(self, name), name)
        object.__setattr__(
            self,
            "protocol_sha256",
            _sha(self.protocol_sha256, "protocol_sha256"),
        )
        frozen = _instant(self.frozen_at, "frozen_at")
        if (
            self.intake_snapshot.campaign_id,
            self.intake_snapshot.research_protocol_id,
            self.intake_snapshot.protocol_sha256,
            self.intake_snapshot.universe_id,
        ) != (
            self.campaign_id,
            self.research_protocol_id,
            self.protocol_sha256,
            self.universe_id,
        ):
            raise EvaluationUniverseError(
                "canonical intake snapshot mismatches universe/campaign/protocol identity"
            )
        if _instant(self.intake_snapshot.committed_at, "intake committed_at") > frozen:
            raise EvaluationUniverseError("canonical intake committed after frozen_at")
        if _instant(
            self.intake_snapshot.evaluation_not_before,
            "intake evaluation_not_before",
        ) > frozen:
            raise EvaluationUniverseError(
                "canonical intake evaluation boundary is after frozen_at"
            )
        if not isinstance(self.rows, tuple) or not self.rows:
            raise EvaluationUniverseError("rows must be a non-empty tuple")

        by_key: dict[str, EvaluationRow] = {}
        by_id: dict[str, EvaluationRow] = {}
        for row in self.rows:
            if type(row) is not EvaluationRow:
                raise EvaluationUniverseError(
                    "rows must contain exact EvaluationRow values"
                )
            if (
                row.universe_id,
                row.campaign_id,
                row.research_protocol_id,
                row.protocol_sha256,
                row.source_id,
            ) != (
                self.universe_id,
                self.campaign_id,
                self.research_protocol_id,
                self.protocol_sha256,
                self.intake_snapshot.source_id,
            ):
                raise EvaluationUniverseError(
                    "row mismatches frozen universe/protocol/source identity"
                )
            if _instant(row.committed_at, "committed_at") > frozen:
                raise EvaluationUniverseError("membership evidence committed after frozen_at")
            if row.decision_at is not None and _instant(row.decision_at, "decision_at") > frozen:
                raise EvaluationUniverseError(
                    "decision-time membership cannot be frozen before decision"
                )
            prior = by_key.get(row.row_key)
            if prior is not None and prior.row_id != row.row_id:
                raise EvaluationUniverseError(
                    "conflicting immutable row_key in frozen universe"
                )
            by_key[row.row_key] = row
            by_id[row.row_id] = row

        if tuple(sorted(by_key)) != self.intake_snapshot.expected_row_keys:
            raise EvaluationUniverseError(
                "supplied rows do not equal canonical pre-result intake membership"
            )
        object.__setattr__(
            self,
            "rows",
            tuple(sorted(by_id.values(), key=lambda item: (item.row_key, item.row_id))),
        )

    @property
    def row_ids(self) -> tuple[str, ...]:
        return tuple(sorted(row.row_id for row in self.rows))

    @property
    def membership_sha256(self) -> str:
        return _digest(
            {
                "universe_id": self.universe_id,
                "campaign_id": self.campaign_id,
                "research_protocol_id": self.research_protocol_id,
                "protocol_sha256": self.protocol_sha256,
                "intake_snapshot_sha256": self.intake_snapshot.snapshot_sha256,
                "row_ids": list(self.row_ids),
            }
        )

    @property
    def universe_sha256(self) -> str:
        return _digest(self.to_payload(include_digest=False))

    def to_payload(self, *, include_digest: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "intake_snapshot": self.intake_snapshot.to_payload(),
            "intake_snapshot_sha256": self.intake_snapshot.snapshot_sha256,
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
                raise EvaluationUniverseIntegrityError(
                    "unsupported evaluation-universe schema"
                )
            raw_snapshot = payload["intake_snapshot"]
            if not isinstance(raw_snapshot, Mapping):
                raise EvaluationUniverseIntegrityError("intake_snapshot must be an object")
            snapshot = ObservationIntakeSnapshot.from_payload(raw_snapshot)
            if payload.get("intake_snapshot_sha256") != snapshot.snapshot_sha256:
                raise EvaluationUniverseIntegrityError(
                    "intake snapshot digest does not bind snapshot payload"
                )
            raw_rows = payload["rows"]
            if not isinstance(raw_rows, list):
                raise EvaluationUniverseIntegrityError("rows must be a list")
            rows: list[EvaluationRow] = []
            for raw in raw_rows:
                if not isinstance(raw, Mapping):
                    raise EvaluationUniverseIntegrityError("row must be an object")
                values = dict(raw)
                claimed = values.pop("row_id", None)
                row = EvaluationRow.from_payload(values)
                if claimed != row.row_id:
                    raise EvaluationUniverseIntegrityError("row_id does not bind row payload")
                rows.append(row)
            universe = cls._construct(
                intake_snapshot=snapshot,
                universe_id=payload["universe_id"],
                campaign_id=payload["campaign_id"],
                research_protocol_id=payload["research_protocol_id"],
                protocol_sha256=payload["protocol_sha256"],
                frozen_at=payload["frozen_at"],
                rows=tuple(rows),
            )
            if payload.get("membership_sha256") != universe.membership_sha256:
                raise EvaluationUniverseIntegrityError(
                    "membership_sha256 does not bind frozen rows"
                )
            if payload.get("universe_sha256") != universe.universe_sha256:
                raise EvaluationUniverseIntegrityError(
                    "universe_sha256 does not bind universe payload"
                )
            return universe
        except KeyError as exc:
            raise EvaluationUniverseIntegrityError(
                "evaluation universe payload is incomplete"
            ) from exc


class CanonicalPaperExecutionResolver:
    """Resolve immutable #623 PAPER attempt evidence without reimplementing execution semantics."""

    def __init__(self, ledger: PaperExecutionLedger) -> None:
        if type(ledger) is not PaperExecutionLedger:
            raise TypeError("ledger must be exact PaperExecutionLedger")
        self.ledger = ledger

    def resolve(
        self,
        *,
        row: EvaluationRow,
        attempt_id: str,
        reality_sha256: str,
    ) -> PaperLegAttempt:
        attempt_id = _text(attempt_id, "attempt_id")
        reality_sha256 = _sha(reality_sha256, "reality_sha256")
        matches = [
            event
            for event in self.ledger.events()
            if event.get("event_type") == "ATTEMPT_RECORDED"
            and isinstance(event.get("payload"), dict)
            and event["payload"].get("attempt_id") == attempt_id
        ]
        if len(matches) != 1:
            raise EvaluationUniverseIntegrityError(
                "PAPER attempt reference does not resolve exactly once"
            )
        event = matches[0]
        if event.get("event_sha256") != reality_sha256:
            raise EvaluationUniverseIntegrityError(
                "execution reality digest is not the canonical PAPER attempt event"
            )
        try:
            attempt = PaperLegAttempt.from_dict(event["payload"])
        except Exception as exc:
            raise EvaluationUniverseIntegrityError(
                "canonical PAPER attempt payload is invalid"
            ) from exc
        expected = (
            row.execution_run_id,
            row.execution_plan_id,
            row.execution_action_id,
            row.event_id,
            row.market_id,
            row.selection_id,
            row.decision_quote_id,
            row.execution_model_id,
        )
        actual = (
            attempt.run_id,
            attempt.plan_id,
            attempt.action_id,
            attempt.event_id,
            attempt.market_id,
            attempt.selection_id,
            attempt.decision_quote_id,
            attempt.model_fingerprint,
        )
        if actual != expected:
            raise EvaluationUniverseIntegrityError(
                "canonical PAPER attempt identity does not match frozen row"
            )
        if row.decision_at is None:
            raise EvaluationUniverseIntegrityError(
                "PAPER attempt requires frozen decision-time evidence"
            )
        if _instant(attempt.decision_observed_at, "PAPER decision_observed_at") > _instant(
            row.decision_at,
            "decision_at",
        ):
            raise EvaluationUniverseIntegrityError(
                "PAPER decision quote was not causally available at frozen decision"
            )
        return attempt


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
        for name in (
            "execution_model_id",
            "execution_attempt_id",
            "settlement_proof_id",
            "reason_code",
        ):
            _optional_text(getattr(self, name), name)
        object.__setattr__(
            self,
            "execution_reality_sha256",
            _optional_sha(self.execution_reality_sha256, "execution_reality_sha256"),
        )
        object.__setattr__(
            self,
            "correction_of",
            _optional_sha(self.correction_of, "correction_of"),
        )
        if self.stage is FunnelStage.ATTEMPTED and (
            self.execution_model_id is None or self.execution_attempt_id is None
        ):
            raise EvaluationUniverseError(
                "ATTEMPTED requires execution model and attempt identity"
            )
        if self.stage in _ATTEMPT_OUTCOMES and (
            self.execution_attempt_id is None or self.execution_reality_sha256 is None
        ):
            raise EvaluationUniverseError(
                "attempt outcome requires attempt identity and canonical reality digest"
            )
        if self.stage in _TERMINAL and self.settlement_proof_id is None:
            raise EvaluationUniverseError(
                "terminal outcome requires settlement_proof_id"
            )
        if self.correction_of is not None and self.stage not in _TERMINAL:
            raise EvaluationUniverseError(
                "only terminal outcome evidence may correct an earlier event"
            )

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
        object.__setattr__(
            self,
            "universe_sha256",
            _sha(self.universe_sha256, "universe_sha256"),
        )
        object.__setattr__(
            self,
            "membership_sha256",
            _sha(self.membership_sha256, "membership_sha256"),
        )
        if not isinstance(self.row_ids, tuple) or not self.row_ids:
            raise EvaluationUniverseError("row_ids must be a non-empty tuple")
        for row_id in self.row_ids:
            _sha(row_id, "row_id")
        if self.row_ids != tuple(sorted(self.row_ids)):
            raise EvaluationUniverseError("row_ids must use canonical sorted order")
        for name, values in (
            ("stage_counts", self.stage_counts),
            ("attrition_counts", self.attrition_counts),
        ):
            if not isinstance(values, tuple) or values != tuple(sorted(values)):
                raise EvaluationUniverseError(
                    f"{name} must use canonical sorted tuple order"
                )
            for key, count in values:
                _text(key, f"{name} key")
                if type(count) is not int or count < 0:
                    raise EvaluationUniverseError(
                        f"{name} counts must be non-negative integers"
                    )

    @property
    def sample_count(self) -> int:
        return len(self.row_ids)

    def assert_same_membership(self, other: "EvaluationCohort") -> None:
        if not isinstance(other, EvaluationCohort) or (
            self.universe_sha256,
            self.membership_sha256,
            self.row_ids,
        ) != (
            getattr(other, "universe_sha256", None),
            getattr(other, "membership_sha256", None),
            getattr(other, "row_ids", None),
        ):
            raise EvaluationUniverseError(
                "candidate and baseline do not share the exact frozen universe"
            )


@dataclass(frozen=True, slots=True)
class EvaluationUniverseLedger:
    universe: EvaluationUniverse
    events: tuple[FunnelEvent, ...] = ()
    paper_resolver: CanonicalPaperExecutionResolver | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.universe, EvaluationUniverse) or not isinstance(
            self.events,
            tuple,
        ):
            raise EvaluationUniverseError("universe/events have invalid type")
        if (
            self.paper_resolver is not None
            and type(self.paper_resolver) is not CanonicalPaperExecutionResolver
        ):
            raise EvaluationUniverseError(
                "paper_resolver must be exact CanonicalPaperExecutionResolver"
            )
        row_map = {row.row_id: row for row in self.universe.rows}
        unique: list[FunnelEvent] = []
        seen: set[str] = set()
        for event in self.events:
            if not isinstance(event, FunnelEvent) or event.row_id not in row_map:
                raise EvaluationUniverseError(
                    "funnel event references row outside frozen universe"
                )
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
                raise EvaluationUniverseError(
                    "post-freeze funnel events require execution-model eligible membership"
                )
            stage = row.decision_stage
            when = frozen
            attempt_id: str | None = None
            canonical_attempt: PaperLegAttempt | None = None
            terminal_tip_id: str | None = None
            for event in events:
                event_at = _instant(event.event_at, "event_at")
                if event_at < frozen:
                    raise EvaluationUniverseError(
                        "post-freeze funnel event cannot predate frozen_at"
                    )
                if event_at < when:
                    raise EvaluationUniverseError("funnel event time cannot move backwards")
                if event.correction_of is not None:
                    if terminal_tip_id is None or event.correction_of != terminal_tip_id:
                        raise EvaluationUniverseError(
                            "terminal correction must extend exact current terminal tip for same row"
                        )
                    corrected = by_id.get(terminal_tip_id)
                    if corrected is None or corrected.row_id != row_id:
                        raise EvaluationUniverseIntegrityError(
                            "current terminal tip is missing from the same row"
                        )
                    if event_at < _instant(corrected.event_at, "corrected event_at"):
                        raise EvaluationUniverseError(
                            "terminal correction cannot predate corrected evidence"
                        )
                    terminal_tip_id = event.event_id
                    stage, when = event.stage, event_at
                    continue
                if not _advance(stage, event.stage):
                    raise EvaluationUniverseError(
                        f"invalid funnel transition {stage.value} -> {event.stage.value}"
                    )
                if event.stage is FunnelStage.ATTEMPTED:
                    if event.execution_model_id != row.execution_model_id:
                        raise EvaluationUniverseError(
                            "attempted execution model does not match frozen execution model"
                        )
                    attempt_id = event.execution_attempt_id
                    canonical_attempt = None
                elif event.stage in _ATTEMPT_OUTCOMES:
                    if event.execution_attempt_id != attempt_id:
                        raise EvaluationUniverseError(
                            "attempt outcome must reference exact attempted execution identity"
                        )
                    if self.paper_resolver is None:
                        raise EvaluationUniverseIntegrityError(
                            "PAPER outcome requires canonical #623 execution resolver"
                        )
                    assert event.execution_reality_sha256 is not None
                    assert event.execution_attempt_id is not None
                    canonical_attempt = self.paper_resolver.resolve(
                        row=row,
                        attempt_id=event.execution_attempt_id,
                        reality_sha256=event.execution_reality_sha256,
                    )
                    if _PAPER_STAGE[canonical_attempt.outcome] is not event.stage:
                        raise EvaluationUniverseIntegrityError(
                            "funnel outcome disagrees with canonical PAPER attempt outcome"
                        )
                    if event_at < _instant(
                        canonical_attempt.execution_observed_at,
                        "PAPER execution_observed_at",
                    ):
                        raise EvaluationUniverseIntegrityError(
                            "funnel outcome predates canonical PAPER execution evidence"
                        )
                elif event.stage is FunnelStage.RECONCILED and canonical_attempt is None:
                    raise EvaluationUniverseIntegrityError(
                        "reconciliation requires a canonically resolved PAPER attempt"
                    )
                if event.stage in _TERMINAL:
                    if canonical_attempt is None:
                        raise EvaluationUniverseIntegrityError(
                            "terminal enrichment requires canonical PAPER attempt chain"
                        )
                    if row.outcome_reveal_not_before is None or event_at < _instant(
                        row.outcome_reveal_not_before,
                        "outcome_reveal_not_before",
                    ):
                        raise EvaluationUniverseError(
                            "terminal outcome cannot attach before authoritative reveal time"
                        )
                    terminal_tip_id = event.event_id
                stage, when = event.stage, event_at

    @property
    def ledger_sha256(self) -> str:
        return _digest(self.to_payload(include_digest=False))

    def append(self, event: FunnelEvent) -> "EvaluationUniverseLedger":
        if any(existing.event_id == event.event_id for existing in self.events):
            return self
        return EvaluationUniverseLedger(
            self.universe,
            (*self.events, event),
            self.paper_resolver,
        )

    def current_stage(self, row_id: str) -> FunnelStage:
        row_id = _sha(row_id, "row_id")
        row = next((item for item in self.universe.rows if item.row_id == row_id), None)
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
            stage = self.current_stage(row.row_id).value
            stages[stage] = stages.get(stage, 0) + 1
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
            "events": [
                {"event_id": event.event_id, **event.to_payload()}
                for event in self.events
            ],
        }
        if include_digest:
            payload["ledger_sha256"] = self.ledger_sha256
        return payload

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, object],
        *,
        paper_resolver: CanonicalPaperExecutionResolver | None = None,
    ) -> "EvaluationUniverseLedger":
        try:
            if (
                payload["schema"] != SCHEMA
                or payload["schema_version"] != SCHEMA_VERSION
                or payload["kind"] != "evaluation_universe_ledger"
            ):
                raise EvaluationUniverseIntegrityError(
                    "unsupported evaluation-universe ledger schema"
                )
            raw_universe = payload["universe"]
            raw_events = payload["events"]
            if not isinstance(raw_universe, Mapping) or not isinstance(raw_events, list):
                raise EvaluationUniverseIntegrityError(
                    "ledger universe/events have invalid JSON type"
                )
            universe = EvaluationUniverse.from_payload(raw_universe)
            events: list[FunnelEvent] = []
            for raw in raw_events:
                if not isinstance(raw, Mapping):
                    raise EvaluationUniverseIntegrityError("event must be an object")
                values = dict(raw)
                claimed = values.pop("event_id", None)
                event = FunnelEvent.from_payload(values)
                if claimed != event.event_id:
                    raise EvaluationUniverseIntegrityError(
                        "event_id does not bind event payload"
                    )
                events.append(event)
            ledger = cls(universe, tuple(events), paper_resolver)
            if payload.get("ledger_sha256") != ledger.ledger_sha256:
                raise EvaluationUniverseIntegrityError(
                    "ledger_sha256 does not bind durable payload"
                )
            return ledger
        except KeyError as exc:
            raise EvaluationUniverseIntegrityError(
                "evaluation-universe ledger payload is incomplete"
            ) from exc


class EvaluationUniverseStore:
    """Durable denominator store protected by the shared monotonic workspace authority."""

    FILE_NAME = "evaluation-universe.json"

    def __init__(
        self,
        workspace: str | Path,
        *,
        intake_ledger: ObservationIntakeLedger,
        paper_resolver: CanonicalPaperExecutionResolver | None = None,
        authority_root: str | Path | None = None,
    ) -> None:
        if not isinstance(intake_ledger, ObservationIntakeLedger):
            raise TypeError("intake_ledger must be ObservationIntakeLedger")
        self.workspace = Path(workspace).expanduser().resolve(strict=False)
        self.path = self.workspace / self.FILE_NAME
        self.intake_ledger = intake_ledger
        self.paper_resolver = paper_resolver
        self.monotonic_authority = MonotonicWorkspaceAuthority(
            workspace=self.workspace,
            domain="evaluation-universe",
            key=self.intake_ledger.authority_id,
            authority_root=authority_root,
        )

    def _read_unlocked(self) -> EvaluationUniverseLedger | None:
        if not self.path.exists():
            return None
        try:
            raw = strict_json_loads(self.path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            raise EvaluationUniverseIntegrityError(
                "cannot read durable evaluation-universe ledger"
            ) from exc
        if not isinstance(raw, dict):
            raise EvaluationUniverseIntegrityError(
                "durable evaluation-universe ledger must be a JSON object"
            )
        return EvaluationUniverseLedger.from_payload(
            raw,
            paper_resolver=self.paper_resolver,
        )

    def _semantic_binding(self, ledger: EvaluationUniverseLedger) -> str:
        return _digest(
            {
                "kind": "evaluation-universe-store-v1",
                "intake_authority_id": self.intake_ledger.authority_id,
                "intake_snapshot_sha256": ledger.universe.intake_snapshot.snapshot_sha256,
                "universe_sha256": ledger.universe.universe_sha256,
            }
        )

    def _recover_unlocked(
        self,
        ledger: EvaluationUniverseLedger | None,
    ) -> None:
        observed = None if ledger is None else ledger.ledger_sha256
        try:
            history = self.monotonic_authority.read_history()
            pending = (
                history[-1]
                if history and history[-1].phase is AuthorityPhase.PREPARE
                else None
            )
            if ledger is None:
                self.monotonic_authority.recover(observed_state_sha256=None)
            else:
                binding = self._semantic_binding(ledger)
                if pending is not None and pending.intended_state_sha256 == observed:
                    if pending.semantic_binding_sha256 != binding:
                        raise EvaluationUniverseIntegrityError(
                            "prepared evaluation-universe semantic binding mismatches published state"
                        )
                    self.monotonic_authority.recover(
                        observed_state_sha256=observed,
                        tx_id=pending.tx_id,
                        semantic_binding_sha256=binding,
                    )
                else:
                    self.monotonic_authority.recover(
                        observed_state_sha256=observed,
                    )
        except MonotonicWorkspaceAuthorityError as exc:
            raise EvaluationUniverseIntegrityError(
                "evaluation-universe state is stale, deleted, rolled back, or unproven"
            ) from exc

    def _next_tx_id(self, intended_state_sha256: str) -> str:
        history = self.monotonic_authority.read_history()
        attempt = len(history) + 1
        return f"evaluation-universe:{attempt}:{intended_state_sha256[:32]}"

    def load(self) -> EvaluationUniverseLedger | None:
        with WorkspaceEconomicLock(self.workspace):
            ledger = self._read_unlocked()
            self._recover_unlocked(ledger)
        if ledger is not None:
            self.intake_ledger.verify_snapshot(ledger.universe.intake_snapshot)
        return ledger

    def save(self, ledger: EvaluationUniverseLedger) -> None:
        if not isinstance(ledger, EvaluationUniverseLedger):
            raise EvaluationUniverseError("ledger must be EvaluationUniverseLedger")
        self.intake_ledger.verify_snapshot(ledger.universe.intake_snapshot)
        with WorkspaceEconomicLock(self.workspace):
            existing = self._read_unlocked()
            self._recover_unlocked(existing)
            if existing is not None:
                if existing.universe.universe_sha256 != ledger.universe.universe_sha256:
                    raise EvaluationUniverseIntegrityError(
                        "cannot replace frozen universe identity"
                    )
                old_ids = tuple(event.event_id for event in existing.events)
                new_ids = tuple(event.event_id for event in ledger.events)
                if len(new_ids) < len(old_ids):
                    raise EvaluationUniverseIntegrityError(
                        "durable funnel history cannot shrink"
                    )
                if new_ids[: len(old_ids)] != old_ids:
                    raise EvaluationUniverseIntegrityError(
                        "durable funnel history is append-only"
                    )
                if existing.ledger_sha256 == ledger.ledger_sha256:
                    return

            observed = None if existing is None else existing.ledger_sha256
            intended = ledger.ledger_sha256
            binding = self._semantic_binding(ledger)
            tx_id = self._next_tx_id(intended)
            try:
                self.monotonic_authority.prepare(
                    tx_id=tx_id,
                    observed_state_sha256=observed,
                    intended_state_sha256=intended,
                    semantic_binding_sha256=binding,
                )
                atomic_write_json(self.path, ledger.to_payload())
                published = self._read_unlocked()
                if published is None or published.ledger_sha256 != intended:
                    raise EvaluationUniverseIntegrityError(
                        "published evaluation-universe state does not match intended digest"
                    )
                self.monotonic_authority.commit(
                    tx_id=tx_id,
                    observed_state_sha256=intended,
                    semantic_binding_sha256=binding,
                )
            except MonotonicWorkspaceAuthorityError as exc:
                raise EvaluationUniverseIntegrityError(
                    "monotonic evaluation-universe publication failed closed"
                ) from exc


def _validate_canonical_intake_rows(
    *,
    intake_ledger: ObservationIntakeLedger,
    snapshot: ObservationIntakeSnapshot,
    rows: tuple[EvaluationRow, ...],
    frozen_at: str,
) -> None:
    actual = tuple(sorted({row.row_key for row in rows}))
    if actual != snapshot.expected_row_keys:
        raise EvaluationUniverseError(
            "supplied rows do not equal canonical pre-result intake membership"
        )
    frozen = _instant(frozen_at, "frozen_at")
    complete_evaluation_not_before = _instant(
        snapshot.evaluation_not_before,
        "snapshot evaluation_not_before",
    )
    if complete_evaluation_not_before > frozen:
        raise EvaluationUniverseError(
            "canonical intake evaluation boundary is after universe freeze"
        )
    for row in rows:
        record = intake_ledger.record_for_row(row.row_key)
        if record.identity != snapshot.identity:
            raise EvaluationUniverseIntegrityError(
                "row intake record escapes canonical snapshot identity"
            )
        if row.row_id != intake_ledger.row_evidence_sha256(row.row_key):
            raise EvaluationUniverseIntegrityError(
                "row evidence does not match immutable upstream intake evidence"
            )
        intake_committed = _instant(record.committed_at, "intake committed_at")
        evaluation_not_before = _instant(
            record.evaluation_not_before,
            "intake evaluation_not_before",
        )
        if evaluation_not_before > frozen:
            raise EvaluationUniverseError(
                "canonical intake evaluation boundary is after universe freeze"
            )
        if _instant(row.committed_at, "row committed_at") > intake_committed:
            raise EvaluationUniverseError(
                "row evidence was committed after canonical intake membership"
            )
        if row.detection_at is not None and _instant(
            row.detection_at,
            "detection_at",
        ) < complete_evaluation_not_before:
            raise EvaluationUniverseError(
                "row detection began before complete intake membership was immutable"
            )
        if row.decision_at is not None and _instant(
            row.decision_at,
            "decision_at",
        ) < complete_evaluation_not_before:
            raise EvaluationUniverseError(
                "row decision began before complete intake membership was immutable"
            )
        if row.outcome_reveal_not_before is None or _instant(
            row.outcome_reveal_not_before,
            "outcome_reveal_not_before",
        ) != _instant(
            record.outcome_reveal_not_before,
            "intake outcome_reveal_not_before",
        ):
            raise EvaluationUniverseError(
                "row reveal boundary does not match canonical pre-result intake"
            )


def build_frozen_universe(
    *,
    intake_ledger: ObservationIntakeLedger,
    universe_id: str,
    campaign_id: str,
    research_protocol_id: str,
    protocol_sha256: str,
    frozen_at: str,
    rows: Iterable[EvaluationRow],
) -> EvaluationUniverse:
    """Freeze exactly the complete durable intake tip; caller cannot choose membership."""

    if not isinstance(intake_ledger, ObservationIntakeLedger):
        raise EvaluationUniverseError("intake_ledger must be canonical ObservationIntakeLedger")
    materialized = tuple(rows)
    if not materialized:
        raise EvaluationUniverseError("cannot freeze an empty evaluation universe")
    records = intake_ledger.records()
    if not records:
        raise EvaluationUniverseError("canonical pre-result intake is empty")
    snapshot = intake_ledger.snapshot(first_cycle=1, last_cycle=len(records))
    expected_identity = (
        campaign_id,
        research_protocol_id,
        _sha(protocol_sha256, "protocol_sha256"),
        universe_id,
    )
    if (
        snapshot.campaign_id,
        snapshot.research_protocol_id,
        snapshot.protocol_sha256,
        snapshot.universe_id,
    ) != expected_identity:
        raise EvaluationUniverseError(
            "canonical intake does not bind requested campaign/protocol/universe"
        )
    _validate_canonical_intake_rows(
        intake_ledger=intake_ledger,
        snapshot=snapshot,
        rows=materialized,
        frozen_at=frozen_at,
    )
    return EvaluationUniverse._construct(
        intake_snapshot=snapshot,
        universe_id=universe_id,
        campaign_id=campaign_id,
        research_protocol_id=research_protocol_id,
        protocol_sha256=protocol_sha256,
        frozen_at=frozen_at,
        rows=materialized,
    )
