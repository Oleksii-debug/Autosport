from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from decimal import Decimal, DecimalException, Inexact, localcontext

from . import paper as _paper
from .domain import PaperTicket, TicketLeg, TicketStatus
from .exchange_exposure import locked_capital_for_exchange_side


_ORIGINAL_OPEN_TICKET = _paper.PaperBook.open_ticket
_ORIGINAL_VALIDATE_TICKET_LEG = _paper.PaperBook._validate_ticket_leg.__func__
_ORIGINAL_SETTLEMENT_RESULT = _paper.PaperBook._settlement_result.__func__
_ORIGINAL_VALIDATE_LIFECYCLE_REACHABILITY = (
    _paper.PaperBook._validate_lifecycle_reachability.__func__
)
_ORIGINAL_VALIDATE_LOADED_STATE = _paper.PaperBook._validate_loaded_state.__func__


def _paperbook_operation_context(book: _paper.PaperBook):
    """Reuse canonical PaperBook serialization when that authority is installed."""
    require_lock = getattr(_paper, "_require_paperbook_operation_lock", None)
    if require_lock is None:
        return nullcontext()
    if not callable(require_lock):
        raise RuntimeError("PaperBook operation lock authority is invalid")
    return require_lock(book)


def _canonical_open_legs(legs):
    """Preserve the canonical exact-container ingress once PaperBook provides it."""
    if hasattr(_paper.PaperBook, "_canonical_decimal_input") and type(legs) not in {
        list,
        tuple,
    }:
        raise ValueError("ticket legs must be an exact list or tuple")
    return tuple(legs)


def _canonical_open_stake(book: _paper.PaperBook, stake) -> Decimal:
    parser = getattr(_paper.PaperBook, "_canonical_decimal_input", None)
    if parser is not None:
        return parser(stake, "stake")
    amount = Decimal(str(stake))
    type(book)._require_finite(amount, "stake")
    return amount


def _is_lay_leg(leg: object) -> bool:
    # This predicate runs before the canonical PaperBook leg validator in several
    # dispatch paths. Never invoke caller-controlled equality on a mutated frozen
    # TicketLeg while deciding which economic authority owns validation.
    return (
        type(leg) is TicketLeg
        and type(leg.exchange_side) is str
        and leg.exchange_side == "lay"
    )


def _book_has_canonical_lay_ticket(book: object) -> bool:
    tickets = getattr(book, "tickets", None)
    if type(tickets) is not dict:
        return False
    for ticket in tickets.values():
        if type(ticket) is not PaperTicket or type(ticket.legs) is not tuple:
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
        return locked_capital_for_exchange_side(
            stake=ticket.stake,
            odds=ticket.legs[0].locked_odds,
            exchange_side="LAY",
        )
    return ticket.stake


