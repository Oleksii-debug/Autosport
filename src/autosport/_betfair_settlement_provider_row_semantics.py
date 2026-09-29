"""Preserve correction-relevant Betfair cleared-order facts in #1272.

The read-only adapter intentionally normalizes provider rows into a small stable DTO.
Betfair ``betOutcome`` is correction-relevant settlement truth, however, and was being
discarded before the append-only settlement revision authority computed content
identity.  Raw JSON-RPC response hashes cannot substitute for this fact because their
request/response ids change on otherwise identical rereads.

This composition layer keeps the existing adapter, capture issuance registry and
settlement store as the sole authorities.  The canonical parser carries the validated
optional provider ``betOutcome`` through one private observation subtype.  The existing
settlement revision type is extended in-place at package composition with a persisted,
readable ``bet_outcome`` fact; revision content identity includes that fact while the
existing raw response and capture evidence hashes keep their original meaning.

No provider write, settlement-finality or real-money capability is introduced.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass

from . import betfair_account_readonly as _adapter
from . import betfair_settlement_revisions as _settlement


_BASE_ORDER = _adapter.BetfairClearedOrderObservation
_BASE_EVIDENCE = _adapter.BetfairEvidence
_ORIGINAL_PARSE = _adapter._parse_cleared_order
_ORIGINAL_SEMANTIC_PAYLOAD = _settlement._semantic_payload
_BASE_REVISION = _settlement.BetfairSettlementRevision
_STORE_TYPE = _settlement.BetfairSettlementRevisionStore
_ORIGINAL_INGEST = _STORE_TYPE.ingest
_NO_OUTCOME_CONTEXT = object()
_MISSING_OUTCOME = object()
_OUTCOME_CONTEXT: ContextVar[object] = ContextVar(
    "autosport_betfair_settlement_outcome",
    default=_NO_OUTCOME_CONTEXT,
)


@dataclass(frozen=True, slots=True)
class _ClearedOrderWithOutcome(_BASE_ORDER):
    bet_outcome: str | None = None

    def __post_init__(self) -> None:
        _BASE_ORDER.__post_init__(self)
        _adapter._optional_text(self.bet_outcome, "bet_outcome")


@dataclass(frozen=True, slots=True)
class _SettlementRevisionWithOutcome(_BASE_REVISION):
    bet_outcome: object = _MISSING_OUTCOME

    def __post_init__(self) -> None:
        outcome = self.bet_outcome
        if outcome is _MISSING_OUTCOME:
            outcome = _OUTCOME_CONTEXT.get()
            if outcome is _NO_OUTCOME_CONTEXT:
                raise _settlement.BetfairSettlementRevisionError(
                    "settlement revision lacks provider betOutcome context"
                )
            object.__setattr__(self, "bet_outcome", outcome)
        if outcome is not None:
            _settlement._text(outcome, "bet_outcome")
        _BASE_REVISION.__post_init__(self)

    def semantic_payload(self) -> dict[str, str | None]:
        payload = _BASE_REVISION.semantic_payload(self)
        payload["bet_outcome"] = self.bet_outcome  # type: ignore[assignment]
        return payload

    @classmethod
    def from_dict(cls, raw: object) -> "_SettlementRevisionWithOutcome":
        if type(raw) is not dict:
            raise _settlement.BetfairSettlementRevisionError(
                "revision must be JSON object"
            )
        required = {
            "revision_id",
            "previous_revision_id",
            "revision_number",
            "bookmaker_id",
            "account_id",
            "adapter_id",
            "adapter_version",
            "plan_id",
            "action_id",
            "attempt_id",
            "external_bet_id",
            "event_id",
            "market_id",
            "selection_id",
            "side",
            "provider_status",
            "placed_date",
            "settled_date",
            "price_requested",
            "price_matched",
            "size_settled",
            "provider_profit",
            "available_at",
            "source_payload_sha256",
            "capture_evidence_sha256",
            "content_sha256",
            "bet_outcome",
        }
        if set(raw) != required:
            raise _settlement.BetfairSettlementRevisionError(
                "revision fields are not canonical"
            )
        values = dict(raw)
        for field in (
            "price_requested",
            "price_matched",
            "size_settled",
            "provider_profit",
        ):
            values[field] = _settlement._dec(values[field], field)
        outcome = values["bet_outcome"]
        if outcome is not None:
            _settlement._text(outcome, "bet_outcome")
        return cls(**values)


def _parse_cleared_order_with_outcome(
    value: object,
    evidence: _BASE_EVIDENCE,
    index: int,
    bet_status: str,
) -> _BASE_ORDER:
    order = _ORIGINAL_PARSE(value, evidence, index, bet_status)
    raw = _adapter._mapping(value, f"clearedOrders[{index}]")
    outcome = _adapter._provider_optional_text(raw, "betOutcome", "bet_outcome")
    return _ClearedOrderWithOutcome(
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
        bet_outcome=outcome,
    )


def _semantic_payload_with_outcome(
    action,
    capture,
    order,
    plan_id: str,
    attempt_id: str,
):
    if type(order) is not _ClearedOrderWithOutcome:
        raise _settlement.BetfairSettlementRevisionError(
            "settlement cleared row lacks canonical provider betOutcome semantics"
        )
    outcome = order.bet_outcome
    _OUTCOME_CONTEXT.set(outcome)
    payload = _ORIGINAL_SEMANTIC_PAYLOAD(
        action,
        capture,
        order,
        plan_id,
        attempt_id,
    )
    payload["bet_outcome"] = outcome
    return payload


def _ingest_with_outcome_context(
    self,
    ledger,
    *,
    plan_id: str,
    attempt_id: str,
    action,
    capture,
):
    token = _OUTCOME_CONTEXT.set(_NO_OUTCOME_CONTEXT)
    try:
        return _ORIGINAL_INGEST(
            self,
            ledger,
            plan_id=plan_id,
            attempt_id=attempt_id,
            action=action,
            capture=capture,
        )
    finally:
        _OUTCOME_CONTEXT.reset(token)


if _adapter._parse_cleared_order is not _ORIGINAL_PARSE:
    raise RuntimeError("Betfair cleared-order parser changed before outcome install")
if _settlement._semantic_payload is not _ORIGINAL_SEMANTIC_PAYLOAD:
    raise RuntimeError("Betfair settlement semantic payload changed before outcome install")
if _settlement.BetfairSettlementRevision is not _BASE_REVISION:
    raise RuntimeError("Betfair settlement revision type changed before outcome install")
if _STORE_TYPE.ingest is not _ORIGINAL_INGEST:
    raise RuntimeError("Betfair settlement ingest changed before outcome install")

_adapter._parse_cleared_order = _parse_cleared_order_with_outcome
_settlement._semantic_payload = _semantic_payload_with_outcome
_settlement.BetfairSettlementRevision = _SettlementRevisionWithOutcome
_STORE_TYPE.ingest = _ingest_with_outcome_context

__all__: list[str] = []
