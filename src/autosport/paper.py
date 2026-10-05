from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import uuid
import weakref
from functools import wraps
from decimal import (
    Context,
    Decimal,
    DecimalException,
    Inexact,
    InvalidOperation,
    Overflow,
    ROUND_HALF_EVEN,
    Underflow,
    localcontext,
)
from pathlib import Path

from .domain import (
    PaperTicket,
    TicketLeg,
    TicketStatus,
    _canonical_semantic_identity,
    utc_now_iso,
)
from .forecasting import parse_iso_timestamp


_CANONICAL_PAPER_DECIMAL_PRECISION = 28
_CANONICAL_PAPER_DECIMAL_EMIN = -999999
_CANONICAL_PAPER_DECIMAL_EMAX = 999999
_CANONICAL_MAX_PAPER_DECIMAL_TEXT_CHARS = 512
_CANONICAL_MAX_PAPER_SNAPSHOT_BYTES = 8 * 1024 * 1024
_CANONICAL_PAPER_SNAPSHOT_SCHEMA_VERSION = 8
_CANONICAL_SUPPORTED_PAPER_SNAPSHOT_SCHEMA_VERSIONS = frozenset({2, 3, 4, 5, 6, 7, 8})
_CANONICAL_SCHEMA_MISSING = object()
_CANONICAL_PAPER_DECIMAL_TYPE = Decimal
_CANONICAL_DECIMAL_CONTEXT_TYPE = Context
_CANONICAL_DECIMAL_EXCEPTION_TYPE = DecimalException
_CANONICAL_INEXACT_SIGNAL = Inexact
_CANONICAL_INVALID_OPERATION_SIGNAL = InvalidOperation
_CANONICAL_OVERFLOW_SIGNAL = Overflow
_CANONICAL_UNDERFLOW_SIGNAL = Underflow
_CANONICAL_ROUND_HALF_EVEN = ROUND_HALF_EVEN
_CANONICAL_LOCALCONTEXT = localcontext
_CANONICAL_PAPER_PATH_CONSTRUCTOR = Path
_CANONICAL_PAPER_PATH_TYPE = type(Path("."))
_CANONICAL_PAPER_PATH_RESOLVE = Path.resolve
_CANONICAL_PAPER_PATH_MKDIR = Path.mkdir
_CANONICAL_PAPER_PATH_EXISTS = Path.exists
_CANONICAL_PAPER_PATH_UNLINK = Path.unlink
_CANONICAL_TICKET_STATUS_TYPE = TicketStatus
_CANONICAL_TICKET_STATUS_OPEN = TicketStatus.OPEN
_CANONICAL_TICKET_STATUS_WON = TicketStatus.WON
_CANONICAL_TICKET_STATUS_LOST = TicketStatus.LOST
_CANONICAL_TICKET_STATUS_VOID = TicketStatus.VOID
_CANONICAL_PAPER_TICKET_TYPE = PaperTicket
_CANONICAL_PAPER_TICKET_CONSTRUCTOR = PaperTicket
_CANONICAL_TICKET_LEG_TYPE = TicketLeg
_CANONICAL_TICKET_LEG_CONSTRUCTOR = TicketLeg
_CANONICAL_UTC_NOW_ISO = utc_now_iso
_CANONICAL_PARSE_ISO_TIMESTAMP = parse_iso_timestamp
_CANONICAL_UUID4 = uuid.uuid4
_CANONICAL_SEMANTIC_IDENTITY = _canonical_semantic_identity
_CANONICAL_OS_OPEN = os.open
_CANONICAL_OS_FSTAT = os.fstat
_CANONICAL_OS_STAT = os.stat
_CANONICAL_OS_CLOSE = os.close
_CANONICAL_OS_FDOPEN = os.fdopen
_CANONICAL_OS_SAMEOPENFILE = os.path.sameopenfile
_CANONICAL_STAT_ISREG = stat.S_ISREG
_CANONICAL_OS_RDONLY = os.O_RDONLY
_CANONICAL_OS_RDWR = os.O_RDWR
_CANONICAL_OS_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_CANONICAL_OS_NAME = os.name

_LifecycleEntry = tuple[str, str, tuple[str, ...], tuple[str, ...]]


def _ticket_opening_commitment(ticket: PaperTicket) -> tuple[object, ...]:
    """Return detached immutable opening facts that authorized PAPER economics."""
    leg_commitments = tuple(
        (
            leg.event_id,
            leg.market_id,
            leg.selection_id,
            leg.locked_odds,
            leg.sport,
            leg.exchange_side,
            leg.market_semantics_id,
        )
        for leg in ticket.legs
    )
    return (
        ticket.stake,
        leg_commitments,
        ticket.placed_at,
        ticket.strategy_reason,
        ticket.provider_source_ids,
        ticket.provider_accounts,
        ticket.bankroll_id,
        ticket.currency,
    )


def _make_ticket_opening_authority_registry():
    # Opening economics are product-issued facts. Keep the authoritative copy
    # outside caller-visible PaperTicket fields so coherent field rewrites cannot
    # become their own witness. Object identity, not caller-overridable hashing or
    # equality, selects the authority record.
    authorities: dict[
        int,
        tuple[
            weakref.ReferenceType[object],
            dict[str, tuple[object, ...]],
        ],
    ] = {}
    guard = threading.RLock()

    def _entry(book: object):
        entry = authorities.get(id(book))
        if entry is None or entry[0]() is not book:
            return None
        return entry

    def register_book(book: object) -> None:
        identity = id(book)

        def cleanup(reference: weakref.ReferenceType[object]) -> None:
            with guard:
                current = authorities.get(identity)
                if current is not None and current[0] is reference:
                    authorities.pop(identity, None)

        reference = weakref.ref(book, cleanup)
        with guard:
            current = authorities.get(identity)
            if current is not None and current[0]() is book:
                raise RuntimeError(
                    "PaperBook opening authority registry is already registered"
                )
            authorities[identity] = (reference, {})

    def record(book: object, ticket: PaperTicket) -> None:
        commitment = _ticket_opening_commitment(ticket)
        with guard:
            entry = _entry(book)
            if entry is None:
                raise RuntimeError("PaperBook opening authority registry is unavailable")
            current = entry[1]
            existing = current.get(ticket.ticket_id)
            if existing is not None and existing != commitment:
                raise ValueError("PaperBook ticket opening authority cannot be rebound")
            current[ticket.ticket_id] = commitment

    def revoke(book: object) -> None:
        with guard:
            entry = _entry(book)
            if entry is not None:
                authorities.pop(id(book), None)

    def install_validated_snapshot(book: object) -> None:
        commitments = {
            ticket_id: _ticket_opening_commitment(ticket)
            for ticket_id, ticket in book.tickets.items()
        }
        with guard:
            entry = _entry(book)
            if entry is None:
                raise RuntimeError("PaperBook opening authority registry is unavailable")
            authorities[id(book)] = (entry[0], commitments)

    def require_current(book: object) -> None:
        with guard:
            entry = _entry(book)
            if entry is None:
                raise ValueError(
                    "PaperBook byte-loaded snapshot lacks product-issued opening authority"
                )
            expected = dict(entry[1])
        if set(expected) != set(book.tickets):
            raise ValueError(
                "PaperBook ticket set changed outside product-issued opening authority"
            )
        for ticket_id, ticket in book.tickets.items():
            if expected[ticket_id] != _ticket_opening_commitment(ticket):
                raise ValueError(
                    "PaperBook ticket opening economic identity changed after admission"
                )

    def require_candidate(source_book: object, candidate_book: object) -> None:
        with guard:
            entry = _entry(source_book)
            if entry is None:
                raise ValueError(
                    "PaperBook byte-loaded snapshot lacks product-issued opening authority"
                )
            expected = dict(entry[1])
        candidate_tickets = getattr(candidate_book, "tickets", None)
        if type(candidate_tickets) is not dict or set(candidate_tickets) != set(expected):
            raise ValueError(
                "PaperBook serialized candidate ticket set differs from product-issued opening authority"
            )
        for ticket_id, ticket in candidate_tickets.items():
            if (
                type(ticket) is not _CANONICAL_PAPER_TICKET_TYPE
                or _ticket_opening_commitment(ticket) != expected[ticket_id]
            ):
                raise ValueError(
                    "PaperBook serialized candidate opening economic identity differs from product-issued authority"
                )

    return (
        register_book,
        record,
        revoke,
        install_validated_snapshot,
        require_current,
        require_candidate,
    )


(
    _register_ticket_opening_authority_book,
    _record_ticket_opening_authority,
    _revoke_ticket_opening_authority,
    _install_validated_ticket_opening_authority,
    _require_ticket_opening_authority,
    _require_snapshot_candidate_opening_authority,
) = _make_ticket_opening_authority_registry()


def _paperbook_causal_history_snapshot(book: object) -> tuple[object, ...]:
    lifecycle = getattr(book, "_lifecycle", None)
    settlement_times = getattr(book, "_settlement_times", None)
    if type(lifecycle) is not list or type(settlement_times) is not dict:
        raise ValueError("PaperBook causal history state is not canonical")
    return (
        tuple(lifecycle),
        tuple(sorted(settlement_times.items())),
    )


