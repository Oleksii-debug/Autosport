"""Preserve lossless Betfair settlement facts and currency-qualified provider profit.

Current main still omits documented cleared-order ``betOutcome``, ``handicap`` and
``voidedDate`` fields while active canonical successor #1496/#1324 already carries
those fields natively.  This composition consumes the native DTO when present and,
only on today's older adapter surface, carries the same validated facts through one
private observation subtype.  It never creates a second provider client/parser stack.

Betfair cleared-order ``profit`` is a provider number, but it is not exact money until
an authenticated account-details read proves the account currency.  The adapter already
owns both read seams.  ``read_currency_qualified_execution_readback`` performs those two
existing canonical reads on the exact same client, binds the account-details result to
the exact adapter-issued execution capture in a private single-capture registry, and
returns only that capture.  Settlement semantic projection fails closed when a capture
was not issued through this currency-qualified seam.  Caller-created account-detail
DTOs, post-hoc reads, account labels and locale cannot mint currency authority.

The persisted revision exposes ``bet_outcome``, ``provider_handicap``,
``provider_voided_date`` and ``provider_profit_currency`` as separate normalized facts.
Those facts participate in immutable content identity while raw response/capture hashes
retain their original evidence meaning.  No provider write, settlement-finality or
real-money capability is introduced.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal
from threading import Lock
from weakref import ref

from . import betfair_account_readonly as _adapter
from . import betfair_settlement_revisions as _settlement


_BASE_ORDER = _adapter.BetfairClearedOrderObservation
_BASE_EVIDENCE = _adapter.BetfairEvidence
_DETAILS_TYPE = _adapter.BetfairAccountDetailsObservation
_CLIENT_TYPE = _adapter.BetfairReadOnlyClient
_CAPTURE_TYPE = _adapter.BetfairExecutionReadbackEnvelope
_ORIGINAL_PARSE = _adapter._parse_cleared_order
_ORIGINAL_DETAILS_READ = _CLIENT_TYPE.read_account_details
_ORIGINAL_EXECUTION_READ = _CLIENT_TYPE.read_execution_readback
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
    provider_profit_currency: str


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


def _currency_code(value: object) -> str:
    raw = _settlement._text(value, "provider_profit_currency")
    if raw != raw.upper() or not raw.isascii() or not raw.isalnum():
        raise _settlement.BetfairSettlementRevisionError(
            "provider_profit_currency must be uppercase ASCII alphanumeric provider currency"
        )
    return raw


def _details_fingerprint(details: _DETAILS_TYPE) -> tuple[object, ...]:
    if type(details) is not _DETAILS_TYPE:
        raise _settlement.BetfairSettlementRevisionError(
            "settlement currency evidence is not canonical account details"
        )
    return (
        _currency_code(details.currency_code),
        details.locale_code,
        details.region,
        details.timezone_name,
        details.evidence.observed_at,
        details.evidence.source_payload_sha256,
    )


def _install_currency_bridge():
    issued: dict[int, tuple[object, _DETAILS_TYPE, tuple[object, ...]]] = {}
    lock = Lock()

    def forget_capture(capture_id: int):
        def forget(_dead) -> None:
            with lock:
                issued.pop(capture_id, None)

        return forget

    def read_currency_qualified_execution_readback(
        client,
        *,
        action_id: str,
        market_id: str,
        provider_order_ref: str | None = None,
        page_size: int = 200,
    ):
        if type(client) is not _CLIENT_TYPE:
            raise _settlement.BetfairSettlementRevisionError(
                "settlement currency acquisition requires exact BetfairReadOnlyClient"
            )
        try:
            state = vars(client)
        except TypeError as exc:
            raise _settlement.BetfairSettlementRevisionError(
                "settlement currency client state is unavailable"
            ) from exc
        if any(
            name in state
            for name in ("read_account_details", "read_execution_readback")
        ):
            raise _settlement.BetfairSettlementRevisionError(
                "settlement currency client read dispatch is instance-shadowed"
            )
        if (
            _CLIENT_TYPE.read_account_details is not _ORIGINAL_DETAILS_READ
            or _CLIENT_TYPE.read_execution_readback is not _ORIGINAL_EXECUTION_READ
        ):
            raise _settlement.BetfairSettlementRevisionError(
                "settlement currency client read dispatch changed"
            )

        details = _ORIGINAL_DETAILS_READ(client)
        details_fingerprint = _details_fingerprint(details)
        capture = _ORIGINAL_EXECUTION_READ(
            client,
            action_id=action_id,
            market_id=market_id,
            provider_order_ref=provider_order_ref,
            page_size=page_size,
        )
        if type(capture) is not _CAPTURE_TYPE:
            raise _settlement.BetfairSettlementRevisionError(
                "settlement readback is not canonical Betfair execution capture"
            )
        try:
            capture.assert_authoritative()
        except _adapter.BetfairReadOnlyError as exc:
            raise _settlement.BetfairSettlementRevisionError(
                "settlement readback is not canonical adapter-issued evidence"
            ) from exc
        if (
            capture.venue_id != state.get("_venue_id")
            or capture.account_id != state.get("_account_id")
            or capture.adapter_id != _adapter.ADAPTER_ID
            or capture.adapter_version != _adapter.ADAPTER_VERSION
        ):
            raise _settlement.BetfairSettlementRevisionError(
                "settlement currency/readback account identity mismatch"
            )
        if _settlement._time(
            details.evidence.observed_at,
            "currency observed_at",
        ) > _settlement._time(capture.observed_at, "capture observed_at"):
            raise _settlement.BetfairSettlementRevisionError(
                "settlement currency evidence is causally later than readback"
            )

        capture_id = id(capture)
        capture_ref = ref(capture, forget_capture(capture_id))
        with lock:
            issued[capture_id] = (
                capture_ref,
                details,
                details_fingerprint,
            )
        return capture

    def currency_for_capture(capture) -> str:
        if type(capture) is not _CAPTURE_TYPE:
            raise _settlement.BetfairSettlementRevisionError(
                "settlement currency authority requires exact execution capture"
            )
        with lock:
            record = issued.get(id(capture))
        if record is None or record[0]() is not capture:
            raise _settlement.BetfairSettlementRevisionError(
                "settlement provider profit lacks authenticated currency authority"
            )
        details = record[1]
        if _details_fingerprint(details) != record[2]:
            raise _settlement.BetfairSettlementRevisionError(
                "settlement currency evidence changed after acquisition"
            )
        if _settlement._time(
            details.evidence.observed_at,
            "currency observed_at",
        ) > _settlement._time(capture.observed_at, "capture observed_at"):
            raise _settlement.BetfairSettlementRevisionError(
                "settlement currency evidence is causally later than readback"
            )
        return _currency_code(details.currency_code)

    return read_currency_qualified_execution_readback, currency_for_capture


(
    read_currency_qualified_execution_readback,
    _currency_for_capture,
) = _install_currency_bridge()


@dataclass(frozen=True, slots=True)
class _SettlementRevisionWithCorrectionFacts(_BASE_REVISION):
    bet_outcome: object = _MISSING_FACT
    provider_handicap: object = _MISSING_FACT
    provider_voided_date: object = _MISSING_FACT
    provider_profit_currency: object = _MISSING_FACT

    def __post_init__(self) -> None:
        values = (
            self.bet_outcome,
            self.provider_handicap,
            self.provider_voided_date,
            self.provider_profit_currency,
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
            object.__setattr__(
                self,
                "provider_profit_currency",
                context.provider_profit_currency,
            )

        if self.bet_outcome is not None:
            _settlement._text(self.bet_outcome, "bet_outcome")
        if self.provider_handicap is not None:
            _settlement._dec(self.provider_handicap, "provider_handicap")
        if self.provider_voided_date is not None:
            _settlement._time(self.provider_voided_date, "provider_voided_date")
        _currency_code(self.provider_profit_currency)
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
        payload["provider_profit_currency"] = self.provider_profit_currency
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
            "provider_profit_currency",
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
        values["provider_profit_currency"] = _currency_code(
            values["provider_profit_currency"]
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


def _correction_facts(order, capture) -> _ProviderCorrectionFacts:
    if type(order) is not _ORDER_WITH_FACTS_TYPE:
        raise _settlement.BetfairSettlementRevisionError(
            "settlement cleared row lacks canonical provider correction facts"
        )
    return _ProviderCorrectionFacts(
        bet_outcome=order.bet_outcome,
        provider_handicap=order.handicap,
        provider_voided_date=order.voided_date,
        provider_profit_currency=_currency_for_capture(capture),
    )


def _semantic_payload_with_correction_facts(
    action,
    capture,
    order,
    plan_id: str,
    attempt_id: str,
):
    facts = _correction_facts(order, capture)
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
    payload["provider_profit_currency"] = facts.provider_profit_currency
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
if _CLIENT_TYPE.read_account_details is not _ORIGINAL_DETAILS_READ:
    raise RuntimeError("Betfair account-details dispatch changed before settlement currency install")
if _CLIENT_TYPE.read_execution_readback is not _ORIGINAL_EXECUTION_READ:
    raise RuntimeError("Betfair execution-readback dispatch changed before settlement currency install")

if not _NATIVE_CORRECTION_FIELDS:
    _adapter._parse_cleared_order = _parse_cleared_order_with_correction_facts
_settlement._semantic_payload = _semantic_payload_with_correction_facts
_settlement.BetfairSettlementRevision = _SettlementRevisionWithCorrectionFacts
_settlement.read_currency_qualified_execution_readback = (
    read_currency_qualified_execution_readback
)
_STORE_TYPE.ingest = _ingest_with_correction_fact_context

__all__ = ["read_currency_qualified_execution_readback"]
