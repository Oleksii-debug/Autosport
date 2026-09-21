from __future__ import annotations

import hashlib
import json
from dataclasses import InitVar, dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .real_execution_ledger import (
    AttemptState,
    EventType,
    ExecutionLedgerIntegrityError,
    RealExecutionLedger,
)

SCHEMA_VERSION = 2

TIMING_STATUS_UNKNOWN = "UNKNOWN"
TIMING_REASON_NO_MONOTONIC_WITNESS = "NO_MONOTONIC_WITNESS"

SLIPPAGE_STATUS_KNOWN = "KNOWN"
SLIPPAGE_STATUS_UNKNOWN = "UNKNOWN"
SLIPPAGE_STATUS_NOT_APPLICABLE = "NOT_APPLICABLE"

_EMPIRICAL_EVIDENCE_ISSUANCE_TOKEN = object()

_ACK_TERMINAL_STATES = frozenset(
    {
        AttemptState.ACCEPTED,
        AttemptState.PARTIAL,
        AttemptState.REJECTED,
    }
)
_TERMINAL_STATES = _ACK_TERMINAL_STATES | {AttemptState.RECONCILED_NOT_FOUND}

_CENSOR_REASON_BY_STATE = {
    AttemptState.RESERVED: "RESERVED_NOT_SUBMITTED",
    AttemptState.SUBMITTED: "SUBMITTED_NO_TERMINAL_ACK",
    AttemptState.UNKNOWN: "UNKNOWN_EXTERNAL_EFFECT",
}


class EmpiricalExecutionEvidenceError(RuntimeError):
    """Base error for empirical execution evidence projection."""


class EmpiricalExecutionEvidenceUnavailable(EmpiricalExecutionEvidenceError):
    """Raised only when no trustworthy attempt-level record can be projected."""


