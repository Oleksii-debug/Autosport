from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .real_execution_ledger import (
    AcknowledgementStatus,
    EventType,
    RealExecutionLedger,
    VerifiedExecutionLedgerSnapshot,
)


# Positive settlement-basis provenance must traverse the exact durable-ledger
# implementation imported with this adapter.  RealExecutionLedger is intentionally
# a normal mutable Python object, so an exact-type check alone does not prevent a
# caller from shadowing verified_snapshot/_parse on one instance or rebinding the
# class methods used transitively by verified_snapshot().  Freeze the complete
# class surface once and reject runtime executable/data-descriptor replacement
# before reading any authority-bearing bytes.
_REAL_EXECUTION_LEDGER_CLASS_SURFACE = dict(RealExecutionLedger.__dict__)
_REAL_EXECUTION_LEDGER_VERIFIED_SNAPSHOT = (
    _REAL_EXECUTION_LEDGER_CLASS_SURFACE["verified_snapshot"]
)


class SettlementExecutionBasisError(RuntimeError):
    """Raised when durable execution truth cannot support a settlement fill basis."""


def _canonical_verified_snapshot(
    ledger: RealExecutionLedger,
) -> VerifiedExecutionLedgerSnapshot:
    """Read only through the captured, unshadowed durable-ledger authority."""

    if type(ledger) is not RealExecutionLedger:
        raise TypeError("ledger must be exact RealExecutionLedger")

    try:
        instance_state = vars(ledger)
    except TypeError as exc:  # pragma: no cover - exact class currently has __dict__
        raise SettlementExecutionBasisError(
            "execution ledger instance authority is unavailable"
        ) from exc
    if set(instance_state).intersection(_REAL_EXECUTION_LEDGER_CLASS_SURFACE):
        raise SettlementExecutionBasisError(
            "execution ledger instance read authority was rebound"
        )

    current_surface = RealExecutionLedger.__dict__
    if set(current_surface) != set(_REAL_EXECUTION_LEDGER_CLASS_SURFACE) or any(
        current_surface[name] is not original
        for name, original in _REAL_EXECUTION_LEDGER_CLASS_SURFACE.items()
    ):
        raise SettlementExecutionBasisError(
            "execution ledger executable read authority was rebound"
        )

    snapshot = _REAL_EXECUTION_LEDGER_VERIFIED_SNAPSHOT(ledger)
    if type(snapshot) is not VerifiedExecutionLedgerSnapshot:
        raise SettlementExecutionBasisError(
            "canonical execution ledger returned unexpected snapshot type"
        )
    if snapshot.sha256 != hashlib.sha256(snapshot.payload).hexdigest():
        raise SettlementExecutionBasisError(
            "canonical execution ledger snapshot digest mismatch"
        )
    if snapshot.event_count != len(snapshot.payload.splitlines()):
        raise SettlementExecutionBasisError(
            "canonical execution ledger snapshot event count mismatch"
        )
    return snapshot


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise SettlementExecutionBasisError(f"{field} must be non-empty trimmed text")
    return value


def _decimal(value: object, field: str) -> Decimal:
    if type(value) is not str or not value or value.strip() != value:
        raise SettlementExecutionBasisError(f"{field} must be canonical decimal text")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise SettlementExecutionBasisError(f"{field} must be finite decimal text") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise SettlementExecutionBasisError(f"{field} must be finite and > 0")
    return parsed


