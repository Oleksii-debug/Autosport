"""Preserve correction-relevant Betfair cleared-order facts in #1272.

The current-main read-only adapter intentionally normalizes provider rows into a small
stable DTO and still omits documented BET-level ``betOutcome``, ``handicap`` and
``voidedDate`` fields.  The active canonical readback successor (#1496/#1324 lineage)
already adds those three fields natively.  Settlement revision authority must work on
both sides of that integration boundary without creating a competing provider client or
second provider-truth schema.

On an adapter that already exposes the native correction fields this composition layer
consumes that exact DTO unchanged.  On today's main, it temporarily carries the same
validated fields through one private subtype at the existing parser seam.  In both
cases the settlement revision persists readable normalized correction facts and hashes
them into immutable revision content identity.  Existing raw response/capture evidence
hashes retain their original meaning; JSON-RPC request ids therefore cannot manufacture
semantic revisions.

No provider write, settlement-finality or real-money capability is introduced.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal

from . import betfair_account_readonly as _adapter
from . import betfair_settlement_revisions as _settlement


_BASE_ORDER = _adapter.BetfairClearedOrderObservation
_BASE_EVIDENCE = _adapter.BetfairEvidence
_ORIGINAL_PARSE = _adapter._parse_cleared_order
_ORIGINAL_SEMANTIC_PAYLOAD = _settlement._semantic_payload
_BASE_REVISION = _settlement.BetfairSettlementRevision
_STORE_TYPE = _settlement.BetfairSettlementRevisionStore
_ORIGINAL_INGEST = _STORE_TYPE.ingest
_NO_FACT_CONTEXT = object()
_MISSING_FACT = object()
_NATIVE_CORRECTION_FIELDS = all(
    name in getattr(_BASE_ORDER, "__dataclass_fields__", {})
    for name in ("bet_outcome", "handicap", "voided_date")
)


@dataclass(frozen=True, slots=True)
class _ProviderCorrectionFacts:
    bet_outcome: str | None
    provider_handicap: Decimal | None
    provider_voided_date: str | None


_FACT_CONTEXT: ContextVar[object] = ContextVar(
    "autosport_betfair_settlement_provider_correction_facts",
    default=_NO_FACT_CONTEXT,
)


if _NATIVE_CORRECTION_FIELDS:
    _ORDER_WITH_FACTS_TYPE = _BASE_ORDER
else:

    @dataclass(frozen=True, slots=True)
    class _ClearedOrderWithCorrectionFacts(_BASE_ORDER):
        bet_outcome: str | None = None
        handicap: Decimal | None = None
        voided_date: str | None = None

        def __post_init__(self) -> None:
            _BASE_ORDER.__post_init__(self)
            _adapter._optional_text(self.bet_outcome, "bet_outcome")
            if self.handicap is not None:
                _adapter._decimal(self.handicap, "handicap")
            if self.voided_date is not None:
                _adapter._iso_timestamp(self.voided_date, "voided_date")

    _ORDER_WITH_FACTS_TYPE = _ClearedOrderWithCorrectionFacts


@dataclass(frozen=True, slots=True)
class _SettlementRevisionWithCorrectionFacts(_BASE_REVISION):
    bet_outcome: object = _MISSING_FACT
    provider_handicap: object = _MISSING_FACT
    provider_voided_date: object = _MISSING_FACT

    def __post_init__(self) -> None:
        values = (
            self.bet_outcome,
            self.provider_handicap,
            self.provider_voided_date,
        )
        missing = tuple(value is _MISSING_FACT for value in values)
        if any(missing):
            if not all(missing):
                raise _settlement.BetfairSettlementRevisionError(
                    "settlement revision provider correction facts are incomplete"
                )
            context = _FACT_CONTEXT.get()
            if type(context) is not _ProviderCorrectionFacts:
                raise _settlement.BetfairSettlementRevisionError(
                    "settlement revision lacks provider correction-fact context"
                )
            object.__setattr__(self, "bet_outcome", context.bet_outcome)
            object.__setattr__(self, "provider_handicap", context.provider_handicap)
            object.__setattr__(
                self,
                "provider_voided_date",
                context.provider_voided_date,
            )

        if self.bet_outcome is not None:
            _settlement._text(self.bet_outcome, "bet_outcome")
        if self.provider_handicap is not None:
            _settlement._dec(self.provider_handicap, "provider_handicap")
        if self.provider_voided_date is not None:
            _settlement._time(self.provider_voided_date, "provider_voided_date")
        _BASE_REVISION.__post_init__(self)

    def semantic_payload(self) -> dict[str, object]:
        payload: dict[str, object] = dict(_BASE_REVISION.semantic_payload(self))
        payload["bet_outcome"] = self.bet_outcome
        payload["provider_handicap"] = (
            None
            if self.provider_handicap is None
            else format(self.provider_handicap, "f")
        )
        payload["provider_voided_date"] = self.provider_voided_date
        return payload

    @classmethod
    def from_dict(cls, raw: object) -> "_SettlementRevisionWithCorrectionFacts":
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
            "provider_handicap",
            "provider_voided_date",
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
        if values["provider_handicap"] is not None:
            values["provider_handicap"] = _settlement._dec(
                values["provider_handicap"],
                "provider_handicap",
            )
        if values["bet_outcome"] is not None:
            _settlement._text(values["bet_outcome"], "bet_outcome")
        if values["provider_voided_date"] is not None:
            _settlement._time(
                values["provider_voided_date"],
                "provider_voided_date",
            )
        return cls(**values)


def _parse_cleared_order_with_correction_facts(
    value: object,
    evidence: _BASE_EVIDENCE,
    index: int,
    bet_status: str,
) -> _BASE_ORDER:
    order = _ORIGINAL_PARSE(value, evidence, index, bet_status)
    if _NATIVE_CORRECTION_FIELDS:
        return order

    raw = _adapter._mapping(value, f"clearedOrders[{index}]")
    outcome = _adapter._provider_optional_text(raw, "betOutcome", "bet_outcome")
    handicap = None
    if raw.get("handicap") is not None:
        handicap = _adapter._number(raw, "handicap", "handicap")
    voided_date = _adapter._provider_optional_text(
        raw,
        "voidedDate",
        "voided_date",
    )
    if voided_date is not None:
        _adapter._iso_timestamp(voided_date, "voided_date")
    return _ORDER_WITH_FACTS_TYPE(
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
        handicap=handicap,
        voided_date=voided_date,
    )


def _correction_facts(order) -> _ProviderCorrectionFacts:
    if type(order) is not _ORDER_WITH_FACTS_TYPE:
        raise _settlement.BetfairSettlementRevisionError(
            "settlement cleared row lacks canonical provider correction facts"
        )
    return _ProviderCorrectionFacts(
        bet_outcome=order.bet_outcome,
        provider_handicap=order.handicap,
        provider_voided_date=order.voided_date,
    )


def _semantic_payload_with_correction_facts(
    action,
    capture,
    order,
    plan_id: str,
    attempt_id: str,
):
    facts = _correction_facts(order)
    _FACT_CONTEXT.set(facts)
    payload = _ORIGINAL_SEMANTIC_PAYLOAD(
        action,
        capture,
        order,
        plan_id,
        attempt_id,
    )
    payload["bet_outcome"] = facts.bet_outcome
    payload["provider_handicap"] = (
        None
        if facts.provider_handicap is None
        else format(facts.provider_handicap, "f")
    )
    payload["provider_voided_date"] = facts.provider_voided_date
    return payload


def _ingest_with_correction_fact_context(
    self,
    ledger,
    *,
    plan_id: str,
    attempt_id: str,
    action,
    capture,
):
    token = _FACT_CONTEXT.set(_NO_FACT_CONTEXT)
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
        _FACT_CONTEXT.reset(token)


if _adapter._parse_cleared_order is not _ORIGINAL_PARSE:
    raise RuntimeError("Betfair cleared-order parser changed before correction-fact install")
if _settlement._semantic_payload is not _ORIGINAL_SEMANTIC_PAYLOAD:
    raise RuntimeError("Betfair settlement semantic payload changed before correction-fact install")
if _settlement.BetfairSettlementRevision is not _BASE_REVISION:
    raise RuntimeError("Betfair settlement revision type changed before correction-fact install")
if _STORE_TYPE.ingest is not _ORIGINAL_INGEST:
    raise RuntimeError("Betfair settlement ingest changed before correction-fact install")

if not _NATIVE_CORRECTION_FIELDS:
    _adapter._parse_cleared_order = _parse_cleared_order_with_correction_facts
_settlement._semantic_payload = _semantic_payload_with_correction_facts
_settlement.BetfairSettlementRevision = _SettlementRevisionWithCorrectionFacts
_STORE_TYPE.ingest = _ingest_with_correction_fact_context

__all__: list[str] = []