def _make_paperbook_causal_history_authority_registry():
    # Lifecycle/settlement chronology is product-issued economic history. Keep
    # the authoritative copy outside caller-visible mutable PaperBook fields and
    # select records only by builtin object identity.
    authorities: dict[
        int,
        tuple[
            weakref.ReferenceType[object],
            tuple[object, ...],
        ],
    ] = {}
    guard = threading.RLock()

    def _entry(book: object):
        entry = authorities.get(id(book))
        if entry is None or entry[0]() is not book:
            return None
        return entry

    def register_book(book: object) -> None:
        identity = id(book)

        def cleanup(reference: weakref.ReferenceType[object]) -> None:
            with guard:
                current = authorities.get(identity)
                if current is not None and current[0] is reference:
                    authorities.pop(identity, None)

        reference = weakref.ref(book, cleanup)
        with guard:
            current = authorities.get(identity)
            if current is not None and current[0]() is book:
                raise RuntimeError(
                    "PaperBook causal history authority registry is already registered"
                )
            authorities[identity] = (reference, ((), ()))

    def revoke(book: object) -> None:
        with guard:
            entry = _entry(book)
            if entry is not None:
                authorities.pop(id(book), None)

    def install_validated_snapshot(book: object) -> None:
        snapshot = _paperbook_causal_history_snapshot(book)
        with guard:
            entry = _entry(book)
            if entry is None:
                raise RuntimeError(
                    "PaperBook causal history authority registry is unavailable"
                )
            authorities[id(book)] = (entry[0], snapshot)

    def require_current(book: object) -> None:
        actual = _paperbook_causal_history_snapshot(book)
        with guard:
            entry = _entry(book)
            expected = None if entry is None else entry[1]
        if expected is None:
            raise ValueError(
                "PaperBook byte-loaded snapshot lacks product-issued causal history authority"
            )
        if actual != expected:
            raise ValueError(
                "PaperBook causal history changed outside product-issued transitions"
            )

    def require_candidate(source_book: object, candidate_book: object) -> None:
        candidate = _paperbook_causal_history_snapshot(candidate_book)
        with guard:
            entry = _entry(source_book)
            expected = None if entry is None else entry[1]
        if expected is None:
            raise ValueError(
                "PaperBook byte-loaded snapshot lacks product-issued causal history authority"
            )
        if candidate != expected:
            raise ValueError(
                "PaperBook serialized candidate causal history differs from product-issued authority"
            )

    def advance_open(book: object, ticket_id: str) -> None:
        with guard:
            entry = _entry(book)
            if entry is None:
                raise ValueError(
                    "PaperBook byte-loaded snapshot lacks product-issued causal history authority"
                )
            lifecycle, settlement_times = entry[1]
            authorities[id(book)] = (
                entry[0],
                (
                    lifecycle + (("open", ticket_id, (), ()),),
                    settlement_times,
                ),
            )

    def advance_settle(
        book: object,
        ticket_id: str,
        winners: tuple[str, ...],
        voids: tuple[str, ...],
        settled_at: str | None,
    ) -> None:
        with guard:
            entry = _entry(book)
            if entry is None:
                raise ValueError(
                    "PaperBook byte-loaded snapshot lacks product-issued causal history authority"
                )
            lifecycle, settlement_times = entry[1]
            settlement_mapping = dict(settlement_times)
            if ticket_id in settlement_mapping:
                raise ValueError(
                    "PaperBook causal history settlement authority cannot be rebound"
                )
            settlement_mapping[ticket_id] = settled_at
            authorities[id(book)] = (
                entry[0],
                (
                    lifecycle + (("settle", ticket_id, winners, voids),),
                    tuple(sorted(settlement_mapping.items())),
                ),
            )

    return (
        register_book,
        revoke,
        install_validated_snapshot,
        require_current,
        require_candidate,
        advance_open,
        advance_settle,
    )


(
    _register_paperbook_causal_history_authority_book,
    _revoke_paperbook_causal_history_authority,
    _install_validated_paperbook_causal_history_authority,
    _require_paperbook_causal_history_authority,
    _require_snapshot_candidate_causal_history_authority,
    _advance_paperbook_causal_history_open,
    _advance_paperbook_causal_history_settle,
) = _make_paperbook_causal_history_authority_registry()


def _make_paperbook_operation_lock_registry():
    # Economic transitions must be linearizable per book. Keep synchronization
    # authority outside caller-visible state so snapshots cannot mint/replace it.
    # Key by builtin object identity rather than object hashing/equality so a
    # PaperBook subtype or runtime class swap cannot execute caller hooks while
    # selecting the economic serialization lock.
    locks: dict[
        int,
        tuple[weakref.ReferenceType[object], threading.RLock],
    ] = {}
    guard = threading.RLock()

    def register_book(book: object) -> None:
        identity = id(book)

        def cleanup(reference: weakref.ReferenceType[object]) -> None:
            with guard:
                current = locks.get(identity)
                if current is not None and current[0] is reference:
                    locks.pop(identity, None)

        reference = weakref.ref(book, cleanup)
        with guard:
            current = locks.get(identity)
            if current is not None and current[0]() is book:
                raise RuntimeError(
                    "PaperBook operation lock registry is already registered"
                )
            locks[identity] = (reference, threading.RLock())

    def require_lock(book: object):
        identity = id(book)
        with guard:
            entry = locks.get(identity)
        if entry is None or entry[0]() is not book:
            raise RuntimeError("PaperBook operation lock registry is unavailable")
        return entry[1]

    return register_book, require_lock


(
    _register_paperbook_operation_lock,
    _require_paperbook_operation_lock,
) = _make_paperbook_operation_lock_registry()

# Freeze the product-issued PAPER authority dispatchers used by the canonical
# PaperBook implementation. Public/private module-name rebinding must not be able
# to disable serialization, admission witnesses, snapshot candidate checks, or
# the load_bytes fail-closed revocation boundary.
_CANONICAL_REGISTER_OPERATION_LOCK = _register_paperbook_operation_lock
_CANONICAL_REQUIRE_OPERATION_LOCK = _require_paperbook_operation_lock
_CANONICAL_REGISTER_OPENING_AUTHORITY = _register_ticket_opening_authority_book
_CANONICAL_RECORD_OPENING_AUTHORITY = _record_ticket_opening_authority
_CANONICAL_REVOKE_OPENING_AUTHORITY = _revoke_ticket_opening_authority
_CANONICAL_INSTALL_OPENING_AUTHORITY = _install_validated_ticket_opening_authority
_CANONICAL_REQUIRE_OPENING_AUTHORITY = _require_ticket_opening_authority
_CANONICAL_REQUIRE_CANDIDATE_OPENING_AUTHORITY = (
    _require_snapshot_candidate_opening_authority
)
_CANONICAL_REGISTER_CAUSAL_AUTHORITY = (
    _register_paperbook_causal_history_authority_book
)
_CANONICAL_REVOKE_CAUSAL_AUTHORITY = _revoke_paperbook_causal_history_authority
_CANONICAL_INSTALL_CAUSAL_AUTHORITY = (
    _install_validated_paperbook_causal_history_authority
)
_CANONICAL_REQUIRE_CAUSAL_AUTHORITY = _require_paperbook_causal_history_authority
_CANONICAL_REQUIRE_CANDIDATE_CAUSAL_AUTHORITY = (
    _require_snapshot_candidate_causal_history_authority
)
_CANONICAL_ADVANCE_CAUSAL_OPEN = _advance_paperbook_causal_history_open
_CANONICAL_ADVANCE_CAUSAL_SETTLE = _advance_paperbook_causal_history_settle


def _serialized_paperbook_operation(method):
    @wraps(method)
    def serialized(self, *args, **kwargs):
        with _CANONICAL_REQUIRE_OPERATION_LOCK(self):
            return method(self, *args, **kwargs)

    return serialized


def _paper_decimal_context() -> Context:
    context = _CANONICAL_DECIMAL_CONTEXT_TYPE(
        prec=_CANONICAL_PAPER_DECIMAL_PRECISION,
        rounding=_CANONICAL_ROUND_HALF_EVEN,
        Emin=_CANONICAL_PAPER_DECIMAL_EMIN,
        Emax=_CANONICAL_PAPER_DECIMAL_EMAX,
    )
    context.traps[_CANONICAL_INVALID_OPERATION_SIGNAL] = True
    context.traps[_CANONICAL_OVERFLOW_SIGNAL] = True
    context.traps[_CANONICAL_UNDERFLOW_SIGNAL] = True
    context.clear_flags()
    return context


_CANONICAL_PAPER_DECIMAL_CONTEXT_FACTORY = _paper_decimal_context


_CANONICAL_PAPER_DECIMAL_CONTEXT = _paper_decimal_context


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"PaperBook snapshot contains duplicate JSON key: {key}")
        payload[key] = value
    return payload


def _reject_nonfinite_json_constant(value: str) -> None:
    raise ValueError(f"PaperBook snapshot contains non-finite JSON constant: {value}")


