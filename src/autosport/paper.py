from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
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
from weakref import WeakKeyDictionary

from .domain import PaperTicket, TicketLeg, TicketStatus, utc_now_iso
from .forecasting import parse_iso_timestamp


_PAPER_DECIMAL_PRECISION = 28
_PAPER_DECIMAL_EMIN = -999999
_PAPER_DECIMAL_EMAX = 999999
_PAPER_SNAPSHOT_SCHEMA_VERSION = 7
_SUPPORTED_PAPER_SNAPSHOT_SCHEMA_VERSIONS = frozenset({2, 3, 4, 5, 6, 7})
_SCHEMA_MISSING = object()

_LifecycleEntry = tuple[str, str, tuple[str, ...], tuple[str, ...]]


def _ticket_opening_commitment(ticket: PaperTicket) -> tuple[object, ...]:
    """Return the immutable opening facts that authorized PAPER economics."""
    return (
        ticket.stake,
        ticket.legs,
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
    # become their own witness.
    authorities = WeakKeyDictionary()
    guard = threading.RLock()

    def register_book(book: object) -> None:
        with guard:
            authorities[book] = {}

    def record(book: object, ticket: PaperTicket) -> None:
        commitment = _ticket_opening_commitment(ticket)
        with guard:
            current = authorities.get(book)
            if current is None:
                raise RuntimeError("PaperBook opening authority registry is unavailable")
            existing = current.get(ticket.ticket_id)
            if existing is not None and existing != commitment:
                raise ValueError("PaperBook ticket opening authority cannot be rebound")
            current[ticket.ticket_id] = commitment

    def install_validated_snapshot(book: object) -> None:
        commitments = {
            ticket_id: _ticket_opening_commitment(ticket)
            for ticket_id, ticket in book.tickets.items()
        }
        with guard:
            if book not in authorities:
                raise RuntimeError("PaperBook opening authority registry is unavailable")
            authorities[book] = commitments

    def require_current(book: object) -> None:
        with guard:
            current = authorities.get(book)
            if current is None:
                raise RuntimeError("PaperBook opening authority registry is unavailable")
            expected = dict(current)
        if set(expected) != set(book.tickets):
            raise ValueError(
                "PaperBook ticket set changed outside product-issued opening authority"
            )
        for ticket_id, ticket in book.tickets.items():
            if expected[ticket_id] != _ticket_opening_commitment(ticket):
                raise ValueError(
                    "PaperBook ticket opening economic identity changed after admission"
                )

    return register_book, record, install_validated_snapshot, require_current


(
    _register_ticket_opening_authority_book,
    _record_ticket_opening_authority,
    _install_validated_ticket_opening_authority,
    _require_ticket_opening_authority,
) = _make_ticket_opening_authority_registry()


def _paper_decimal_context() -> Context:
    context = Context(
        prec=_PAPER_DECIMAL_PRECISION,
        rounding=ROUND_HALF_EVEN,
        Emin=_PAPER_DECIMAL_EMIN,
        Emax=_PAPER_DECIMAL_EMAX,
    )
    context.traps[InvalidOperation] = True
    context.traps[Overflow] = True
    context.traps[Underflow] = True
    context.clear_flags()
    return context


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"PaperBook snapshot contains duplicate JSON key: {key}")
        payload[key] = value
    return payload


def _reject_nonfinite_json_constant(value: str) -> None:
    raise ValueError(f"PaperBook snapshot contains non-finite JSON constant: {value}")


