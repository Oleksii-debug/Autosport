"""Fail-closed disposition authority for stale or ambiguous live-market state.

This module owns only *non-action* outcomes. It can say wait, decline, escalate,
or abort; it never grants execution authority. Durable records deliberately carry
the original waiting deadline so process restart cannot reset a stale-data timeout.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Final, Mapping


SCHEMA: Final = "autosport.live_state_disposition"
SCHEMA_VERSION: Final = 1


class LiveStateDispositionError(ValueError):
    """Raised when live-state evidence or a durable disposition record is invalid."""


class LiveStateDisposition(str, Enum):
    """Explicit non-action outcomes for one live decision cycle."""

    WAIT_EVIDENCE = "WAIT_EVIDENCE"
    NO_BET_POLICY = "NO_BET_POLICY"
    NO_BET_INFEASIBLE = "NO_BET_INFEASIBLE"
    ESCALATE_OPERATOR = "ESCALATE_OPERATOR"
    SAFE_ABORT = "SAFE_ABORT"


UK_UA_DISPOSITION_LABELS: Final[dict[LiveStateDisposition, str]] = {
    LiveStateDisposition.WAIT_EVIDENCE: "Очікуємо нові ринкові дані",
    LiveStateDisposition.NO_BET_POLICY: "Ставку відхилено політикою",
    LiveStateDisposition.NO_BET_INFEASIBLE: "Ставка неможлива за поточним ринком",
    LiveStateDisposition.ESCALATE_OPERATOR: "Потрібне рішення оператора",
    LiveStateDisposition.SAFE_ABORT: "Автоматичне виконання зупинено",
}


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise LiveStateDispositionError(f"{field} must be a non-empty trimmed string")
    return value


def _aware(value: object, field: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise LiveStateDispositionError(f"{field} must be a timezone-aware datetime")
    return value


def _parse_datetime(value: object, field: str) -> datetime:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise LiveStateDispositionError(f"{field} must be ISO-8601") from exc
    return _aware(parsed, field)


@dataclass(frozen=True, slots=True)
class LiveStateEvidence:
    """Exact evidence identity and state used by the disposition authority."""

    decision_cycle_id: str
    canonical_input_bundle_id: str
    evidence_version: str
    policy_version: str
    state_observed_at: datetime
    freshness_cutoff_at: datetime
    evidence_complete: bool
    contradictory: bool = False
    unsafe_restart_state: bool = False
    policy_declined: bool = False
    infeasible: bool = False

    def __post_init__(self) -> None:
        _text(self.decision_cycle_id, "decision_cycle_id")
        _text(self.canonical_input_bundle_id, "canonical_input_bundle_id")
        _text(self.evidence_version, "evidence_version")
        _text(self.policy_version, "policy_version")
        _aware(self.state_observed_at, "state_observed_at")
        _aware(self.freshness_cutoff_at, "freshness_cutoff_at")
        for field in (
            "evidence_complete",
            "contradictory",
            "unsafe_restart_state",
            "policy_declined",
            "infeasible",
        ):
            if type(getattr(self, field)) is not bool:
                raise LiveStateDispositionError(f"{field} must be bool")

    @property
    def identity(self) -> tuple[str, str, str, str]:
        return (
            self.decision_cycle_id,
            self.canonical_input_bundle_id,
            self.evidence_version,
            self.policy_version,
        )

    @property
    def is_fresh(self) -> bool:
        return self.state_observed_at >= self.freshness_cutoff_at


@dataclass(frozen=True, slots=True)
class LiveDispositionRecord:
    """Durable non-action decision that is safe to persist across restart."""

    decision_cycle_id: str
    canonical_input_bundle_id: str
    freshness_cutoff_at: datetime
    state_observed_at: datetime
    disposition: LiveStateDisposition
    reason_code: str
    previous_disposition: LiveStateDisposition | None
    evidence_version: str
    policy_version: str
    retry_not_before_at: datetime | None
    waiting_expires_at: datetime | None
    operator_required: bool
    auto_submit_allowed: bool
    restart_reloaded: bool
    first_seen_at: datetime
    last_checked_at: datetime
    schema: str = SCHEMA
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema != SCHEMA or self.schema_version != SCHEMA_VERSION:
            raise LiveStateDispositionError("unsupported live-state disposition schema")
        _text(self.decision_cycle_id, "decision_cycle_id")
        _text(self.canonical_input_bundle_id, "canonical_input_bundle_id")
        _text(self.evidence_version, "evidence_version")
        _text(self.policy_version, "policy_version")
        _text(self.reason_code, "reason_code")
        _aware(self.freshness_cutoff_at, "freshness_cutoff_at")
        _aware(self.state_observed_at, "state_observed_at")
        _aware(self.first_seen_at, "first_seen_at")
        _aware(self.last_checked_at, "last_checked_at")
        if type(self.disposition) is not LiveStateDisposition:
            raise LiveStateDispositionError("disposition must be LiveStateDisposition")
        if self.previous_disposition is not None and type(self.previous_disposition) is not LiveStateDisposition:
            raise LiveStateDispositionError("previous_disposition must be LiveStateDisposition or None")
        if self.retry_not_before_at is not None:
            _aware(self.retry_not_before_at, "retry_not_before_at")
        if self.waiting_expires_at is not None:
            _aware(self.waiting_expires_at, "waiting_expires_at")
        if type(self.operator_required) is not bool or type(self.restart_reloaded) is not bool:
            raise LiveStateDispositionError("operator_required and restart_reloaded must be bool")
        if type(self.auto_submit_allowed) is not bool or self.auto_submit_allowed:
            raise LiveStateDispositionError("non-action dispositions can never allow auto-submit")
        if self.last_checked_at < self.first_seen_at:
            raise LiveStateDispositionError("last_checked_at cannot predate first_seen_at")
        if self.disposition is LiveStateDisposition.WAIT_EVIDENCE:
            if self.waiting_expires_at is None:
                raise LiveStateDispositionError("WAIT_EVIDENCE requires waiting_expires_at")
            if self.operator_required:
                raise LiveStateDispositionError("WAIT_EVIDENCE cannot require operator")
        if self.disposition in {LiveStateDisposition.ESCALATE_OPERATOR, LiveStateDisposition.SAFE_ABORT}:
            if not self.operator_required:
                raise LiveStateDispositionError(f"{self.disposition.value} must require operator")
        elif self.operator_required:
            raise LiveStateDispositionError(f"{self.disposition.value} cannot require operator")

    @property
    def identity(self) -> tuple[str, str, str, str]:
        return (
            self.decision_cycle_id,
            self.canonical_input_bundle_id,
            self.evidence_version,
            self.policy_version,
        )

    @property
    def operator_label_uk(self) -> str:
        """Stable Ukrainian operator label; not a claim of NVDA verification."""

        return UK_UA_DISPOSITION_LABELS[self.disposition]

    def to_dict(self) -> dict[str, object]:
        """Return a strict JSON-compatible durable representation."""

        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "decision_cycle_id": self.decision_cycle_id,
            "canonical_input_bundle_id": self.canonical_input_bundle_id,
            "freshness_cutoff_at": self.freshness_cutoff_at.isoformat(),
            "state_observed_at": self.state_observed_at.isoformat(),
            "disposition": self.disposition.value,
            "reason_code": self.reason_code,
            "previous_disposition": self.previous_disposition.value if self.previous_disposition is not None else None,
            "evidence_version": self.evidence_version,
            "policy_version": self.policy_version,
            "retry_not_before_at": self.retry_not_before_at.isoformat() if self.retry_not_before_at is not None else None,
            "waiting_expires_at": self.waiting_expires_at.isoformat() if self.waiting_expires_at is not None else None,
            "operator_required": self.operator_required,
            "auto_submit_allowed": self.auto_submit_allowed,
            "restart_reloaded": self.restart_reloaded,
            "first_seen_at": self.first_seen_at.isoformat(),
            "last_checked_at": self.last_checked_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "LiveDispositionRecord":
        """Strictly reconstruct one persisted record without resetting its clock."""

        if type(payload) is not dict:
            raise LiveStateDispositionError("persisted disposition payload must be an exact dict")
        expected = {
            "schema", "schema_version", "decision_cycle_id", "canonical_input_bundle_id",
            "freshness_cutoff_at", "state_observed_at", "disposition", "reason_code",
            "previous_disposition", "evidence_version", "policy_version", "retry_not_before_at",
            "waiting_expires_at", "operator_required", "auto_submit_allowed", "restart_reloaded",
            "first_seen_at", "last_checked_at",
        }
        if set(payload) != expected:
            missing = sorted(expected - set(payload))
            extra = sorted(set(payload) - expected)
            raise LiveStateDispositionError(
                f"persisted disposition fields mismatch: missing={missing}, extra={extra}"
            )
        if payload["schema"] != SCHEMA or payload["schema_version"] != SCHEMA_VERSION:
            raise LiveStateDispositionError("unsupported live-state disposition schema")
        try:
            disposition = LiveStateDisposition(_text(payload["disposition"], "disposition"))
            previous_raw = payload["previous_disposition"]
            previous = None if previous_raw is None else LiveStateDisposition(_text(previous_raw, "previous_disposition"))
        except ValueError as exc:
            raise LiveStateDispositionError("unknown persisted disposition") from exc

        retry_raw = payload["retry_not_before_at"]
        expires_raw = payload["waiting_expires_at"]
        return cls(
            decision_cycle_id=_text(payload["decision_cycle_id"], "decision_cycle_id"),
            canonical_input_bundle_id=_text(payload["canonical_input_bundle_id"], "canonical_input_bundle_id"),
            freshness_cutoff_at=_parse_datetime(payload["freshness_cutoff_at"], "freshness_cutoff_at"),
            state_observed_at=_parse_datetime(payload["state_observed_at"], "state_observed_at"),
            disposition=disposition,
            reason_code=_text(payload["reason_code"], "reason_code"),
            previous_disposition=previous,
            evidence_version=_text(payload["evidence_version"], "evidence_version"),
            policy_version=_text(payload["policy_version"], "policy_version"),
            retry_not_before_at=None if retry_raw is None else _parse_datetime(retry_raw, "retry_not_before_at"),
            waiting_expires_at=None if expires_raw is None else _parse_datetime(expires_raw, "waiting_expires_at"),
            operator_required=payload["operator_required"],
            auto_submit_allowed=payload["auto_submit_allowed"],
            restart_reloaded=payload["restart_reloaded"],
            first_seen_at=_parse_datetime(payload["first_seen_at"], "first_seen_at"),
            last_checked_at=_parse_datetime(payload["last_checked_at"], "last_checked_at"),
        )


def _record(
    *,
    evidence: LiveStateEvidence,
    disposition: LiveStateDisposition,
    reason_code: str,
    evaluated_at: datetime,
    prior: LiveDispositionRecord | None,
    first_seen_at: datetime,
    waiting_expires_at: datetime | None,
    restart_reloaded: bool,
) -> LiveDispositionRecord:
    return LiveDispositionRecord(
        decision_cycle_id=evidence.decision_cycle_id,
        canonical_input_bundle_id=evidence.canonical_input_bundle_id,
        freshness_cutoff_at=evidence.freshness_cutoff_at,
        state_observed_at=evidence.state_observed_at,
        disposition=disposition,
        reason_code=reason_code,
        previous_disposition=prior.disposition if prior is not None else None,
        evidence_version=evidence.evidence_version,
        policy_version=evidence.policy_version,
        retry_not_before_at=None,
        waiting_expires_at=waiting_expires_at,
        operator_required=disposition in {LiveStateDisposition.ESCALATE_OPERATOR, LiveStateDisposition.SAFE_ABORT},
        auto_submit_allowed=False,
        restart_reloaded=(prior.restart_reloaded if prior is not None else False) or restart_reloaded,
        first_seen_at=first_seen_at,
        last_checked_at=evaluated_at,
    )


def decide_live_state_disposition(
    evidence: LiveStateEvidence,
    *,
    evaluated_at: datetime,
    wait_ttl: timedelta,
    prior: LiveDispositionRecord | None = None,
    restart_reloaded: bool = False,
    operator_resolution_token: str | None = None,
) -> LiveDispositionRecord | None:
    """Resolve one bounded non-action disposition, or ``None`` for the action path.

    ``None`` is deliberately *not* execution authority. It only means this narrow
    non-action authority found no reason to wait/decline/escalate/abort; downstream
    policy, risk, provider and execution gates remain responsible for any action.
    """

    if type(evidence) is not LiveStateEvidence:
        raise TypeError("evidence must be exact LiveStateEvidence")
    evaluated_at = _aware(evaluated_at, "evaluated_at")
    if type(wait_ttl) is not timedelta or wait_ttl <= timedelta(0):
        raise LiveStateDispositionError("wait_ttl must be a positive timedelta")
    if prior is not None and type(prior) is not LiveDispositionRecord:
        raise TypeError("prior must be exact LiveDispositionRecord or None")
    if type(restart_reloaded) is not bool:
        raise LiveStateDispositionError("restart_reloaded must be bool")
    if operator_resolution_token is not None:
        _text(operator_resolution_token, "operator_resolution_token")

    same_identity = prior is not None and prior.identity == evidence.identity
    first_seen_at = prior.first_seen_at if same_identity else evaluated_at
    inherited_deadline = prior.waiting_expires_at if same_identity else None

    if evidence.unsafe_restart_state:
        return _record(
            evidence=evidence, disposition=LiveStateDisposition.SAFE_ABORT,
            reason_code="UNSAFE_RESTART_STATE", evaluated_at=evaluated_at, prior=prior,
            first_seen_at=first_seen_at, waiting_expires_at=inherited_deadline,
            restart_reloaded=restart_reloaded,
        )
    if evidence.contradictory or (evidence.policy_declined and evidence.infeasible):
        reason = "CONFLICTING_TERMINAL_ASSESSMENTS" if evidence.policy_declined and evidence.infeasible else "EVIDENCE_CONTRADICTION"
        return _record(
            evidence=evidence, disposition=LiveStateDisposition.SAFE_ABORT,
            reason_code=reason, evaluated_at=evaluated_at, prior=prior,
            first_seen_at=first_seen_at, waiting_expires_at=inherited_deadline,
            restart_reloaded=restart_reloaded,
        )

    if same_identity and prior is not None:
        if prior.disposition in {
            LiveStateDisposition.SAFE_ABORT,
            LiveStateDisposition.NO_BET_POLICY,
            LiveStateDisposition.NO_BET_INFEASIBLE,
        }:
            return _record(
                evidence=evidence, disposition=prior.disposition, reason_code=prior.reason_code,
                evaluated_at=evaluated_at, prior=prior, first_seen_at=prior.first_seen_at,
                waiting_expires_at=prior.waiting_expires_at, restart_reloaded=restart_reloaded,
            )
        if prior.disposition is LiveStateDisposition.ESCALATE_OPERATOR and operator_resolution_token is None:
            return _record(
                evidence=evidence, disposition=LiveStateDisposition.ESCALATE_OPERATOR,
                reason_code=prior.reason_code, evaluated_at=evaluated_at, prior=prior,
                first_seen_at=prior.first_seen_at, waiting_expires_at=prior.waiting_expires_at,
                restart_reloaded=restart_reloaded,
            )

    if not evidence.evidence_complete or not evidence.is_fresh:
        waiting_expires_at = inherited_deadline or (first_seen_at + wait_ttl)
        if evaluated_at >= waiting_expires_at:
            return _record(
                evidence=evidence, disposition=LiveStateDisposition.ESCALATE_OPERATOR,
                reason_code="WAIT_EVIDENCE_EXPIRED", evaluated_at=evaluated_at, prior=prior,
                first_seen_at=first_seen_at, waiting_expires_at=waiting_expires_at,
                restart_reloaded=restart_reloaded,
            )
        return _record(
            evidence=evidence, disposition=LiveStateDisposition.WAIT_EVIDENCE,
            reason_code="LIVE_EVIDENCE_INCOMPLETE_OR_STALE", evaluated_at=evaluated_at,
            prior=prior, first_seen_at=first_seen_at, waiting_expires_at=waiting_expires_at,
            restart_reloaded=restart_reloaded,
        )

    if evidence.policy_declined:
        return _record(
            evidence=evidence, disposition=LiveStateDisposition.NO_BET_POLICY,
            reason_code="POLICY_DECLINED", evaluated_at=evaluated_at, prior=prior,
            first_seen_at=first_seen_at, waiting_expires_at=inherited_deadline,
            restart_reloaded=restart_reloaded,
        )
    if evidence.infeasible:
        return _record(
            evidence=evidence, disposition=LiveStateDisposition.NO_BET_INFEASIBLE,
            reason_code="MARKET_INFEASIBLE", evaluated_at=evaluated_at, prior=prior,
            first_seen_at=first_seen_at, waiting_expires_at=inherited_deadline,
            restart_reloaded=restart_reloaded,
        )

    return None
