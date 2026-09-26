from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from weakref import ref

from .betfair_account_readonly import (
    BetfairClearedOrderObservation,
    BetfairCurrentOrderObservation,
    BetfairExecutionReadbackEnvelope,
    BetfairReadOnlyError,
)
from .real_execution_ledger import (
    AttemptState,
    ExecutionAction,
    ExecutionLedgerError,
    ExecutionPlan,
    RealExecutionLedger,
)


SCHEMA_VERSION = 1
_LEDGER_VERIFIED_SNAPSHOT = RealExecutionLedger.verified_snapshot
_LEDGER_SAGA = RealExecutionLedger.saga
_LEDGER_PROVIDER_ORDER_REFERENCE = RealExecutionLedger.provider_order_reference
_LEDGER_READ_SURFACES = (
    ("verified_snapshot", _LEDGER_VERIFIED_SNAPSHOT),
    ("saga", _LEDGER_SAGA),
    ("provider_order_reference", _LEDGER_PROVIDER_ORDER_REFERENCE),
)
_READBACK_ASSERT_AUTHORITATIVE = BetfairExecutionReadbackEnvelope.assert_authoritative
_READBACK_AUTHORITY_FINGERPRINT = BetfairExecutionReadbackEnvelope._authority_fingerprint


class RealizedMatchEvidenceError(RuntimeError):
    """Raised when provider match economics cannot be resolved safely."""


class RealizedMatchSource(str, Enum):
    INCOMPLETE_EVIDENCE = "INCOMPLETE_EVIDENCE"
    CURRENT_ORDER = "CURRENT_ORDER"
    CLEARED_BET = "CLEARED_BET"


_SOURCE_RANK = {
    RealizedMatchSource.INCOMPLETE_EVIDENCE: 0,
    RealizedMatchSource.CURRENT_ORDER: 1,
    RealizedMatchSource.CLEARED_BET: 2,
}