def _timestamp(value: object, name: str) -> datetime:
    if type(value) is not str or not value or value != value.strip():
        raise EmpiricalExecutionEvidenceError(f"{name} must be canonical timestamp text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EmpiricalExecutionEvidenceError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EmpiricalExecutionEvidenceError(f"{name} must be timezone-aware")
    return parsed


def _optional_timestamp(value: object, name: str) -> str | None:
    if value is None:
        return None
    _timestamp(value, name)
    return value


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise EmpiricalExecutionEvidenceError(f"{name} must be non-empty canonical text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise EmpiricalExecutionEvidenceError(f"{name} must be UTF-8 encodable") from exc
    return value


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _text(value, name)


def _sha256(value: object, name: str) -> str:
    raw = _text(value, name)
    if len(raw) != 64 or any(ch not in "0123456789abcdef" for ch in raw):
        raise EmpiricalExecutionEvidenceError(f"{name} must be lowercase SHA-256 hex")
    return raw


def _optional_sha256(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _sha256(value, name)


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


def _single_event(
    events: list[dict[str, Any]],
    event_type: EventType,
    *,
    label: str,
) -> dict[str, Any]:
    matches = [event for event in events if event["event_type"] == event_type.value]
    if len(matches) != 1:
        raise EmpiricalExecutionEvidenceUnavailable(
            f"{label} requires exactly one durable {event_type.value} event"
        )
    return matches[0]


def _optional_single_event(
    events: list[dict[str, Any]],
    event_type: EventType,
    *,
    label: str,
) -> dict[str, Any] | None:
    matches = [event for event in events if event["event_type"] == event_type.value]
    if len(matches) > 1:
        raise EmpiricalExecutionEvidenceUnavailable(
            f"{label} has multiple durable {event_type.value} events"
        )
    return matches[0] if matches else None


def _reconciliation_tuple_present(
    evidence_id: str | None,
    source: str | None,
    observed_at: str | None,
    external_effect_found: bool | None,
) -> bool:
    values = (evidence_id, source, observed_at, external_effect_found)
    present = tuple(value is not None for value in values)
    if any(present) and not all(present):
        raise EmpiricalExecutionEvidenceError(
            "reconciliation identity/source/time/effect must be all present or all absent"
        )
    return all(present)


@dataclass(frozen=True, slots=True)
class EmpiricalExecutionEvidence:
    source_ledger_sha256: str
    source_event_count: int

    plan_id: str
    plan_fingerprint: str
    action_id: str
    attempt_id: str
    attempt_state: str
    terminal: bool

    bookmaker_id: str
    account_id: str
    event_id: str
    market_id: str
    selection_id: str
    side: str
    quote_id: str

    decision_at: str
    quote_observed_at: str
    reserved_at: str
    submitted_at: str | None
    provider_evidence_observed_at: str | None
    acknowledged_at: str | None

    external_receipt_id: str | None
    provider_evidence_id: str | None
    provider_evidence_source: str | None
    acknowledgement_status: str | None

    reconciliation_evidence_id: str | None
    reconciliation_evidence_source: str | None
    reconciliation_evidence_observed_at: str | None
    reconciliation_external_effect_found: bool | None

    requested_odds: Decimal
    requested_stake: Decimal

    slippage_status: str
    accepted_odds: Decimal | None
    accepted_stake: Decimal | None
    accepted_minus_requested_odds: Decimal | None
    adverse_odds_delta: Decimal | None
    unaccepted_stake: Decimal | None

    causal_timing_status: str
    causal_timing_reason: str
    quote_age_at_decision_us: int | None
    decision_to_reserve_us: int | None
    decision_to_submit_us: int | None
    submit_to_provider_evidence_us: int | None
    submit_to_acknowledgement_us: int | None
    decision_to_acknowledgement_us: int | None

    right_censored: bool
    censor_reason: str | None
    censor_cutoff_recorded_at: str | None
    censor_cutoff_event_count: int | None

    schema_version: int = SCHEMA_VERSION
    evidence_sha256: str = field(init=False)
    _issuance_token: InitVar[object | None] = None

    def __post_init__(self, _issuance_token: object | None) -> None:
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
            "decision_at",
            "quote_observed_at",
            "reserved_at",
            "attempt_state",
            "slippage_status",
            "causal_timing_status",
            "causal_timing_reason",
        ):
            _text(getattr(self, name), name)

        _sha256(self.source_ledger_sha256, "source_ledger_sha256")
        _sha256(self.plan_fingerprint, "plan_fingerprint")
        _optional_sha256(self.provider_evidence_id, "provider_evidence_id")
        _optional_text(self.provider_evidence_source, "provider_evidence_source")
        _optional_text(self.external_receipt_id, "external_receipt_id")
        _optional_text(self.acknowledgement_status, "acknowledgement_status")
        _optional_text(self.reconciliation_evidence_id, "reconciliation_evidence_id")
        _optional_text(self.reconciliation_evidence_source, "reconciliation_evidence_source")

        if type(self.source_event_count) is not int or self.source_event_count < 1:
            raise EmpiricalExecutionEvidenceError("source_event_count must be positive int")
        if type(self.terminal) is not bool or type(self.right_censored) is not bool:
            raise EmpiricalExecutionEvidenceError("terminal/censor flags must be bool")
        if (
            self.reconciliation_external_effect_found is not None
            and type(self.reconciliation_external_effect_found) is not bool
        ):
            raise EmpiricalExecutionEvidenceError(
                "reconciliation_external_effect_found must be bool when present"
            )

        _timestamp(self.decision_at, "decision_at")
        _timestamp(self.quote_observed_at, "quote_observed_at")
        _timestamp(self.reserved_at, "reserved_at")
        _optional_timestamp(self.submitted_at, "submitted_at")
        _optional_timestamp(
            self.provider_evidence_observed_at,
            "provider_evidence_observed_at",
        )
        _optional_timestamp(self.acknowledged_at, "acknowledged_at")
        _optional_timestamp(
            self.reconciliation_evidence_observed_at,
            "reconciliation_evidence_observed_at",
        )
        _optional_timestamp(self.censor_cutoff_recorded_at, "censor_cutoff_recorded_at")

        reconciliation_present = _reconciliation_tuple_present(
            self.reconciliation_evidence_id,
            self.reconciliation_evidence_source,
            self.reconciliation_evidence_observed_at,
            self.reconciliation_external_effect_found,
        )

        try:
            state = AttemptState(self.attempt_state)
        except ValueError as exc:
            raise EmpiricalExecutionEvidenceError("attempt_state must be canonical") from exc

        expected_terminal = state in _TERMINAL_STATES
        if self.terminal is not expected_terminal:
            raise EmpiricalExecutionEvidenceError("terminal flag mismatches attempt_state")

        if expected_terminal:
            if self.right_censored:
                raise EmpiricalExecutionEvidenceError(
                    "terminal attempt cannot be right-censored"
                )
            if (
                self.censor_reason is not None
                or self.censor_cutoff_recorded_at is not None
                or self.censor_cutoff_event_count is not None
            ):
                raise EmpiricalExecutionEvidenceError(
                    "terminal attempt cannot carry censor metadata"
                )
            if state in _ACK_TERMINAL_STATES:
                if self.acknowledgement_status != state.value:
                    raise EmpiricalExecutionEvidenceError(
                        "terminal state must match acknowledgement_status"
                    )
                if self.acknowledged_at is None or self.external_receipt_id is None:
                    raise EmpiricalExecutionEvidenceError(
                        "acknowledged terminal attempt requires durable acknowledgement identity"
                    )
                if reconciliation_present:
                    raise EmpiricalExecutionEvidenceError(
                        "acknowledged terminal attempt cannot claim not-found reconciliation"
                    )
            elif state is AttemptState.RECONCILED_NOT_FOUND:
                if (
                    self.acknowledgement_status is not None
                    or self.acknowledged_at is not None
                    or self.external_receipt_id is not None
                ):
                    raise EmpiricalExecutionEvidenceError(
                        "RECONCILED_NOT_FOUND cannot claim terminal acknowledgement"
                    )
                if not reconciliation_present:
                    raise EmpiricalExecutionEvidenceError(
                        "RECONCILED_NOT_FOUND requires durable reconciliation identity"
                    )
                if self.reconciliation_external_effect_found is not False:
                    raise EmpiricalExecutionEvidenceError(
                        "RECONCILED_NOT_FOUND requires external_effect_found=false"
                    )
        else:
            expected_reason = _CENSOR_REASON_BY_STATE.get(state)
            if expected_reason is None:
                raise EmpiricalExecutionEvidenceError(
                    "unsupported nonterminal attempt state"
                )
            if not self.right_censored:
                raise EmpiricalExecutionEvidenceError(
                    "nonterminal attempt must be explicitly right-censored"
                )
            if self.censor_reason != expected_reason:
                raise EmpiricalExecutionEvidenceError(
                    "censor_reason mismatches durable attempt state"
                )
            if (
                self.censor_cutoff_recorded_at is None
                or type(self.censor_cutoff_event_count) is not int
                or self.censor_cutoff_event_count < 1
            ):
                raise EmpiricalExecutionEvidenceError(
                    "right-censored attempt requires durable observation cutoff"
                )
            if self.censor_cutoff_event_count != self.source_event_count:
                raise EmpiricalExecutionEvidenceError(
                    "censor cutoff must bind the verified source snapshot"
                )
            if (
                self.acknowledgement_status is not None
                or self.acknowledged_at is not None
                or self.external_receipt_id is not None
            ):
                raise EmpiricalExecutionEvidenceError(
                    "nonterminal attempt cannot claim terminal acknowledgement"
                )
            if reconciliation_present:
                raise EmpiricalExecutionEvidenceError(
                    "nonterminal attempt cannot claim terminal reconciliation"
                )

        if state is AttemptState.RESERVED and self.submitted_at is not None:
            raise EmpiricalExecutionEvidenceError(
                "RESERVED attempt cannot claim submitted_at"
            )
        if state is AttemptState.SUBMITTED and self.submitted_at is None:
            raise EmpiricalExecutionEvidenceError(
                "SUBMITTED attempt requires submitted_at"
            )

        if (self.provider_evidence_id is None) != (
            self.provider_evidence_source is None
        ) or (self.provider_evidence_id is None) != (
            self.provider_evidence_observed_at is None
        ):
            raise EmpiricalExecutionEvidenceError(
                "provider evidence identity/source/time must be all present or all absent"
            )

        requested_odds = _decimal(self.requested_odds, "requested_odds")
        requested_stake = _decimal(self.requested_stake, "requested_stake")
        if requested_odds <= 0 or requested_stake <= 0:
            raise EmpiricalExecutionEvidenceError("requested odds/stake must be > 0")

        slippage_values = (
            self.accepted_odds,
            self.accepted_stake,
            self.accepted_minus_requested_odds,
            self.adverse_odds_delta,
            self.unaccepted_stake,
        )
        if state in {AttemptState.ACCEPTED, AttemptState.PARTIAL}:
            if self.slippage_status == SLIPPAGE_STATUS_KNOWN:
                raise EmpiricalExecutionEvidenceError(
                    "KNOWN slippage requires typed accepted-price provider evidence"
                )
            if self.slippage_status != SLIPPAGE_STATUS_UNKNOWN:
                raise EmpiricalExecutionEvidenceError(
                    "accepted/partial attempt requires UNKNOWN slippage status"
                )
            if any(value is not None for value in slippage_values):
                raise EmpiricalExecutionEvidenceError(
                    "UNKNOWN slippage cannot claim accepted-price metrics"
                )
        elif state is AttemptState.REJECTED:
            if self.slippage_status != SLIPPAGE_STATUS_NOT_APPLICABLE:
                raise EmpiricalExecutionEvidenceError(
                    "REJECTED attempt requires NOT_APPLICABLE slippage status"
                )
            if any(value is not None for value in slippage_values):
                raise EmpiricalExecutionEvidenceError(
                    "rejected evidence cannot claim accepted/slippage metrics"
                )
        else:
            if self.slippage_status != SLIPPAGE_STATUS_UNKNOWN:
                raise EmpiricalExecutionEvidenceError(
                    "unresolved/not-found attempt requires UNKNOWN slippage status"
                )
            if any(value is not None for value in slippage_values):
                raise EmpiricalExecutionEvidenceError(
                    "unresolved/not-found attempt cannot claim accepted/slippage metrics"
                )

        timing_values = (
            self.quote_age_at_decision_us,
            self.decision_to_reserve_us,
            self.decision_to_submit_us,
            self.submit_to_provider_evidence_us,
            self.submit_to_acknowledgement_us,
            self.decision_to_acknowledgement_us,
        )
        if self.causal_timing_status != TIMING_STATUS_UNKNOWN:
            raise EmpiricalExecutionEvidenceError(
                "ledger-backed causal timing cannot be marked KNOWN without monotonic witness"
            )
        if self.causal_timing_reason != TIMING_REASON_NO_MONOTONIC_WITNESS:
            raise EmpiricalExecutionEvidenceError(
                "causal timing reason must identify missing monotonic witness"
            )
        if any(value is not None for value in timing_values):
            raise EmpiricalExecutionEvidenceError(
                "wall-clock timestamps cannot mint causal latency values"
            )

        if _issuance_token is not _EMPIRICAL_EVIDENCE_ISSUANCE_TOKEN:
            raise EmpiricalExecutionEvidenceError(
                "empirical execution evidence must be issued by canonical ledger projection"
            )

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
            "attempt_state": self.attempt_state,
            "terminal": self.terminal,
            "bookmaker_id": self.bookmaker_id,
            "account_id": self.account_id,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "side": self.side,
            "quote_id": self.quote_id,
            "decision_at": self.decision_at,
            "quote_observed_at": self.quote_observed_at,
            "reserved_at": self.reserved_at,
            "submitted_at": self.submitted_at,
            "provider_evidence_observed_at": self.provider_evidence_observed_at,
            "acknowledged_at": self.acknowledged_at,
            "external_receipt_id": self.external_receipt_id,
            "provider_evidence_id": self.provider_evidence_id,
            "provider_evidence_source": self.provider_evidence_source,
            "acknowledgement_status": self.acknowledgement_status,
            "reconciliation_evidence_id": self.reconciliation_evidence_id,
            "reconciliation_evidence_source": self.reconciliation_evidence_source,
            "reconciliation_evidence_observed_at": self.reconciliation_evidence_observed_at,
            "reconciliation_external_effect_found": self.reconciliation_external_effect_found,
            "requested_odds": _decimal_text(self.requested_odds),
            "requested_stake": _decimal_text(self.requested_stake),
            "slippage_status": self.slippage_status,
            "accepted_odds": _decimal_text(self.accepted_odds),
            "accepted_stake": _decimal_text(self.accepted_stake),
            "accepted_minus_requested_odds": _decimal_text(
                self.accepted_minus_requested_odds
            ),
            "adverse_odds_delta": _decimal_text(self.adverse_odds_delta),
            "unaccepted_stake": _decimal_text(self.unaccepted_stake),
            "causal_timing_status": self.causal_timing_status,
            "causal_timing_reason": self.causal_timing_reason,
            "quote_age_at_decision_us": self.quote_age_at_decision_us,
            "decision_to_reserve_us": self.decision_to_reserve_us,
            "decision_to_submit_us": self.decision_to_submit_us,
            "submit_to_provider_evidence_us": self.submit_to_provider_evidence_us,
            "submit_to_acknowledgement_us": self.submit_to_acknowledgement_us,
            "decision_to_acknowledgement_us": self.decision_to_acknowledgement_us,
            "right_censored": self.right_censored,
            "censor_reason": self.censor_reason,
            "censor_cutoff_recorded_at": self.censor_cutoff_recorded_at,
            "censor_cutoff_event_count": self.censor_cutoff_event_count,
        }
        if include_evidence_sha256:
            payload["evidence_sha256"] = self.evidence_sha256
        return payload


def build_empirical_execution_evidence(
    ledger: RealExecutionLedger,
    *,
    attempt_id: str,
) -> EmpiricalExecutionEvidence:
    """Project one unbiased attempt-level record from verified durable execution facts.

    The canonical ledger currently persists wall-clock correlation timestamps but no
    product-captured monotonic same-clock-domain samples. Therefore this projection
    must not derive causal latency numbers from timestamp subtraction: every causal
    timing metric remains explicitly UNKNOWN until a monotonic witness exists.

    Unresolved attempts are evidence too. RESERVED/SUBMITTED/UNKNOWN attempts are
    explicit right-censored observations. RECONCILED_NOT_FOUND is different: the
    canonical ledger has durably resolved that no external effect exists, so it is a
    terminal empirical outcome with reconciliation provenance and UNKNOWN slippage.
    This function remains read-only and grants no provider-write, retry, settlement,
    profitability, real-money, or readiness authority.
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
    if state is None:
        raise EmpiricalExecutionEvidenceUnavailable(
            "attempt has no durable lifecycle state"
        )

    reservation = _single_event(
        attempt_events,
        EventType.ATTEMPT_RESERVED,
        label="empirical evidence",
    )
    submission = _optional_single_event(
        attempt_events,
        EventType.ATTEMPT_SUBMITTED,
        label="empirical evidence",
    )
    acknowledgement_event = _optional_single_event(
        attempt_events,
        EventType.EXTERNAL_ACKNOWLEDGEMENT,
        label="empirical evidence",
    )
    reconciliation_event = _optional_single_event(
        attempt_events,
        EventType.RECONCILED_NOT_FOUND,
        label="empirical evidence",
    )
    provider_events = [
        event
        for event in attempt_events
        if event["event_type"] == EventType.PROVIDER_EVIDENCE_BOUND.value
    ]
    provider_event = provider_events[-1] if provider_events else None

    terminal = state in _TERMINAL_STATES
    if state in _ACK_TERMINAL_STATES:
        if acknowledgement_event is None:
            raise EmpiricalExecutionEvidenceUnavailable(
                "acknowledged terminal attempt lacks durable acknowledgement"
            )
        if reconciliation_event is not None:
            raise EmpiricalExecutionEvidenceUnavailable(
                "acknowledged terminal attempt conflicts with not-found reconciliation"
            )
    elif state is AttemptState.RECONCILED_NOT_FOUND:
        if acknowledgement_event is not None:
            raise EmpiricalExecutionEvidenceUnavailable(
                "RECONCILED_NOT_FOUND conflicts with durable acknowledgement"
            )
        if reconciliation_event is None:
            raise EmpiricalExecutionEvidenceUnavailable(
                "RECONCILED_NOT_FOUND lacks durable reconciliation"
            )
    else:
        if acknowledgement_event is not None:
            raise EmpiricalExecutionEvidenceUnavailable(
                "nonterminal state conflicts with durable acknowledgement"
            )
        if reconciliation_event is not None:
            raise EmpiricalExecutionEvidenceUnavailable(
                "nonterminal state conflicts with durable not-found reconciliation"
            )

    plan_id = _text(reservation["plan_id"], "plan_id")
    action_id = _text(reservation["action_id"], "action_id")
    plan_event, action = RealExecutionLedger._action_payload(events, plan_id, action_id)
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
    submitted_at = (
        _text(submission["payload"].get("submitted_at"), "submitted_at")
        if submission is not None
        else None
    )

    provider_evidence_id: str | None = None
    provider_evidence_source: str | None = None
    provider_evidence_observed_at: str | None = None
    if provider_event is not None:
        provider_payload = provider_event["payload"]
        provider_evidence_id = _sha256(
            provider_payload.get("evidence_id"), "provider_evidence_id"
        )
        provider_evidence_source = _text(
            provider_payload.get("source"), "provider_evidence_source"
        )
        provider_evidence_observed_at = _text(
            provider_payload.get("observed_at"),
            "provider_evidence_observed_at",
        )

    acknowledgement = (
        RealExecutionLedger._acknowledgement_from_dict(
            acknowledgement_event["payload"]
        )
        if acknowledgement_event is not None
        else None
    )

    reconciliation = (
        RealExecutionLedger._reconciliation_snapshot_from_dict(
            reconciliation_event["payload"]
        )
        if reconciliation_event is not None
        else None
    )
    if reconciliation is not None:
        if reconciliation.attempt_id != attempt:
            raise EmpiricalExecutionEvidenceUnavailable(
                "reconciliation attempt identity mismatch"
            )
        if reconciliation.external_effect_found is not False:
            raise EmpiricalExecutionEvidenceUnavailable(
                "RECONCILED_NOT_FOUND requires external_effect_found=false"
            )

    requested_odds = _decimal(action.get("requested_odds"), "requested_odds")
    requested_stake = _decimal(action.get("requested_stake"), "requested_stake")
    if requested_odds <= 0 or requested_stake <= 0:
        raise EmpiricalExecutionEvidenceUnavailable(
            "requested execution odds/stake must be positive"
        )

    slippage_status = SLIPPAGE_STATUS_UNKNOWN
    accepted_odds: Decimal | None = None
    accepted_stake: Decimal | None = None
    accepted_minus_requested: Decimal | None = None
    adverse_delta: Decimal | None = None
    unaccepted_stake: Decimal | None = None

    if state in {AttemptState.ACCEPTED, AttemptState.PARTIAL}:
        if acknowledgement is None:
            raise EmpiricalExecutionEvidenceUnavailable(
                "accepted/partial state lacks canonical acknowledgement"
            )
        if acknowledgement.accepted_odds is None or acknowledgement.accepted_stake is None:
            raise EmpiricalExecutionEvidenceUnavailable(
                "accepted acknowledgement lacks accepted odds/stake"
            )
        # PROVIDER_EVIDENCE_BOUND is generic correlation metadata. Its durable
        # schema does not bind an external receipt or the accepted odds/stake,
        # so it cannot promote acknowledgement prices into KNOWN slippage truth.
    elif state is AttemptState.REJECTED:
        slippage_status = SLIPPAGE_STATUS_NOT_APPLICABLE

    if not events:
        raise EmpiricalExecutionEvidenceUnavailable(
            "verified source snapshot has no durable observation boundary"
        )

    right_censored = state in _CENSOR_REASON_BY_STATE
    censor_reason = _CENSOR_REASON_BY_STATE.get(state) if right_censored else None
    censor_cutoff_recorded_at = (
        _text(events[-1].get("recorded_at"), "censor_cutoff_recorded_at")
        if right_censored
        else None
    )
    censor_cutoff_event_count = snapshot.event_count if right_censored else None

    return EmpiricalExecutionEvidence(
        source_ledger_sha256=snapshot.sha256,
        source_event_count=snapshot.event_count,
        plan_id=plan_id,
        plan_fingerprint=plan_fingerprint,
        action_id=action_id,
        attempt_id=attempt,
        attempt_state=state.value,
        terminal=terminal,
        bookmaker_id=_text(action.get("bookmaker_id"), "bookmaker_id"),
        account_id=_text(action.get("account_id"), "account_id"),
        event_id=_text(action.get("event_id"), "event_id"),
        market_id=_text(action.get("market_id"), "market_id"),
        selection_id=_text(action.get("selection_id"), "selection_id"),
        side=_text(action.get("side"), "side"),
        quote_id=_text(action.get("quote_id"), "quote_id"),
        decision_at=decision_at,
        quote_observed_at=quote_observed_at,
        reserved_at=reserved_at,
        submitted_at=submitted_at,
        provider_evidence_observed_at=provider_evidence_observed_at,
        acknowledged_at=(
            acknowledgement.acknowledged_at if acknowledgement is not None else None
        ),
        external_receipt_id=(
            acknowledgement.external_receipt_id if acknowledgement is not None else None
        ),
        provider_evidence_id=provider_evidence_id,
        provider_evidence_source=provider_evidence_source,
        acknowledgement_status=(
            acknowledgement.status.value if acknowledgement is not None else None
        ),
        reconciliation_evidence_id=(
            reconciliation.evidence_id if reconciliation is not None else None
        ),
        reconciliation_evidence_source=(
            reconciliation.source if reconciliation is not None else None
        ),
        reconciliation_evidence_observed_at=(
            reconciliation.observed_at if reconciliation is not None else None
        ),
        reconciliation_external_effect_found=(
            reconciliation.external_effect_found if reconciliation is not None else None
        ),
        requested_odds=requested_odds,
        requested_stake=requested_stake,
        slippage_status=slippage_status,
        accepted_odds=accepted_odds,
        accepted_stake=accepted_stake,
        accepted_minus_requested_odds=accepted_minus_requested,
        adverse_odds_delta=adverse_delta,
        unaccepted_stake=unaccepted_stake,
        causal_timing_status=TIMING_STATUS_UNKNOWN,
        causal_timing_reason=TIMING_REASON_NO_MONOTONIC_WITNESS,
        quote_age_at_decision_us=None,
        decision_to_reserve_us=None,
        decision_to_submit_us=None,
        submit_to_provider_evidence_us=None,
        submit_to_acknowledgement_us=None,
        decision_to_acknowledgement_us=None,
        right_censored=right_censored,
        censor_reason=censor_reason,
        censor_cutoff_recorded_at=censor_cutoff_recorded_at,
        censor_cutoff_event_count=censor_cutoff_event_count,
        _issuance_token=_EMPIRICAL_EVIDENCE_ISSUANCE_TOKEN,
    )
