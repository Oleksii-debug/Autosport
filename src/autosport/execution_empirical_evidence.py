from __future__ import annotations

import hashlib
import json
from dataclasses import InitVar, dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from weakref import ReferenceType, ref

from .real_execution_ledger import (
    AttemptState,
    EventType,
    ExecutionLedgerIntegrityError,
    RealExecutionLedger,
)

SCHEMA_VERSION = 4

SOURCE_ROOT_AUTHORITY_UNQUALIFIED = "UNQUALIFIED_CALLER_SELECTED_LEDGER"
EVALUATION_PROTOCOL_AUTHORITY_UNQUALIFIED = "UNQUALIFIED_CALLER_PROTOCOL_DIGEST"

TIMING_STATUS_UNKNOWN = "UNKNOWN"
TIMING_REASON_NO_MONOTONIC_WITNESS = "NO_MONOTONIC_WITNESS"

SLIPPAGE_STATUS_KNOWN = "KNOWN"
SLIPPAGE_STATUS_UNKNOWN = "UNKNOWN"
SLIPPAGE_STATUS_NOT_APPLICABLE = "NOT_APPLICABLE"

PROVIDER_OUTCOME_UNVERIFIED_ACK = "UNVERIFIED_ACK_PROVENANCE"
PROVIDER_OUTCOME_UNVERIFIED_ABSENCE = "UNVERIFIED_ABSENCE_AUTHORITY"
PROVIDER_OUTCOME_NOT_APPLICABLE = "NOT_APPLICABLE_UNRESOLVED"

_EMPIRICAL_EVIDENCE_ISSUANCE_TOKEN = object()

_ACK_TERMINAL_STATES = frozenset(
    {
        AttemptState.ACCEPTED,
        AttemptState.PARTIAL,
        AttemptState.REJECTED,
    }
)
_TERMINAL_STATES = _ACK_TERMINAL_STATES

