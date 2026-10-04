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

from . import _betfair_settlement_provider_row_semantics as _provider_semantics
from . import betfair_settlement_revisions as _settlement


_ERROR = _settlement.BetfairSettlementRevisionError
_LEDGER_TYPE = _settlement.RealExecutionLedger
_ATTEMPT_STATE = _settlement.AttemptState
_RECEIPT_TYPE = _settlement.ExternalReceiptIdentity
_LEDGER_ERROR = _settlement.ExecutionLedgerError
_ACTION_TYPE = _settlement.ExecutionAction
_CAPTURE_TYPE = _settlement.BetfairExecutionReadbackEnvelope
_CAPTURE_ERROR = _settlement.BetfairReadOnlyError
_STORE_TYPE = _settlement.BetfairSettlementRevisionStore

_ORIGINAL_INGEST = _STORE_TYPE.ingest
_ORIGINAL_INGEST_CODE = getattr(_ORIGINAL_INGEST, "__code__", None)
_PROVIDER_INGEST_DELEGATE = getattr(_provider_semantics, "_ORIGINAL_INGEST", None)
_PROVIDER_INGEST_DELEGATE_CODE = getattr(
    _PROVIDER_INGEST_DELEGATE,
    "__code__",
    None,
)
_STORE_RELOAD = vars(_STORE_TYPE).get("_reload")
_STORE_RELOAD_CODE = getattr(_STORE_RELOAD, "__code__", None)
_STORE_APPEND = vars(_STORE_TYPE).get("_append")
_STORE_APPEND_CODE = getattr(_STORE_APPEND, "__code__", None)
_ACTION_TO_DICT = vars(_ACTION_TYPE).get("to_dict")
_ACTION_TO_DICT_CODE = getattr(_ACTION_TO_DICT, "__code__", None)
_CAPTURE_VERIFY = vars(_CAPTURE_TYPE).get("assert_authoritative")
_CAPTURE_VERIFY_CODE = getattr(_CAPTURE_VERIFY, "__code__", None)
_CAPTURE_FINGERPRINT = vars(_CAPTURE_TYPE).get("_authority_fingerprint")
_CAPTURE_FINGERPRINT_CODE = getattr(_CAPTURE_FINGERPRINT, "__code__", None)
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
_LEDGER_DISPATCH_CODE = {
    name: getattr(value, "__code__", None)
    for name, value in _LEDGER_DISPATCH.items()
}

if (
    _ORIGINAL_INGEST_CODE is None
    or not callable(_PROVIDER_INGEST_DELEGATE)
    or _PROVIDER_INGEST_DELEGATE_CODE is None
    or not callable(_STORE_RELOAD)
    or _STORE_RELOAD_CODE is None
    or not callable(_STORE_APPEND)
    or _STORE_APPEND_CODE is None
    or not callable(_ACTION_TO_DICT)
    or _ACTION_TO_DICT_CODE is None
    or not callable(_CAPTURE_VERIFY)
    or _CAPTURE_VERIFY_CODE is None
    or not callable(_CAPTURE_FINGERPRINT)
    or _CAPTURE_FINGERPRINT_CODE is None
    or _ORIGINAL_MATCH_ORDER_CODE is None
    or _ORIGINAL_REQUIRE_OWNER_CODE is None
    or _CANONICAL_TIME_CODE is None
    or any(value is None for value in _LEDGER_DISPATCH.values())
    or any(value is None for value in _LEDGER_DISPATCH_CODE.values())
):
    raise RuntimeError("Betfair settlement execution identity dispatch is unavailable")


def _require_dispatch() -> None:
    if (
        _STORE_TYPE.ingest is not _ingest_with_exact_authority
        or getattr(_ORIGINAL_INGEST, "__code__", None) is not _ORIGINAL_INGEST_CODE
        or getattr(_provider_semantics, "_ORIGINAL_INGEST", None)
        is not _PROVIDER_INGEST_DELEGATE
        or getattr(_PROVIDER_INGEST_DELEGATE, "__code__", None)
        is not _PROVIDER_INGEST_DELEGATE_CODE
        or vars(_STORE_TYPE).get("_reload") is not _STORE_RELOAD
        or getattr(_STORE_RELOAD, "__code__", None) is not _STORE_RELOAD_CODE
        or vars(_STORE_TYPE).get("_append") is not _STORE_APPEND
        or getattr(_STORE_APPEND, "__code__", None) is not _STORE_APPEND_CODE
        or vars(_ACTION_TYPE).get("to_dict") is not _ACTION_TO_DICT
        or getattr(_ACTION_TO_DICT, "__code__", None) is not _ACTION_TO_DICT_CODE
        or vars(_CAPTURE_TYPE).get("assert_authoritative") is not _CAPTURE_VERIFY
        or getattr(_CAPTURE_VERIFY, "__code__", None) is not _CAPTURE_VERIFY_CODE
        or vars(_CAPTURE_TYPE).get("_authority_fingerprint") is not _CAPTURE_FINGERPRINT
        or getattr(_CAPTURE_FINGERPRINT, "__code__", None)
        is not _CAPTURE_FINGERPRINT_CODE
        or _settlement.ExecutionAction is not _ACTION_TYPE
        or _settlement.BetfairExecutionReadbackEnvelope is not _CAPTURE_TYPE
        or _settlement.BetfairReadOnlyError is not _CAPTURE_ERROR
        or _settlement.BetfairSettlementRevisionStore is not _STORE_TYPE
        or _settlement._match_order is not _match_order_with_execution_identity
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
            or getattr(expected, "__code__", None) is not _LEDGER_DISPATCH_CODE[name]
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


def _require_exact_action_capture(action, capture) -> None:
    if type(action) is not _ACTION_TYPE:
        raise _ERROR("settlement action must be the exact ExecutionAction")
    if type(capture) is not _CAPTURE_TYPE:
        raise _ERROR(
            "settlement capture must be the exact BetfairExecutionReadbackEnvelope"
        )
    _require_dispatch()
    try:
        _CAPTURE_VERIFY(capture)
    except _CAPTURE_ERROR as exc:
        raise _ERROR(
            "settlement capture is not canonical adapter-issued evidence"
        ) from exc
    _require_dispatch()


def _ingest_with_exact_authority(
    self,
    ledger,
    *,
    plan_id,
    attempt_id,
    action,
    capture,
):
    _require_dispatch()
    _require_exact_action_capture(action, capture)
    result = _ORIGINAL_INGEST(
        self,
        ledger,
        plan_id=plan_id,
        attempt_id=attempt_id,
        action=action,
        capture=capture,
    )
    _require_dispatch()
    return result


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


if _STORE_TYPE.ingest is not _ORIGINAL_INGEST:
    raise RuntimeError("Betfair settlement ingest dispatch changed before guard install")
if _settlement._match_order is not _ORIGINAL_MATCH_ORDER:
    raise RuntimeError("Betfair settlement match-order dispatch changed before guard install")
if _settlement._require_attempt_receipt_owner is not _ORIGINAL_REQUIRE_OWNER:
    raise RuntimeError("Betfair settlement ledger-owner dispatch changed before guard install")
_STORE_TYPE.ingest = _ingest_with_exact_authority
_settlement._match_order = _match_order_with_execution_identity
_settlement._require_attempt_receipt_owner = _require_owner_with_exact_ledger_dispatch

# Install the credential-origin composition only after the settlement/currency and
# exact execution-identity guards above own their canonical dispatch seams.
from . import _betfair_settlement_credential_origin_guard as _credential_origin_guard

del _credential_origin_guard

__all__: list[str] = []