_CANONICAL_JSON_LOADS = json.loads
_CANONICAL_JSON_DUMP = json.dump
_CANONICAL_JSON_DUMPS = json.dumps
_CANONICAL_REJECT_DUPLICATE_JSON_KEYS = _reject_duplicate_json_keys
_CANONICAL_REJECT_NONFINITE_JSON_CONSTANT = _reject_nonfinite_json_constant
_CANONICAL_NAMED_TEMPORARY_FILE = tempfile.NamedTemporaryFile
_CANONICAL_OS_FSYNC = os.fsync
_CANONICAL_OS_REPLACE = os.replace


class PaperBook:
    """Virtual bankroll and auditable paper tickets. No real-money execution path exists."""

    def __init__(self, initial_bankroll: Decimal | str = Decimal("10000")) -> None:
        if type(self) is not __class__:
            raise TypeError("PaperBook economic authority requires the exact book type")
        _CANONICAL_REGISTER_OPERATION_LOCK(self)
        _CANONICAL_REGISTER_OPENING_AUTHORITY(self)
        _CANONICAL_REGISTER_CAUSAL_AUTHORITY(self)
        initial = _CANONICAL_DECIMAL_INPUT(
            initial_bankroll,
            "initial_bankroll",
        )
        if initial <= 0:
            raise ValueError("initial virtual bankroll must be positive")
        self.initial_bankroll = initial
        self.balance = self.initial_bankroll
        self.tickets: dict[str, PaperTicket] = {}
        # PaperBook owns the minimum lifecycle witness required to replay bankroll
        # chronology and settlement economics without changing the domain model.
        self._lifecycle: list[_LifecycleEntry] = []
        # Settlement timestamps are causal lifecycle evidence, not mutable ticket
        # decoration. Keeping them in a sidecar preserves the canonical four-field
        # lifecycle tuple used by rollback/state hashes while making timestamp
        # mutation mechanically detectable.
        self._settlement_times: dict[str, str | None] = {}

    @property
    @_serialized_paperbook_operation
    def committed_stake(self) -> Decimal:
        if type(self) is not __class__:
            raise TypeError("PaperBook economic authority requires the exact book type")
        _CANONICAL_VALIDATE_LOADED_STATE(self)
        _CANONICAL_REQUIRE_OPENING_AUTHORITY(self)
        _CANONICAL_REQUIRE_CAUSAL_AUTHORITY(self)
        try:
            with _CANONICAL_LOCALCONTEXT(
                _CANONICAL_PAPER_DECIMAL_CONTEXT_FACTORY()
            ) as context:
                total = _CANONICAL_PAPER_DECIMAL_TYPE("0")
                for ticket in self.tickets.values():
                    if ticket.status is _CANONICAL_TICKET_STATUS_OPEN:
                        total += ticket.stake
                if context.flags[_CANONICAL_INEXACT_SIGNAL]:
                    raise ValueError("PaperBook committed stake loses Decimal precision")
        except _CANONICAL_DECIMAL_EXCEPTION_TYPE as exc:
            raise ValueError(
                "PaperBook committed stake arithmetic is not representable"
            ) from exc
        _CANONICAL_REQUIRE_FINITE(total, "committed_stake")
        return total

    @classmethod
    def _canonical_decimal_input(cls, value: object, label: str) -> Decimal:
        if type(value) not in {_CANONICAL_PAPER_DECIMAL_TYPE, str, int, float}:
            raise ValueError(
                f"PaperBook {label} must be an exact built-in Decimal, string, integer or float"
            )
        if type(value) is _CANONICAL_PAPER_DECIMAL_TYPE:
            parsed = value
        else:
            if type(value) is str and len(value) > _CANONICAL_MAX_PAPER_DECIMAL_TEXT_CHARS:
                raise ValueError(
                    f"PaperBook {label} decimal text exceeds the canonical size limit"
                )
            try:
                parsed = _CANONICAL_PAPER_DECIMAL_TYPE(str(value))
            except (_CANONICAL_DECIMAL_EXCEPTION_TYPE, ValueError) as exc:
                raise ValueError(f"PaperBook {label} is not a valid Decimal value") from exc
        _CANONICAL_REQUIRE_FINITE(parsed, label)
        return parsed

    @classmethod
    def _debit_balance(cls, balance: Decimal, amount: Decimal) -> Decimal:
        _CANONICAL_REQUIRE_FINITE(balance, "balance")
        _CANONICAL_REQUIRE_FINITE(amount, "stake")
        if amount <= 0:
            raise ValueError("stake must be positive")
        if amount > balance:
            raise ValueError("insufficient virtual bankroll")
        try:
            with _CANONICAL_LOCALCONTEXT(_CANONICAL_PAPER_DECIMAL_CONTEXT_FACTORY()) as context:
                new_balance = balance - amount
                if context.flags[_CANONICAL_INEXACT_SIGNAL]:
                    raise ValueError("PaperBook stake debit loses Decimal precision")
        except _CANONICAL_DECIMAL_EXCEPTION_TYPE as exc:
            raise ValueError("PaperBook stake debit arithmetic is not representable") from exc
        return new_balance

    @_serialized_paperbook_operation
    def open_ticket(
        self,
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
        if type(self) is not __class__:
            raise TypeError("PaperBook economic authority requires the exact book type")
        _CANONICAL_VALIDATE_LOADED_STATE(self)
        _CANONICAL_REQUIRE_OPENING_AUTHORITY(self)
        _CANONICAL_REQUIRE_CAUSAL_AUTHORITY(self)
        amount = _CANONICAL_DECIMAL_INPUT(stake, "stake")
        new_balance = _CANONICAL_DEBIT_BALANCE(self.balance, amount)

        ticket_placed_at = _CANONICAL_VALIDATE_PLACED_AT(
            placed_at if placed_at is not None else _CANONICAL_UTC_NOW_ISO()
        )
        _CANONICAL_REQUIRE_UTF8_STRING(reason, "strategy_reason")
        (
            provider_source_ids,
            provider_accounts,
            bankroll_id,
            currency,
        ) = _CANONICAL_VALIDATE_TICKET_PROVENANCE(
            provider_source_ids,
            provider_accounts,
            bankroll_id,
            currency,
        )
        if type(legs) not in {list, tuple}:
            raise ValueError("ticket legs must be an exact list or tuple")
        ticket_legs = tuple(legs)
        if not ticket_legs:
            raise ValueError("ticket requires at least one leg")
        for leg in ticket_legs:
            _CANONICAL_VALIDATE_TICKET_LEG(leg)
        quote_keys = [leg.quote_key for leg in ticket_legs]
        if len(quote_keys) != len(set(quote_keys)):
            raise ValueError("ticket contains duplicate quote_key leg")
        ticket = _CANONICAL_PAPER_TICKET_CONSTRUCTOR(
            ticket_id=str(_CANONICAL_UUID4()),
            stake=amount,
            legs=ticket_legs,
            placed_at=ticket_placed_at,
            strategy_reason=reason,
            provider_source_ids=provider_source_ids,
            provider_accounts=provider_accounts,
            bankroll_id=bankroll_id,
            currency=currency,
        )
        _CANONICAL_RECORD_OPENING_AUTHORITY(self, ticket)
        self.balance = new_balance
        self.tickets[ticket.ticket_id] = ticket
        self._lifecycle.append(("open", ticket.ticket_id, (), ()))
        _CANONICAL_ADVANCE_CAUSAL_OPEN(self, ticket.ticket_id)
        return ticket

    @staticmethod
    def _normalize_resolution_keys(values: object, label: str) -> set[str]:
        if type(values) not in {set, frozenset, list, tuple}:
            raise ValueError(
                f"PaperBook {label} must be an exact built-in collection of quote keys"
            )
        if any(type(value) is not str or not value for value in values):
            raise ValueError(
                f"PaperBook {label} must contain non-empty string quote keys"
            )
        return set(values)

    @classmethod
    def _settlement_result(
        cls,
        ticket: PaperTicket,
        balance: Decimal,
        winning_quote_keys: set[str],
        void_quote_keys: set[str],
    ) -> tuple[TicketStatus, Decimal, Decimal]:
        _CANONICAL_REQUIRE_FINITE(balance, "balance")
        for leg in ticket.legs:
            _CANONICAL_VALIDATE_TICKET_LEG(leg, ticket_id=ticket.ticket_id)
        leg_settlement_keys = {leg.settlement_key for leg in ticket.legs}
        unknown_winners = winning_quote_keys - leg_settlement_keys
        unknown_voids = void_quote_keys - leg_settlement_keys
        if unknown_winners:
            raise ValueError("PaperBook settlement contains unknown winning settlement key")
        if unknown_voids:
            raise ValueError("PaperBook settlement contains unknown void settlement key")
        if winning_quote_keys & void_quote_keys:
            raise ValueError("PaperBook settlement key cannot be both winning and void")

        effective_legs = tuple(
            leg for leg in ticket.legs if leg.settlement_key not in void_quote_keys
        )
        if any(leg.settlement_key not in winning_quote_keys for leg in effective_legs):
            return _CANONICAL_TICKET_STATUS_LOST, _CANONICAL_PAPER_DECIMAL_TYPE("0"), balance

        status = _CANONICAL_TICKET_STATUS_VOID if not effective_legs else _CANONICAL_TICKET_STATUS_WON
        try:
            with _CANONICAL_LOCALCONTEXT(
                _CANONICAL_PAPER_DECIMAL_CONTEXT_FACTORY()
            ) as context:
                effective_odds = _CANONICAL_PAPER_DECIMAL_TYPE("1")
                for leg in effective_legs:
                    effective_odds *= leg.locked_odds
                payout = ticket.stake if status is _CANONICAL_TICKET_STATUS_VOID else ticket.stake * effective_odds
                _CANONICAL_REQUIRE_FINITE(payout, f"settlement payout for ticket {ticket.ticket_id}")
                if status is _CANONICAL_TICKET_STATUS_WON and payout <= ticket.stake:
                    raise ValueError(
                        "PaperBook winning settlement payout must exceed stake after canonical Decimal rounding"
                    )
                new_balance = balance + payout
                _CANONICAL_REQUIRE_FINITE(
                    new_balance,
                    f"balance after settling ticket {ticket.ticket_id}",
                )
                if context.flags[_CANONICAL_INEXACT_SIGNAL]:
                    raise ValueError("PaperBook settlement arithmetic loses Decimal precision")
                if payout != 0 and new_balance == balance:
                    raise ValueError("PaperBook settlement payout loses all Decimal balance effect")
        except _CANONICAL_DECIMAL_EXCEPTION_TYPE as exc:
            raise ValueError("PaperBook settlement arithmetic is not representable") from exc
        return status, payout, new_balance

    @_serialized_paperbook_operation
    def settle(
        self,
        ticket_id: str,
        winning_quote_keys: set[str],
        void_quote_keys: set[str] | None = None,
        *,
        settled_at: str | None = None,
    ) -> PaperTicket:
        if type(self) is not __class__:
            raise TypeError("PaperBook economic authority requires the exact book type")
        _CANONICAL_VALIDATE_LOADED_STATE(self)
        _CANONICAL_REQUIRE_OPENING_AUTHORITY(self)
        _CANONICAL_REQUIRE_CAUSAL_AUTHORITY(self)
        canonical_ticket_id = _CANONICAL_REQUIRE_CANONICAL_TEXT(ticket_id, "ticket_id")
        ticket = self.tickets[canonical_ticket_id]
        if ticket.status is not _CANONICAL_TICKET_STATUS_OPEN:
            raise ValueError("ticket already settled")

        winners = _CANONICAL_NORMALIZE_RESOLUTION_KEYS(winning_quote_keys, "winning_quote_keys")
        voids = (
            set()
            if void_quote_keys is None
            else _CANONICAL_NORMALIZE_RESOLUTION_KEYS(void_quote_keys, "void_quote_keys")
        )
        settlement_time = (
            None
            if settled_at is None
            else _CANONICAL_VALIDATE_SETTLED_AT(settled_at, ticket.placed_at)
        )
        status, payout, new_balance = _CANONICAL_SETTLEMENT_RESULT(
            ticket,
            self.balance,
            winners,
            voids,
        )

        ticket.payout = payout
        ticket.status = status
        ticket.settled_at = settlement_time
        self.balance = new_balance
        self._lifecycle.append(
            (
                "settle",
                ticket.ticket_id,
                tuple(sorted(winners)),
                tuple(sorted(voids)),
            )
        )
        self._settlement_times[ticket.ticket_id] = settlement_time
        _CANONICAL_ADVANCE_CAUSAL_SETTLE(
            self,
            ticket.ticket_id,
            tuple(sorted(winners)),
            tuple(sorted(voids)),
            settlement_time,
        )
        return ticket

    def _lifecycle_to_json(self) -> list[dict[str, object]]:
        payload: list[dict[str, object]] = []
        for action, ticket_id, winners, voids in self._lifecycle:
            if action == "open":
                payload.append({"action": "open", "ticket_id": ticket_id})
            else:
                payload.append(
                    {
                        "action": "settle",
                        "ticket_id": ticket_id,
                        "winning_quote_keys": list(winners),
                        "void_quote_keys": list(voids),
                        "settled_at": self._settlement_times[ticket_id],
                    }
                )
        return payload

    @staticmethod
    def _canonical_snapshot_path(path: object) -> Path:
        if type(path) not in {str, _CANONICAL_PAPER_PATH_TYPE}:
            raise TypeError(
                "PaperBook snapshot path must be exact str or exact Path"
            )
        try:
            raw = _CANONICAL_PAPER_PATH_CONSTRUCTOR(path)
            parent = _CANONICAL_PAPER_PATH_RESOLVE(raw.parent, strict=False)
            return parent / raw.name
        except (OSError, RuntimeError) as exc:
            raise ValueError(
                "PaperBook snapshot path cannot be canonically resolved"
            ) from exc

    @staticmethod
    def _fsync_snapshot_directory(directory: Path) -> None:
        """Persist a published snapshot directory entry where supported."""
        if _CANONICAL_OS_NAME == "nt":
            return
        descriptor = _CANONICAL_OS_OPEN(
            directory,
            _CANONICAL_OS_RDONLY | _CANONICAL_OS_DIRECTORY,
        )
        try:
            _CANONICAL_OS_FSYNC(descriptor)
        finally:
            _CANONICAL_OS_CLOSE(descriptor)

    @classmethod
    def _ensure_snapshot_parent_durable(cls, directory: Path) -> None:
        """Create missing snapshot directories and persist each published entry."""
        missing: list[Path] = []
        cursor = directory
        while not _CANONICAL_PAPER_PATH_EXISTS(cursor):
            missing.append(cursor)
            parent = cursor.parent
            if parent == cursor:
                break
            cursor = parent

        _CANONICAL_PAPER_PATH_MKDIR(directory, parents=True, exist_ok=True)
        for created in reversed(missing):
            _CANONICAL_FSYNC_SNAPSHOT_DIRECTORY(created.parent)

    @_serialized_paperbook_operation
    def save(self, path: str | Path) -> None:
        if type(self) is not __class__:
            raise TypeError("PaperBook economic authority requires the exact book type")
        # PaperBook and PaperTicket are intentionally mutable during a paper run.
        # Validate caller-visible state before consulting hidden authorities so a
        # hostile subclass cannot execute comparison hooks during fail-closed
        # authority checks, and do so before any durable replacement.
        _CANONICAL_VALIDATE_LOADED_STATE(self)
        _CANONICAL_REQUIRE_OPENING_AUTHORITY(self)
        _CANONICAL_REQUIRE_CAUSAL_AUTHORITY(self)
        destination = _CANONICAL_SNAPSHOT_PATH(path)
        raw = {
            "schema_version": _CANONICAL_PAPER_SNAPSHOT_SCHEMA_VERSION,
            "initial_bankroll": str(self.initial_bankroll),
            "balance": str(self.balance),
            "tickets": [
                {
                    "ticket_id": t.ticket_id,
                    "stake": str(t.stake),
                    "placed_at": t.placed_at,
                    "settled_at": t.settled_at,
                    "status": t.status.value,
                    "payout": str(t.payout),
                    "strategy_reason": t.strategy_reason,
                    "provider_source_ids": list(t.provider_source_ids),
                    "provider_accounts": [
                        {"source_id": source_id, "account_id": account_id}
                        for source_id, account_id in t.provider_accounts
                    ],
                    "bankroll_id": t.bankroll_id,
                    "currency": t.currency,
                    "legs": [
                        {
                            "event_id": leg.event_id,
                            "market_id": leg.market_id,
                            "selection_id": leg.selection_id,
                            "locked_odds": str(leg.locked_odds),
                            "sport": leg.sport,
                            "exchange_side": leg.exchange_side,
                            "market_semantics_id": leg.market_semantics_id,
                        }
                        for leg in t.legs
                    ],
                }
                for t in self.tickets.values()
            ],
            "lifecycle": _CANONICAL_LIFECYCLE_TO_JSON(self),
        }

        # The raw snapshot is detached from the mutable live object. Validate
        # that exact candidate against product-issued opening commitments before
        # any durable replacement, closing coherent mutation during collection.
        candidate = _CANONICAL_FROM_RAW_SNAPSHOT(raw)
        _CANONICAL_REQUIRE_CANDIDATE_OPENING_AUTHORITY(self, candidate)
        _CANONICAL_REQUIRE_CANDIDATE_CAUSAL_AUTHORITY(self, candidate)

        try:
            serialized = _CANONICAL_JSON_DUMPS(
                raw,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            serialized_bytes = serialized.encode("utf-8", errors="strict")
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise ValueError(
                "PaperBook snapshot is not canonically serializable"
            ) from exc
        if len(serialized_bytes) > _CANONICAL_MAX_PAPER_SNAPSHOT_BYTES:
            raise ValueError(
                "PaperBook snapshot exceeds the canonical byte-size limit"
            )

        # Structural/economic/authority/serialization validation is complete.
        # Only now may this operation publish new filesystem directory entries.
        _CANONICAL_ENSURE_SNAPSHOT_PARENT_DURABLE(destination.parent)

        temporary: Path | None = None
        try:
            with _CANONICAL_NAMED_TEMPORARY_FILE(
                "w",
                encoding="utf-8",
                newline="\n",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = _CANONICAL_PAPER_PATH_CONSTRUCTOR(handle.name)
                handle.write(serialized)
                handle.flush()
                _CANONICAL_OS_FSYNC(handle.fileno())
            _CANONICAL_OS_REPLACE(temporary, destination)
            temporary = None

            published_descriptor: int | None = None
            directory_descriptor: int | None = None
            try:
                published_descriptor = _CANONICAL_OS_OPEN(
                    destination,
                    (
                        _CANONICAL_OS_RDWR
                        if _CANONICAL_OS_NAME == "nt"
                        else _CANONICAL_OS_RDONLY
                    ),
                )
                _CANONICAL_OS_FSYNC(published_descriptor)
                if _CANONICAL_OS_NAME != "nt":
                    directory_descriptor = _CANONICAL_OS_OPEN(
                        destination.parent,
                        _CANONICAL_OS_RDONLY | _CANONICAL_OS_DIRECTORY,
                    )
                    _CANONICAL_OS_FSYNC(directory_descriptor)
            finally:
                for descriptor in (directory_descriptor, published_descriptor):
                    if descriptor is None:
                        continue
                    try:
                        _CANONICAL_OS_CLOSE(descriptor)
                    except OSError:
                        pass
        finally:
            if temporary is not None:
                try:
                    _CANONICAL_PAPER_PATH_UNLINK(temporary)
                except FileNotFoundError:
                    pass

    @staticmethod
    def _require_finite(value: object, label: str) -> None:
        if type(value) is not _CANONICAL_PAPER_DECIMAL_TYPE or not value.is_finite():
            raise ValueError(f"PaperBook snapshot contains non-finite {label}")

    @staticmethod
    def _require_utf8_string(value: object, label: str) -> str:
        if type(value) is not str:
            raise ValueError(f"PaperBook {label} must be a string")
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ValueError(f"PaperBook {label} must be valid UTF-8 text") from exc
        return value

    @classmethod
    def _require_canonical_text(
        cls,
        value: object,
        label: str,
        *,
        forbid_quote_key_delimiter: bool = False,
    ) -> str:
        text = _CANONICAL_REQUIRE_UTF8_STRING(value, label)
        if not text or text.strip() != text:
            raise ValueError(f"PaperBook {label} must be a non-empty trimmed string")
        if forbid_quote_key_delimiter and "|" in text:
            raise ValueError(f"PaperBook {label} must not contain quote-key delimiter '|'")
        return text

    @classmethod
    def _validate_ticket_provenance(
        cls,
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
            _CANONICAL_REQUIRE_CANONICAL_TEXT(source_id, "provider_source_id")
            for source_id in provider_source_ids
        )
        if (
            canonical_sources != tuple(sorted(canonical_sources))
            or len(canonical_sources) != len(set(canonical_sources))
        ):
            raise ValueError("PaperBook provider_source_ids must be sorted and unique")

        if type(provider_accounts) is not tuple:
            raise ValueError("PaperBook provider_accounts must be a canonical tuple")
        account_bindings: list[tuple[str, str]] = []
        for binding in provider_accounts:
            if type(binding) is not tuple or len(binding) != 2:
                raise ValueError(
                    "PaperBook provider_accounts must contain (source_id, account_id) tuples"
                )
            source_id, account_id = binding
            account_bindings.append(
                (
                    _CANONICAL_REQUIRE_CANONICAL_TEXT(source_id, "provider account source_id"),
                    _CANONICAL_REQUIRE_CANONICAL_TEXT(account_id, "provider account_id"),
                )
            )
        canonical_accounts = tuple(account_bindings)
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

        canonical_bankroll = _CANONICAL_REQUIRE_CANONICAL_TEXT(bankroll_id, "bankroll_id")
        canonical_currency = _CANONICAL_REQUIRE_CANONICAL_TEXT(currency, "currency")
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

    @classmethod
    def _validate_timestamp(cls, value: object, label: str) -> str:
        message = f"PaperBook {label} must be a non-empty trimmed timezone-aware ISO timestamp"
        try:
            text = _CANONICAL_REQUIRE_UTF8_STRING(value, label)
        except ValueError as exc:
            raise ValueError(message) from exc
        if not text or text.strip() != text:
            raise ValueError(message)
        try:
            _CANONICAL_PARSE_ISO_TIMESTAMP(text)
        except ValueError as exc:
            raise ValueError(message) from exc
        return text

    @classmethod
    def _validate_placed_at(cls, value: object, *, snapshot: bool = False) -> str:
        label = "snapshot placed_at" if snapshot else "placed_at"
        return _CANONICAL_VALIDATE_TIMESTAMP(value, label)

    @classmethod
    def _validate_settled_at(
        cls,
        value: object,
        placed_at: object,
        *,
        snapshot: bool = False,
    ) -> str:
        label = "snapshot settled_at" if snapshot else "settled_at"
        settled_text = _CANONICAL_VALIDATE_TIMESTAMP(value, label)
        placed_text = _CANONICAL_VALIDATE_PLACED_AT(placed_at, snapshot=snapshot)
        if _CANONICAL_PARSE_ISO_TIMESTAMP(settled_text) < _CANONICAL_PARSE_ISO_TIMESTAMP(placed_text):
            raise ValueError("PaperBook settled_at must not precede placed_at")
        return settled_text

    @classmethod
    def _validate_ticket_leg(cls, leg: object, *, ticket_id: str | None = None) -> TicketLeg:
        if type(leg) is not _CANONICAL_TICKET_LEG_TYPE:
            raise ValueError("PaperBook ticket legs must be canonical TicketLeg values")
        suffix = f" for ticket {ticket_id}" if ticket_id is not None else ""
        _CANONICAL_REQUIRE_CANONICAL_TEXT(
            leg.event_id,
            f"event_id{suffix}",
            forbid_quote_key_delimiter=True,
        )
        _CANONICAL_REQUIRE_CANONICAL_TEXT(
            leg.market_id,
            f"market_id{suffix}",
            forbid_quote_key_delimiter=True,
        )
        _CANONICAL_REQUIRE_CANONICAL_TEXT(
            leg.selection_id,
            f"selection_id{suffix}",
            forbid_quote_key_delimiter=True,
        )
        if leg.sport is not None:
            sport = _CANONICAL_REQUIRE_CANONICAL_TEXT(
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
        if leg.exchange_side is not None:
            exchange_side = _CANONICAL_REQUIRE_CANONICAL_TEXT(
                leg.exchange_side,
                f"exchange_side{suffix}",
                forbid_quote_key_delimiter=True,
            )
            if exchange_side not in {"back", "lay"}:
                raise ValueError(
                    "PaperBook ticket exchange_side must be canonical 'back' or 'lay'"
                )
            if exchange_side == "lay":
                raise ValueError(
                    "PaperBook LAY economic materialization is not supported"
                )
        if leg.market_semantics_id is not None:
            try:
                _CANONICAL_SEMANTIC_IDENTITY(
                    leg.market_semantics_id,
                    f"market_semantics_id{suffix}",
                )
            except ValueError as exc:
                raise ValueError(
                    "PaperBook ticket market_semantics_id must be canonical"
                ) from exc
        _CANONICAL_REQUIRE_FINITE(leg.locked_odds, f"locked_odds{suffix}")
        if leg.locked_odds <= 1:
            raise ValueError("PaperBook snapshot decimal odds must be greater than 1")
        return leg

    @classmethod
    def _validate_lifecycle_entry(cls, entry: object) -> _LifecycleEntry:
        if type(entry) is not tuple or len(entry) != 4:
            raise ValueError("PaperBook lifecycle entries must be canonical tuples")
        action, ticket_id, winners, voids = entry
        if type(action) is not str or action not in {"open", "settle"}:
            raise ValueError("PaperBook lifecycle action must be canonical open or settle text")
        _CANONICAL_REQUIRE_CANONICAL_TEXT(ticket_id, "lifecycle ticket_id")
        if type(winners) is not tuple or type(voids) is not tuple:
            raise ValueError("PaperBook lifecycle settlement keys must be canonical tuples")
        for values, label in ((winners, "winning_quote_keys"), (voids, "void_quote_keys")):
            for value in values:
                _CANONICAL_REQUIRE_CANONICAL_TEXT(value, f"lifecycle {label}")
            if values != tuple(sorted(values)) or len(values) != len(set(values)):
                raise ValueError(f"PaperBook lifecycle {label} must be sorted and unique")
        if action == "open" and (winners or voids):
            raise ValueError("PaperBook lifecycle open action cannot contain settlement keys")
        return action, ticket_id, winners, voids

    @classmethod
    def _validate_lifecycle_reachability(cls, book: "PaperBook") -> None:
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
            action, ticket_id, winners_raw, voids_raw = _CANONICAL_VALIDATE_LIFECYCLE_ENTRY(
                raw_entry
            )
            ticket = book.tickets.get(ticket_id)
            if ticket is None:
                raise ValueError("PaperBook lifecycle references unknown ticket_id")

            if action == "open":
                if ticket_id in opened:
                    raise ValueError("PaperBook lifecycle opens a ticket more than once")
                try:
                    replay_balance = _CANONICAL_DEBIT_BALANCE(replay_balance, ticket.stake)
                except ValueError as exc:
                    raise ValueError(
                        f"PaperBook lifecycle stake for ticket {ticket_id} was not affordable"
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
                _CANONICAL_VALIDATE_SETTLED_AT(
                    settlement_time,
                    ticket.placed_at,
                    snapshot=True,
                )
            winners = set(winners_raw)
            voids = set(voids_raw)
            status, payout, replay_balance = _CANONICAL_SETTLEMENT_RESULT(
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
            if ticket_id not in settled and ticket.status is not _CANONICAL_TICKET_STATUS_OPEN:
                raise ValueError(
                    f"PaperBook ticket {ticket_id} settled state is missing lifecycle provenance"
                )
            if ticket_id in settled and ticket.status is _CANONICAL_TICKET_STATUS_OPEN:
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

    @classmethod
    def _validate_loaded_state(cls, book: "PaperBook") -> None:
        _CANONICAL_REQUIRE_FINITE(book.initial_bankroll, "initial_bankroll")
        _CANONICAL_REQUIRE_FINITE(book.balance, "balance")
        if book.initial_bankroll <= 0:
            raise ValueError("PaperBook snapshot initial_bankroll must be positive")
        if book.balance < 0:
            raise ValueError("PaperBook snapshot balance cannot be negative")
        if type(book.tickets) is not dict:
            raise ValueError("PaperBook tickets must be a canonical ticket mapping")

        for ticket_key, ticket in book.tickets.items():
            _CANONICAL_REQUIRE_CANONICAL_TEXT(ticket_key, "ticket mapping key")
            if type(ticket) is not _CANONICAL_PAPER_TICKET_TYPE:
                raise ValueError("PaperBook tickets must contain canonical PaperTicket values")
            _CANONICAL_REQUIRE_CANONICAL_TEXT(ticket.ticket_id, "ticket_id")
            if ticket_key != ticket.ticket_id:
                raise ValueError("PaperBook ticket mapping key must match ticket_id")
            _CANONICAL_VALIDATE_PLACED_AT(ticket.placed_at, snapshot=True)
            if ticket.settled_at is not None:
                _CANONICAL_VALIDATE_SETTLED_AT(
                    ticket.settled_at,
                    ticket.placed_at,
                    snapshot=True,
                )
            if ticket.status is _CANONICAL_TICKET_STATUS_OPEN and ticket.settled_at is not None:
                raise ValueError("PaperBook snapshot open ticket cannot have settled_at")
            _CANONICAL_REQUIRE_UTF8_STRING(ticket.strategy_reason, "snapshot strategy_reason")
            _CANONICAL_VALIDATE_TICKET_PROVENANCE(
                ticket.provider_source_ids,
                ticket.provider_accounts,
                ticket.bankroll_id,
                ticket.currency,
            )
            if type(ticket.status) is not _CANONICAL_TICKET_STATUS_TYPE:
                raise ValueError("PaperBook snapshot ticket status must be canonical TicketStatus")
            _CANONICAL_REQUIRE_FINITE(ticket.stake, f"stake for ticket {ticket.ticket_id}")
            _CANONICAL_REQUIRE_FINITE(ticket.payout, f"payout for ticket {ticket.ticket_id}")
            if ticket.stake <= 0:
                raise ValueError("PaperBook snapshot ticket stake must be positive")
            if ticket.payout < 0:
                raise ValueError("PaperBook snapshot ticket payout cannot be negative")
            if type(ticket.legs) is not tuple or not ticket.legs:
                raise ValueError("PaperBook snapshot ticket requires a canonical non-empty leg tuple")
            for leg in ticket.legs:
                _CANONICAL_VALIDATE_TICKET_LEG(leg, ticket_id=ticket.ticket_id)
            quote_keys = [leg.quote_key for leg in ticket.legs]
            if len(quote_keys) != len(set(quote_keys)):
                raise ValueError("PaperBook snapshot ticket contains duplicate quote_key leg")

            if ticket.status in {_CANONICAL_TICKET_STATUS_OPEN, _CANONICAL_TICKET_STATUS_LOST} and ticket.payout != 0:
                raise ValueError("PaperBook snapshot open/lost ticket payout must be zero")
            if ticket.status is _CANONICAL_TICKET_STATUS_VOID and ticket.payout != ticket.stake:
                raise ValueError("PaperBook snapshot void ticket payout must equal stake")
            if ticket.status is _CANONICAL_TICKET_STATUS_WON and ticket.payout <= ticket.stake:
                raise ValueError("PaperBook snapshot won ticket payout must exceed stake")

        _CANONICAL_VALIDATE_LIFECYCLE_REACHABILITY(book)

    @classmethod
    def _parse_lifecycle_key_list(cls, value: object, label: str) -> tuple[str, ...]:
        if type(value) is not list:
            raise ValueError(f"PaperBook snapshot lifecycle {label} must be a list")
        for item in value:
            _CANONICAL_REQUIRE_CANONICAL_TEXT(item, f"snapshot lifecycle {label}")
        normalized = tuple(value)
        if normalized != tuple(sorted(normalized)) or len(normalized) != len(set(normalized)):
            raise ValueError(
                f"PaperBook snapshot lifecycle {label} must be sorted and unique"
            )
        return normalized

    @classmethod
    def _parse_lifecycle(
        cls,
        value: object,
        schema_version: int,
    ) -> tuple[list[_LifecycleEntry], dict[str, str | None]]:
        if type(value) is not list:
            raise ValueError("PaperBook snapshot lifecycle must be a list")
        entries: list[_LifecycleEntry] = []
        settlement_times: dict[str, str | None] = {}
        for item in value:
            if type(item) is not dict:
                raise ValueError("PaperBook snapshot lifecycle entry must be an object")
            action = item.get("action")
            ticket_id = item.get("ticket_id")
            if action == "open":
                if set(item) != {"action", "ticket_id"}:
                    raise ValueError("PaperBook snapshot open lifecycle entry has unexpected fields")
                entry: _LifecycleEntry = ("open", ticket_id, (), ())
            elif action == "settle":
                expected_fields = {
                    "action",
                    "ticket_id",
                    "winning_quote_keys",
                    "void_quote_keys",
                }
                if schema_version >= 5:
                    expected_fields.add("settled_at")
                if set(item) != expected_fields:
                    raise ValueError("PaperBook snapshot settle lifecycle entry has unexpected fields")
                settled_at = item.get("settled_at") if schema_version >= 5 else None
                if settled_at is not None:
                    _CANONICAL_VALIDATE_TIMESTAMP(
                        settled_at,
                        "snapshot lifecycle settled_at",
                    )
                entry = (
                    "settle",
                    ticket_id,
                    _CANONICAL_PARSE_LIFECYCLE_KEY_LIST(
                        item["winning_quote_keys"], "winning_quote_keys"
                    ),
                    _CANONICAL_PARSE_LIFECYCLE_KEY_LIST(
                        item["void_quote_keys"], "void_quote_keys"
                    ),
                )
            else:
                raise ValueError("PaperBook snapshot lifecycle action must be open or settle")
            canonical_entry = _CANONICAL_VALIDATE_LIFECYCLE_ENTRY(entry)
            entries.append(canonical_entry)
            if action == "settle":
                settlement_times[canonical_entry[1]] = settled_at
        return entries, settlement_times

    @classmethod
    def _parse_snapshot_decimal(cls, value: object, label: str) -> Decimal:
        if type(value) is not str or not value or value.strip() != value:
            raise ValueError(
                f"PaperBook snapshot {label} must be a non-empty trimmed decimal string"
            )
        if len(value) > _CANONICAL_MAX_PAPER_DECIMAL_TEXT_CHARS:
            raise ValueError(
                f"PaperBook snapshot {label} decimal text exceeds the canonical size limit"
            )
        try:
            parsed = _CANONICAL_PAPER_DECIMAL_TYPE(value)
        except _CANONICAL_DECIMAL_EXCEPTION_TYPE as exc:
            raise ValueError(f"PaperBook snapshot {label} is not a valid Decimal string") from exc
        _CANONICAL_REQUIRE_FINITE(parsed, label)
        return parsed

    @staticmethod
    def _required_snapshot_field(
        mapping: dict[str, object], field: str, context: str
    ) -> object:
        if field not in mapping:
            raise ValueError(
                f"PaperBook snapshot {context} is missing required field: {field}"
            )
        return mapping[field]

    @classmethod
    def _parse_snapshot_legs(
        cls,
        value: object,
        ticket_id: str,
        *,
        schema_version: int | None,
    ) -> tuple[TicketLeg, ...]:
        if type(value) is not list:
            raise ValueError(f"PaperBook snapshot legs for ticket {ticket_id} must be a list")
        legs: list[TicketLeg] = []
        for index, raw_leg in enumerate(value):
            if type(raw_leg) is not dict:
                raise ValueError(
                    f"PaperBook snapshot leg {index} for ticket {ticket_id} must be an object"
                )
            sport = (
                _CANONICAL_REQUIRED_SNAPSHOT_FIELD(raw_leg, "sport", "ticket leg")
                if schema_version is not None and schema_version >= 6
                else None
            )
            exchange_side = (
                _CANONICAL_REQUIRED_SNAPSHOT_FIELD(raw_leg, "exchange_side", "ticket leg")
                if schema_version is not None and schema_version >= 7
                else None
            )
            if (
                (schema_version is None or schema_version < 8)
                and "market_semantics_id" in raw_leg
            ):
                raise ValueError(
                    "ticket leg market_semantics_id is unsupported before schema 8"
                )
            expected_leg_fields = {
                "event_id",
                "market_id",
                "selection_id",
                "locked_odds",
            }
            if schema_version is not None and schema_version >= 6:
                expected_leg_fields.add("sport")
            if schema_version is not None and schema_version >= 7:
                expected_leg_fields.add("exchange_side")
            if schema_version is not None and schema_version >= 8:
                expected_leg_fields.add("market_semantics_id")
            unexpected_leg_fields = set(raw_leg) - expected_leg_fields
            if unexpected_leg_fields:
                version_label = (
                    "legacy" if schema_version is None else f"schema {schema_version}"
                )
                raise ValueError(
                    f"PaperBook snapshot {version_label} ticket leg contains unexpected fields"
                )
            market_semantics_id = (
                _CANONICAL_REQUIRED_SNAPSHOT_FIELD(
                    raw_leg,
                    "market_semantics_id",
                    "ticket leg",
                )
                if schema_version is not None and schema_version >= 8
                else None
            )
            if sport is not None:
                _CANONICAL_REQUIRE_CANONICAL_TEXT(
                    sport,
                    f"sport for ticket {ticket_id}",
                    forbid_quote_key_delimiter=True,
                )
            legs.append(
                _CANONICAL_TICKET_LEG_CONSTRUCTOR(
                    _CANONICAL_REQUIRED_SNAPSHOT_FIELD(raw_leg, "event_id", "ticket leg"),
                    _CANONICAL_REQUIRED_SNAPSHOT_FIELD(raw_leg, "market_id", "ticket leg"),
                    _CANONICAL_REQUIRED_SNAPSHOT_FIELD(raw_leg, "selection_id", "ticket leg"),
                    _CANONICAL_PARSE_SNAPSHOT_DECIMAL(
                        _CANONICAL_REQUIRED_SNAPSHOT_FIELD(raw_leg, "locked_odds", "ticket leg"),
                        f"locked_odds for ticket {ticket_id}",
                    ),
                    sport=sport,
                    exchange_side=exchange_side,
                    market_semantics_id=market_semantics_id,
                )
            )
        return tuple(legs)

    @classmethod
    def _parse_snapshot_provider_accounts(
        cls, value: object, ticket_id: str
    ) -> tuple[tuple[str, str], ...]:
        if type(value) is not list:
            raise ValueError(
                f"PaperBook snapshot provider_accounts for ticket {ticket_id} must be a list"
            )
        bindings: list[tuple[str, str]] = []
        for index, raw_binding in enumerate(value):
            if type(raw_binding) is not dict or set(raw_binding) != {"source_id", "account_id"}:
                raise ValueError(
                    "PaperBook snapshot provider account binding "
                    f"{index} for ticket {ticket_id} must contain exactly source_id and account_id"
                )
            bindings.append(
                (
                    _CANONICAL_REQUIRE_CANONICAL_TEXT(
                        raw_binding["source_id"], "snapshot provider account source_id"
                    ),
                    _CANONICAL_REQUIRE_CANONICAL_TEXT(
                        raw_binding["account_id"], "snapshot provider account_id"
                    ),
                )
            )
        return tuple(bindings)

    @staticmethod
    def _parse_snapshot_status(value: object, ticket_id: str) -> TicketStatus:
        if type(value) is not str:
            raise ValueError(
                f"PaperBook snapshot status for ticket {ticket_id} must be a string"
            )
        try:
            return _CANONICAL_TICKET_STATUS_TYPE(value)
        except ValueError as exc:
            raise ValueError(
                f"PaperBook snapshot status for ticket {ticket_id} is invalid"
            ) from exc

    @classmethod
    def _from_raw_snapshot(cls, raw: object) -> "PaperBook":
        if type(raw) is not dict:
            raise ValueError("PaperBook snapshot root must be an object")
        schema_version = raw.get("schema_version", _CANONICAL_SCHEMA_MISSING)
        is_legacy = schema_version is _CANONICAL_SCHEMA_MISSING
        if not is_legacy and (
            type(schema_version) is not int
            or schema_version not in _CANONICAL_SUPPORTED_PAPER_SNAPSHOT_SCHEMA_VERSIONS
        ):
            raise ValueError("unsupported PaperBook snapshot schema_version")
        expected_root_fields = (
            {"initial_bankroll", "balance", "tickets"}
            if is_legacy
            else {
                "schema_version",
                "initial_bankroll",
                "balance",
                "tickets",
                "lifecycle",
            }
        )
        unexpected_root_fields = set(raw) - expected_root_fields
        if unexpected_root_fields:
            version_label = "legacy" if is_legacy else f"schema {schema_version}"
            raise ValueError(
                f"PaperBook snapshot {version_label} root contains unexpected fields"
            )

        initial_bankroll = _CANONICAL_PARSE_SNAPSHOT_DECIMAL(
            _CANONICAL_REQUIRED_SNAPSHOT_FIELD(raw, "initial_bankroll", "root"),
            "initial_bankroll",
        )
        book = cls(initial_bankroll)
        book.balance = _CANONICAL_PARSE_SNAPSHOT_DECIMAL(
            _CANONICAL_REQUIRED_SNAPSHOT_FIELD(raw, "balance", "root"),
            "balance",
        )
        tickets_raw = _CANONICAL_REQUIRED_SNAPSHOT_FIELD(raw, "tickets", "root")
        if type(tickets_raw) is not list:
            raise ValueError("PaperBook snapshot tickets must be a list")
        seen_ticket_ids: set[str] = set()
        for item in tickets_raw:
            if type(item) is not dict:
                raise ValueError("PaperBook snapshot ticket must be an object")
            expected_ticket_fields = {
                "ticket_id",
                "stake",
                "legs",
                "placed_at",
                "status",
                "payout",
                "strategy_reason",
            }
            if not is_legacy and schema_version >= 3:
                expected_ticket_fields.update(
                    {"provider_source_ids", "bankroll_id", "currency"}
                )
            if not is_legacy and schema_version >= 4:
                expected_ticket_fields.add("provider_accounts")
            if not is_legacy and schema_version >= 5:
                expected_ticket_fields.add("settled_at")
            unexpected_ticket_fields = set(item) - expected_ticket_fields
            if unexpected_ticket_fields:
                version_label = "legacy" if is_legacy else f"schema {schema_version}"
                raise ValueError(
                    f"PaperBook snapshot {version_label} ticket contains unexpected fields"
                )
            ticket_id = _CANONICAL_REQUIRE_CANONICAL_TEXT(
                _CANONICAL_REQUIRED_SNAPSHOT_FIELD(item, "ticket_id", "ticket"),
                "snapshot ticket_id",
            )
            if ticket_id in seen_ticket_ids:
                raise ValueError("PaperBook snapshot contains duplicate ticket_id")
            seen_ticket_ids.add(ticket_id)
            if schema_version in {3, 4, 5, 6, 7, 8}:
                provider_source_ids_raw = _CANONICAL_REQUIRED_SNAPSHOT_FIELD(
                    item, "provider_source_ids", f"ticket {ticket_id}"
                )
                if type(provider_source_ids_raw) is not list:
                    raise ValueError(
                        f"PaperBook snapshot provider_source_ids for ticket {ticket_id} must be a list"
                    )
                provider_source_ids = tuple(provider_source_ids_raw)
                provider_accounts = (
                    _CANONICAL_PARSE_SNAPSHOT_PROVIDER_ACCOUNTS(
                        _CANONICAL_REQUIRED_SNAPSHOT_FIELD(
                            item, "provider_accounts", f"ticket {ticket_id}"
                        ),
                        ticket_id,
                    )
                    if schema_version in {4, 5, 6, 7, 8}
                    else ()
                )
                bankroll_id = _CANONICAL_REQUIRED_SNAPSHOT_FIELD(
                    item, "bankroll_id", f"ticket {ticket_id}"
                )
                currency = _CANONICAL_REQUIRED_SNAPSHOT_FIELD(
                    item, "currency", f"ticket {ticket_id}"
                )
            else:
                provider_source_ids = ()
                provider_accounts = ()
                bankroll_id = None
                currency = None

            ticket = _CANONICAL_PAPER_TICKET_CONSTRUCTOR(
                ticket_id=ticket_id,
                stake=_CANONICAL_PARSE_SNAPSHOT_DECIMAL(
                    _CANONICAL_REQUIRED_SNAPSHOT_FIELD(item, "stake", f"ticket {ticket_id}"),
                    f"stake for ticket {ticket_id}",
                ),
                legs=_CANONICAL_PARSE_SNAPSHOT_LEGS(
                    _CANONICAL_REQUIRED_SNAPSHOT_FIELD(item, "legs", f"ticket {ticket_id}"),
                    ticket_id,
                    schema_version=None if is_legacy else schema_version,
                ),
                placed_at=_CANONICAL_REQUIRED_SNAPSHOT_FIELD(
                    item, "placed_at", f"ticket {ticket_id}"
                ),
                settled_at=(
                    _CANONICAL_REQUIRED_SNAPSHOT_FIELD(
                        item, "settled_at", f"ticket {ticket_id}"
                    )
                    if schema_version in {5, 6, 7, 8}
                    else None
                ),
                status=_CANONICAL_PARSE_SNAPSHOT_STATUS(
                    _CANONICAL_REQUIRED_SNAPSHOT_FIELD(item, "status", f"ticket {ticket_id}"),
                    ticket_id,
                ),
                payout=_CANONICAL_PARSE_SNAPSHOT_DECIMAL(
                    _CANONICAL_REQUIRED_SNAPSHOT_FIELD(item, "payout", f"ticket {ticket_id}"),
                    f"payout for ticket {ticket_id}",
                ),
                strategy_reason=item.get("strategy_reason", ""),
                provider_source_ids=provider_source_ids,
                provider_accounts=provider_accounts,
                bankroll_id=bankroll_id,
                currency=currency,
            )
            book.tickets[ticket.ticket_id] = ticket

        if is_legacy:
            # Legacy snapshots did not persist settlement chronology or resolution
            # witnesses. Open-only books are still exactly replayable from ticket
            # insertion order. Settled legacy books fail closed later in lifecycle
            # validation after all older structural/status invariants have run.
            book._lifecycle = [
                ("open", ticket_id, (), ())
                for ticket_id in book.tickets
            ]
            book._settlement_times = {}
        else:
            if "lifecycle" not in raw:
                raise ValueError(
                    f"PaperBook snapshot schema {schema_version} requires lifecycle provenance"
                )
            book._lifecycle, book._settlement_times = _CANONICAL_PARSE_LIFECYCLE(
                raw["lifecycle"],
                schema_version,
            )

        _CANONICAL_VALIDATE_LOADED_STATE(book)
        # Decoding arbitrary bytes proves structure only. It must not mint the
        # product-issued opening authority needed for economic mutation/readout.
        _CANONICAL_REVOKE_OPENING_AUTHORITY(book)
        _CANONICAL_REVOKE_CAUSAL_AUTHORITY(book)
        return book

    @classmethod
    def load_bytes(cls, payload: bytes) -> "PaperBook":
        if cls is not __class__:
            raise TypeError("PaperBook economic authority requires the exact book type")
        if type(payload) is not bytes:
            raise TypeError("PaperBook.load_bytes payload must be exact bytes")
        if len(payload) > _CANONICAL_MAX_PAPER_SNAPSHOT_BYTES:
            raise ValueError("PaperBook snapshot exceeds the canonical byte-size limit")
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("PaperBook snapshot must be valid UTF-8") from exc
        try:
            raw = _CANONICAL_JSON_LOADS(
                text,
                object_pairs_hook=_CANONICAL_REJECT_DUPLICATE_JSON_KEYS,
                parse_constant=_CANONICAL_REJECT_NONFINITE_JSON_CONSTANT,
            )
        except RecursionError as exc:
            raise ValueError("PaperBook snapshot JSON nesting is too deep") from exc
        return _CANONICAL_FROM_RAW_SNAPSHOT(raw)

    @classmethod
    def load(cls, path: str | Path) -> "PaperBook":
        if cls is not __class__:
            raise TypeError("PaperBook economic authority requires the exact book type")
        snapshot_path = _CANONICAL_SNAPSHOT_PATH(path)
        descriptor: int | None = None
        verification_descriptor: int | None = None
        post_read_descriptor: int | None = None
        try:
            descriptor = _CANONICAL_OS_OPEN(snapshot_path, _CANONICAL_OS_RDONLY)
            opened = _CANONICAL_OS_FSTAT(descriptor)
            path_stat = _CANONICAL_OS_STAT(snapshot_path, follow_symlinks=False)
            if not _CANONICAL_STAT_ISREG(opened.st_mode) or not _CANONICAL_STAT_ISREG(path_stat.st_mode):
                raise ValueError(
                    "PaperBook snapshot must be a regular non-symlink file"
                )
            if opened.st_nlink != 1 or path_stat.st_nlink != 1:
                raise ValueError("PaperBook snapshot must not have hard-link aliases")
            with _CANONICAL_OS_FDOPEN(descriptor, "rb", closefd=False) as handle:
                payload = handle.read(_CANONICAL_MAX_PAPER_SNAPSHOT_BYTES + 1)

            verification_descriptor = _CANONICAL_OS_OPEN(snapshot_path, _CANONICAL_OS_RDONLY)
            verification = _CANONICAL_OS_FSTAT(verification_descriptor)
            if (
                not _CANONICAL_STAT_ISREG(verification.st_mode)
                or verification.st_nlink != 1
                or not _CANONICAL_OS_SAMEOPENFILE(descriptor, verification_descriptor)
            ):
                raise ValueError("PaperBook snapshot changed during verified read")
            with _CANONICAL_OS_FDOPEN(
                verification_descriptor,
                "rb",
                closefd=False,
            ) as verification_handle:
                verification_payload = verification_handle.read(
                    _CANONICAL_MAX_PAPER_SNAPSHOT_BYTES + 1
                )
            if payload != verification_payload:
                raise ValueError(
                    "PaperBook snapshot bytes changed during verified read"
                )

            post_read_descriptor = _CANONICAL_OS_OPEN(snapshot_path, _CANONICAL_OS_RDONLY)
            post_read = _CANONICAL_OS_FSTAT(post_read_descriptor)
            if (
                not _CANONICAL_STAT_ISREG(post_read.st_mode)
                or post_read.st_nlink != 1
                or not _CANONICAL_OS_SAMEOPENFILE(descriptor, post_read_descriptor)
            ):
                raise ValueError(
                    "PaperBook snapshot changed after verified read"
                )
        except ValueError:
            raise
        except OSError as exc:
            raise ValueError(f"PaperBook snapshot cannot be safely read: {exc}") from exc
        finally:
            for candidate in (
                post_read_descriptor,
                verification_descriptor,
                descriptor,
            ):
                if candidate is None:
                    continue
                try:
                    _CANONICAL_OS_CLOSE(candidate)
                except OSError:
                    pass
        if len(payload) > _CANONICAL_MAX_PAPER_SNAPSHOT_BYTES:
            raise ValueError("PaperBook snapshot exceeds the canonical byte-size limit")
        book = _CANONICAL_LOAD_BYTES(payload)
        _CANONICAL_INSTALL_OPENING_AUTHORITY(book)
        _CANONICAL_INSTALL_CAUSAL_AUTHORITY(book)
        return book


# Stable method witnesses: exact-type checks alone do not prevent caller shadowing
# or class-attribute rebinding of economic validation and replay methods.
_CANONICAL_VALIDATE_LOADED_STATE = PaperBook._validate_loaded_state
_CANONICAL_DECIMAL_INPUT = PaperBook._canonical_decimal_input
_CANONICAL_DEBIT_BALANCE = PaperBook._debit_balance
_CANONICAL_VALIDATE_PLACED_AT = PaperBook._validate_placed_at
_CANONICAL_REQUIRE_UTF8_STRING = PaperBook._require_utf8_string
_CANONICAL_REQUIRE_FINITE = PaperBook._require_finite
_CANONICAL_VALIDATE_TIMESTAMP = PaperBook._validate_timestamp
_CANONICAL_REQUIRE_CANONICAL_TEXT = PaperBook._require_canonical_text
_CANONICAL_VALIDATE_TICKET_PROVENANCE = PaperBook._validate_ticket_provenance
_CANONICAL_VALIDATE_TICKET_LEG = PaperBook._validate_ticket_leg
_CANONICAL_NORMALIZE_RESOLUTION_KEYS = PaperBook._normalize_resolution_keys
_CANONICAL_VALIDATE_SETTLED_AT = PaperBook._validate_settled_at
_CANONICAL_SETTLEMENT_RESULT = PaperBook._settlement_result
_CANONICAL_SNAPSHOT_PATH = PaperBook._canonical_snapshot_path
_CANONICAL_FSYNC_SNAPSHOT_DIRECTORY = PaperBook._fsync_snapshot_directory
_CANONICAL_ENSURE_SNAPSHOT_PARENT_DURABLE = PaperBook._ensure_snapshot_parent_durable
_CANONICAL_FROM_RAW_SNAPSHOT = PaperBook._from_raw_snapshot
_CANONICAL_LIFECYCLE_TO_JSON = PaperBook._lifecycle_to_json
_CANONICAL_PARSE_SNAPSHOT_DECIMAL = PaperBook._parse_snapshot_decimal
_CANONICAL_PARSE_SNAPSHOT_STATUS = PaperBook._parse_snapshot_status
_CANONICAL_PARSE_SNAPSHOT_LEGS = PaperBook._parse_snapshot_legs
_CANONICAL_PARSE_SNAPSHOT_PROVIDER_ACCOUNTS = PaperBook._parse_snapshot_provider_accounts
_CANONICAL_REQUIRED_SNAPSHOT_FIELD = PaperBook._required_snapshot_field
_CANONICAL_PARSE_LIFECYCLE = PaperBook._parse_lifecycle
_CANONICAL_PARSE_LIFECYCLE_KEY_LIST = PaperBook._parse_lifecycle_key_list
_CANONICAL_VALIDATE_LIFECYCLE_ENTRY = PaperBook._validate_lifecycle_entry
_CANONICAL_VALIDATE_LIFECYCLE_REACHABILITY = PaperBook._validate_lifecycle_reachability
_CANONICAL_LOAD_BYTES = PaperBook.load_bytes
