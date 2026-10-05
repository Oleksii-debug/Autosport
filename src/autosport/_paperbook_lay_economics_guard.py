from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from decimal import Decimal, DecimalException, Inexact, localcontext

from . import paper as _paper
from .domain import PaperTicket, TicketLeg, TicketStatus
from .exchange_exposure import locked_capital_for_exchange_side
from .real_execution_ledger import _validate_decimal_text_resource_bound


_DECIMAL_TYPE = Decimal
_PAPER_TICKET_TYPE = PaperTicket
_TICKET_LEG_TYPE = TicketLeg
_TICKET_STATUS_TYPE = TicketStatus
_LOCALCONTEXT = localcontext
_CANONICAL_LOCALCONTEXT = _LOCALCONTEXT

_ORIGINAL_LOCKED_CAPITAL = locked_capital_for_exchange_side
_ORIGINAL_LOCKED_CAPITAL_CODE = _ORIGINAL_LOCKED_CAPITAL.__code__
_ORIGINAL_DECIMAL_RESOURCE_BOUND = _validate_decimal_text_resource_bound
_ORIGINAL_DECIMAL_RESOURCE_BOUND_CODE = _ORIGINAL_DECIMAL_RESOURCE_BOUND.__code__

_ORIGINAL_OPEN_TICKET = _paper.PaperBook.open_ticket
_ORIGINAL_SETTLE = _paper.PaperBook.settle
_ORIGINAL_VALIDATE_TICKET_LEG = _paper.PaperBook._validate_ticket_leg.__func__
_ORIGINAL_SETTLEMENT_RESULT = _paper.PaperBook._settlement_result.__func__
_ORIGINAL_VALIDATE_LIFECYCLE_REACHABILITY = (
    _paper.PaperBook._validate_lifecycle_reachability.__func__
)
_ORIGINAL_VALIDATE_LOADED_STATE = _paper.PaperBook._validate_loaded_state.__func__
_ORIGINAL_REQUIRE_FINITE = _paper.PaperBook._require_finite
_ORIGINAL_REQUIRE_CANONICAL_TEXT = _paper.PaperBook._require_canonical_text
_ORIGINAL_DEBIT_BALANCE = _paper.PaperBook._debit_balance.__func__
_ORIGINAL_DEBIT_BALANCE_CODE = _ORIGINAL_DEBIT_BALANCE.__code__
_ORIGINAL_VALIDATE_PLACED_AT = _paper.PaperBook._validate_placed_at.__func__
_ORIGINAL_REQUIRE_UTF8_STRING = _paper.PaperBook._require_utf8_string
_ORIGINAL_VALIDATE_TICKET_PROVENANCE = _paper.PaperBook._validate_ticket_provenance.__func__
_ORIGINAL_VALIDATE_LIFECYCLE_ENTRY = _paper.PaperBook._validate_lifecycle_entry.__func__
_ORIGINAL_VALIDATE_SETTLED_AT = _paper.PaperBook._validate_settled_at.__func__
_ORIGINAL_PARSE_ISO_TIMESTAMP = _paper.parse_iso_timestamp
_ORIGINAL_UTC_NOW_ISO = _paper.utc_now_iso
_ORIGINAL_UUID4 = _paper.uuid.uuid4
_CANONICAL_UUID4 = _ORIGINAL_UUID4
_ORIGINAL_UUID4_CODE = _ORIGINAL_UUID4.__code__
_ORIGINAL_UUID_TYPE = _paper.uuid.UUID
_ORIGINAL_UUID_STR = _ORIGINAL_UUID_TYPE.__str__
_ORIGINAL_UUID_STR_CODE = _ORIGINAL_UUID_STR.__code__
_ORIGINAL_PAPER_DECIMAL_CONTEXT = _paper._paper_decimal_context
_ORIGINAL_PAPER_DECIMAL_CONTEXT_CODE = _ORIGINAL_PAPER_DECIMAL_CONTEXT.__code__
_ORIGINAL_REQUIRE_TICKET_OPENING_AUTHORITY = _paper._require_ticket_opening_authority
_ORIGINAL_REQUIRE_CAUSAL_HISTORY_AUTHORITY = _paper._require_paperbook_causal_history_authority
_ORIGINAL_RECORD_TICKET_OPENING_AUTHORITY = _paper._record_ticket_opening_authority
_ORIGINAL_ADVANCE_CAUSAL_HISTORY_OPEN = _paper._advance_paperbook_causal_history_open
_ORIGINAL_ADVANCE_CAUSAL_HISTORY_SETTLE = _paper._advance_paperbook_causal_history_settle


def _require_decimal_arithmetic_authority() -> None:
    if (
        _LOCALCONTEXT is not _CANONICAL_LOCALCONTEXT
        or _ORIGINAL_PAPER_DECIMAL_CONTEXT.__code__
        is not _ORIGINAL_PAPER_DECIMAL_CONTEXT_CODE
    ):
        raise ValueError("PaperBook canonical Decimal arithmetic authority changed")