_CENSOR_REASON_BY_STATE = {
    AttemptState.RESERVED: "RESERVED_NOT_SUBMITTED",
    AttemptState.SUBMITTED: "SUBMITTED_NO_TERMINAL_ACK",
    AttemptState.UNKNOWN: "UNKNOWN_EXTERNAL_EFFECT",
    AttemptState.RECONCILED_NOT_FOUND: "RECONCILED_NOT_FOUND_UNVERIFIED_ABSENCE_AUTHORITY",
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


@dataclass(frozen=True, slots=True, weakref_slot=True)
class EmpiricalExecutionEvidence:
    source_ledger_sha256: str
    source_event_count: int
    source_product_authority_verified: bool
    source_root_authority_status: str

    plan_id: str
    plan_fingerprint: str
    action_id: str
    attempt_id: str
    attempt_state: str
    ledger_terminal: bool
    provider_outcome_verified: bool
    provider_outcome_verification_reason: str

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
    _evidence_sha256: str = field(init=False, repr=False)
    _issuance_token: InitVar[object | None] = None

    def __post_init__(self, _issuance_token: object | None) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise EmpiricalExecutionEvidenceError("unsupported empirical evidence schema")
        if type(self.source_product_authority_verified) is not bool:
            raise EmpiricalExecutionEvidenceError(
                "source_product_authority_verified must be bool"
            )
        _text(self.source_root_authority_status, "source_root_authority_status")
        if (
            self.source_product_authority_verified is not False
            or self.source_root_authority_status
            != SOURCE_ROOT_AUTHORITY_UNQUALIFIED
        ):
            raise EmpiricalExecutionEvidenceError(
                "current empirical projection does not verify product-owned "
                "ledger/workspace authority"
            )

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
            "provider_outcome_verification_reason",
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
        if (
            type(self.ledger_terminal) is not bool
            or type(self.right_censored) is not bool
            or type(self.provider_outcome_verified) is not bool
        ):
            raise EmpiricalExecutionEvidenceError(
                "terminal/censor/provider-outcome flags must be bool"
            )
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
        if self.ledger_terminal is not expected_terminal:
            raise EmpiricalExecutionEvidenceError(
                "ledger_terminal flag mismatches attempt_state"
            )

        if state in _ACK_TERMINAL_STATES:
            expected_provider_outcome_reason = PROVIDER_OUTCOME_UNVERIFIED_ACK
        elif state is AttemptState.RECONCILED_NOT_FOUND:
            expected_provider_outcome_reason = PROVIDER_OUTCOME_UNVERIFIED_ABSENCE
        else:
            expected_provider_outcome_reason = PROVIDER_OUTCOME_NOT_APPLICABLE
        if self.provider_outcome_verified:
            raise EmpiricalExecutionEvidenceError(
                "current ledger projection cannot verify provider outcome provenance"
            )
        if (
            self.provider_outcome_verification_reason
            != expected_provider_outcome_reason
        ):
            raise EmpiricalExecutionEvidenceError(
                "provider outcome verification reason mismatches durable attempt state"
            )

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
            if state is AttemptState.RECONCILED_NOT_FOUND:
                if not reconciliation_present:
                    raise EmpiricalExecutionEvidenceError(
                        "unverified RECONCILED_NOT_FOUND requires durable reconciliation correlation"
                    )
                if self.reconciliation_external_effect_found is not False:
                    raise EmpiricalExecutionEvidenceError(
                        "unverified RECONCILED_NOT_FOUND requires external_effect_found=false"
                    )
            elif reconciliation_present:
                raise EmpiricalExecutionEvidenceError(
                    "nonterminal attempt cannot claim reconciliation"
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
            "_evidence_sha256",
            _digest(self.to_dict(include_evidence_sha256=False)),
        )

    def assert_projection_issued(self) -> None:
        """Verify canonical projection integrity, not product-root provenance."""

        issued = _ISSUED_EMPIRICAL_EVIDENCE.get(id(self))
        try:
            current_fingerprint = _digest(
                self.to_dict(include_evidence_sha256=False)
            )
        except Exception as exc:
            raise EmpiricalExecutionEvidenceError(
                "empirical execution evidence is no longer canonical"
            ) from exc
        if (
            issued is None
            or issued[0]() is not self
            or issued[1] != current_fingerprint
            or self._evidence_sha256 != current_fingerprint
        ):
            raise EmpiricalExecutionEvidenceError(
                "empirical execution evidence was not issued by canonical ledger projection"
            )

    @property
    def evidence_sha256(self) -> str:
        self.assert_projection_issued()
        return self._evidence_sha256

    def to_dict(self, *, include_evidence_sha256: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": "autosport.empirical_execution_evidence",
            "schema_version": self.schema_version,
            "source_ledger_sha256": self.source_ledger_sha256,
            "source_event_count": self.source_event_count,
            "source_product_authority_verified": self.source_product_authority_verified,
            "source_root_authority_status": self.source_root_authority_status,
            "plan_id": self.plan_id,
            "plan_fingerprint": self.plan_fingerprint,
            "action_id": self.action_id,
            "attempt_id": self.attempt_id,
            "attempt_state": self.attempt_state,
            "ledger_terminal": self.ledger_terminal,
            "provider_outcome_verified": self.provider_outcome_verified,
            "provider_outcome_verification_reason": (
                self.provider_outcome_verification_reason
            ),
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


_ISSUED_EMPIRICAL_EVIDENCE: dict[
    int, tuple[ReferenceType[EmpiricalExecutionEvidence], str]
] = {}


def _register_issued_empirical_evidence(
    evidence: EmpiricalExecutionEvidence,
) -> None:
    identity = id(evidence)

    def _discard(reference: ReferenceType[EmpiricalExecutionEvidence]) -> None:
        current = _ISSUED_EMPIRICAL_EVIDENCE.get(identity)
        if current is not None and current[0] is reference:
            _ISSUED_EMPIRICAL_EVIDENCE.pop(identity, None)

    reference = ref(evidence, _discard)
    _ISSUED_EMPIRICAL_EVIDENCE[identity] = (
        reference,
        evidence._evidence_sha256,
    )


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
    explicit right-censored observations. Legacy RECONCILED_NOT_FOUND is also
    right-censored here: the durable event remains correlation evidence, but current
    ledger semantics do not prove that provider absence came from a product-issued
    complete readback authority. A future typed upstream authority may qualify that
    state without rewriting this legacy evidence. This function remains read-only
    and grants no provider-write, retry, settlement, profitability, real-money, or
    readiness authority. Builder issuance proves projection integrity only: because
    the ledger object is caller-supplied, this API also records that product-owned
    workspace/ledger-root provenance is currently unverified.
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

    ledger_terminal = state in _TERMINAL_STATES
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

    if state in _ACK_TERMINAL_STATES:
        provider_outcome_verification_reason = PROVIDER_OUTCOME_UNVERIFIED_ACK
    elif state is AttemptState.RECONCILED_NOT_FOUND:
        provider_outcome_verification_reason = PROVIDER_OUTCOME_UNVERIFIED_ABSENCE
    else:
        provider_outcome_verification_reason = PROVIDER_OUTCOME_NOT_APPLICABLE

    evidence = EmpiricalExecutionEvidence(
        source_ledger_sha256=snapshot.sha256,
        source_event_count=snapshot.event_count,
        source_product_authority_verified=False,
        source_root_authority_status=SOURCE_ROOT_AUTHORITY_UNQUALIFIED,
        plan_id=plan_id,
        plan_fingerprint=plan_fingerprint,
        action_id=action_id,
        attempt_id=attempt,
        attempt_state=state.value,
        ledger_terminal=ledger_terminal,
        provider_outcome_verified=False,
        provider_outcome_verification_reason=provider_outcome_verification_reason,
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
    _register_issued_empirical_evidence(evidence)
    return evidence


POPULATION_SCHEMA_VERSION = 4


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class EmpiricalExecutionPopulationEvidence:
    """Frozen whole-ledger execution-quality denominator.

    This aggregate is deliberately built from every durable attempt in one verified
    RealExecutionLedger snapshot. It accepts no caller-selected sample list in its
    public builder, so rejected, partial, unresolved and reconciled-not-found
    attempts cannot disappear merely because a price or timing scalar is missing.

    The evaluation protocol SHA is a binding label only. This class does not claim
    that the protocol itself was product-issued or scientifically qualified.
    """

    source_ledger_sha256: str
    source_event_count: int
    source_product_authority_verified: bool
    source_root_authority_status: str
    evaluation_protocol_sha256: str
    evaluation_protocol_authority_verified: bool
    evaluation_protocol_authority_status: str
    samples: tuple[EmpiricalExecutionEvidence, ...]
    schema_version: int = POPULATION_SCHEMA_VERSION

    total_attempts: int = field(init=False)
    state_counts: tuple[tuple[str, int], ...] = field(init=False)
    ledger_terminal_count: int = field(init=False)
    provider_verified_terminal_count: int = field(init=False)
    unverified_ledger_terminal_count: int = field(init=False)
    right_censored_count: int = field(init=False)
    provider_evidence_count: int = field(init=False)
    provider_outcome_unverified_ack_count: int = field(init=False)
    provider_outcome_unverified_absence_count: int = field(init=False)
    provider_outcome_not_applicable_count: int = field(init=False)
    slippage_known_count: int = field(init=False)
    slippage_unknown_count: int = field(init=False)
    slippage_not_applicable_count: int = field(init=False)
    causal_timing_known_count: int = field(init=False)
    causal_timing_unknown_count: int = field(init=False)
    denominator_sha256: str = field(init=False)
    _evidence_sha256: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != POPULATION_SCHEMA_VERSION
        ):
            raise EmpiricalExecutionEvidenceError(
                "unsupported empirical population evidence schema"
            )
        _sha256(self.source_ledger_sha256, "source_ledger_sha256")
        if type(self.source_product_authority_verified) is not bool:
            raise EmpiricalExecutionEvidenceError(
                "source_product_authority_verified must be bool"
            )
        _text(self.source_root_authority_status, "source_root_authority_status")
        if (
            self.source_product_authority_verified is not False
            or self.source_root_authority_status
            != SOURCE_ROOT_AUTHORITY_UNQUALIFIED
        ):
            raise EmpiricalExecutionEvidenceError(
                "current empirical population does not verify product-owned "
                "ledger/workspace authority"
            )
        _sha256(self.evaluation_protocol_sha256, "evaluation_protocol_sha256")
        if type(self.evaluation_protocol_authority_verified) is not bool:
            raise EmpiricalExecutionEvidenceError(
                "evaluation_protocol_authority_verified must be bool"
            )
        _text(
            self.evaluation_protocol_authority_status,
            "evaluation_protocol_authority_status",
        )
        if (
            self.evaluation_protocol_authority_verified is not False
            or self.evaluation_protocol_authority_status
            != EVALUATION_PROTOCOL_AUTHORITY_UNQUALIFIED
        ):
            raise EmpiricalExecutionEvidenceError(
                "current empirical population does not verify product/scientific "
                "evaluation protocol authority"
            )
        if type(self.source_event_count) is not int or self.source_event_count < 1:
            raise EmpiricalExecutionEvidenceError(
                "source_event_count must be positive int"
            )
        if type(self.samples) is not tuple or not self.samples:
            raise EmpiricalExecutionEvidenceError(
                "population evidence requires non-empty canonical sample tuple"
            )
        if any(type(sample) is not EmpiricalExecutionEvidence for sample in self.samples):
            raise EmpiricalExecutionEvidenceError(
                "population samples must be canonical EmpiricalExecutionEvidence"
            )

        attempt_ids = tuple(sample.attempt_id for sample in self.samples)
        if attempt_ids != tuple(sorted(attempt_ids)):
            raise EmpiricalExecutionEvidenceError(
                "population samples must be ordered by canonical attempt_id"
            )
        if len(set(attempt_ids)) != len(attempt_ids):
            raise EmpiricalExecutionEvidenceError(
                "population evidence cannot duplicate an attempt"
            )
        for sample in self.samples:
            if sample.source_ledger_sha256 != self.source_ledger_sha256:
                raise EmpiricalExecutionEvidenceError(
                    "population sample source ledger mismatch"
                )
            if sample.source_event_count != self.source_event_count:
                raise EmpiricalExecutionEvidenceError(
                    "population sample source event-count mismatch"
                )
            if (
                sample.source_product_authority_verified
                is not self.source_product_authority_verified
                or sample.source_root_authority_status
                != self.source_root_authority_status
            ):
                raise EmpiricalExecutionEvidenceError(
                    "population sample source-root authority mismatch"
                )

        total = len(self.samples)
        counts = tuple(
            (
                state.value,
                sum(sample.attempt_state == state.value for sample in self.samples),
            )
            for state in AttemptState
        )
        if sum(count for _, count in counts) != total:
            raise EmpiricalExecutionEvidenceError(
                "population state counts do not cover denominator"
            )

        ledger_terminal_count = sum(
            sample.ledger_terminal for sample in self.samples
        )
        provider_verified_terminal_count = sum(
            sample.ledger_terminal and sample.provider_outcome_verified
            for sample in self.samples
        )
        unverified_ledger_terminal_count = (
            ledger_terminal_count - provider_verified_terminal_count
        )
        right_censored_count = sum(sample.right_censored for sample in self.samples)
        if ledger_terminal_count + right_censored_count != total:
            raise EmpiricalExecutionEvidenceError(
                "population terminal/censor counts do not cover denominator"
            )

        provider_evidence_count = sum(
            sample.provider_evidence_id is not None for sample in self.samples
        )
        provider_outcome_unverified_ack_count = sum(
            sample.provider_outcome_verification_reason
            == PROVIDER_OUTCOME_UNVERIFIED_ACK
            for sample in self.samples
        )
        provider_outcome_unverified_absence_count = sum(
            sample.provider_outcome_verification_reason
            == PROVIDER_OUTCOME_UNVERIFIED_ABSENCE
            for sample in self.samples
        )
        provider_outcome_not_applicable_count = sum(
            sample.provider_outcome_verification_reason
            == PROVIDER_OUTCOME_NOT_APPLICABLE
            for sample in self.samples
        )
        if (
            provider_outcome_unverified_ack_count
            + provider_outcome_unverified_absence_count
            + provider_outcome_not_applicable_count
            != total
        ):
            raise EmpiricalExecutionEvidenceError(
                "population provider-outcome qualification does not cover denominator"
            )
        slippage_known_count = sum(
            sample.slippage_status == SLIPPAGE_STATUS_KNOWN for sample in self.samples
        )
        slippage_unknown_count = sum(
            sample.slippage_status == SLIPPAGE_STATUS_UNKNOWN for sample in self.samples
        )
        slippage_not_applicable_count = sum(
            sample.slippage_status == SLIPPAGE_STATUS_NOT_APPLICABLE
            for sample in self.samples
        )
        if (
            slippage_known_count
            + slippage_unknown_count
            + slippage_not_applicable_count
            != total
        ):
            raise EmpiricalExecutionEvidenceError(
                "population slippage statuses do not cover denominator"
            )

        causal_timing_known_count = sum(
            sample.causal_timing_status != TIMING_STATUS_UNKNOWN
            for sample in self.samples
        )
        causal_timing_unknown_count = sum(
            sample.causal_timing_status == TIMING_STATUS_UNKNOWN
            for sample in self.samples
        )
        if causal_timing_known_count + causal_timing_unknown_count != total:
            raise EmpiricalExecutionEvidenceError(
                "population timing statuses do not cover denominator"
            )

        object.__setattr__(self, "total_attempts", total)
        object.__setattr__(self, "state_counts", counts)
        object.__setattr__(
            self,
            "ledger_terminal_count",
            ledger_terminal_count,
        )
        object.__setattr__(
            self,
            "provider_verified_terminal_count",
            provider_verified_terminal_count,
        )
        object.__setattr__(
            self,
            "unverified_ledger_terminal_count",
            unverified_ledger_terminal_count,
        )
        object.__setattr__(self, "right_censored_count", right_censored_count)
        object.__setattr__(self, "provider_evidence_count", provider_evidence_count)
        object.__setattr__(
            self,
            "provider_outcome_unverified_ack_count",
            provider_outcome_unverified_ack_count,
        )
        object.__setattr__(
            self,
            "provider_outcome_unverified_absence_count",
            provider_outcome_unverified_absence_count,
        )
        object.__setattr__(
            self,
            "provider_outcome_not_applicable_count",
            provider_outcome_not_applicable_count,
        )
        object.__setattr__(self, "slippage_known_count", slippage_known_count)
        object.__setattr__(self, "slippage_unknown_count", slippage_unknown_count)
        object.__setattr__(
            self,
            "slippage_not_applicable_count",
            slippage_not_applicable_count,
        )
        object.__setattr__(
            self,
            "causal_timing_known_count",
            causal_timing_known_count,
        )
        object.__setattr__(
            self,
            "causal_timing_unknown_count",
            causal_timing_unknown_count,
        )

        denominator_payload = {
            "source_ledger_sha256": self.source_ledger_sha256,
            "source_event_count": self.source_event_count,
            "attempt_ids": list(attempt_ids),
            "sample_evidence_sha256s": [
                sample.evidence_sha256 for sample in self.samples
            ],
        }
        object.__setattr__(
            self,
            "denominator_sha256",
            _digest(denominator_payload),
        )
        object.__setattr__(
            self,
            "_evidence_sha256",
            _digest(self.to_dict(include_evidence_sha256=False)),
        )

    def assert_projection_issued(self) -> None:
        issued = _ISSUED_EMPIRICAL_POPULATIONS.get(id(self))
        try:
            current_fingerprint = _digest(
                self.to_dict(include_evidence_sha256=False)
            )
        except Exception as exc:
            raise EmpiricalExecutionEvidenceError(
                "empirical execution population is no longer canonical"
            ) from exc
        if (
            issued is None
            or issued[0]() is not self
            or issued[1] != current_fingerprint
            or self._evidence_sha256 != current_fingerprint
        ):
            raise EmpiricalExecutionEvidenceError(
                "empirical execution population was not issued by whole-ledger projection"
            )

    @property
    def evidence_sha256(self) -> str:
        self.assert_projection_issued()
        return self._evidence_sha256

    @staticmethod
    def _rate(count: int, denominator: int) -> dict[str, int]:
        return {"numerator": count, "denominator": denominator}

    def to_dict(self, *, include_evidence_sha256: bool = True) -> dict[str, Any]:
        state_counts = dict(self.state_counts)
        payload: dict[str, Any] = {
            "schema": "autosport.empirical_execution_population_evidence",
            "schema_version": self.schema_version,
            "source_ledger_sha256": self.source_ledger_sha256,
            "source_event_count": self.source_event_count,
            "source_product_authority_verified": self.source_product_authority_verified,
            "source_root_authority_status": self.source_root_authority_status,
            "evaluation_protocol_sha256": self.evaluation_protocol_sha256,
            "evaluation_protocol_authority_verified": (
                self.evaluation_protocol_authority_verified
            ),
            "evaluation_protocol_authority_status": (
                self.evaluation_protocol_authority_status
            ),
            "denominator_sha256": self.denominator_sha256,
            "attempt_ids": [sample.attempt_id for sample in self.samples],
            "sample_evidence_sha256s": [
                sample.evidence_sha256 for sample in self.samples
            ],
            "total_attempts": self.total_attempts,
            "state_counts": state_counts,
            "state_rates": {
                state: self._rate(count, self.total_attempts)
                for state, count in self.state_counts
            },
            "ledger_terminal_count": self.ledger_terminal_count,
            "ledger_terminal_rate": self._rate(
                self.ledger_terminal_count,
                self.total_attempts,
            ),
            "provider_verified_terminal_count": (
                self.provider_verified_terminal_count
            ),
            "provider_verified_terminal_rate": self._rate(
                self.provider_verified_terminal_count,
                self.total_attempts,
            ),
            "unverified_ledger_terminal_count": (
                self.unverified_ledger_terminal_count
            ),
            "unverified_ledger_terminal_rate": self._rate(
                self.unverified_ledger_terminal_count,
                self.total_attempts,
            ),
            "right_censored_count": self.right_censored_count,
            "right_censored_rate": self._rate(
                self.right_censored_count,
                self.total_attempts,
            ),
            "provider_evidence_count": self.provider_evidence_count,
            "provider_evidence_rate": self._rate(
                self.provider_evidence_count,
                self.total_attempts,
            ),
            "provider_outcome_verification_counts": {
                PROVIDER_OUTCOME_UNVERIFIED_ACK: (
                    self.provider_outcome_unverified_ack_count
                ),
                PROVIDER_OUTCOME_UNVERIFIED_ABSENCE: (
                    self.provider_outcome_unverified_absence_count
                ),
                PROVIDER_OUTCOME_NOT_APPLICABLE: (
                    self.provider_outcome_not_applicable_count
                ),
            },
            "slippage_status_counts": {
                SLIPPAGE_STATUS_KNOWN: self.slippage_known_count,
                SLIPPAGE_STATUS_UNKNOWN: self.slippage_unknown_count,
                SLIPPAGE_STATUS_NOT_APPLICABLE: self.slippage_not_applicable_count,
            },
            "causal_timing_status_counts": {
                "KNOWN": self.causal_timing_known_count,
                TIMING_STATUS_UNKNOWN: self.causal_timing_unknown_count,
            },
        }
        if include_evidence_sha256:
            payload["evidence_sha256"] = self.evidence_sha256
        return payload


_ISSUED_EMPIRICAL_POPULATIONS: dict[
    int, tuple[ReferenceType[EmpiricalExecutionPopulationEvidence], str]
] = {}


def _register_issued_empirical_population(
    evidence: EmpiricalExecutionPopulationEvidence,
) -> None:
    identity = id(evidence)

    def _discard(
        reference: ReferenceType[EmpiricalExecutionPopulationEvidence],
    ) -> None:
        current = _ISSUED_EMPIRICAL_POPULATIONS.get(identity)
        if current is not None and current[0] is reference:
            _ISSUED_EMPIRICAL_POPULATIONS.pop(identity, None)

    reference = ref(evidence, _discard)
    _ISSUED_EMPIRICAL_POPULATIONS[identity] = (
        reference,
        evidence._evidence_sha256,
    )


def _issue_empirical_execution_population_evidence(
    *,
    source_ledger_sha256: str,
    source_event_count: int,
    evaluation_protocol_sha256: str,
    samples: tuple[EmpiricalExecutionEvidence, ...],
) -> EmpiricalExecutionPopulationEvidence:
    evidence = object.__new__(EmpiricalExecutionPopulationEvidence)
    object.__setattr__(evidence, "source_ledger_sha256", source_ledger_sha256)
    object.__setattr__(evidence, "source_event_count", source_event_count)
    object.__setattr__(evidence, "source_product_authority_verified", False)
    object.__setattr__(
        evidence,
        "source_root_authority_status",
        SOURCE_ROOT_AUTHORITY_UNQUALIFIED,
    )
    object.__setattr__(
        evidence,
        "evaluation_protocol_sha256",
        evaluation_protocol_sha256,
    )
    object.__setattr__(
        evidence,
        "evaluation_protocol_authority_verified",
        False,
    )
    object.__setattr__(
        evidence,
        "evaluation_protocol_authority_status",
        EVALUATION_PROTOCOL_AUTHORITY_UNQUALIFIED,
    )
    object.__setattr__(evidence, "samples", samples)
    object.__setattr__(evidence, "schema_version", POPULATION_SCHEMA_VERSION)
    evidence.__post_init__()
    _register_issued_empirical_population(evidence)
    return evidence


def build_empirical_execution_population_evidence(
    ledger: RealExecutionLedger,
    *,
    evaluation_protocol_sha256: str,
) -> EmpiricalExecutionPopulationEvidence:
    """Project a frozen complete attempt denominator from one ledger snapshot.

    The function deliberately takes no caller-provided sample/attempt subset.
    Every ATTEMPT_RESERVED identity in the verified snapshot is projected. A
    concurrent ledger mutation fails closed rather than silently mixing snapshots.
    The aggregate is still explicitly unqualified as product-owned root provenance
    until a separate durable workspace authority is composed on this lineage.
    The supplied evaluation protocol digest is likewise a binding label only and
    carries explicit negative product/scientific authority until independently
    qualified protocol evidence is composed.
    """
    if type(ledger) is not RealExecutionLedger:
        raise TypeError("ledger must be canonical RealExecutionLedger")
    protocol_sha256 = _sha256(
        evaluation_protocol_sha256,
        "evaluation_protocol_sha256",
    )

    initial_snapshot = ledger.verified_snapshot()
    try:
        events = RealExecutionLedger._parse(initial_snapshot.payload)
    except ExecutionLedgerIntegrityError:
        raise

    attempt_ids: list[str] = []
    seen: set[str] = set()
    for event in events:
        if event.get("event_type") != EventType.ATTEMPT_RESERVED.value:
            continue
        attempt_id = _text(event.get("attempt_id"), "attempt_id")
        if attempt_id in seen:
            raise EmpiricalExecutionEvidenceUnavailable(
                "verified snapshot contains duplicate attempt reservation"
            )
        seen.add(attempt_id)
        attempt_ids.append(attempt_id)

    if not attempt_ids:
        raise EmpiricalExecutionEvidenceUnavailable(
            "verified snapshot contains no execution attempts"
        )

    samples = tuple(
        build_empirical_execution_evidence(ledger, attempt_id=attempt_id)
        for attempt_id in sorted(attempt_ids)
    )

    final_snapshot = ledger.verified_snapshot()
    if (
        final_snapshot.sha256 != initial_snapshot.sha256
        or final_snapshot.event_count != initial_snapshot.event_count
    ):
        raise EmpiricalExecutionEvidenceUnavailable(
            "ledger changed during population projection"
        )

    return _issue_empirical_execution_population_evidence(
        source_ledger_sha256=initial_snapshot.sha256,
        source_event_count=initial_snapshot.event_count,
        evaluation_protocol_sha256=protocol_sha256,
        samples=samples,
    )