def _canonical_decimal(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _basis_id(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class SettlementExecutionBasis:
    """Immutable fill evidence derived from exact durable execution events."""

    plan_event_sha256: str
    reservation_event_sha256: str
    acknowledgement_event_sha256: str
    reconciliation_event_sha256: str | None
    plan_id: str
    action_id: str
    attempt_id: str
    bookmaker_id: str
    account_id: str
    event_id: str
    market_id: str
    selection_id: str
    side: str
    quote_id: str
    requested_odds: Decimal
    requested_stake: Decimal
    external_receipt_id: str
    acknowledgement_status: AcknowledgementStatus
    acknowledged_at: str
    accepted_odds: Decimal
    accepted_stake: Decimal
    reconciliation_evidence_id: str | None
    basis_id: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "autosport.settlement_execution_basis",
            "schema_version": 1,
            "plan_event_sha256": self.plan_event_sha256,
            "reservation_event_sha256": self.reservation_event_sha256,
            "acknowledgement_event_sha256": self.acknowledgement_event_sha256,
            "reconciliation_event_sha256": self.reconciliation_event_sha256,
            "plan_id": self.plan_id,
            "action_id": self.action_id,
            "attempt_id": self.attempt_id,
            "bookmaker_id": self.bookmaker_id,
            "account_id": self.account_id,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "side": self.side,
            "quote_id": self.quote_id,
            "requested_odds": _canonical_decimal(self.requested_odds),
            "requested_stake": _canonical_decimal(self.requested_stake),
            "external_receipt_id": self.external_receipt_id,
            "acknowledgement_status": self.acknowledgement_status.value,
            "acknowledged_at": self.acknowledged_at,
            "accepted_odds": _canonical_decimal(self.accepted_odds),
            "accepted_stake": _canonical_decimal(self.accepted_stake),
            "reconciliation_evidence_id": self.reconciliation_evidence_id,
            "provider_verified": False,
            "real_money_authorized": False,
            "settlement_outcome_authorized": False,
        }


def derive_settlement_execution_basis(
    ledger: RealExecutionLedger,
    *,
    attempt_id: str,
) -> SettlementExecutionBasis:
    """Project one terminal fill from the canonical verified execution ledger.

    The relevant event-envelope SHA-256 identities, rather than the mutable
    whole-ledger tail, anchor this projection so later unrelated appends cannot
    rewrite historical fill identity. This does not decide a sporting outcome,
    certify provider signatures, or authorize money movement.
    """

    if type(ledger) is not RealExecutionLedger:
        raise TypeError("ledger must be exact RealExecutionLedger")
    target_attempt = _text(attempt_id, "attempt_id")

    snapshot = _canonical_verified_snapshot(ledger)
    records: list[tuple[str, dict[str, object]]] = []
    for raw_line in snapshot.payload.splitlines():
        try:
            envelope = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:  # pragma: no cover
            raise SettlementExecutionBasisError(
                "verified execution snapshot could not be decoded"
            ) from exc
        if (
            type(envelope) is not dict
            or type(envelope.get("sha256")) is not str
            or type(envelope.get("event")) is not dict
        ):
            raise SettlementExecutionBasisError(
                "verified execution snapshot envelope is invalid"
            )
        records.append((envelope["sha256"], envelope["event"]))

    attempt_records = [
        (digest, event)
        for digest, event in records
        if event.get("attempt_id") == target_attempt
    ]
    reservations = [
        record
        for record in attempt_records
        if record[1].get("event_type") == EventType.ATTEMPT_RESERVED.value
    ]
    acknowledgements = [
        record
        for record in attempt_records
        if record[1].get("event_type")
        == EventType.EXTERNAL_ACKNOWLEDGEMENT.value
    ]
    if len(reservations) != 1:
        raise SettlementExecutionBasisError(
            "settlement basis requires one durable attempt reservation"
        )
    if len(acknowledgements) != 1:
        raise SettlementExecutionBasisError(
            "settlement basis requires one terminal acknowledgement"
        )

    reservation_sha256, reservation = reservations[0]
    acknowledgement_sha256, acknowledgement_event = acknowledgements[0]
    plan_id = _text(reservation.get("plan_id"), "plan_id")
    action_id = _text(reservation.get("action_id"), "action_id")
    if (
        acknowledgement_event.get("plan_id") != plan_id
        or acknowledgement_event.get("action_id") != action_id
    ):
        raise SettlementExecutionBasisError("acknowledgement execution identity drift")

    plan_records = [
        (digest, event)
        for digest, event in records
        if event.get("plan_id") == plan_id
        and event.get("event_type") == EventType.PLAN_RESERVED.value
    ]
    if len(plan_records) != 1:
        raise SettlementExecutionBasisError(
            "settlement basis requires one durable plan"
        )
    plan_sha256, plan_event = plan_records[0]
    try:
        actions = plan_event["payload"]["plan"]["actions"]
    except (KeyError, TypeError) as exc:
        raise SettlementExecutionBasisError(
            "durable execution plan payload is invalid"
        ) from exc
    if type(actions) is not list:
        raise SettlementExecutionBasisError(
            "durable execution plan actions are invalid"
        )
    matching_actions = [
        action
        for action in actions
        if type(action) is dict and action.get("action_id") == action_id
    ]
    if len(matching_actions) != 1:
        raise SettlementExecutionBasisError(
            "settlement basis action is not unique in durable plan"
        )
    action = matching_actions[0]

    payload = acknowledgement_event.get("payload")
    if type(payload) is not dict:
        raise SettlementExecutionBasisError(
            "durable acknowledgement payload is invalid"
        )
    try:
        status = AcknowledgementStatus(payload["status"])
    except (KeyError, ValueError) as exc:
        raise SettlementExecutionBasisError(
            "durable acknowledgement status is invalid"
        ) from exc
    if status is AcknowledgementStatus.PARTIAL:
        raise SettlementExecutionBasisError(
            "PARTIAL execution requires provider-finalized realization before settlement basis"
        )
    if status is not AcknowledgementStatus.ACCEPTED:
        raise SettlementExecutionBasisError(
            "rejected execution has no settlement fill basis"
        )

    accepted_odds = _decimal(payload.get("accepted_odds"), "accepted_odds")
    accepted_stake = _decimal(payload.get("accepted_stake"), "accepted_stake")
    requested_odds = _decimal(action.get("requested_odds"), "requested_odds")
    requested_stake = _decimal(action.get("requested_stake"), "requested_stake")
    if accepted_stake > requested_stake:
        raise SettlementExecutionBasisError(
            "accepted_stake exceeds durable requested_stake"
        )

    fields = {
        name: _text(action.get(name), name)
        for name in (
            "bookmaker_id",
            "account_id",
            "event_id",
            "market_id",
            "selection_id",
            "side",
            "quote_id",
        )
    }
    external_receipt_id = _text(
        payload.get("external_receipt_id"), "external_receipt_id"
    )
    acknowledged_at = _text(payload.get("acknowledged_at"), "acknowledged_at")
    reconciliation_evidence_id = payload.get("reconciliation_evidence_id")
    reconciliation_sha256: str | None = None
    if reconciliation_evidence_id is not None:
        reconciliation_evidence_id = _text(
            reconciliation_evidence_id, "reconciliation_evidence_id"
        )
        matches = [
            (digest, event)
            for digest, event in attempt_records
            if event.get("event_type") == EventType.RECONCILED_FOUND.value
            and type(event.get("payload")) is dict
            and event["payload"].get("evidence_id") == reconciliation_evidence_id
        ]
        if len(matches) != 1:
            raise SettlementExecutionBasisError(
                "reconciled acknowledgement lacks one exact evidence event"
            )
        reconciliation_sha256 = matches[0][0]

    identity_payload: dict[str, object] = {
        "schema": "autosport.settlement_execution_basis",
        "schema_version": 1,
        "plan_event_sha256": plan_sha256,
        "reservation_event_sha256": reservation_sha256,
        "acknowledgement_event_sha256": acknowledgement_sha256,
        "reconciliation_event_sha256": reconciliation_sha256,
        "plan_id": plan_id,
        "action_id": action_id,
        "attempt_id": target_attempt,
        **fields,
        "requested_odds": _canonical_decimal(requested_odds),
        "requested_stake": _canonical_decimal(requested_stake),
        "external_receipt_id": external_receipt_id,
        "acknowledgement_status": status.value,
        "acknowledged_at": acknowledged_at,
        "accepted_odds": _canonical_decimal(accepted_odds),
        "accepted_stake": _canonical_decimal(accepted_stake),
        "reconciliation_evidence_id": reconciliation_evidence_id,
        "provider_verified": False,
        "real_money_authorized": False,
        "settlement_outcome_authorized": False,
    }
    return SettlementExecutionBasis(
        plan_event_sha256=plan_sha256,
        reservation_event_sha256=reservation_sha256,
        acknowledgement_event_sha256=acknowledgement_sha256,
        reconciliation_event_sha256=reconciliation_sha256,
        plan_id=plan_id,
        action_id=action_id,
        attempt_id=target_attempt,
        requested_odds=requested_odds,
        requested_stake=requested_stake,
        external_receipt_id=external_receipt_id,
        acknowledgement_status=status,
        acknowledged_at=acknowledged_at,
        accepted_odds=accepted_odds,
        accepted_stake=accepted_stake,
        reconciliation_evidence_id=reconciliation_evidence_id,
        basis_id=_basis_id(identity_payload),
        **fields,
    )


def verify_settlement_execution_basis(
    ledger: RealExecutionLedger,
    basis: SettlementExecutionBasis,
) -> SettlementExecutionBasis:
    """Fail closed unless a supplied basis exactly re-resolves from durable truth."""

    if type(basis) is not SettlementExecutionBasis:
        raise TypeError("basis must be exact SettlementExecutionBasis")
    expected = derive_settlement_execution_basis(
        ledger,
        attempt_id=basis.attempt_id,
    )
    if basis != expected:
        raise SettlementExecutionBasisError(
            "settlement execution basis does not match canonical durable execution"
        )
    return expected
