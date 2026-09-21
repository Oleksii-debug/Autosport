from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .real_execution_ledger import (
    AcknowledgementStatus,
    AttemptState,
    EventType,
    ExecutionLedgerIntegrityError,
    RealExecutionLedger,
)

SCHEMA_VERSION = 1


class EmpiricalExecutionEvidenceError(RuntimeError):
    """Base error for empirical execution evidence projection."""


class EmpiricalExecutionEvidenceUnavailable(EmpiricalExecutionEvidenceError):
    """Raised when durable facts are insufficient for a complete measurement."""


def _timestamp(value: object, name: str) -> datetime:
    if type(value) is not str or not value or value != value.strip():
        raise EmpiricalExecutionEvidenceError(f"{name} must be canonical timestamp text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EmpiricalExecutionEvidenceError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EmpiricalExecutionEvidenceError(f"{name} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise EmpiricalExecutionEvidenceError(f"{name} must be non-empty canonical text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise EmpiricalExecutionEvidenceError(f"{name} must be UTF-8 encodable") from exc
    return value


def _sha256(value: object, name: str) -> str:
    raw = _text(value, name)
    if len(raw) != 64 or any(ch not in "0123456789abcdef" for ch in raw):
        raise EmpiricalExecutionEvidenceError(f"{name} must be lowercase SHA-256 hex")
    return raw


def _decimal(value: object, name: str) -> Decimal:
    try:
        parsed = value if type(value) is Decimal else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise EmpiricalExecutionEvidenceError(f"{name} must be finite Decimal") from exc
    if not parsed.is_finite():
        raise EmpiricalExecutionEvidenceError(f"{name} must be finite Decimal")
    return parsed


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    raw = format(value, "f")
    if "." in raw:
        raw = raw.rstrip("0").rstrip(".")
    return raw or "0"


def _duration_us(later: datetime, earlier: datetime, name: str) -> int:
    delta = later - earlier
    micros = (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )
    if micros < 0:
        raise EmpiricalExecutionEvidenceUnavailable(
            f"{name} is negative; durable causal timing is incomplete"
        )
    return micros


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise EmpiricalExecutionEvidenceError("evidence is not canonical JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class EmpiricalExecutionEvidence:
    source_ledger_sha256: str
    source_event_count: int
    plan_id: str
    plan_fingerprint: str
    action_id: str
    attempt_id: str
    bookmaker_id: str
    account_id: str
    event_id: str
    market_id: str
    selection_id: str
    side: str
    quote_id: str
    external_receipt_id: str
    provider_evidence_id: str
    provider_evidence_source: str
    decision_at: str
    quote_observed_at: str
    reserved_at: str
    submitted_at: str
    provider_evidence_observed_at: str
    acknowledged_at: str
    acknowledgement_status: str
    requested_odds: Decimal
    requested_stake: Decimal
    accepted_odds: Decimal | None
    accepted_stake: Decimal | None
    accepted_minus_requested_odds: Decimal | None
    adverse_odds_delta: Decimal | None
    unaccepted_stake: Decimal | None
    quote_age_at_decision_us: int
    decision_to_reserve_us: int
    decision_to_submit_us: int
    submit_to_provider_evidence_us: int
    submit_to_acknowledgement_us: int
    decision_to_acknowledgement_us: int
    schema_version: int = SCHEMA_VERSION
    evidence_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise EmpiricalExecutionEvidenceError("unsupported empirical evidence schema")
        for name in (
            "plan_id",
            "action_id",
            "attempt_id",
            "bookmaker_id",
            "account_id",
            "event_id",
            "market_id",
            "selection_id",
            "side",
            "quote_id",
            "external_receipt_id",
            "provider_evidence_source",
            "decision_at",
            "quote_observed_at",
            "reserved_at",
            "submitted_at",
            "provider_evidence_observed_at",
            "acknowledged_at",
            "acknowledgement_status",
        ):
            _text(getattr(self, name), name)
        _sha256(self.source_ledger_sha256, "source_ledger_sha256")
        _sha256(self.plan_fingerprint, "plan_fingerprint")
        _sha256(self.provider_evidence_id, "provider_evidence_id")
        if type(self.source_event_count) is not int or self.source_event_count < 1:
            raise EmpiricalExecutionEvidenceError("source_event_count must be positive int")
        for name in (
            "quote_age_at_decision_us",
            "decision_to_reserve_us",
            "decision_to_submit_us",
            "submit_to_provider_evidence_us",
            "submit_to_acknowledgement_us",
            "decision_to_acknowledgement_us",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise EmpiricalExecutionEvidenceError(f"{name} must be non-negative int")
        for name in ("requested_odds", "requested_stake"):
            if _decimal(getattr(self, name), name) <= 0:
                raise EmpiricalExecutionEvidenceError(f"{name} must be > 0")
        for name in (
            "accepted_odds",
            "accepted_stake",
            "accepted_minus_requested_odds",
            "adverse_odds_delta",
            "unaccepted_stake",
        ):
            value = getattr(self, name)
            if value is not None:
                _decimal(value, name)
        object.__setattr__(
            self,
            "evidence_sha256",
            _digest(self.to_dict(include_evidence_sha256=False)),
        )

    def to_dict(self, *, include_evidence_sha256: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": "autosport.empirical_execution_evidence",
            "schema_version": self.schema_version,
            "source_ledger_sha256": self.source_ledger_sha256,
            "source_event_count": self.source_event_count,
            "plan_id": self.plan_id,
            "plan_fingerprint": self.plan_fingerprint,
            "action_id": self.action_id,
            "attempt_id": self.attempt_id,
            "bookmaker_id": self.bookmaker_id,
            "account_id": self.account_id,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "side": self.side,
            "quote_id": self.quote_id,
            "external_receipt_id": self.external_receipt_id,
            "provider_evidence_id": self.provider_evidence_id,
            "provider_evidence_source": self.provider_evidence_source,
            "decision_at": self.decision_at,
            "quote_observed_at": self.quote_observed_at,
            "reserved_at": self.reserved_at,
            "submitted_at": self.submitted_at,
            "provider_evidence_observed_at": self.provider_evidence_observed_at,
            "acknowledged_at": self.acknowledged_at,
            "acknowledgement_status": self.acknowledgement_status,
            "requested_odds": _decimal_text(self.requested_odds),
            "requested_stake": _decimal_text(self.requested_stake),
            "accepted_odds": _decimal_text(self.accepted_odds),
            "accepted_stake": _decimal_text(self.accepted_stake),
            "accepted_minus_requested_odds": _decimal_text(
                self.accepted_minus_requested_odds
            ),
            "adverse_odds_delta": _decimal_text(self.adverse_odds_delta),
            "unaccepted_stake": _decimal_text(self.unaccepted_stake),
            "quote_age_at_decision_us": self.quote_age_at_decision_us,
            "decision_to_reserve_us": self.decision_to_reserve_us,
            "decision_to_submit_us": self.decision_to_submit_us,
            "submit_to_provider_evidence_us": self.submit_to_provider_evidence_us,
            "submit_to_acknowledgement_us": self.submit_to_acknowledgement_us,
            "decision_to_acknowledgement_us": self.decision_to_acknowledgement_us,
        }
        if include_evidence_sha256:
            payload["evidence_sha256"] = self.evidence_sha256
        return payload


def _single_event(
    events: list[dict[str, Any]],
    event_type: EventType,
    *,
    label: str,
) -> dict[str, Any]:
    matches = [
        event for event in events if event["event_type"] == event_type.value
    ]
    if len(matches) != 1:
        raise EmpiricalExecutionEvidenceUnavailable(
            f"{label} requires exactly one durable {event_type.value} event"
        )
    return matches[0]


def build_empirical_execution_evidence(
    ledger: RealExecutionLedger,
    *,
    attempt_id: str,
) -> EmpiricalExecutionEvidence:
    """Project descriptive latency/slippage metrics from verified durable execution facts.

    This function is read-only. It does not grant provider-write, retry, settlement,
    profitability, or real-money authority.
    """
    if type(ledger) is not RealExecutionLedger:
        raise TypeError("ledger must be canonical RealExecutionLedger")
    attempt = _text(attempt_id, "attempt_id")

    snapshot = ledger.verified_snapshot()
    try:
        events = RealExecutionLedger._parse(snapshot.payload)
    except ExecutionLedgerIntegrityError:
        raise
    attempt_events = RealExecutionLedger._attempt_events(events, attempt)
    if not attempt_events:
        raise EmpiricalExecutionEvidenceUnavailable("attempt is not present in ledger")

    state = RealExecutionLedger._state(attempt_events)
    if state not in {
        AttemptState.ACCEPTED,
        AttemptState.PARTIAL,
        AttemptState.REJECTED,
    }:
        raise EmpiricalExecutionEvidenceUnavailable(
            "empirical evidence requires terminal durable acknowledgement"
        )

    reservation = _single_event(
        attempt_events, EventType.ATTEMPT_RESERVED, label="empirical evidence"
    )
    submission = _single_event(
        attempt_events, EventType.ATTEMPT_SUBMITTED, label="empirical evidence"
    )
    acknowledgement_event = _single_event(
        attempt_events, EventType.EXTERNAL_ACKNOWLEDGEMENT, label="empirical evidence"
    )
    provider_event = _single_event(
        attempt_events, EventType.PROVIDER_EVIDENCE_BOUND, label="empirical evidence"
    )

    plan_id = _text(reservation["plan_id"], "plan_id")
    action_id = _text(reservation["action_id"], "action_id")
    plan_event, action = RealExecutionLedger._action_payload(
        events, plan_id, action_id
    )
    acknowledgement = RealExecutionLedger._acknowledgement_from_dict(
        acknowledgement_event["payload"]
    )

    plan_payload = plan_event["payload"]
    plan_fingerprint = _sha256(
        plan_payload.get("plan_fingerprint"), "plan_fingerprint"
    )
    plan = plan_payload.get("plan")
    if type(plan) is not dict:
        raise EmpiricalExecutionEvidenceUnavailable("stored plan payload is invalid")

    decision_at = _text(plan.get("created_at"), "decision_at")
    quote_observed_at = _text(action.get("quote_observed_at"), "quote_observed_at")
    reserved_at = _text(reservation["payload"].get("reserved_at"), "reserved_at")
    submitted_at = _text(submission["payload"].get("submitted_at"), "submitted_at")
    provider_payload = provider_event["payload"]
    provider_observed_at = _text(
        provider_payload.get("observed_at"), "provider_evidence_observed_at"
    )
    acknowledged_at = acknowledgement.acknowledged_at

    quote_time = _timestamp(quote_observed_at, "quote_observed_at")
    decision_time = _timestamp(decision_at, "decision_at")
    reserve_time = _timestamp(reserved_at, "reserved_at")
    submit_time = _timestamp(submitted_at, "submitted_at")
    provider_time = _timestamp(provider_observed_at, "provider_evidence_observed_at")
    acknowledgement_time = _timestamp(acknowledged_at, "acknowledged_at")

    quote_age = _duration_us(
        decision_time, quote_time, "quote_age_at_decision"
    )
    decision_to_reserve = _duration_us(
        reserve_time, decision_time, "decision_to_reserve"
    )
    decision_to_submit = _duration_us(
        submit_time, decision_time, "decision_to_submit"
    )
    submit_to_provider = _duration_us(
        provider_time, submit_time, "submit_to_provider_evidence"
    )
    submit_to_ack = _duration_us(
        acknowledgement_time, submit_time, "submit_to_acknowledgement"
    )
    decision_to_ack = _duration_us(
        acknowledgement_time, decision_time, "decision_to_acknowledgement"
    )
    if provider_time > acknowledgement_time:
        raise EmpiricalExecutionEvidenceUnavailable(
            "provider evidence cannot postdate the durable acknowledgement"
        )

    requested_odds = _decimal(action.get("requested_odds"), "requested_odds")
    requested_stake = _decimal(action.get("requested_stake"), "requested_stake")
    if requested_odds <= 0 or requested_stake <= 0:
        raise EmpiricalExecutionEvidenceUnavailable(
            "requested execution odds/stake must be positive"
        )

    accepted_odds: Decimal | None = None
    accepted_stake: Decimal | None = None
    accepted_minus_requested: Decimal | None = None
    adverse_delta: Decimal | None = None
    unaccepted_stake: Decimal | None = None

    if acknowledgement.status in {
        AcknowledgementStatus.ACCEPTED,
        AcknowledgementStatus.PARTIAL,
    }:
        if acknowledgement.accepted_odds is None or acknowledgement.accepted_stake is None:
            raise EmpiricalExecutionEvidenceUnavailable(
                "accepted acknowledgement lacks accepted odds/stake"
            )
        accepted_odds = _decimal(acknowledgement.accepted_odds, "accepted_odds")
        accepted_stake = _decimal(acknowledgement.accepted_stake, "accepted_stake")
        if accepted_odds <= 0 or accepted_stake <= 0:
            raise EmpiricalExecutionEvidenceUnavailable(
                "accepted odds/stake must be positive"
            )
        accepted_minus_requested = accepted_odds - requested_odds
        side = _text(action.get("side"), "side")
        if side == "BACK":
            adverse_delta = max(requested_odds - accepted_odds, Decimal("0"))
        elif side == "LAY":
            adverse_delta = max(accepted_odds - requested_odds, Decimal("0"))
        else:
            raise EmpiricalExecutionEvidenceUnavailable(
                "accepted-price slippage semantics support canonical BACK/LAY only"
            )
        unaccepted_stake = requested_stake - accepted_stake
        if unaccepted_stake < 0:
            raise EmpiricalExecutionEvidenceUnavailable(
                "accepted stake exceeds requested stake"
            )

    evidence = EmpiricalExecutionEvidence(
        source_ledger_sha256=snapshot.sha256,
        source_event_count=snapshot.event_count,
        plan_id=plan_id,
        plan_fingerprint=plan_fingerprint,
        action_id=action_id,
        attempt_id=attempt,
        bookmaker_id=_text(action.get("bookmaker_id"), "bookmaker_id"),
        account_id=_text(action.get("account_id"), "account_id"),
        event_id=_text(action.get("event_id"), "event_id"),
        market_id=_text(action.get("market_id"), "market_id"),
        selection_id=_text(action.get("selection_id"), "selection_id"),
        side=_text(action.get("side"), "side"),
        quote_id=_text(action.get("quote_id"), "quote_id"),
        external_receipt_id=acknowledgement.external_receipt_id,
        provider_evidence_id=_sha256(
            provider_payload.get("evidence_id"), "provider_evidence_id"
        ),
        provider_evidence_source=_text(
            provider_payload.get("source"), "provider_evidence_source"
        ),
        decision_at=decision_at,
        quote_observed_at=quote_observed_at,
        reserved_at=reserved_at,
        submitted_at=submitted_at,
        provider_evidence_observed_at=provider_observed_at,
        acknowledged_at=acknowledged_at,
        acknowledgement_status=acknowledgement.status.value,
        requested_odds=requested_odds,
        requested_stake=requested_stake,
        accepted_odds=accepted_odds,
        accepted_stake=accepted_stake,
        accepted_minus_requested_odds=accepted_minus_requested,
        adverse_odds_delta=adverse_delta,
        unaccepted_stake=unaccepted_stake,
        quote_age_at_decision_us=quote_age,
        decision_to_reserve_us=decision_to_reserve,
        decision_to_submit_us=decision_to_submit,
        submit_to_provider_evidence_us=submit_to_provider,
        submit_to_acknowledgement_us=submit_to_ack,
        decision_to_acknowledgement_us=decision_to_ack,
    )
    return evidence
