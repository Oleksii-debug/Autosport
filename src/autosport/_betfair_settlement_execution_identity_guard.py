"""Bind #1272 settlement rows to the exact durable execution action.

The canonical read-only adapter proves that a cleared row came from the authenticated
Betfair readback surface, while #1272 proves that the external bet id belongs to the
durable execution attempt.  Neither boundary alone proves that the provider row still
describes the same executable economics as that durable action.

This composition guard keeps the existing readback, execution-ledger and settlement
stores as the only authorities.  It rejects a cleared row before revision publication
when:

* provider placement time is after provider settlement time;
* provider ``priceRequested`` differs from the durable action's requested odds; or
* provider ``sizeSettled`` exceeds the durable action's requested stake.

It also exact-fences the existing execution-ledger owner check.  A subclass, an exact
instance with authority-bearing methods shadowed in ``__dict__``, or class/module
dispatch drift cannot substitute fabricated plan/saga/provider-reference evidence.
The product-workspace/path trust root remains a separate composition prerequisite;
this guard does not pretend that a syntactically valid caller-selected ledger path is
product-owned.

The economic checks are deliberately bounded.  They do not equate PARTIAL
acknowledgement quantity with eventual settlement quantity, do not infer fill price
from requested price, and do not claim permanent finality or complete terminal-state
authority.
"""

from __future__ import annotations

from . import betfair_settlement_revisions as _settlement


_ERROR = _settlement.BetfairSettlementRevisionError
_LEDGER_TYPE = _settlement.RealExecutionLedger
_ATTEMPT_STATE = _settlement.AttemptState
_RECEIPT_TYPE = _settlement.ExternalReceiptIdentity
_LEDGER_ERROR = _settlement.ExecutionLedgerError

_ORIGINAL_MATCH_ORDER = _settlement._match_order
_ORIGINAL_MATCH_ORDER_CODE = getattr(_ORIGINAL_MATCH_ORDER, "__code__", None)
_ORIGINAL_REQUIRE_OWNER = _settlement._require_attempt_receipt_owner
_ORIGINAL_REQUIRE_OWNER_CODE = getattr(_ORIGINAL_REQUIRE_OWNER, "__code__", None)
_CANONICAL_TIME = _settlement._time
_CANONICAL_TIME_CODE = getattr(_CANONICAL_TIME, "__code__", None)
_LEDGER_DISPATCH_NAMES = (
    "_events",
    "_action_payload",
    "saga",
    "provider_order_reference",
)
_LEDGER_DISPATCH = {
    name: vars(_LEDGER_TYPE).get(name) for name in _LEDGER_DISPATCH_NAMES
}

if (
    _ORIGINAL_MATCH_ORDER_CODE is None
    or _ORIGINAL_REQUIRE_OWNER_CODE is None
    or _CANONICAL_TIME_CODE is None
    or any(value is None for value in _LEDGER_DISPATCH.values())
):
    raise RuntimeError("Betfair settlement execution identity dispatch is unavailable")


def _require_dispatch() -> None:
    if (
        _settlement._match_order is not _match_order_with_execution_identity
        or _settlement._require_attempt_receipt_owner
        is not _require_owner_with_exact_ledger_dispatch
        or getattr(_ORIGINAL_MATCH_ORDER, "__code__", None)
        is not _ORIGINAL_MATCH_ORDER_CODE
        or getattr(_ORIGINAL_REQUIRE_OWNER, "__code__", None)
        is not _ORIGINAL_REQUIRE_OWNER_CODE
        or _settlement._time is not _CANONICAL_TIME
        or getattr(_CANONICAL_TIME, "__code__", None) is not _CANONICAL_TIME_CODE
        or _settlement.RealExecutionLedger is not _LEDGER_TYPE
        or _settlement.AttemptState is not _ATTEMPT_STATE
        or _settlement.ExternalReceiptIdentity is not _RECEIPT_TYPE
        or _settlement.ExecutionLedgerError is not _LEDGER_ERROR
        or any(
            vars(_LEDGER_TYPE).get(name) is not expected
            for name, expected in _LEDGER_DISPATCH.items()
        )
    ):
        raise _ERROR("settlement execution identity dispatch changed")


def _require_exact_ledger_surface(ledger) -> None:
    if type(ledger) is not _LEDGER_TYPE:
        raise _ERROR("settlement ledger must be the exact RealExecutionLedger")
    try:
        instance_values = vars(ledger)
    except TypeError as exc:  # pragma: no cover - exact canonical type has __dict__
        raise _ERROR("settlement ledger instance surface is unavailable") from exc
    shadowed = sorted(
        name for name in _LEDGER_DISPATCH_NAMES if name in instance_values
    )
    if shadowed:
        raise _ERROR(
            "settlement ledger authority dispatch is instance-shadowed: "
            + ", ".join(shadowed)
        )


def _match_order_with_execution_identity(action, capture):
    _require_dispatch()
    order = _ORIGINAL_MATCH_ORDER(action, capture)
    _require_dispatch()

    placed_at = _CANONICAL_TIME(order.placed_date, "placed_date")
    settled_at = _CANONICAL_TIME(order.settled_date, "settled_date")
    if placed_at > settled_at:
        raise _ERROR("settlement provider chronology predates order placement")

    if order.price_requested != action.requested_odds:
        raise _ERROR(
            "settlement requested price differs from durable execution action"
        )
    if order.size_settled > action.requested_stake:
        raise _ERROR(
            "settlement size exceeds durable execution action requested stake"
        )

    _require_dispatch()
    return order


def _require_owner_with_exact_ledger_dispatch(
    ledger,
    *,
    plan_id,
    attempt_id,
    action,
    capture,
    external_bet_id,
) -> None:
    _require_dispatch()
    _require_exact_ledger_surface(ledger)
    _ORIGINAL_REQUIRE_OWNER(
        ledger,
        plan_id=plan_id,
        attempt_id=attempt_id,
        action=action,
        capture=capture,
        external_bet_id=external_bet_id,
    )
    _require_exact_ledger_surface(ledger)
    _require_dispatch()


if _settlement._match_order is not _ORIGINAL_MATCH_ORDER:
    raise RuntimeError("Betfair settlement match-order dispatch changed before guard install")
if _settlement._require_attempt_receipt_owner is not _ORIGINAL_REQUIRE_OWNER:
    raise RuntimeError("Betfair settlement ledger-owner dispatch changed before guard install")
_settlement._match_order = _match_order_with_execution_identity
_settlement._require_attempt_receipt_owner = _require_owner_with_exact_ledger_dispatch

__all__: list[str] = []