def _validate_ticket_leg(
    cls,
    leg: object,
    *,
    ticket_id: str | None = None,
) -> TicketLeg:
    if _is_lay_leg(leg):
        validated = _ORIGINAL_VALIDATE_TICKET_LEG(
            cls,
            replace(leg, exchange_side="back"),
            ticket_id=ticket_id,
        )
        if validated.exchange_side != "back":
            raise RuntimeError("PaperBook canonical leg validator changed unexpectedly")
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

    if any(type(leg) is not TicketLeg for leg in ticket_legs):
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

    type(self)._validate_loaded_state(self)
    _paper._require_ticket_opening_authority(self)
    _paper._require_paperbook_causal_history_authority(self)

    leg = ticket_legs[0]
    type(self)._validate_ticket_leg(leg)
    amount = _canonical_open_stake(self, stake)
    if amount <= 0:
        raise ValueError("stake must be positive")
    locked_capital = locked_capital_for_exchange_side(
        stake=amount,
        odds=leg.locked_odds,
        exchange_side="LAY",
    )
    new_balance = type(self)._debit_balance(self.balance, locked_capital)

    ticket_placed_at = type(self)._validate_placed_at(
        placed_at if placed_at is not None else _paper.utc_now_iso()
    )
    type(self)._require_utf8_string(reason, "strategy_reason")
    (
        provider_source_ids,
        provider_accounts,
        bankroll_id,
        currency,
    ) = type(self)._validate_ticket_provenance(
        provider_source_ids,
        provider_accounts,
        bankroll_id,
        currency,
    )

    ticket = PaperTicket(
        ticket_id=str(_paper.uuid.uuid4()),
        stake=amount,
        legs=ticket_legs,
        placed_at=ticket_placed_at,
        strategy_reason=reason,
        provider_source_ids=provider_source_ids,
        provider_accounts=provider_accounts,
        bankroll_id=bankroll_id,
        currency=currency,
    )
    _paper._record_ticket_opening_authority(self, ticket)
    self.balance = new_balance
    self.tickets[ticket.ticket_id] = ticket
    self._lifecycle.append(("open", ticket.ticket_id, (), ()))
    _paper._advance_paperbook_causal_history_open(self, ticket.ticket_id)
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
    cls._require_finite(balance, "balance")
    leg = ticket.legs[0]
    cls._validate_ticket_leg(leg, ticket_id=ticket.ticket_id)
    known = {leg.settlement_key}
    if winning_quote_keys - known:
        raise ValueError("PaperBook settlement contains unknown winning settlement key")
    if void_quote_keys - known:
        raise ValueError("PaperBook settlement contains unknown void settlement key")
    if winning_quote_keys & void_quote_keys:
        raise ValueError("PaperBook settlement key cannot be both winning and void")

    locked_capital = _locked_capital_for_ticket(ticket)
    if leg.settlement_key in void_quote_keys:
        status = TicketStatus.VOID
        payout = locked_capital
    elif leg.settlement_key in winning_quote_keys:
        status = TicketStatus.LOST
        payout = Decimal("0")
    else:
        status = TicketStatus.WON
        try:
            with localcontext(_paper._paper_decimal_context()) as context:
                payout = locked_capital + ticket.stake
                if context.flags[Inexact]:
                    raise ValueError("PaperBook LAY payout loses Decimal precision")
        except DecimalException as exc:
            raise ValueError(
                "PaperBook LAY settlement arithmetic is not representable"
            ) from exc

    cls._require_finite(payout, f"settlement payout for ticket {ticket.ticket_id}")
    try:
        with localcontext(_paper._paper_decimal_context()) as context:
            new_balance = balance + payout
            if context.flags[Inexact]:
                raise ValueError("PaperBook LAY balance credit loses Decimal precision")
    except DecimalException as exc:
        raise ValueError(
            "PaperBook LAY settlement arithmetic is not representable"
        ) from exc
    cls._require_finite(
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
        action, ticket_id, winners_raw, voids_raw = cls._validate_lifecycle_entry(
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
                replay_balance = cls._debit_balance(
                    replay_balance,
                    _locked_capital_for_ticket(ticket),
                )
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
            cls._validate_settled_at(
                settlement_time,
                ticket.placed_at,
                snapshot=True,
            )
        winners = set(winners_raw)
        voids = set(voids_raw)
        status, payout, replay_balance = cls._settlement_result(
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
        if ticket_id not in settled and ticket.status is not TicketStatus.OPEN:
            raise ValueError(
                f"PaperBook ticket {ticket_id} settled state is missing lifecycle provenance"
            )
        if ticket_id in settled and ticket.status is TicketStatus.OPEN:
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

    cls._require_finite(book.initial_bankroll, "initial_bankroll")
    cls._require_finite(book.balance, "balance")
    if book.initial_bankroll <= 0:
        raise ValueError("PaperBook snapshot initial_bankroll must be positive")
    if book.balance < 0:
        raise ValueError("PaperBook snapshot balance cannot be negative")
    if type(book.tickets) is not dict:
        raise ValueError("PaperBook tickets must be a canonical ticket mapping")

    for ticket_key, ticket in book.tickets.items():
        cls._require_canonical_text(ticket_key, "ticket mapping key")
        if type(ticket) is not PaperTicket:
            raise ValueError("PaperBook tickets must contain canonical PaperTicket values")
        cls._require_canonical_text(ticket.ticket_id, "ticket_id")
        if ticket_key != ticket.ticket_id:
            raise ValueError("PaperBook ticket mapping key must match ticket_id")
        cls._validate_placed_at(ticket.placed_at, snapshot=True)
        if ticket.settled_at is not None:
            cls._validate_settled_at(
                ticket.settled_at,
                ticket.placed_at,
                snapshot=True,
            )
        if ticket.status is TicketStatus.OPEN and ticket.settled_at is not None:
            raise ValueError("PaperBook snapshot open ticket cannot have settled_at")
        cls._require_utf8_string(
            ticket.strategy_reason,
            "snapshot strategy_reason",
        )
        cls._validate_ticket_provenance(
            ticket.provider_source_ids,
            ticket.provider_accounts,
            ticket.bankroll_id,
            ticket.currency,
        )
        if type(ticket.status) is not TicketStatus:
            raise ValueError(
                "PaperBook snapshot ticket status must be canonical TicketStatus"
            )
        cls._require_finite(ticket.stake, f"stake for ticket {ticket.ticket_id}")
        cls._require_finite(ticket.payout, f"payout for ticket {ticket.ticket_id}")
        if ticket.stake <= 0:
            raise ValueError("PaperBook snapshot ticket stake must be positive")
        if ticket.payout < 0:
            raise ValueError("PaperBook snapshot ticket payout cannot be negative")
        if type(ticket.legs) is not tuple or not ticket.legs:
            raise ValueError(
                "PaperBook snapshot ticket requires a canonical non-empty leg tuple"
            )
        for leg in ticket.legs:
            cls._validate_ticket_leg(leg, ticket_id=ticket.ticket_id)
        _require_supported_ticket_shape(ticket)
        quote_keys = [leg.quote_key for leg in ticket.legs]
        if len(quote_keys) != len(set(quote_keys)):
            raise ValueError(
                "PaperBook snapshot ticket contains duplicate quote_key leg"
            )

        is_lay = len(ticket.legs) == 1 and _is_lay_leg(ticket.legs[0])
        if not is_lay:
            if ticket.status in {TicketStatus.OPEN, TicketStatus.LOST} and ticket.payout != 0:
                raise ValueError(
                    "PaperBook snapshot open/lost ticket payout must be zero"
                )
            if ticket.status is TicketStatus.VOID and ticket.payout != ticket.stake:
                raise ValueError(
                    "PaperBook snapshot void ticket payout must equal stake"
                )
            if ticket.status is TicketStatus.WON and ticket.payout <= ticket.stake:
                raise ValueError(
                    "PaperBook snapshot won ticket payout must exceed stake"
                )
            continue

        locked_capital = _locked_capital_for_ticket(ticket)
        if ticket.status in {TicketStatus.OPEN, TicketStatus.LOST}:
            expected_payout = Decimal("0")
        elif ticket.status is TicketStatus.VOID:
            expected_payout = locked_capital
        elif ticket.status is TicketStatus.WON:
            try:
                with localcontext(_paper._paper_decimal_context()) as context:
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

    cls._validate_lifecycle_reachability(book)


def _committed_capital_unlocked(self: _paper.PaperBook) -> Decimal:
    type(self)._validate_loaded_state(self)
    _paper._require_ticket_opening_authority(self)
    _paper._require_paperbook_causal_history_authority(self)
    try:
        with localcontext(_paper._paper_decimal_context()) as context:
            total = Decimal("0")
            for ticket in self.tickets.values():
                if ticket.status is TicketStatus.OPEN:
                    total += _locked_capital_for_ticket(ticket)
            if context.flags[Inexact]:
                raise ValueError("PaperBook committed capital loses Decimal precision")
    except DecimalException as exc:
        raise ValueError(
            "PaperBook committed capital arithmetic is not representable"
        ) from exc
    type(self)._require_finite(total, "committed_capital")
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
    _paper.PaperBook.committed_capital = property(_committed_capital)
    setattr(_paper.PaperBook, marker, True)


_install()


__all__ = []