def _require_exact_decimal(value: object, label: str) -> Decimal:
    if type(value) is not _DECIMAL_TYPE:
        raise ValueError(f"PaperBook {label} must be an exact Decimal")
    if not value.is_finite():
        raise ValueError(f"PaperBook snapshot contains non-finite {label}")
    if _ORIGINAL_DECIMAL_RESOURCE_BOUND.__code__ is not _ORIGINAL_DECIMAL_RESOURCE_BOUND_CODE:
        raise ValueError("PaperBook canonical Decimal resource authority changed")
    try:
        _ORIGINAL_DECIMAL_RESOURCE_BOUND(value)
    except ValueError as exc:
        raise ValueError(
            f"PaperBook {label} exceeds canonical Decimal resource bounds"
        ) from exc
    if _ORIGINAL_DECIMAL_RESOURCE_BOUND.__code__ is not _ORIGINAL_DECIMAL_RESOURCE_BOUND_CODE:
        raise ValueError("PaperBook canonical Decimal resource authority changed")
    return value


def _require_exact_text(
    value: object,
    label: str,
    *,
    allow_empty: bool = False,
    forbid_quote_key_delimiter: bool = False,
) -> str:
    if type(value) is not str:
        raise ValueError(f"PaperBook {label} must be an exact string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"PaperBook {label} must be valid UTF-8 text") from exc
    if not allow_empty and (not value or value.strip() != value):
        raise ValueError(f"PaperBook {label} must be a non-empty trimmed string")
    if forbid_quote_key_delimiter and "|" in value:
        raise ValueError(
            f"PaperBook {label} must not contain quote-key delimiter '|'"
        )
    return value


def _validate_exact_timestamp(value: object, label: str) -> str:
    text = _require_exact_text(value, label)
    try:
        _ORIGINAL_PARSE_ISO_TIMESTAMP(text)
    except ValueError as exc:
        raise ValueError(
            f"PaperBook {label} must be a timezone-aware ISO timestamp"
        ) from exc
    return text


def _validate_exact_ticket_provenance(
    provider_source_ids: object,
    provider_accounts: object,
    bankroll_id: object,
    currency: object,
) -> tuple[
    tuple[str, ...],
    tuple[tuple[str, str], ...],
    str | None,
    str | None,
]:
    if type(provider_source_ids) is not tuple:
        raise ValueError("PaperBook provider_source_ids must be a canonical tuple")
    canonical_sources = tuple(
        _require_exact_text(source_id, "provider_source_id")
        for source_id in provider_source_ids
    )
    if (
        canonical_sources != tuple(sorted(canonical_sources))
        or len(canonical_sources) != len(set(canonical_sources))
    ):
        raise ValueError("PaperBook provider_source_ids must be sorted and unique")

    if type(provider_accounts) is not tuple:
        raise ValueError("PaperBook provider_accounts must be a canonical tuple")
    canonical_accounts_list: list[tuple[str, str]] = []
    for binding in provider_accounts:
        if type(binding) is not tuple or len(binding) != 2:
            raise ValueError(
                "PaperBook provider_accounts must contain (source_id, account_id) tuples"
            )
        source_id, account_id = binding
        canonical_accounts_list.append(
            (
                _require_exact_text(source_id, "provider account source_id"),
                _require_exact_text(account_id, "provider account_id"),
            )
        )
    canonical_accounts = tuple(canonical_accounts_list)
    if (
        canonical_accounts != tuple(sorted(canonical_accounts))
        or len(canonical_accounts) != len(set(canonical_accounts))
    ):
        raise ValueError("PaperBook provider_accounts must be sorted and unique")
    account_sources = tuple(source_id for source_id, _ in canonical_accounts)
    if len(account_sources) != len(set(account_sources)):
        raise ValueError(
            "PaperBook provider_accounts may bind at most one account_id per provider source"
        )
    if canonical_accounts and frozenset(account_sources) != frozenset(canonical_sources):
        raise ValueError(
            "PaperBook provider_accounts must cover provider_source_ids exactly"
        )

    if (bankroll_id is None) != (currency is None):
        raise ValueError(
            "PaperBook bankroll_id and currency provenance must be supplied together"
        )
    if bankroll_id is None:
        return canonical_sources, canonical_accounts, None, None

    canonical_bankroll = _require_exact_text(bankroll_id, "bankroll_id")
    canonical_currency = _require_exact_text(currency, "currency")
    if (
        len(canonical_currency) != 3
        or not canonical_currency.isascii()
        or not canonical_currency.isalpha()
        or canonical_currency != canonical_currency.upper()
    ):
        raise ValueError(
            "PaperBook currency provenance must be a three-letter uppercase ASCII code"
        )
    return canonical_sources, canonical_accounts, canonical_bankroll, canonical_currency


def _validate_exact_lifecycle_entry(entry: object):
    if type(entry) is not tuple or len(entry) != 4:
        raise ValueError("PaperBook lifecycle entries must be canonical tuples")
    action, ticket_id, winners, voids = entry
    if type(action) is not str or action not in {"open", "settle"}:
        raise ValueError("PaperBook lifecycle action must be open or settle")
    _require_exact_text(ticket_id, "lifecycle ticket_id")
    if type(winners) is not tuple or type(voids) is not tuple:
        raise ValueError(
            "PaperBook lifecycle settlement keys must be canonical tuples"
        )
    for values, label in (
        (winners, "winning_quote_keys"),
        (voids, "void_quote_keys"),
    ):
        for value in values:
            _require_exact_text(value, f"lifecycle {label}")
        if values != tuple(sorted(values)) or len(values) != len(set(values)):
            raise ValueError(
                f"PaperBook lifecycle {label} must be sorted and unique"
            )
    if action == "open" and (winners or voids):
        raise ValueError(
            "PaperBook lifecycle open action cannot contain settlement keys"
        )
    return action, ticket_id, winners, voids


def _paperbook_operation_context(book: _paper.PaperBook):
    """Reuse canonical PaperBook serialization when that authority is installed."""
    require_lock = getattr(_paper, "_require_paperbook_operation_lock", None)
    if require_lock is None:
        return nullcontext()
    if not callable(require_lock):
        raise RuntimeError("PaperBook operation lock authority is invalid")
    return require_lock(book)


def _canonical_open_legs(legs):
    """Reject caller-controlled iteration before any LAY economic authority read."""
    if type(legs) not in {list, tuple}:
        raise ValueError("ticket legs must be an exact list or tuple")
    return tuple(legs)


def _canonical_open_stake(book: _paper.PaperBook, stake) -> Decimal:
    parser = getattr(_paper.PaperBook, "_canonical_decimal_input", None)
    if parser is not None:
        amount = parser(stake, "stake")
        if type(amount) is not _DECIMAL_TYPE:
            raise ValueError("canonical stake parser must return exact Decimal")
        return amount
    if type(stake) not in {_DECIMAL_TYPE, str, int, float}:
        raise TypeError(
            "stake must be an exact Decimal, str, int, or float"
        )
    amount = _DECIMAL_TYPE(str(stake))
    if type(amount) is not _DECIMAL_TYPE:
        raise ValueError("stake normalization must produce exact Decimal")
    _require_exact_decimal(amount, "stake")
    return amount


def _is_lay_leg(leg: object) -> bool:
    # This predicate runs before the canonical PaperBook leg validator in several
    # dispatch paths. Never invoke caller-controlled equality on a mutated frozen
    # TicketLeg while deciding which economic authority owns validation.
    return (
        type(leg) is _TICKET_LEG_TYPE
        and type(leg.exchange_side) is str
        and leg.exchange_side == "lay"
    )


def _book_has_canonical_lay_ticket(book: object) -> bool:
    tickets = getattr(book, "tickets", None)
    if type(tickets) is not dict:
        return False
    for ticket in tickets.values():
        if type(ticket) is not _PAPER_TICKET_TYPE or type(ticket.legs) is not tuple:
            continue
        if any(_is_lay_leg(leg) for leg in ticket.legs):
            return True
    return False


def _require_supported_ticket_shape(ticket: PaperTicket) -> None:
    lay_count = sum(1 for leg in ticket.legs if _is_lay_leg(leg))
    if lay_count and (lay_count != 1 or len(ticket.legs) != 1):
        raise ValueError(
            "PaperBook LAY economics require exactly one canonical single-leg LAY ticket"
        )


def _locked_capital_for_ticket(ticket: PaperTicket) -> Decimal:
    _require_supported_ticket_shape(ticket)
    if len(ticket.legs) == 1 and _is_lay_leg(ticket.legs[0]):
        if _ORIGINAL_LOCKED_CAPITAL.__code__ is not _ORIGINAL_LOCKED_CAPITAL_CODE:
            raise ValueError("PaperBook canonical LAY liability authority changed")
        locked_capital = _ORIGINAL_LOCKED_CAPITAL(
            stake=ticket.stake,
            odds=ticket.legs[0].locked_odds,
            exchange_side="LAY",
        )
        if _ORIGINAL_LOCKED_CAPITAL.__code__ is not _ORIGINAL_LOCKED_CAPITAL_CODE:
            raise ValueError("PaperBook canonical LAY liability authority changed")
        return locked_capital
    return ticket.stake


def _validate_ticket_leg(
    cls,
    leg: object,
    *,
    ticket_id: str | None = None,
) -> TicketLeg:
    if _is_lay_leg(leg):
        suffix = f" for ticket {ticket_id}" if ticket_id is not None else ""
        _require_exact_text(
            leg.event_id,
            f"event_id{suffix}",
            forbid_quote_key_delimiter=True,
        )
        _require_exact_text(
            leg.market_id,
            f"market_id{suffix}",
            forbid_quote_key_delimiter=True,
        )
        _require_exact_text(
            leg.selection_id,
            f"selection_id{suffix}",
            forbid_quote_key_delimiter=True,
        )
        if leg.sport is not None:
            sport = _require_exact_text(
                leg.sport,
                f"sport{suffix}",
                forbid_quote_key_delimiter=True,
            )
            if sport != sport.lower() or any(
                character not in "abcdefghijklmnopqrstuvwxyz0123456789_-"
                for character in sport
            ):
                raise ValueError(
                    "PaperBook ticket sport must be a lowercase canonical sport identity"
                )
        if type(leg.locked_odds) is not _DECIMAL_TYPE:
            raise ValueError(
                f"PaperBook locked_odds{suffix} must be an exact Decimal"
            )
        _require_exact_decimal(leg.locked_odds, f"locked_odds{suffix}")
        if leg.locked_odds <= 1:
            raise ValueError("PaperBook snapshot decimal odds must be greater than 1")
        return leg
    return _ORIGINAL_VALIDATE_TICKET_LEG(cls, leg, ticket_id=ticket_id)


def _open_ticket_unlocked(
    self: _paper.PaperBook,
    legs,
    stake,
    reason: str = "",
    placed_at: str | None = None,
    *,
    provider_source_ids: tuple[str, ...] = (),
    provider_accounts: tuple[tuple[str, str], ...] = (),
    bankroll_id: str | None = None,
    currency: str | None = None,
) -> PaperTicket:
    ticket_legs = _canonical_open_legs(legs)
    if not any(_is_lay_leg(leg) for leg in ticket_legs):
        return _ORIGINAL_OPEN_TICKET(
            self,
            ticket_legs,
            stake,
            reason,
            placed_at,
            provider_source_ids=provider_source_ids,
            provider_accounts=provider_accounts,
            bankroll_id=bankroll_id,
            currency=currency,
        )

    if any(type(leg) is not _TICKET_LEG_TYPE for leg in ticket_legs):
        return _ORIGINAL_OPEN_TICKET(
            self,
            ticket_legs,
            stake,
            reason,
            placed_at,
            provider_source_ids=provider_source_ids,
            provider_accounts=provider_accounts,
            bankroll_id=bankroll_id,
            currency=currency,
        )
    if len(ticket_legs) != 1 or ticket_legs[0].exchange_side != "lay":
        raise ValueError(
            "PaperBook LAY economics require exactly one canonical single-leg LAY ticket"
        )

    # These containers are written after the economic debit. Require their exact
    # built-in authority before validation so a patched fallback validator cannot
    # admit caller-controlled append/mapping dispatch on the first LAY ticket.
    if type(self._lifecycle) is not list:
        raise ValueError("PaperBook lifecycle must be a canonical list")
    if type(self._settlement_times) is not dict:
        raise ValueError("PaperBook settlement-time witness must be a canonical mapping")
    _validate_loaded_state(_paper.PaperBook, self)
    _ORIGINAL_REQUIRE_TICKET_OPENING_AUTHORITY(self)
    _ORIGINAL_REQUIRE_CAUSAL_HISTORY_AUTHORITY(self)

    leg = ticket_legs[0]
    _validate_ticket_leg(_paper.PaperBook, leg)
    amount = _canonical_open_stake(self, stake)
    if amount <= 0:
        raise ValueError("stake must be positive")
    if _ORIGINAL_LOCKED_CAPITAL.__code__ is not _ORIGINAL_LOCKED_CAPITAL_CODE:
        raise ValueError("PaperBook canonical LAY liability authority changed")
    locked_capital = _ORIGINAL_LOCKED_CAPITAL(
        stake=amount,
        odds=leg.locked_odds,
        exchange_side="LAY",
    )
    if _ORIGINAL_LOCKED_CAPITAL.__code__ is not _ORIGINAL_LOCKED_CAPITAL_CODE:
        raise ValueError("PaperBook canonical LAY liability authority changed")
    if _ORIGINAL_DEBIT_BALANCE.__code__ is not _ORIGINAL_DEBIT_BALANCE_CODE:
        raise ValueError("PaperBook canonical debit authority changed")
    new_balance = _ORIGINAL_DEBIT_BALANCE(_paper.PaperBook, self.balance, locked_capital)
    if _ORIGINAL_DEBIT_BALANCE.__code__ is not _ORIGINAL_DEBIT_BALANCE_CODE:
        raise ValueError("PaperBook canonical debit authority changed")

    ticket_placed_at = _validate_exact_timestamp(
        placed_at if placed_at is not None else _ORIGINAL_UTC_NOW_ISO(),
        "placed_at",
    )
    _require_exact_text(reason, "strategy_reason", allow_empty=True)
    (
        provider_source_ids,
        provider_accounts,
        bankroll_id,
        currency,
    ) = _validate_exact_ticket_provenance(
        provider_source_ids,
        provider_accounts,
        bankroll_id,
        currency,
    )

    if (
        _ORIGINAL_UUID4 is not _CANONICAL_UUID4
        or _ORIGINAL_UUID4.__code__ is not _ORIGINAL_UUID4_CODE
    ):
        raise ValueError("PaperBook canonical ticket-id generator authority changed")
    ticket_uuid = _ORIGINAL_UUID4()
    if (
        _ORIGINAL_UUID4 is not _CANONICAL_UUID4
        or _ORIGINAL_UUID4.__code__ is not _ORIGINAL_UUID4_CODE
    ):
        raise ValueError("PaperBook canonical ticket-id generator authority changed")
    if type(ticket_uuid) is not _ORIGINAL_UUID_TYPE:
        raise ValueError("PaperBook canonical ticket-id generator returned non-canonical UUID")
    if (
        _ORIGINAL_UUID_TYPE.__str__ is not _ORIGINAL_UUID_STR
        or _ORIGINAL_UUID_STR.__code__ is not _ORIGINAL_UUID_STR_CODE
    ):
        raise ValueError("PaperBook canonical UUID string authority changed")
    ticket_id = _ORIGINAL_UUID_STR(ticket_uuid)
    if (
        _ORIGINAL_UUID_TYPE.__str__ is not _ORIGINAL_UUID_STR
        or _ORIGINAL_UUID_STR.__code__ is not _ORIGINAL_UUID_STR_CODE
    ):
        raise ValueError("PaperBook canonical UUID string authority changed")
    _require_exact_text(ticket_id, "ticket_id")
    if ticket_id in self.tickets:
        raise ValueError("PaperBook canonical ticket_id already exists")

    ticket = _PAPER_TICKET_TYPE(
        ticket_id=ticket_id,
        stake=amount,
        legs=ticket_legs,
        placed_at=ticket_placed_at,
        strategy_reason=reason,
        provider_source_ids=provider_source_ids,
        provider_accounts=provider_accounts,
        bankroll_id=bankroll_id,
        currency=currency,
    )
    _ORIGINAL_RECORD_TICKET_OPENING_AUTHORITY(self, ticket)
    self.balance = new_balance
    self.tickets[ticket.ticket_id] = ticket
    self._lifecycle.append(("open", ticket.ticket_id, (), ()))
    _ORIGINAL_ADVANCE_CAUSAL_HISTORY_OPEN(self, ticket.ticket_id)
    return ticket


def _open_ticket(
    self: _paper.PaperBook,
    legs,
    stake,
    reason: str = "",
    placed_at: str | None = None,
    *,
    provider_source_ids: tuple[str, ...] = (),
    provider_accounts: tuple[tuple[str, str], ...] = (),
    bankroll_id: str | None = None,
    currency: str | None = None,
) -> PaperTicket:
    with _paperbook_operation_context(self):
        return _open_ticket_unlocked(
            self,
            legs,
            stake,
            reason,
            placed_at,
            provider_source_ids=provider_source_ids,
            provider_accounts=provider_accounts,
            bankroll_id=bankroll_id,
            currency=currency,
        )


def _normalize_exact_resolution_keys(values: object, label: str) -> set[str]:
    if type(values) not in {set, frozenset, list, tuple}:
        raise ValueError(
            f"PaperBook {label} must be an exact built-in collection of quote keys"
        )
    for value in values:
        _require_exact_text(value, label)
    return set(values)


def _settle_unlocked(
    self: _paper.PaperBook,
    ticket_id: str,
    winning_quote_keys,
    void_quote_keys=None,
    *,
    settled_at: str | None = None,
) -> PaperTicket:
    _require_exact_text(ticket_id, "ticket_id")
    _validate_loaded_state(_paper.PaperBook, self)
    _ORIGINAL_REQUIRE_TICKET_OPENING_AUTHORITY(self)
    _ORIGINAL_REQUIRE_CAUSAL_HISTORY_AUTHORITY(self)
    ticket = self.tickets[ticket_id]
    if not (len(ticket.legs) == 1 and _is_lay_leg(ticket.legs[0])):
        return _ORIGINAL_SETTLE(
            self,
            ticket_id,
            winning_quote_keys,
            void_quote_keys,
            settled_at=settled_at,
        )
    if ticket.status is not _TICKET_STATUS_TYPE.OPEN:
        raise ValueError("ticket already settled")

    winners = _normalize_exact_resolution_keys(
        winning_quote_keys,
        "winning_quote_keys",
    )
    voids = (
        set()
        if void_quote_keys is None
        else _normalize_exact_resolution_keys(
            void_quote_keys,
            "void_quote_keys",
        )
    )
    settlement_time = None
    if settled_at is not None:
        settlement_time = _validate_exact_timestamp(settled_at, "settled_at")
        if _ORIGINAL_PARSE_ISO_TIMESTAMP(
            settlement_time
        ) < _ORIGINAL_PARSE_ISO_TIMESTAMP(ticket.placed_at):
            raise ValueError("PaperBook settled_at must not precede placed_at")

    status, payout, new_balance = _settlement_result(
        _paper.PaperBook,
        ticket,
        self.balance,
        winners,
        voids,
    )
    winners_tuple = tuple(sorted(winners))
    voids_tuple = tuple(sorted(voids))

    # All visible containers and the hidden authority were revalidated above.
    # Advance the product-issued causal witness immediately adjacent to the
    # corresponding visible transition.
    _ORIGINAL_ADVANCE_CAUSAL_HISTORY_SETTLE(
        self,
        ticket.ticket_id,
        winners_tuple,
        voids_tuple,
        settlement_time,
    )
    ticket.payout = payout
    ticket.status = status
    ticket.settled_at = settlement_time
    self.balance = new_balance
    self._lifecycle.append(
        ("settle", ticket.ticket_id, winners_tuple, voids_tuple)
    )
    self._settlement_times[ticket.ticket_id] = settlement_time
    return ticket


def _settle(
    self: _paper.PaperBook,
    ticket_id: str,
    winning_quote_keys,
    void_quote_keys=None,
    *,
    settled_at: str | None = None,
) -> PaperTicket:
    with _paperbook_operation_context(self):
        return _settle_unlocked(
            self,
            ticket_id,
            winning_quote_keys,
            void_quote_keys,
            settled_at=settled_at,
        )


def _settlement_result(
    cls,
    ticket: PaperTicket,
    balance: Decimal,
    winning_quote_keys: set[str],
    void_quote_keys: set[str],
) -> tuple[TicketStatus, Decimal, Decimal]:
    if not any(_is_lay_leg(leg) for leg in ticket.legs):
        return _ORIGINAL_SETTLEMENT_RESULT(
            cls,
            ticket,
            balance,
            winning_quote_keys,
            void_quote_keys,
        )

    _require_supported_ticket_shape(ticket)
    _require_exact_decimal(balance, "balance")
    leg = ticket.legs[0]
    _validate_ticket_leg(_paper.PaperBook, leg, ticket_id=ticket.ticket_id)
    known = {leg.quote_key}
    if winning_quote_keys - known:
        raise ValueError("PaperBook settlement contains unknown winning quote_key")
    if void_quote_keys - known:
        raise ValueError("PaperBook settlement contains unknown void quote_key")
    if winning_quote_keys & void_quote_keys:
        raise ValueError("PaperBook settlement quote_key cannot be both winning and void")

    locked_capital = _locked_capital_for_ticket(ticket)
    if leg.quote_key in void_quote_keys:
        status = _TICKET_STATUS_TYPE.VOID
        payout = locked_capital
    elif leg.quote_key in winning_quote_keys:
        status = _TICKET_STATUS_TYPE.LOST
        payout = _DECIMAL_TYPE("0")
    else:
        status = _TICKET_STATUS_TYPE.WON
        try:
            _require_decimal_arithmetic_authority()
            with _CANONICAL_LOCALCONTEXT(_ORIGINAL_PAPER_DECIMAL_CONTEXT()) as context:
                payout = locked_capital + ticket.stake
                if context.flags[Inexact]:
                    raise ValueError("PaperBook LAY payout loses Decimal precision")
        except DecimalException as exc:
            raise ValueError(
                "PaperBook LAY settlement arithmetic is not representable"
            ) from exc

    _require_exact_decimal(payout, f"settlement payout for ticket {ticket.ticket_id}")
    try:
        _require_decimal_arithmetic_authority()
        with _CANONICAL_LOCALCONTEXT(_ORIGINAL_PAPER_DECIMAL_CONTEXT()) as context:
            new_balance = balance + payout
            if context.flags[Inexact]:
                raise ValueError("PaperBook LAY balance credit loses Decimal precision")
    except DecimalException as exc:
        raise ValueError(
            "PaperBook LAY settlement arithmetic is not representable"
        ) from exc
    _require_exact_decimal(
        new_balance,
        f"balance after settling ticket {ticket.ticket_id}",
    )
    if payout != 0 and new_balance == balance:
        raise ValueError("PaperBook settlement payout loses all Decimal balance effect")
    return status, payout, new_balance


def _validate_lifecycle_reachability(cls, book: _paper.PaperBook) -> None:
    if not _book_has_canonical_lay_ticket(book):
        return _ORIGINAL_VALIDATE_LIFECYCLE_REACHABILITY(cls, book)

    if type(book._lifecycle) is not list:
        raise ValueError("PaperBook lifecycle must be a canonical list")
    if type(book._settlement_times) is not dict:
        raise ValueError("PaperBook settlement-time witness must be a canonical mapping")
    if any(type(ticket_id) is not str for ticket_id in book._settlement_times):
        raise ValueError(
            "PaperBook settlement-time witness keys must be canonical strings"
        )

    replay_balance = book.initial_bankroll
    opened: set[str] = set()
    settled: set[str] = set()
    open_order: list[str] = []

    for raw_entry in book._lifecycle:
        action, ticket_id, winners_raw, voids_raw = _validate_exact_lifecycle_entry(
            raw_entry
        )
        ticket = book.tickets.get(ticket_id)
        if ticket is None:
            raise ValueError("PaperBook lifecycle references unknown ticket_id")
        _require_supported_ticket_shape(ticket)

        if action == "open":
            if ticket_id in opened:
                raise ValueError("PaperBook lifecycle opens a ticket more than once")
            try:
                if _ORIGINAL_DEBIT_BALANCE.__code__ is not _ORIGINAL_DEBIT_BALANCE_CODE:
                    raise ValueError("PaperBook canonical debit authority changed")
                replay_balance = _ORIGINAL_DEBIT_BALANCE(
                    _paper.PaperBook,
                    replay_balance,
                    _locked_capital_for_ticket(ticket),
                )
                if _ORIGINAL_DEBIT_BALANCE.__code__ is not _ORIGINAL_DEBIT_BALANCE_CODE:
                    raise ValueError("PaperBook canonical debit authority changed")
            except ValueError as exc:
                raise ValueError(
                    f"PaperBook lifecycle locked capital for ticket {ticket_id} was not affordable"
                ) from exc
            opened.add(ticket_id)
            open_order.append(ticket_id)
            continue

        if ticket_id not in opened:
            raise ValueError("PaperBook lifecycle settles a ticket before opening it")
        if ticket_id in settled:
            raise ValueError("PaperBook lifecycle settles a ticket more than once")
        if ticket_id not in book._settlement_times:
            raise ValueError(
                f"PaperBook ticket {ticket_id} settlement is missing timestamp provenance witness"
            )
        settlement_time = book._settlement_times[ticket_id]
        if ticket.settled_at != settlement_time:
            raise ValueError(
                f"PaperBook ticket {ticket_id} settled_at is inconsistent with lifecycle provenance"
            )
        if settlement_time is not None:
            settlement_text = _validate_exact_timestamp(
                settlement_time,
                "snapshot settled_at",
            )
            if _ORIGINAL_PARSE_ISO_TIMESTAMP(settlement_text) < _ORIGINAL_PARSE_ISO_TIMESTAMP(
                ticket.placed_at
            ):
                raise ValueError("PaperBook settled_at must not precede placed_at")
        winners = set(winners_raw)
        voids = set(voids_raw)
        status, payout, replay_balance = _settlement_result(
            _paper.PaperBook,
            ticket,
            replay_balance,
            winners,
            voids,
        )
        if ticket.status is not status or ticket.payout != payout:
            raise ValueError(
                f"PaperBook ticket {ticket_id} state is inconsistent with lifecycle settlement witness"
            )
        settled.add(ticket_id)

    if tuple(open_order) != tuple(book.tickets):
        raise ValueError("PaperBook lifecycle open order must match canonical ticket order")
    for ticket_id, ticket in book.tickets.items():
        if ticket_id not in opened:
            raise ValueError("PaperBook lifecycle is missing ticket open action")
        if ticket_id not in settled and ticket.status is not _TICKET_STATUS_TYPE.OPEN:
            raise ValueError(
                f"PaperBook ticket {ticket_id} settled state is missing lifecycle provenance"
            )
        if ticket_id in settled and ticket.status is _TICKET_STATUS_TYPE.OPEN:
            raise ValueError(
                f"PaperBook ticket {ticket_id} open state conflicts with lifecycle settlement witness"
            )
    if set(book._settlement_times) != settled:
        raise ValueError(
            "PaperBook settlement-time witness must match settled lifecycle tickets exactly"
        )
    if replay_balance != book.balance:
        raise ValueError(
            "PaperBook snapshot balance is inconsistent with lifecycle-replayed ticket economics"
        )


def _validate_loaded_state(cls, book: _paper.PaperBook) -> None:
    if not _book_has_canonical_lay_ticket(book):
        return _ORIGINAL_VALIDATE_LOADED_STATE(cls, book)

    _require_exact_decimal(book.initial_bankroll, "initial_bankroll")
    _require_exact_decimal(book.balance, "balance")
    if book.initial_bankroll <= 0:
        raise ValueError("PaperBook snapshot initial_bankroll must be positive")
    if book.balance < 0:
        raise ValueError("PaperBook snapshot balance cannot be negative")
    if type(book.tickets) is not dict:
        raise ValueError("PaperBook tickets must be a canonical ticket mapping")

    for ticket_key, ticket in book.tickets.items():
        _require_exact_text(ticket_key, "ticket mapping key")
        if type(ticket) is not _PAPER_TICKET_TYPE:
            raise ValueError("PaperBook tickets must contain canonical PaperTicket values")
        _require_exact_text(ticket.ticket_id, "ticket_id")
        if ticket_key != ticket.ticket_id:
            raise ValueError("PaperBook ticket mapping key must match ticket_id")
        _validate_exact_timestamp(ticket.placed_at, "snapshot placed_at")
        if ticket.settled_at is not None:
            settled_at = _validate_exact_timestamp(
                ticket.settled_at,
                "snapshot settled_at",
            )
            if _ORIGINAL_PARSE_ISO_TIMESTAMP(settled_at) < _ORIGINAL_PARSE_ISO_TIMESTAMP(
                ticket.placed_at
            ):
                raise ValueError("PaperBook settled_at must not precede placed_at")
        if ticket.status is _TICKET_STATUS_TYPE.OPEN and ticket.settled_at is not None:
            raise ValueError("PaperBook snapshot open ticket cannot have settled_at")
        _require_exact_text(
            ticket.strategy_reason,
            "snapshot strategy_reason",
            allow_empty=True,
        )
        _validate_exact_ticket_provenance(
            ticket.provider_source_ids,
            ticket.provider_accounts,
            ticket.bankroll_id,
            ticket.currency,
        )
        if type(ticket.status) is not _TICKET_STATUS_TYPE:
            raise ValueError(
                "PaperBook snapshot ticket status must be canonical TicketStatus"
            )
        _require_exact_decimal(ticket.stake, f"stake for ticket {ticket.ticket_id}")
        _require_exact_decimal(ticket.payout, f"payout for ticket {ticket.ticket_id}")
        if ticket.stake <= 0:
            raise ValueError("PaperBook snapshot ticket stake must be positive")
        if ticket.payout < 0:
            raise ValueError("PaperBook snapshot ticket payout cannot be negative")
        if type(ticket.legs) is not tuple or not ticket.legs:
            raise ValueError(
                "PaperBook snapshot ticket requires a canonical non-empty leg tuple"
            )
        for leg in ticket.legs:
            _validate_ticket_leg(_paper.PaperBook, leg, ticket_id=ticket.ticket_id)
        _require_supported_ticket_shape(ticket)
        quote_keys = [leg.quote_key for leg in ticket.legs]
        if len(quote_keys) != len(set(quote_keys)):
            raise ValueError(
                "PaperBook snapshot ticket contains duplicate quote_key leg"
            )

        is_lay = len(ticket.legs) == 1 and _is_lay_leg(ticket.legs[0])
        if not is_lay:
            if ticket.status in {_TICKET_STATUS_TYPE.OPEN, _TICKET_STATUS_TYPE.LOST} and ticket.payout != 0:
                raise ValueError(
                    "PaperBook snapshot open/lost ticket payout must be zero"
                )
            if ticket.status is _TICKET_STATUS_TYPE.VOID and ticket.payout != ticket.stake:
                raise ValueError(
                    "PaperBook snapshot void ticket payout must equal stake"
                )
            if ticket.status is _TICKET_STATUS_TYPE.WON and ticket.payout <= ticket.stake:
                raise ValueError(
                    "PaperBook snapshot won ticket payout must exceed stake"
                )
            continue

        locked_capital = _locked_capital_for_ticket(ticket)
        if ticket.status in {_TICKET_STATUS_TYPE.OPEN, _TICKET_STATUS_TYPE.LOST}:
            expected_payout = _DECIMAL_TYPE("0")
        elif ticket.status is _TICKET_STATUS_TYPE.VOID:
            expected_payout = locked_capital
        elif ticket.status is _TICKET_STATUS_TYPE.WON:
            try:
                _require_decimal_arithmetic_authority()
            with _CANONICAL_LOCALCONTEXT(_ORIGINAL_PAPER_DECIMAL_CONTEXT()) as context:
                    expected_payout = locked_capital + ticket.stake
                    if context.flags[Inexact]:
                        raise ValueError(
                            "PaperBook LAY payout witness loses Decimal precision"
                        )
            except DecimalException as exc:
                raise ValueError(
                    "PaperBook LAY payout witness is not representable"
                ) from exc
        else:
            raise ValueError("PaperBook snapshot ticket status is unsupported")
        if ticket.payout != expected_payout:
            raise ValueError(
                "PaperBook snapshot LAY payout is inconsistent with locked-capital economics"
            )

    _validate_lifecycle_reachability(_paper.PaperBook, book)


def _committed_capital_unlocked(self: _paper.PaperBook) -> Decimal:
    _validate_loaded_state(_paper.PaperBook, self)
    _ORIGINAL_REQUIRE_TICKET_OPENING_AUTHORITY(self)
    _ORIGINAL_REQUIRE_CAUSAL_HISTORY_AUTHORITY(self)
    try:
        _require_decimal_arithmetic_authority()
        with _CANONICAL_LOCALCONTEXT(_ORIGINAL_PAPER_DECIMAL_CONTEXT()) as context:
            total = _DECIMAL_TYPE("0")
            for ticket in self.tickets.values():
                if ticket.status is _TICKET_STATUS_TYPE.OPEN:
                    total += _locked_capital_for_ticket(ticket)
            if context.flags[Inexact]:
                raise ValueError("PaperBook committed capital loses Decimal precision")
    except DecimalException as exc:
        raise ValueError(
            "PaperBook committed capital arithmetic is not representable"
        ) from exc
    _require_exact_decimal(total, "committed_capital")
    return total


def _committed_capital(self: _paper.PaperBook) -> Decimal:
    with _paperbook_operation_context(self):
        return _committed_capital_unlocked(self)


def _install() -> None:
    marker = "_autosport_lay_economics_guard"
    if getattr(_paper.PaperBook, marker, False):
        return
    _paper.PaperBook._validate_ticket_leg = classmethod(_validate_ticket_leg)
    _paper.PaperBook._settlement_result = classmethod(_settlement_result)
    _paper.PaperBook._validate_lifecycle_reachability = classmethod(
        _validate_lifecycle_reachability
    )
    _paper.PaperBook._validate_loaded_state = classmethod(_validate_loaded_state)
    _paper.PaperBook.open_ticket = _open_ticket
    _paper.PaperBook.settle = _settle
    _paper.PaperBook.committed_capital = property(_committed_capital)
    setattr(_paper.PaperBook, marker, True)


_install()


__all__ = []