class PaperBook:
    """Virtual bankroll and auditable paper tickets. No real-money execution path exists."""

    def __init__(self, initial_bankroll: Decimal | str = Decimal("10000")) -> None:
        _register_ticket_opening_authority_book(self)
        initial = Decimal(str(initial_bankroll))
        self._require_finite(initial, "initial_bankroll")
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
    def committed_stake(self) -> Decimal:
        _require_ticket_opening_authority(self)
        return sum((t.stake for t in self.tickets.values() if t.status is TicketStatus.OPEN), Decimal("0"))

    @classmethod
    def _debit_balance(cls, balance: Decimal, amount: Decimal) -> Decimal:
        cls._require_finite(balance, "balance")
        cls._require_finite(amount, "stake")
        if amount <= 0:
            raise ValueError("stake must be positive")
        if amount > balance:
            raise ValueError("insufficient virtual bankroll")
        try:
            with localcontext(_paper_decimal_context()) as context:
                new_balance = balance - amount
                if context.flags[Inexact]:
                    raise ValueError("PaperBook stake debit loses Decimal precision")
        except DecimalException as exc:
            raise ValueError("PaperBook stake debit arithmetic is not representable") from exc
        return new_balance

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
        _require_ticket_opening_authority(self)
        self._validate_loaded_state(self)
        amount = Decimal(str(stake))
        new_balance = self._debit_balance(self.balance, amount)

        ticket_placed_at = self._validate_placed_at(
            placed_at if placed_at is not None else utc_now_iso()
        )
        self._require_utf8_string(reason, "strategy_reason")
        (
            provider_source_ids,
            provider_accounts,
            bankroll_id,
            currency,
        ) = self._validate_ticket_provenance(
            provider_source_ids,
            provider_accounts,
            bankroll_id,
            currency,
        )
        ticket_legs = tuple(legs)
        if not ticket_legs:
            raise ValueError("ticket requires at least one leg")
        for leg in ticket_legs:
            self._validate_ticket_leg(leg)
        quote_keys = [leg.quote_key for leg in ticket_legs]
        if len(quote_keys) != len(set(quote_keys)):
            raise ValueError("ticket contains duplicate quote_key leg")
        ticket = PaperTicket(
            ticket_id=str(uuid.uuid4()),
            stake=amount,
            legs=ticket_legs,
            placed_at=ticket_placed_at,
            strategy_reason=reason,
            provider_source_ids=provider_source_ids,
            provider_accounts=provider_accounts,
            bankroll_id=bankroll_id,
            currency=currency,
        )
        _record_ticket_opening_authority(self, ticket)
        self.balance = new_balance
        self.tickets[ticket.ticket_id] = ticket
        self._lifecycle.append(("open", ticket.ticket_id, (), ()))
        return ticket

    @staticmethod
    def _normalize_resolution_keys(values: object, label: str) -> set[str]:
        if isinstance(values, str) or values is None:
            raise ValueError(f"PaperBook {label} must be a collection of quote keys")
        try:
            normalized = set(values)
        except TypeError as exc:
            raise ValueError(f"PaperBook {label} must be a collection of quote keys") from exc
        if any(not isinstance(value, str) or not value for value in normalized):
            raise ValueError(f"PaperBook {label} must contain non-empty string quote keys")
        return normalized

    @classmethod
    def _settlement_result(
        cls,
        ticket: PaperTicket,
        balance: Decimal,
        winning_quote_keys: set[str],
        void_quote_keys: set[str],
    ) -> tuple[TicketStatus, Decimal, Decimal]:
        cls._require_finite(balance, "balance")
        for leg in ticket.legs:
            cls._validate_ticket_leg(leg, ticket_id=ticket.ticket_id)
        leg_quote_keys = {leg.quote_key for leg in ticket.legs}
        unknown_winners = winning_quote_keys - leg_quote_keys
        unknown_voids = void_quote_keys - leg_quote_keys
        if unknown_winners:
            raise ValueError("PaperBook settlement contains unknown winning quote_key")
        if unknown_voids:
            raise ValueError("PaperBook settlement contains unknown void quote_key")
        if winning_quote_keys & void_quote_keys:
            raise ValueError("PaperBook settlement quote_key cannot be both winning and void")

        effective_legs = tuple(
            leg for leg in ticket.legs if leg.quote_key not in void_quote_keys
        )
        if any(leg.quote_key not in winning_quote_keys for leg in effective_legs):
            return TicketStatus.LOST, Decimal("0"), balance

        status = TicketStatus.VOID if not effective_legs else TicketStatus.WON
        try:
            with localcontext(_paper_decimal_context()):
                effective_odds = Decimal("1")
                for leg in effective_legs:
                    effective_odds *= leg.locked_odds
                payout = ticket.stake if status is TicketStatus.VOID else ticket.stake * effective_odds
                cls._require_finite(payout, f"settlement payout for ticket {ticket.ticket_id}")
                if status is TicketStatus.WON and payout <= ticket.stake:
                    raise ValueError(
                        "PaperBook winning settlement payout must exceed stake after canonical Decimal rounding"
                    )
                new_balance = balance + payout
                cls._require_finite(new_balance, f"balance after settling ticket {ticket.ticket_id}")
                if payout != 0 and new_balance == balance:
                    raise ValueError("PaperBook settlement payout loses all Decimal balance effect")
        except DecimalException as exc:
            raise ValueError("PaperBook settlement arithmetic is not representable") from exc
        return status, payout, new_balance

    def settle(
        self,
        ticket_id: str,
        winning_quote_keys: set[str],
        void_quote_keys: set[str] | None = None,
        *,
        settled_at: str | None = None,
    ) -> PaperTicket:
        _require_ticket_opening_authority(self)
        self._validate_loaded_state(self)
        ticket = self.tickets[ticket_id]
        if ticket.status is not TicketStatus.OPEN:
            raise ValueError("ticket already settled")

        winners = self._normalize_resolution_keys(winning_quote_keys, "winning_quote_keys")
        voids = (
            set()
            if void_quote_keys is None
            else self._normalize_resolution_keys(void_quote_keys, "void_quote_keys")
        )
        settlement_time = (
            None
            if settled_at is None
            else self._validate_settled_at(settled_at, ticket.placed_at)
        )
        status, payout, new_balance = self._settlement_result(
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

    def save(self, path: str | Path) -> None:
        _require_ticket_opening_authority(self)
        # PaperBook and PaperTicket are intentionally mutable during a paper run.
        # Revalidate the complete economic/identity state immediately before any
        # durable replacement so caller/agent mutation cannot persist a snapshot
        # that a trusted fresh load would reject.
        self._validate_loaded_state(self)
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        raw = {
            "schema_version": _PAPER_SNAPSHOT_SCHEMA_VERSION,
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
                        }
                        for leg in t.legs
                    ],
                }
                for t in self.tickets.values()
            ],
            "lifecycle": self._lifecycle_to_json(),
        }
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                newline="\n",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(raw, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

    @staticmethod
    def _require_finite(value: object, label: str) -> None:
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ValueError(f"PaperBook snapshot contains non-finite {label}")

    @staticmethod
    def _require_utf8_string(value: object, label: str) -> str:
        if not isinstance(value, str):
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
        text = cls._require_utf8_string(value, label)
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
            cls._require_canonical_text(source_id, "provider_source_id")
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
                    cls._require_canonical_text(source_id, "provider account source_id"),
                    cls._require_canonical_text(account_id, "provider account_id"),
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

        canonical_bankroll = cls._require_canonical_text(bankroll_id, "bankroll_id")
        canonical_currency = cls._require_canonical_text(currency, "currency")
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
            text = cls._require_utf8_string(value, label)
        except ValueError as exc:
            raise ValueError(message) from exc
        if not text or text.strip() != text:
            raise ValueError(message)
        try:
            parse_iso_timestamp(text)
        except ValueError as exc:
            raise ValueError(message) from exc
        return text

    @classmethod
    def _validate_placed_at(cls, value: object, *, snapshot: bool = False) -> str:
        label = "snapshot placed_at" if snapshot else "placed_at"
        return cls._validate_timestamp(value, label)

    @classmethod
    def _validate_settled_at(
        cls,
        value: object,
        placed_at: object,
        *,
        snapshot: bool = False,
    ) -> str:
        label = "snapshot settled_at" if snapshot else "settled_at"
        settled_text = cls._validate_timestamp(value, label)
        placed_text = cls._validate_placed_at(placed_at, snapshot=snapshot)
        if parse_iso_timestamp(settled_text) < parse_iso_timestamp(placed_text):
            raise ValueError("PaperBook settled_at must not precede placed_at")
        return settled_text

    @classmethod
    def _validate_ticket_leg(cls, leg: object, *, ticket_id: str | None = None) -> TicketLeg:
        if type(leg) is not TicketLeg:
            raise ValueError("PaperBook ticket legs must be canonical TicketLeg values")
        suffix = f" for ticket {ticket_id}" if ticket_id is not None else ""
        cls._require_canonical_text(
            leg.event_id,
            f"event_id{suffix}",
            forbid_quote_key_delimiter=True,
        )
        cls._require_canonical_text(
            leg.market_id,
            f"market_id{suffix}",
            forbid_quote_key_delimiter=True,
        )
        cls._require_canonical_text(
            leg.selection_id,
            f"selection_id{suffix}",
            forbid_quote_key_delimiter=True,
        )
        if leg.sport is not None:
            sport = cls._require_canonical_text(
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
            exchange_side = cls._require_canonical_text(
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
        cls._require_finite(leg.locked_odds, f"locked_odds{suffix}")
        if leg.locked_odds <= 1:
            raise ValueError("PaperBook snapshot decimal odds must be greater than 1")
        return leg

    @classmethod
    def _validate_lifecycle_entry(cls, entry: object) -> _LifecycleEntry:
        if type(entry) is not tuple or len(entry) != 4:
            raise ValueError("PaperBook lifecycle entries must be canonical tuples")
        action, ticket_id, winners, voids = entry
        if action not in {"open", "settle"}:
            raise ValueError("PaperBook lifecycle action must be open or settle")
        cls._require_canonical_text(ticket_id, "lifecycle ticket_id")
        if type(winners) is not tuple or type(voids) is not tuple:
            raise ValueError("PaperBook lifecycle settlement keys must be canonical tuples")
        for values, label in ((winners, "winning_quote_keys"), (voids, "void_quote_keys")):
            for value in values:
                cls._require_canonical_text(value, f"lifecycle {label}")
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

            if action == "open":
                if ticket_id in opened:
                    raise ValueError("PaperBook lifecycle opens a ticket more than once")
                try:
                    replay_balance = cls._debit_balance(replay_balance, ticket.stake)
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

    @classmethod
    def _validate_loaded_state(cls, book: "PaperBook") -> None:
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
            cls._require_utf8_string(ticket.strategy_reason, "snapshot strategy_reason")
            cls._validate_ticket_provenance(
                ticket.provider_source_ids,
                ticket.provider_accounts,
                ticket.bankroll_id,
                ticket.currency,
            )
            if type(ticket.status) is not TicketStatus:
                raise ValueError("PaperBook snapshot ticket status must be canonical TicketStatus")
            cls._require_finite(ticket.stake, f"stake for ticket {ticket.ticket_id}")
            cls._require_finite(ticket.payout, f"payout for ticket {ticket.ticket_id}")
            if ticket.stake <= 0:
                raise ValueError("PaperBook snapshot ticket stake must be positive")
            if ticket.payout < 0:
                raise ValueError("PaperBook snapshot ticket payout cannot be negative")
            if type(ticket.legs) is not tuple or not ticket.legs:
                raise ValueError("PaperBook snapshot ticket requires a canonical non-empty leg tuple")
            for leg in ticket.legs:
                cls._validate_ticket_leg(leg, ticket_id=ticket.ticket_id)
            quote_keys = [leg.quote_key for leg in ticket.legs]
            if len(quote_keys) != len(set(quote_keys)):
                raise ValueError("PaperBook snapshot ticket contains duplicate quote_key leg")

            if ticket.status in {TicketStatus.OPEN, TicketStatus.LOST} and ticket.payout != 0:
                raise ValueError("PaperBook snapshot open/lost ticket payout must be zero")
            if ticket.status is TicketStatus.VOID and ticket.payout != ticket.stake:
                raise ValueError("PaperBook snapshot void ticket payout must equal stake")
            if ticket.status is TicketStatus.WON and ticket.payout <= ticket.stake:
                raise ValueError("PaperBook snapshot won ticket payout must exceed stake")

        cls._validate_lifecycle_reachability(book)

    @classmethod
    def _parse_lifecycle_key_list(cls, value: object, label: str) -> tuple[str, ...]:
        if type(value) is not list:
            raise ValueError(f"PaperBook snapshot lifecycle {label} must be a list")
        for item in value:
            cls._require_canonical_text(item, f"snapshot lifecycle {label}")
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
                    cls._validate_timestamp(
                        settled_at,
                        "snapshot lifecycle settled_at",
                    )
                entry = (
                    "settle",
                    ticket_id,
                    cls._parse_lifecycle_key_list(
                        item["winning_quote_keys"], "winning_quote_keys"
                    ),
                    cls._parse_lifecycle_key_list(
                        item["void_quote_keys"], "void_quote_keys"
                    ),
                )
            else:
                raise ValueError("PaperBook snapshot lifecycle action must be open or settle")
            canonical_entry = cls._validate_lifecycle_entry(entry)
            entries.append(canonical_entry)
            if action == "settle":
                settlement_times[canonical_entry[1]] = settled_at
        return entries, settlement_times

    @classmethod
    def _parse_snapshot_decimal(cls, value: object, label: str) -> Decimal:
        if not isinstance(value, str) or not value or value.strip() != value:
            raise ValueError(
                f"PaperBook snapshot {label} must be a non-empty trimmed decimal string"
            )
        try:
            parsed = Decimal(value)
        except DecimalException as exc:
            raise ValueError(f"PaperBook snapshot {label} is not a valid Decimal string") from exc
        cls._require_finite(parsed, label)
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
                cls._required_snapshot_field(raw_leg, "sport", "ticket leg")
                if schema_version is not None and schema_version >= 6
                else None
            )
            exchange_side = (
                cls._required_snapshot_field(raw_leg, "exchange_side", "ticket leg")
                if schema_version is not None and schema_version >= 7
                else None
            )
            if sport is not None:
                cls._require_canonical_text(
                    sport,
                    f"sport for ticket {ticket_id}",
                    forbid_quote_key_delimiter=True,
                )
            legs.append(
                TicketLeg(
                    cls._required_snapshot_field(raw_leg, "event_id", "ticket leg"),
                    cls._required_snapshot_field(raw_leg, "market_id", "ticket leg"),
                    cls._required_snapshot_field(raw_leg, "selection_id", "ticket leg"),
                    cls._parse_snapshot_decimal(
                        cls._required_snapshot_field(raw_leg, "locked_odds", "ticket leg"),
                        f"locked_odds for ticket {ticket_id}",
                    ),
                    sport=sport,
                    exchange_side=exchange_side,
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
                    cls._require_canonical_text(
                        raw_binding["source_id"], "snapshot provider account source_id"
                    ),
                    cls._require_canonical_text(
                        raw_binding["account_id"], "snapshot provider account_id"
                    ),
                )
            )
        return tuple(bindings)

    @staticmethod
    def _parse_snapshot_status(value: object, ticket_id: str) -> TicketStatus:
        if not isinstance(value, str):
            raise ValueError(
                f"PaperBook snapshot status for ticket {ticket_id} must be a string"
            )
        try:
            return TicketStatus(value)
        except ValueError as exc:
            raise ValueError(
                f"PaperBook snapshot status for ticket {ticket_id} is invalid"
            ) from exc

    @classmethod
    def _from_raw_snapshot(cls, raw: object) -> "PaperBook":
        if type(raw) is not dict:
            raise ValueError("PaperBook snapshot root must be an object")
        schema_version = raw.get("schema_version", _SCHEMA_MISSING)
        is_legacy = schema_version is _SCHEMA_MISSING
        if not is_legacy and (
            type(schema_version) is not int
            or schema_version not in _SUPPORTED_PAPER_SNAPSHOT_SCHEMA_VERSIONS
        ):
            raise ValueError("unsupported PaperBook snapshot schema_version")

        initial_bankroll = cls._parse_snapshot_decimal(
            cls._required_snapshot_field(raw, "initial_bankroll", "root"),
            "initial_bankroll",
        )
        book = cls(initial_bankroll)
        book.balance = cls._parse_snapshot_decimal(
            cls._required_snapshot_field(raw, "balance", "root"),
            "balance",
        )
        tickets_raw = cls._required_snapshot_field(raw, "tickets", "root")
        if type(tickets_raw) is not list:
            raise ValueError("PaperBook snapshot tickets must be a list")
        seen_ticket_ids: set[str] = set()
        for item in tickets_raw:
            if type(item) is not dict:
                raise ValueError("PaperBook snapshot ticket must be an object")
            ticket_id = cls._require_canonical_text(
                cls._required_snapshot_field(item, "ticket_id", "ticket"),
                "snapshot ticket_id",
            )
            if ticket_id in seen_ticket_ids:
                raise ValueError("PaperBook snapshot contains duplicate ticket_id")
            seen_ticket_ids.add(ticket_id)
            if schema_version in {3, 4, 5, 6, 7}:
                provider_source_ids_raw = cls._required_snapshot_field(
                    item, "provider_source_ids", f"ticket {ticket_id}"
                )
                if type(provider_source_ids_raw) is not list:
                    raise ValueError(
                        f"PaperBook snapshot provider_source_ids for ticket {ticket_id} must be a list"
                    )
                provider_source_ids = tuple(provider_source_ids_raw)
                provider_accounts = (
                    cls._parse_snapshot_provider_accounts(
                        cls._required_snapshot_field(
                            item, "provider_accounts", f"ticket {ticket_id}"
                        ),
                        ticket_id,
                    )
                    if schema_version in {4, 5, 6, 7}
                    else ()
                )
                bankroll_id = cls._required_snapshot_field(
                    item, "bankroll_id", f"ticket {ticket_id}"
                )
                currency = cls._required_snapshot_field(
                    item, "currency", f"ticket {ticket_id}"
                )
            else:
                provider_source_ids = ()
                provider_accounts = ()
                bankroll_id = None
                currency = None

            ticket = PaperTicket(
                ticket_id=ticket_id,
                stake=cls._parse_snapshot_decimal(
                    cls._required_snapshot_field(item, "stake", f"ticket {ticket_id}"),
                    f"stake for ticket {ticket_id}",
                ),
                legs=cls._parse_snapshot_legs(
                    cls._required_snapshot_field(item, "legs", f"ticket {ticket_id}"),
                    ticket_id,
                    schema_version=None if is_legacy else schema_version,
                ),
                placed_at=cls._required_snapshot_field(
                    item, "placed_at", f"ticket {ticket_id}"
                ),
                settled_at=(
                    cls._required_snapshot_field(
                        item, "settled_at", f"ticket {ticket_id}"
                    )
                    if schema_version in {5, 6, 7}
                    else None
                ),
                status=cls._parse_snapshot_status(
                    cls._required_snapshot_field(item, "status", f"ticket {ticket_id}"),
                    ticket_id,
                ),
                payout=cls._parse_snapshot_decimal(
                    cls._required_snapshot_field(item, "payout", f"ticket {ticket_id}"),
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
            book._lifecycle, book._settlement_times = cls._parse_lifecycle(
                raw["lifecycle"],
                schema_version,
            )

        cls._validate_loaded_state(book)
        _install_validated_ticket_opening_authority(book)
        return book

    @classmethod
    def load_bytes(cls, payload: bytes) -> "PaperBook":
        if not isinstance(payload, bytes):
            raise TypeError("PaperBook.load_bytes payload must be bytes")
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("PaperBook snapshot must be valid UTF-8") from exc
        try:
            raw = json.loads(
                text,
                object_pairs_hook=_reject_duplicate_json_keys,
                parse_constant=_reject_nonfinite_json_constant,
            )
        except RecursionError as exc:
            raise ValueError("PaperBook snapshot JSON nesting is too deep") from exc
        return cls._from_raw_snapshot(raw)

    @classmethod
    def load(cls, path: str | Path) -> "PaperBook":
        return cls.load_bytes(Path(path).read_bytes())