def _decimal_text(value: Decimal) -> str:
    """Serialize Decimal exactly without consulting the ambient Decimal context."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise RealizedMatchEvidenceError("economic decimal must be finite")
    sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):
        raise RealizedMatchEvidenceError("economic decimal exponent is invalid")

    coefficient = "".join(str(digit) for digit in digits) or "0"
    if all(digit == "0" for digit in coefficient):
        return "0"

    while len(coefficient) > 1 and coefficient.endswith("0"):
        coefficient = coefficient[:-1]
        exponent += 1

    max_text_length = 4096
    if exponent >= 0:
        if len(coefficient) + exponent > max_text_length:
            raise RealizedMatchEvidenceError("economic decimal text is too large")
        body = coefficient + ("0" * exponent)
    else:
        point = len(coefficient) + exponent
        if point > 0:
            body = coefficient[:point] + "." + coefficient[point:]
        else:
            if 2 + (-point) + len(coefficient) > max_text_length:
                raise RealizedMatchEvidenceError("economic decimal text is too large")
            body = "0." + ("0" * (-point)) + coefficient

    return ("-" if sign else "") + body


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise RealizedMatchEvidenceError(
            "realized match evidence is not canonical JSON"
        ) from exc


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _timestamp(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise RealizedMatchEvidenceError(
            f"{name} must be ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RealizedMatchEvidenceError(
            f"{name} must be timezone-aware"
        )
    return parsed


@dataclass(frozen=True, slots=True, weakref_slot=True)
class BetfairRealizedMatchEvidence:
    """Read-only projection of provider matched-price/size truth.

    This object is not a provider write, settlement, or execution-state authority.
    Positive authority is valid only when ``assert_authoritative`` succeeds.
    """

    source: RealizedMatchSource
    plan_id: str
    plan_fingerprint: str
    attempt_id: str
    attempt_state: AttemptState
    action_id: str
    bookmaker_id: str
    account_id: str
    event_id: str
    market_id: str
    selection_id: str
    side: str
    provider_order_ref: str
    bet_id: str | None
    requested_odds: Decimal
    requested_stake: Decimal
    provider_matched_odds: Decimal | None
    provider_matched_stake: Decimal | None
    unrealized_requested_stake: Decimal | None
    provider_status: str | None
    provider_observed_at: str | None
    provider_settled_at: str | None
    source_payload_sha256: str | None
    readback_observed_at: str
    readback_evidence_sha256: str
    ledger_snapshot_sha256: str
    finalized: bool
    evidence_id: str
    schema_version: int = SCHEMA_VERSION

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source": self.source.value,
            "plan_id": self.plan_id,
            "plan_fingerprint": self.plan_fingerprint,
            "attempt_id": self.attempt_id,
            "attempt_state": self.attempt_state.value,
            "action_id": self.action_id,
            "bookmaker_id": self.bookmaker_id,
            "account_id": self.account_id,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "side": self.side,
            "provider_order_ref": self.provider_order_ref,
            "bet_id": self.bet_id,
            "requested_odds": _decimal_text(self.requested_odds),
            "requested_stake": _decimal_text(self.requested_stake),
            "provider_matched_odds": (
                _decimal_text(self.provider_matched_odds)
                if self.provider_matched_odds is not None
                else None
            ),
            "provider_matched_stake": (
                _decimal_text(self.provider_matched_stake)
                if self.provider_matched_stake is not None
                else None
            ),
            "unrealized_requested_stake": (
                _decimal_text(self.unrealized_requested_stake)
                if self.unrealized_requested_stake is not None
                else None
            ),
            "provider_status": self.provider_status,
            "provider_observed_at": self.provider_observed_at,
            "provider_settled_at": self.provider_settled_at,
            "source_payload_sha256": self.source_payload_sha256,
            "readback_observed_at": self.readback_observed_at,
            "readback_evidence_sha256": self.readback_evidence_sha256,
            "ledger_snapshot_sha256": self.ledger_snapshot_sha256,
            "finalized": self.finalized,
        }

    def _identity_payload(self) -> dict[str, object]:
        """Return stable historical identity, excluding mutable ledger-tail audit state."""
        payload = self._payload()
        payload.pop("ledger_snapshot_sha256")
        return payload

    def to_dict(self) -> dict[str, object]:
        payload = self._payload()
        payload["evidence_id"] = self.evidence_id
        return payload

    @property
    def has_matched_economics(self) -> bool:
        return (
            self.provider_matched_stake is not None
            and self.provider_matched_stake > 0
            and self.provider_matched_odds is not None
        )

    def _validate_integrity(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise RealizedMatchEvidenceError(
                "unsupported realized match evidence schema"
            )
        if self.evidence_id != _sha256(self._identity_payload()):
            raise RealizedMatchEvidenceError(
                "realized match evidence digest mismatch"
            )

    def assert_authoritative(self) -> None:
        """Replaced below with an origin-aware validator."""
        self._validate_integrity()
        raise RealizedMatchEvidenceError(
            "realized match evidence was not issued by canonical resolver"
        )


def _exact_plan_fingerprint(plan: ExecutionPlan) -> str:
    """Recompute the durable plan digest without context-sensitive Decimal.normalize."""
    return _sha256(
        {
            "schema_version": plan.schema_version,
            "plan_id": plan.plan_id,
            "bookmaker_profile_version": plan.bookmaker_profile_version,
            "decision_id": plan.decision_id,
            "approval_id": plan.approval_id,
            "created_at": plan.created_at,
            "actions": [
                {
                    "action_id": action.action_id,
                    "bookmaker_id": action.bookmaker_id,
                    "account_id": action.account_id,
                    "event_id": action.event_id,
                    "market_id": action.market_id,
                    "selection_id": action.selection_id,
                    "side": action.side,
                    "requested_odds": _decimal_text(action.requested_odds),
                    "requested_stake": _decimal_text(action.requested_stake),
                    "quote_id": action.quote_id,
                    "quote_observed_at": action.quote_observed_at,
                    "expires_at": action.expires_at,
                }
                for action in plan.actions
            ],
        }
    )


@dataclass(frozen=True, slots=True)
class _AttemptBinding:
    action: ExecutionAction
    provider_order_ref: str
    state: AttemptState
    plan_fingerprint: str
    ledger_snapshot_sha256: str


def _attempt_binding(
    plan: ExecutionPlan,
    ledger: RealExecutionLedger,
    attempt_id: str,
) -> _AttemptBinding:
    if not isinstance(plan, ExecutionPlan):
        raise TypeError("plan must be ExecutionPlan")
    if type(ledger) is not RealExecutionLedger:
        raise TypeError("ledger must be exact RealExecutionLedger")
    if type(attempt_id) is not str or not attempt_id.strip():
        raise RealizedMatchEvidenceError(
            "attempt_id must be non-empty text"
        )

    instance_state = getattr(ledger, "__dict__", {})
    for method_name, canonical_method in _LEDGER_READ_SURFACES:
        if method_name in instance_state:
            raise RealizedMatchEvidenceError(
                "execution ledger read authority is rebound on the instance"
            )
        if getattr(RealExecutionLedger, method_name, None) is not canonical_method:
            raise RealizedMatchEvidenceError(
                "canonical execution ledger read authority changed"
            )

    try:
        before = _LEDGER_VERIFIED_SNAPSHOT(ledger)
        saga = _LEDGER_SAGA(ledger, plan.plan_id)
        if saga.plan_fingerprint != _exact_plan_fingerprint(plan):
            raise RealizedMatchEvidenceError(
                "caller plan does not match durable execution plan fingerprint"
            )
        action_id = saga.attempt_action_ids.get(attempt_id)
        state = saga.attempts.get(attempt_id)
        if action_id is None or state is None:
            raise RealizedMatchEvidenceError(
                "attempt is not durably bound to the execution plan"
            )
        actions = tuple(
            action for action in plan.actions if action.action_id == action_id
        )
        if len(actions) != 1:
            raise RealizedMatchEvidenceError(
                "durable attempt action cannot be resolved uniquely"
            )
        action = actions[0]
        provider_order_ref = _LEDGER_PROVIDER_ORDER_REFERENCE(
            ledger,
            attempt_id=attempt_id,
            provider_id=action.bookmaker_id,
        )
        if provider_order_ref is None:
            raise RealizedMatchEvidenceError(
                "attempt lacks durable provider order reference"
            )
        after = _LEDGER_VERIFIED_SNAPSHOT(ledger)
    except RealizedMatchEvidenceError:
        raise
    except (ExecutionLedgerError, KeyError, OSError) as exc:
        raise RealizedMatchEvidenceError(
            "durable execution attempt cannot be verified"
        ) from exc

    if (
        before.sha256 != after.sha256
        or before.event_count != after.event_count
    ):
        raise RealizedMatchEvidenceError(
            "execution ledger changed during realized-match resolution"
        )
    return _AttemptBinding(
        action=action,
        provider_order_ref=provider_order_ref,
        state=state,
        plan_fingerprint=saga.plan_fingerprint,
        ledger_snapshot_sha256=after.sha256,
    )


def _identity_check(
    action: ExecutionAction,
    readback: BetfairExecutionReadbackEnvelope,
    provider_order_ref: str,
) -> int:
    if type(readback) is not BetfairExecutionReadbackEnvelope:
        raise TypeError("readback must be exact BetfairExecutionReadbackEnvelope")
    if (
        BetfairExecutionReadbackEnvelope.assert_authoritative
        is not _READBACK_ASSERT_AUTHORITATIVE
        or BetfairExecutionReadbackEnvelope._authority_fingerprint
        is not _READBACK_AUTHORITY_FINGERPRINT
    ):
        raise RealizedMatchEvidenceError(
            "canonical execution readback authority changed"
        )
    try:
        before_fingerprint = _READBACK_AUTHORITY_FINGERPRINT(readback)
        _READBACK_ASSERT_AUTHORITATIVE(readback)
        after_fingerprint = _READBACK_AUTHORITY_FINGERPRINT(readback)
    except BetfairReadOnlyError as exc:
        raise RealizedMatchEvidenceError(
            "execution readback is not canonical provider evidence"
        ) from exc
    if before_fingerprint != after_fingerprint:
        raise RealizedMatchEvidenceError(
            "execution readback changed during authority verification"
        )
    if (
        BetfairExecutionReadbackEnvelope.assert_authoritative
        is not _READBACK_ASSERT_AUTHORITATIVE
        or BetfairExecutionReadbackEnvelope._authority_fingerprint
        is not _READBACK_AUTHORITY_FINGERPRINT
    ):
        raise RealizedMatchEvidenceError(
            "canonical execution readback authority changed during verification"
        )

    if action.bookmaker_id != "betfair" or readback.venue_id != action.bookmaker_id:
        raise RealizedMatchEvidenceError(
            "execution action/readback bookmaker identity mismatch"
        )
    if readback.account_id != action.account_id:
        raise RealizedMatchEvidenceError(
            "execution action/readback account identity mismatch"
        )
    if readback.action_id != action.action_id:
        raise RealizedMatchEvidenceError(
            "execution action/readback action identity mismatch"
        )
    if readback.market_id != action.market_id:
        raise RealizedMatchEvidenceError(
            "execution action/readback market identity mismatch"
        )
    if readback.market_event.event_id != action.event_id:
        raise RealizedMatchEvidenceError(
            "execution action/readback event identity mismatch"
        )
    if readback.provider_order_ref != provider_order_ref:
        raise RealizedMatchEvidenceError(
            "execution readback provider order reference mismatch"
        )
    try:
        selection_id = int(action.selection_id)
    except (TypeError, ValueError) as exc:
        raise RealizedMatchEvidenceError(
            "execution action selection_id is not a Betfair integer"
        ) from exc
    if selection_id <= 0 or str(selection_id) != action.selection_id:
        raise RealizedMatchEvidenceError(
            "execution action selection_id is not canonical Betfair text"
        )
    return selection_id


def _check_common_row_identity(
    row: BetfairCurrentOrderObservation | BetfairClearedOrderObservation,
    *,
    action: ExecutionAction,
    selection_id: int,
    provider_order_ref: str,
) -> None:
    if row.market_id != action.market_id:
        raise RealizedMatchEvidenceError(
            "provider order row belongs to a different market"
        )
    if row.selection_id != selection_id:
        raise RealizedMatchEvidenceError(
            "provider order row belongs to a different selection"
        )
    if row.side != action.side:
        raise RealizedMatchEvidenceError(
            "provider order row has a different side"
        )
    if row.customer_order_ref != provider_order_ref:
        raise RealizedMatchEvidenceError(
            "provider order row lacks the exact durable customer order reference"
        )


def _flatten_rows(
    readback: BetfairExecutionReadbackEnvelope,
) -> tuple[
    tuple[BetfairCurrentOrderObservation, ...],
    tuple[BetfairClearedOrderObservation, ...],
]:
    current = tuple(
        order
        for page in readback.current_pages
        for order in page.orders
    )
    cleared = tuple(
        order
        for _status, pages in readback.cleared_pages_by_status
        for page in pages
        for order in page.orders
    )
    return current, cleared


def _cleared_source_digest(
    cleared: tuple[BetfairClearedOrderObservation, ...],
) -> str:
    if len(cleared) == 1:
        return cleared[0].evidence.source_payload_sha256
    rows = [
        {
            "bet_id": row.bet_id,
            "bet_status": row.bet_status,
            "placed_date": row.placed_date,
            "settled_date": row.settled_date,
            "price_requested": _decimal_text(row.price_requested),
            "price_matched": _decimal_text(row.price_matched),
            "size_settled": _decimal_text(row.size_settled),
            "source_payload_sha256": row.evidence.source_payload_sha256,
            "observed_at": row.evidence.observed_at,
        }
        for row in cleared
    ]
    rows.sort(key=_canonical)
    return _sha256(
        {
            "schema": "autosport.betfair_realized_match.cleared_source_set",
            "schema_version": 1,
            "rows": rows,
        }
    )


def _resolve_cleared_economics(
    cleared: tuple[BetfairClearedOrderObservation, ...],
    *,
    action: ExecutionAction,
) -> tuple[
    str,
    Decimal | None,
    Decimal,
    str,
    str,
    str,
]:
    positive: dict[tuple[Decimal, Decimal], BetfairClearedOrderObservation] = {}
    for row in cleared:
        if row.price_requested != action.requested_odds:
            raise RealizedMatchEvidenceError(
                "cleared BET requested price differs from execution action"
            )
        if row.size_settled > action.requested_stake:
            raise RealizedMatchEvidenceError(
                "cleared BET settled size exceeds requested stake"
            )
        if row.size_settled > 0:
            if row.price_matched <= 0:
                raise RealizedMatchEvidenceError(
                    "cleared BET matched stake lacks a positive matched price"
                )
            positive.setdefault((row.size_settled, row.price_matched), row)
        elif row.price_matched != 0:
            raise RealizedMatchEvidenceError(
                "cleared BET zero settled size has a non-zero matched price"
            )

    if len(positive) > 1:
        raise RealizedMatchEvidenceError(
            "provider readback contains ambiguous cleared BET economics"
        )
    if positive:
        (matched_stake, matched_odds), representative = next(iter(positive.items()))
    else:
        if len(cleared) != 1:
            raise RealizedMatchEvidenceError(
                "provider readback contains ambiguous zero cleared BET states"
            )
        representative = cleared[0]
        matched_stake = Decimal("0")
        matched_odds = None

    statuses = sorted({row.bet_status for row in cleared})
    provider_status = statuses[0] if len(statuses) == 1 else "+".join(statuses)
    latest_observed = max(
        cleared,
        key=lambda row: _timestamp(row.evidence.observed_at, "cleared observed_at"),
    ).evidence.observed_at
    latest_settled = max(
        cleared,
        key=lambda row: _timestamp(row.settled_date, "cleared settled_date"),
    ).settled_date
    return (
        representative.bet_id,
        matched_odds,
        matched_stake,
        provider_status,
        latest_observed,
        latest_settled,
    )


def _make_evidence(
    *,
    source: RealizedMatchSource,
    plan: ExecutionPlan,
    binding: _AttemptBinding,
    attempt_id: str,
    readback: BetfairExecutionReadbackEnvelope,
    bet_id: str | None,
    provider_matched_odds: Decimal | None,
    provider_matched_stake: Decimal | None,
    provider_status: str | None,
    provider_observed_at: str | None,
    provider_settled_at: str | None,
    source_payload_sha256: str | None,
    finalized: bool,
) -> BetfairRealizedMatchEvidence:
    action = binding.action
    unrealized = (
        action.requested_stake - provider_matched_stake
        if provider_matched_stake is not None
        else None
    )
    draft = BetfairRealizedMatchEvidence(
        source=source,
        plan_id=plan.plan_id,
        plan_fingerprint=binding.plan_fingerprint,
        attempt_id=attempt_id,
        attempt_state=binding.state,
        action_id=action.action_id,
        bookmaker_id=action.bookmaker_id,
        account_id=action.account_id,
        event_id=action.event_id,
        market_id=action.market_id,
        selection_id=action.selection_id,
        side=action.side,
        provider_order_ref=binding.provider_order_ref,
        bet_id=bet_id,
        requested_odds=action.requested_odds,
        requested_stake=action.requested_stake,
        provider_matched_odds=provider_matched_odds,
        provider_matched_stake=provider_matched_stake,
        unrealized_requested_stake=unrealized,
        provider_status=provider_status,
        provider_observed_at=provider_observed_at,
        provider_settled_at=provider_settled_at,
        source_payload_sha256=source_payload_sha256,
        readback_observed_at=readback.observed_at,
        readback_evidence_sha256=readback.evidence_sha256,
        ledger_snapshot_sha256=binding.ledger_snapshot_sha256,
        finalized=finalized,
        evidence_id="pending",
    )
    return replace(
        draft,
        evidence_id=_sha256(draft._identity_payload()),
    )


def _resolve_betfair_realized_match(
    plan: ExecutionPlan,
    ledger: RealExecutionLedger,
    readback: BetfairExecutionReadbackEnvelope,
    *,
    attempt_id: str,
) -> BetfairRealizedMatchEvidence:
    if type(readback) is not BetfairExecutionReadbackEnvelope:
        raise TypeError("readback must be exact BetfairExecutionReadbackEnvelope")

    binding = _attempt_binding(plan, ledger, attempt_id)
    action = binding.action
    selection_id = _identity_check(
        action,
        readback,
        binding.provider_order_ref,
    )
    current, cleared = _flatten_rows(readback)

    for row in (*current, *cleared):
        _check_common_row_identity(
            row,
            action=action,
            selection_id=selection_id,
            provider_order_ref=binding.provider_order_ref,
        )

    if (
        (current or cleared)
        and binding.state
        in {
            AttemptState.RESERVED,
            AttemptState.REJECTED,
            AttemptState.RECONCILED_NOT_FOUND,
        }
    ):
        raise RealizedMatchEvidenceError(
            "provider order evidence conflicts with durable attempt state"
        )

    bet_ids = {row.bet_id for row in (*current, *cleared)}
    if len(bet_ids) > 1:
        raise RealizedMatchEvidenceError(
            "provider readback maps one durable order reference to multiple bet ids"
        )

    if cleared:
        (
            bet_id,
            matched_odds,
            matched_stake,
            provider_status,
            provider_observed_at,
            provider_settled_at,
        ) = _resolve_cleared_economics(cleared, action=action)
        return _make_evidence(
            source=RealizedMatchSource.CLEARED_BET,
            plan=plan,
            binding=binding,
            attempt_id=attempt_id,
            readback=readback,
            bet_id=bet_id,
            provider_matched_odds=matched_odds,
            provider_matched_stake=matched_stake,
            provider_status=provider_status,
            provider_observed_at=provider_observed_at,
            provider_settled_at=provider_settled_at,
            source_payload_sha256=_cleared_source_digest(cleared),
            finalized=True,
        )

    if current:
        if len(current) != 1:
            raise RealizedMatchEvidenceError(
                "provider readback contains multiple current BET rows"
            )
        row = current[0]
        if row.price is not None and row.price != action.requested_odds:
            raise RealizedMatchEvidenceError(
                "current order requested price differs from execution action"
            )
        if (
            row.requested_size is not None
            and row.requested_size != action.requested_stake
        ):
            raise RealizedMatchEvidenceError(
                "current order requested size differs from execution action"
            )
        if row.size_matched > action.requested_stake:
            raise RealizedMatchEvidenceError(
                "current order matched size exceeds requested stake"
            )
        if row.size_remaining > action.requested_stake:
            raise RealizedMatchEvidenceError(
                "current order remaining size exceeds requested stake"
            )
        if row.size_matched + row.size_remaining > action.requested_stake:
            raise RealizedMatchEvidenceError(
                "current order matched plus remaining size exceeds requested stake"
            )
        if row.size_matched > 0 and row.average_price_matched <= 0:
            raise RealizedMatchEvidenceError(
                "current matched stake lacks a positive average matched price"
            )
        if row.size_matched == 0 and row.average_price_matched != 0:
            raise RealizedMatchEvidenceError(
                "current zero matched size has a non-zero average matched price"
            )
        return _make_evidence(
            source=RealizedMatchSource.CURRENT_ORDER,
            plan=plan,
            binding=binding,
            attempt_id=attempt_id,
            readback=readback,
            bet_id=row.bet_id,
            provider_matched_odds=(
                row.average_price_matched if row.size_matched > 0 else None
            ),
            provider_matched_stake=row.size_matched,
            provider_status=row.status,
            provider_observed_at=row.evidence.observed_at,
            provider_settled_at=None,
            source_payload_sha256=row.evidence.source_payload_sha256,
            finalized=False,
        )

    return _make_evidence(
        source=RealizedMatchSource.INCOMPLETE_EVIDENCE,
        plan=plan,
        binding=binding,
        attempt_id=attempt_id,
        readback=readback,
        bet_id=None,
        provider_matched_odds=None,
        provider_matched_stake=None,
        provider_status=None,
        provider_observed_at=None,
        provider_settled_at=None,
        source_payload_sha256=None,
        finalized=False,
    )


def resolve_betfair_realized_match(
    plan: ExecutionPlan,
    ledger: RealExecutionLedger,
    readback: BetfairExecutionReadbackEnvelope,
    *,
    attempt_id: str,
) -> BetfairRealizedMatchEvidence:
    """Resolve matched-price/size truth without mutating execution state."""
    return _resolve_betfair_realized_match(
        plan,
        ledger,
        readback,
        attempt_id=attempt_id,
    )


def validate_betfair_realized_match_revision(
    previous: BetfairRealizedMatchEvidence,
    current: BetfairRealizedMatchEvidence,
) -> BetfairRealizedMatchEvidence:
    """Validate fields after the installed origin-aware wrapper authenticates both values."""
    if type(previous) is not BetfairRealizedMatchEvidence:
        raise TypeError("previous must be exact BetfairRealizedMatchEvidence")
    if type(current) is not BetfairRealizedMatchEvidence:
        raise TypeError("current must be exact BetfairRealizedMatchEvidence")

    immutable_fields = (
        "plan_id",
        "plan_fingerprint",
        "attempt_id",
        "action_id",
        "bookmaker_id",
        "account_id",
        "event_id",
        "market_id",
        "selection_id",
        "side",
        "provider_order_ref",
        "requested_odds",
        "requested_stake",
    )
    if any(
        getattr(previous, field) != getattr(current, field)
        for field in immutable_fields
    ):
        raise RealizedMatchEvidenceError(
            "realized match revision changes immutable execution identity"
        )
    if _timestamp(
        current.readback_observed_at,
        "current readback_observed_at",
    ) < _timestamp(
        previous.readback_observed_at,
        "previous readback_observed_at",
    ):
        raise RealizedMatchEvidenceError(
            "realized match revision moves provider observation backward"
        )
    if _SOURCE_RANK[current.source] < _SOURCE_RANK[previous.source]:
        raise RealizedMatchEvidenceError(
            "realized match revision regresses provider evidence source"
        )
    if previous.provider_matched_stake is not None:
        if current.provider_matched_stake is None:
            raise RealizedMatchEvidenceError(
                "realized match revision loses known matched stake"
            )
        if current.provider_matched_stake < previous.provider_matched_stake:
            raise RealizedMatchEvidenceError(
                "realized match revision decreases matched stake"
            )
        if (
            current.provider_matched_stake
            == previous.provider_matched_stake
            and current.provider_matched_stake > 0
            and current.provider_matched_odds
            != previous.provider_matched_odds
        ):
            final_provider_correction = (
                previous.source is RealizedMatchSource.CURRENT_ORDER
                and current.source is RealizedMatchSource.CLEARED_BET
                and current.finalized
            )
            if not final_provider_correction:
                raise RealizedMatchEvidenceError(
                    "realized match revision changes matched odds without new matched stake"
                )
    if previous.finalized and not current.finalized:
        raise RealizedMatchEvidenceError(
            "realized match revision reopens finalized provider evidence"
        )
    return current


def _install_realized_match_authority() -> None:
    issued: dict[int, tuple[object, str, str]] = {}
    # Capture the owning implementation, not the thin public wrapper.  The wrapper
    # performs a late module-global lookup of _resolve_betfair_realized_match and
    # would otherwise let a post-install rebind mint origin-sealed forged evidence.
    owning_resolve = _resolve_betfair_realized_match
    owning_validate_revision = validate_betfair_realized_match_revision
    evidence_type = BetfairRealizedMatchEvidence
    error_type = RealizedMatchEvidenceError
    hash_payload = _sha256
    weak_ref = ref
    validate_integrity = evidence_type._validate_integrity

    def authoritative_resolve(
        plan: ExecutionPlan,
        ledger: RealExecutionLedger,
        readback: BetfairExecutionReadbackEnvelope,
        *,
        attempt_id: str,
    ) -> BetfairRealizedMatchEvidence:
        if globals().get("_resolve_betfair_realized_match") is not owning_resolve:
            raise error_type(
                "canonical realized match resolver implementation changed"
            )
        if globals().get("resolve_betfair_realized_match") is not authoritative_resolve:
            raise error_type("canonical realized match resolver dispatch changed")
        evidence = owning_resolve(
            plan,
            ledger,
            readback,
            attempt_id=attempt_id,
        )
        if globals().get("_resolve_betfair_realized_match") is not owning_resolve:
            raise error_type(
                "canonical realized match resolver implementation changed during resolution"
            )
        if type(evidence) is not evidence_type:
            raise error_type("canonical realized match resolver returned invalid evidence")
        evidence_id = id(evidence)

        def forget(_weakref: object, *, key: int = evidence_id) -> None:
            issued.pop(key, None)

        issued[evidence_id] = (
            weak_ref(evidence, forget),
            evidence.evidence_id,
            hash_payload(evidence._payload()),
        )
        return evidence

    def assert_authoritative(
        self: BetfairRealizedMatchEvidence,
    ) -> None:
        if type(self) is not evidence_type:
            raise TypeError("evidence must be exact BetfairRealizedMatchEvidence")
        validate_integrity(self)
        record = issued.get(id(self))
        if record is None or record[0]() is not self:
            raise error_type(
                "realized match evidence was not issued by canonical resolver"
            )
        if record[1] != self.evidence_id:
            raise error_type(
                "realized match evidence changed after canonical resolution"
            )
        if record[2] != hash_payload(self._payload()):
            raise error_type(
                "realized match evidence payload changed after canonical resolution"
            )

    def authoritative_validate_revision(
        previous: BetfairRealizedMatchEvidence,
        current: BetfairRealizedMatchEvidence,
    ) -> BetfairRealizedMatchEvidence:
        if (
            globals().get("validate_betfair_realized_match_revision")
            is not authoritative_validate_revision
        ):
            raise error_type("canonical realized match revision dispatch changed")
        if evidence_type.assert_authoritative is not assert_authoritative:
            raise error_type(
                "canonical realized match evidence authority changed"
            )
        assert_authoritative(previous)
        assert_authoritative(current)
        result = owning_validate_revision(previous, current)
        if (
            globals().get("validate_betfair_realized_match_revision")
            is not authoritative_validate_revision
        ):
            raise error_type(
                "canonical realized match revision dispatch changed during validation"
            )
        return result

    globals()["resolve_betfair_realized_match"] = authoritative_resolve
    globals()["validate_betfair_realized_match_revision"] = (
        authoritative_validate_revision
    )
    evidence_type.assert_authoritative = assert_authoritative


_install_realized_match_authority()
del _install_realized_match_authority