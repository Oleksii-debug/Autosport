"""Preserve correction-relevant Betfair cleared-order row semantics in #1272.

The canonical read-only adapter binds every order to the exact JSON-RPC response hash,
but that response hash also includes the per-request JSON-RPC id and therefore changes
on an otherwise identical re-read.  Conversely, the normalized settlement projection
historically discarded provider fields such as ``betOutcome`` before revision identity
was computed.

This composition layer keeps the existing adapter and settlement store as the sole
authorities.  It captures a stable canonical digest of the exact provider cleared-order
row before normalization, carries that digest only as an internal observation subtype,
and makes the settlement projection persist that row digest in its existing
``source_payload_sha256`` field.  Revision content identity includes the same digest.
Thus a real provider-row correction (including betOutcome-only changes) creates a new
revision, while an identical row observed through a later JSON-RPC request id remains
idempotent.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import betfair_account_readonly as _adapter
from . import betfair_settlement_revisions as _settlement


_BASE_ORDER = _adapter.BetfairClearedOrderObservation
_BASE_EVIDENCE = _adapter.BetfairEvidence
_ORIGINAL_PARSE = _adapter._parse_cleared_order
_ORIGINAL_MATCH = _settlement._match_order
_ORIGINAL_INGEST_PAYLOAD = _settlement._semantic_payload
_REVISION_TYPE = _settlement.BetfairSettlementRevision
_ORIGINAL_REVISION_PAYLOAD = _REVISION_TYPE.semantic_payload


@dataclass(frozen=True, slots=True)
class _ClearedOrderWithProviderRowDigest(_BASE_ORDER):
    provider_row_sha256: str = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        _settlement._sha(self.provider_row_sha256, "provider_row_sha256")


def _parse_cleared_order_with_provider_row_digest(
    value: object,
    evidence: _BASE_EVIDENCE,
    index: int,
    bet_status: str,
) -> _BASE_ORDER:
    order = _ORIGINAL_PARSE(value, evidence, index, bet_status)
    raw = _adapter._mapping(value, f"clearedOrders[{index}]")
    row_sha256 = _adapter._canonical_sha256(raw)
    return _ClearedOrderWithProviderRowDigest(
        bet_id=order.bet_id,
        market_id=order.market_id,
        selection_id=order.selection_id,
        side=order.side,
        bet_status=order.bet_status,
        placed_date=order.placed_date,
        settled_date=order.settled_date,
        price_requested=order.price_requested,
        price_matched=order.price_matched,
        size_settled=order.size_settled,
        profit=order.profit,
        customer_order_ref=order.customer_order_ref,
        customer_strategy_ref=order.customer_strategy_ref,
        evidence=order.evidence,
        event_id=order.event_id,
        provider_row_sha256=row_sha256,
    )


def _match_order_with_provider_row_digest(action, capture):
    order = _ORIGINAL_MATCH(action, capture)
    if type(order) is not _ClearedOrderWithProviderRowDigest:
        raise _settlement.BetfairSettlementRevisionError(
            "settlement cleared row lacks canonical provider-row identity"
        )
    row_evidence = _BASE_EVIDENCE(
        observed_at=order.evidence.observed_at,
        source_payload_sha256=order.provider_row_sha256,
    )
    return _BASE_ORDER(
        bet_id=order.bet_id,
        market_id=order.market_id,
        selection_id=order.selection_id,
        side=order.side,
        bet_status=order.bet_status,
        placed_date=order.placed_date,
        settled_date=order.settled_date,
        price_requested=order.price_requested,
        price_matched=order.price_matched,
        size_settled=order.size_settled,
        profit=order.profit,
        customer_order_ref=order.customer_order_ref,
        customer_strategy_ref=order.customer_strategy_ref,
        evidence=row_evidence,
        event_id=order.event_id,
    )


def _semantic_payload_with_provider_row(
    action,
    capture,
    order,
    plan_id: str,
    attempt_id: str,
):
    payload = _ORIGINAL_INGEST_PAYLOAD(
        action,
        capture,
        order,
        plan_id,
        attempt_id,
    )
    payload["source_payload_sha256"] = _settlement._sha(
        order.evidence.source_payload_sha256,
        "source_payload_sha256",
    )
    return payload


def _revision_semantic_payload_with_provider_row(self):
    payload = _ORIGINAL_REVISION_PAYLOAD(self)
    payload["source_payload_sha256"] = _settlement._sha(
        self.source_payload_sha256,
        "source_payload_sha256",
    )
    return payload


if _adapter._parse_cleared_order is not _ORIGINAL_PARSE:
    raise RuntimeError("Betfair cleared-order parser changed before row-semantics install")
if _settlement._match_order is not _ORIGINAL_MATCH:
    raise RuntimeError("Betfair settlement match dispatch changed before row-semantics install")
if _settlement._semantic_payload is not _ORIGINAL_INGEST_PAYLOAD:
    raise RuntimeError("Betfair settlement semantic payload changed before row-semantics install")
if _REVISION_TYPE.semantic_payload is not _ORIGINAL_REVISION_PAYLOAD:
    raise RuntimeError("Betfair settlement revision payload changed before row-semantics install")

_adapter._parse_cleared_order = _parse_cleared_order_with_provider_row_digest
_settlement._match_order = _match_order_with_provider_row_digest
_settlement._semantic_payload = _semantic_payload_with_provider_row
_REVISION_TYPE.semantic_payload = _revision_semantic_payload_with_provider_row

__all__: list[str] = []
