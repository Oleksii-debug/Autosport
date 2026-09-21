from __future__ import annotations

"""Fail-closed Betfair live capital-at-risk projection.

A positive result is point-in-time reserve truth for one durable standard-BACK
supervised attempt.  It is derived only from the attempt's durable provider order
reference plus a canonical ``BetfairExecutionReadbackEnvelope``.

The Betfair write seam persists ``accepted_stake=sizeMatched``.  That matched-only
value is not total live exposure while an unmatched remainder may still be live.
For the supported current-order shape this authority therefore uses the exact,
context-independent sum ``sizeMatched + sizeRemaining``.  Missing, cleared,
ambiguous, contradictory, unsupported, or non-authoritative evidence stays
UNKNOWN and cannot release risk.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import Any
from weakref import ref

from .betfair_account_readonly import (
    BetfairExecutionReadbackEnvelope,
    BetfairReadOnlyError,
)
from .real_execution_ledger import AttemptState, ExecutionLedgerError, RealExecutionLedger
from .supervised_execution import (
    BoundSupervisedExecutionPlan,
    SupervisedExecutionError,
)


class BetfairLiveCapitalAtRiskError(RuntimeError):
    pass


class BetfairLiveCapitalAtRiskStatus(StrEnum):
    EXACT_CURRENT_ORDER = "EXACT_CURRENT_ORDER"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class BetfairLiveCapitalAtRiskTruth:
    status: BetfairLiveCapitalAtRiskStatus
    reason: str
    plan_id: str
    attempt_id: str
    action_id: str | None
    provider_order_ref: str | None
    provider_bet_id: str | None
    observed_at: str | None
    readback_evidence_sha256: str | None
    ledger_snapshot_sha256: str | None
    ledger_event_count: int | None
    matched_stake: Decimal | None
    unmatched_stake: Decimal | None
    exact_capital_at_risk: Decimal | None
    execution_authority: bool
    settlement_authority: bool
    risk_release_authority: bool

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise BetfairLiveCapitalAtRiskError(
            "BetfairLiveCapitalAtRiskTruth is issued only by the canonical resolver"
        )

    def _validate(self) -> None:
        if type(self.status) is not BetfairLiveCapitalAtRiskStatus:
            raise BetfairLiveCapitalAtRiskError("invalid live-risk status")
        if type(self.reason) is not str or not self.reason.strip():
            raise BetfairLiveCapitalAtRiskError("live-risk reason must be non-empty")
        if type(self.plan_id) is not str or not self.plan_id.strip():
            raise BetfairLiveCapitalAtRiskError("plan_id must be non-empty")
        if type(self.attempt_id) is not str or not self.attempt_id.strip():
            raise BetfairLiveCapitalAtRiskError("attempt_id must be non-empty")
        if (
            self.execution_authority is not False
            or self.settlement_authority is not False
            or self.risk_release_authority is not False
        ):
            raise BetfairLiveCapitalAtRiskError(
                "live-risk truth cannot authorize execution, settlement, or risk release"
            )
        if self.ledger_event_count is not None and (
            type(self.ledger_event_count) is not int or self.ledger_event_count < 0
        ):
            raise BetfairLiveCapitalAtRiskError("invalid ledger_event_count")

        if self.status is BetfairLiveCapitalAtRiskStatus.EXACT_CURRENT_ORDER:
            required_text = (
                self.action_id,
                self.provider_order_ref,
                self.provider_bet_id,
                self.observed_at,
                self.readback_evidence_sha256,
                self.ledger_snapshot_sha256,
            )
            if any(type(value) is not str or not value for value in required_text):
                raise BetfairLiveCapitalAtRiskError(
                    "exact live-risk truth lacks required identity/evidence"
                )
            if self.ledger_event_count is None:
                raise BetfairLiveCapitalAtRiskError(
                    "exact live-risk truth lacks ledger generation"
                )
            values = (
                self.matched_stake,
                self.unmatched_stake,
                self.exact_capital_at_risk,
            )
            if any(
                type(value) is not Decimal or not value.is_finite() or value < 0
                for value in values
            ):
                raise BetfairLiveCapitalAtRiskError(
                    "exact live-risk amounts must be finite non-negative Decimal"
                )
            assert self.exact_capital_at_risk is not None
            if self.exact_capital_at_risk <= 0:
                raise BetfairLiveCapitalAtRiskError(
                    "zero exposure is not positive live-risk authority"
                )
        elif any(
            value is not None
            for value in (
                self.provider_bet_id,
                self.observed_at,
                self.readback_evidence_sha256,
                self.matched_stake,
                self.unmatched_stake,
                self.exact_capital_at_risk,
            )
        ):
            raise BetfairLiveCapitalAtRiskError(
                "UNKNOWN live-risk truth cannot carry positive provider evidence"
            )

    def _authority_fingerprint(self) -> str:
        return sha256(
            repr(
                tuple(
                    getattr(self, name)
                    for name in self.__dataclass_fields__
                )
            ).encode("utf-8")
        ).hexdigest()

    def assert_authoritative(self) -> None:
        raise BetfairLiveCapitalAtRiskError("live-risk authority is unavailable")


def _exact_nonnegative_sum(left: Decimal, right: Decimal) -> Decimal:
    """Add finite non-negative Decimal values without ambient-context rounding."""

    def parts(value: Decimal) -> tuple[int, int]:
        if type(value) is not Decimal or not value.is_finite() or value < 0:
            raise BetfairLiveCapitalAtRiskError(
                "provider live-risk amounts must be finite non-negative Decimal"
            )
        item = value.as_tuple()
        if item.sign or type(item.exponent) is not int:
            raise BetfairLiveCapitalAtRiskError("invalid provider Decimal")
        coefficient = 0
        for digit in item.digits:
            coefficient = coefficient * 10 + digit
        return coefficient, item.exponent

    left_coefficient, left_exponent = parts(left)
    right_coefficient, right_exponent = parts(right)
    exponent = min(left_exponent, right_exponent)
    total = (
        left_coefficient * 10 ** (left_exponent - exponent)
        + right_coefficient * 10 ** (right_exponent - exponent)
    )
    digits = tuple(int(char) for char in str(total)) if total else (0,)
    return Decimal((0, digits, exponent))


def _unknown_fields(
    reason: str,
    *,
    plan_id: str,
    attempt_id: str,
    action_id: str | None,
    provider_order_ref: str | None,
    ledger_snapshot_sha256: str | None,
    ledger_event_count: int | None,
) -> dict[str, Any]:
    return {
        "status": BetfairLiveCapitalAtRiskStatus.UNKNOWN,
        "reason": reason,
        "plan_id": plan_id,
        "attempt_id": attempt_id,
        "action_id": action_id,
        "provider_order_ref": provider_order_ref,
        "provider_bet_id": None,
        "observed_at": None,
        "readback_evidence_sha256": None,
        "ledger_snapshot_sha256": ledger_snapshot_sha256,
        "ledger_event_count": ledger_event_count,
        "matched_stake": None,
        "unmatched_stake": None,
        "exact_capital_at_risk": None,
        "execution_authority": False,
        "settlement_authority": False,
        "risk_release_authority": False,
    }


def _resolve_fields(
    bound: BoundSupervisedExecutionPlan,
    ledger: RealExecutionLedger,
    *,
    attempt_id: str,
    readback: BetfairExecutionReadbackEnvelope,
) -> dict[str, Any]:
    if not isinstance(bound, BoundSupervisedExecutionPlan):
        raise BetfairLiveCapitalAtRiskError(
            "bound must be BoundSupervisedExecutionPlan"
        )
    if not isinstance(ledger, RealExecutionLedger):
        raise BetfairLiveCapitalAtRiskError("ledger must be RealExecutionLedger")
    if type(attempt_id) is not str or not attempt_id.strip():
        raise BetfairLiveCapitalAtRiskError("attempt_id must be non-empty text")

    plan_id = bound.execution_plan.plan_id
    action_id: str | None = None
    provider_order_ref: str | None = None
    snapshot_sha256: str | None = None
    event_count: int | None = None

    def unknown(reason: str) -> dict[str, Any]:
        return _unknown_fields(
            reason,
            plan_id=plan_id,
            attempt_id=attempt_id,
            action_id=action_id,
            provider_order_ref=provider_order_ref,
            ledger_snapshot_sha256=snapshot_sha256,
            ledger_event_count=event_count,
        )

    try:
        bound.verify_binding()
        before = ledger.verified_snapshot()
        snapshot_sha256, event_count = before.sha256, before.event_count
        saga = ledger.saga(plan_id)
        if saga.plan_fingerprint != bound.execution_plan.fingerprint:
            return unknown(
                "durable execution plan fingerprint mismatches bound supervised plan"
            )
        action_id = saga.attempt_action_ids.get(attempt_id)
        state = saga.attempts.get(attempt_id)
        if action_id is None or state is None:
            return unknown(
                "attempt is not durably bound to the supervised execution plan"
            )
        action = bound.action_for(action_id)
        provider_order_ref = ledger.provider_order_reference(
            attempt_id=attempt_id,
            provider_id=action.bookmaker_id,
        )
        after = ledger.verified_snapshot()
    except (ExecutionLedgerError, SupervisedExecutionError, KeyError, ValueError) as exc:
        return unknown(
            f"durable execution identity is unavailable: {type(exc).__name__}"
        )

    if before.sha256 != after.sha256 or before.event_count != after.event_count:
        snapshot_sha256, event_count = after.sha256, after.event_count
        return unknown(
            "execution ledger changed while live-risk identity was being resolved"
        )
    snapshot_sha256, event_count = after.sha256, after.event_count

    if state not in {
        AttemptState.SUBMITTED,
        AttemptState.UNKNOWN,
        AttemptState.ACCEPTED,
        AttemptState.PARTIAL,
    }:
        return unknown(
            f"attempt state {state.value} cannot prove a live provider exposure"
        )
    if action.bookmaker_id != "betfair" or action.side != "BACK":
        return unknown(
            "live-risk resolver supports only canonical Betfair standard BACK actions"
        )
    if provider_order_ref is None:
        return unknown("attempt lacks a durable Betfair provider order reference")
    if not isinstance(readback, BetfairExecutionReadbackEnvelope):
        return unknown("canonical Betfair execution readback is required")
    try:
        readback.assert_authoritative()
    except BetfairReadOnlyError:
        return unknown(
            "Betfair execution readback is not canonical authoritative evidence"
        )
    try:
        profile = bound.profile_for(action.bookmaker_id, action.account_id)
    except SupervisedExecutionError:
        return unknown("bound plan lacks the exact Betfair account profile")

    if (
        readback.venue_id,
        readback.account_id,
        readback.action_id,
        readback.provider_order_ref,
        readback.market_id,
        readback.market_event.event_id,
        readback.adapter_id,
        readback.adapter_version,
    ) != (
        action.bookmaker_id,
        action.account_id,
        action.action_id,
        provider_order_ref,
        action.market_id,
        action.event_id,
        profile.adapter_id,
        profile.adapter_version,
    ):
        return unknown(
            "Betfair execution readback identity mismatches durable supervised action"
        )

    current_orders = tuple(
        order for page in readback.current_pages for order in page.orders
    )
    cleared_orders = tuple(
        order
        for _status, pages in readback.cleared_pages_by_status
        for page in pages
        for order in page.orders
    )
    if any(order.customer_order_ref != provider_order_ref for order in current_orders):
        return unknown(
            "currentOrders returned an order outside the durable provider reference"
        )
    if any(order.customer_order_ref != provider_order_ref for order in cleared_orders):
        return unknown(
            "clearedOrders returned an order outside the durable provider reference"
        )
    if cleared_orders:
        return unknown(
            "provider order appears in cleared evidence; finality is outside live-risk authority"
        )
    if len(current_orders) != 1:
        return unknown("exactly one provider current-order row is required")

    row = current_orders[0]
    try:
        selection_id = int(action.selection_id)
    except (TypeError, ValueError):
        selection_id = -1
    if selection_id <= 0 or str(selection_id) != action.selection_id:
        return unknown(
            "durable Betfair selection identity is not canonical positive integer text"
        )
    if (
        row.market_id,
        row.selection_id,
        row.side,
        row.price,
        row.requested_size,
    ) != (
        action.market_id,
        selection_id,
        action.side,
        action.requested_odds,
        action.requested_stake,
    ):
        return unknown(
            "provider current-order economics mismatch durable execution action"
        )
    if row.status not in {"EXECUTABLE", "EXECUTION_COMPLETE"}:
        return unknown(
            "provider current-order status is unsupported for live-risk truth"
        )
    if row.size_matched > 0 and row.average_price_matched <= 0:
        return unknown(
            "matched provider stake lacks a positive average matched price"
        )
    if row.size_matched == 0 and row.average_price_matched != 0:
        return unknown(
            "unmatched provider order reports a nonzero average matched price"
        )
    if row.status == "EXECUTION_COMPLETE" and row.size_remaining != 0:
        return unknown(
            "execution-complete provider order still reports unmatched live size"
        )

    try:
        capital_at_risk = _exact_nonnegative_sum(
            row.size_matched,
            row.size_remaining,
        )
    except BetfairLiveCapitalAtRiskError:
        return unknown("provider current-order stake arithmetic is invalid")
    if capital_at_risk <= 0 or capital_at_risk > action.requested_stake:
        return unknown(
            "provider current-order reserve is zero or exceeds requested stake"
        )

    return {
        "status": BetfairLiveCapitalAtRiskStatus.EXACT_CURRENT_ORDER,
        "reason": (
            "authoritative currentOrders capture proves exact standard-BACK "
            "matched plus unmatched reserve"
        ),
        "plan_id": plan_id,
        "attempt_id": attempt_id,
        "action_id": action_id,
        "provider_order_ref": provider_order_ref,
        "provider_bet_id": row.bet_id,
        "observed_at": row.evidence.observed_at,
        "readback_evidence_sha256": readback.evidence_sha256,
        "ledger_snapshot_sha256": snapshot_sha256,
        "ledger_event_count": event_count,
        "matched_stake": row.size_matched,
        "unmatched_stake": row.size_remaining,
        "exact_capital_at_risk": capital_at_risk,
        "execution_authority": False,
        "settlement_authority": False,
        "risk_release_authority": False,
    }


def _install_live_risk_authority() -> None:
    issued: dict[int, tuple[object, str]] = {}

    def issue(fields: dict[str, Any]) -> BetfairLiveCapitalAtRiskTruth:
        truth = object.__new__(BetfairLiveCapitalAtRiskTruth)
        for field_name, value in fields.items():
            object.__setattr__(truth, field_name, value)
        truth._validate()
        truth_id = id(truth)

        def forget(_weakref: object, *, key: int = truth_id) -> None:
            issued.pop(key, None)

        issued[truth_id] = (ref(truth, forget), truth._authority_fingerprint())
        return truth

    def assert_authoritative(self: BetfairLiveCapitalAtRiskTruth) -> None:
        self._validate()
        record = issued.get(id(self))
        if record is None or record[0]() is not self:
            raise BetfairLiveCapitalAtRiskError(
                "live-risk truth was not issued by the canonical resolver"
            )
        if record[1] != self._authority_fingerprint():
            raise BetfairLiveCapitalAtRiskError(
                "live-risk truth changed after canonical issuance"
            )

    def resolve_betfair_live_capital_at_risk(
        bound: BoundSupervisedExecutionPlan,
        ledger: RealExecutionLedger,
        *,
        attempt_id: str,
        readback: BetfairExecutionReadbackEnvelope,
    ) -> BetfairLiveCapitalAtRiskTruth:
        return issue(
            _resolve_fields(
                bound,
                ledger,
                attempt_id=attempt_id,
                readback=readback,
            )
        )

    BetfairLiveCapitalAtRiskTruth.assert_authoritative = assert_authoritative
    globals()["resolve_betfair_live_capital_at_risk"] = (
        resolve_betfair_live_capital_at_risk
    )


_install_live_risk_authority()
del _install_live_risk_authority
