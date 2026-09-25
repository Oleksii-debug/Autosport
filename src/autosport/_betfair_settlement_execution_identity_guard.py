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

The checks are deliberately bounded.  They do not equate PARTIAL acknowledgement
quantity with eventual settlement quantity, do not infer fill price from requested
price, and do not claim permanent finality or complete terminal-state authority.
"""

from __future__ import annotations

from . import betfair_settlement_revisions as _settlement


_ERROR = _settlement.BetfairSettlementRevisionError
_ORIGINAL_MATCH_ORDER = _settlement._match_order
_ORIGINAL_MATCH_ORDER_CODE = getattr(_ORIGINAL_MATCH_ORDER, "__code__", None)
_CANONICAL_TIME = _settlement._time
_CANONICAL_TIME_CODE = getattr(_CANONICAL_TIME, "__code__", None)

if _ORIGINAL_MATCH_ORDER_CODE is None or _CANONICAL_TIME_CODE is None:
    raise RuntimeError("Betfair settlement execution identity dispatch is unavailable")


def _require_dispatch() -> None:
    if (
        _settlement._match_order is not _match_order_with_execution_identity
        or getattr(_ORIGINAL_MATCH_ORDER, "__code__", None) is not _ORIGINAL_MATCH_ORDER_CODE
        or _settlement._time is not _CANONICAL_TIME
        or getattr(_CANONICAL_TIME, "__code__", None) is not _CANONICAL_TIME_CODE
    ):
        raise _ERROR("settlement execution identity dispatch changed")


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


if _settlement._match_order is not _ORIGINAL_MATCH_ORDER:
    raise RuntimeError("Betfair settlement match-order dispatch changed before guard install")
_settlement._match_order = _match_order_with_execution_identity

__all__: list[str] = []
