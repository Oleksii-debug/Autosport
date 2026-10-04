from __future__ import annotations

import itertools
import random
from dataclasses import dataclass
from decimal import (
    Context,
    Decimal,
    DecimalException,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    ROUND_HALF_EVEN,
    Underflow,
    localcontext,
)

from .domain import PaperTicket, TicketLeg, TicketStatus
from .paper import PaperBook


# Portfolio reports are persisted as run evidence, so their values cannot depend
# on an unrelated caller's thread-local/default Decimal configuration.  This
# explicit policy matches the canonical PaperBook settlement range and rounding.
_PORTFOLIO_DECIMAL_CONTEXT = Context(
    prec=28,
    rounding=ROUND_HALF_EVEN,
    Emin=-999999,
    Emax=999999,
    capitals=1,
    clamp=0,
    flags=[],
    traps=[InvalidOperation, DivisionByZero, Overflow, Underflow],
)


def _canonical_decimal(
    value: object,
    *,
    _decimal_type: type[Decimal] = Decimal,
) -> Decimal:
    return _decimal_type(value)


def _require_finite_decimal(
    value: object,
    label: str,
    *,
    _decimal_type: type[Decimal] = Decimal,
) -> Decimal:
    if type(value) is not _decimal_type or not value.is_finite():
        raise ValueError(f"{label} must be a finite Decimal")
    return value


def _portfolio_arithmetic_error(exc: DecimalException) -> ValueError:
    return ValueError(
        "portfolio economics are not representable in the canonical Decimal context"
    )


def _is_open_ticket_status(
    value: object,
    *,
    _status_type: type[TicketStatus] = TicketStatus,
    _open_status: TicketStatus = TicketStatus.OPEN,
) -> bool:
    if type(value) is not _status_type:
        raise ValueError("portfolio ticket status must be exact TicketStatus")
    return value is _open_status


_AnalysisLegFingerprint = tuple[
    str,
    str,
    str,
    Decimal,
    str | None,
    str | None,
]


def _analysis_leg_fingerprint(
    leg: TicketLeg,
    *,
    _ticket_leg_type: type[TicketLeg] = TicketLeg,
) -> _AnalysisLegFingerprint:
    """Detach the exact nested leg fields consumed by portfolio economics."""

    if type(leg) is not _ticket_leg_type:
        raise ValueError("portfolio ticket leg must be exact TicketLeg")
    return (
        leg.event_id,
        leg.market_id,
        leg.selection_id,
        leg.locked_odds,
        leg.sport,
        leg.exchange_side,
    )


def _analysis_ticket_fingerprint(
    ticket: PaperTicket,
    *,
    _paper_ticket_type: type[PaperTicket] = PaperTicket,
    _leg_fingerprint=_analysis_leg_fingerprint,
) -> tuple[
    str,
    Decimal,
    tuple[_AnalysisLegFingerprint, ...],
    str,
    TicketStatus,
    Decimal,
    str | None,
    tuple[str, ...],
]:
    """Return detached mutable ticket fields consumed by scenario analysis."""

    if type(ticket) is not _paper_ticket_type:
        raise ValueError("portfolio ticket must be exact PaperTicket")
    legs = ticket.legs
    if type(legs) is not tuple:
        raise ValueError("portfolio ticket legs must be an exact tuple")
    return (
        ticket.ticket_id,
        ticket.stake,
        tuple(_leg_fingerprint(leg) for leg in legs),
        ticket.placed_at,
        ticket.status,
        ticket.payout,
        ticket.settled_at,
        ticket.provider_source_ids,
    )


def _validate_open_ticket_economics_for_analysis(
    ticket_id: object,
    stake: object,
    legs: object,
    *,
    _validate_ticket_leg=PaperBook._validate_ticket_leg,
) -> None:
    amount = _require_finite_decimal(
        stake,
        f"portfolio ticket {ticket_id} stake",
    )
    if amount <= 0:
        raise ValueError(f"portfolio ticket {ticket_id} stake must be positive")
    if type(legs) is not tuple or not legs:
        raise ValueError(
            f"portfolio ticket {ticket_id} requires at least one canonical leg"
        )
    for leg in legs:
        _validate_ticket_leg(leg, ticket_id=ticket_id)
    quote_keys = tuple(leg.quote_key for leg in legs)
    if len(quote_keys) != len(set(quote_keys)):
        raise ValueError(
            f"portfolio ticket {ticket_id} contains duplicate quote_key leg"
        )


def _snapshot_open_tickets_for_analysis(
    tickets: list[PaperTicket],
    *,
    _paper_ticket_type: type[PaperTicket] = PaperTicket,
    _ticket_leg_type: type[TicketLeg] = TicketLeg,
    _open_status: TicketStatus = TicketStatus.OPEN,
    _validate_placed_at=PaperBook._validate_placed_at,
) -> list[PaperTicket]:
    """Detach one causally coherent cut of mutable ticket economics.

    ``PaperTicket`` is intentionally mutable because settlement updates it in
    place.  Merely copying tickets one-by-one is not enough: a settlement between
    two copies could otherwise create a mixed OPEN-ticket set that never existed at
    a single instant.  Capture the fields consumed by this engine, then revalidate
    the source identities and those fields before publishing the detached cut.
    """

    source_tickets = tuple(tickets)
    captured: list[tuple[object, ...]] = []
    snapshots: list[PaperTicket] = []
    seen_ticket_ids: set[str] = set()

    for ticket in source_tickets:
        fingerprint = _analysis_ticket_fingerprint(ticket)
        captured.append(fingerprint)
        (
            ticket_id,
            stake,
            leg_fingerprints,
            placed_at,
            status,
            payout,
            settled_at,
            provider_source_ids,
        ) = fingerprint
        if type(ticket_id) is not str or not ticket_id or ticket_id != ticket_id.strip():
            raise ValueError("portfolio ticket_id must be canonical non-empty text")
        if ticket_id in seen_ticket_ids:
            raise ValueError("portfolio ticket_id values must be unique")
        seen_ticket_ids.add(ticket_id)
        if not _is_open_ticket_status(status):
            continue
        open_payout = _require_finite_decimal(
            payout,
            f"portfolio ticket {ticket_id} payout",
        )
        if open_payout != 0:
            raise ValueError(
                f"portfolio open ticket {ticket_id} payout must be zero"
            )
        if settled_at is not None:
            raise ValueError(
                f"portfolio open ticket {ticket_id} cannot have settled_at"
            )
        _validate_placed_at(placed_at, snapshot=True)
        detached_legs = tuple(
            _ticket_leg_type(
                event_id,
                market_id,
                selection_id,
                locked_odds,
                sport=sport,
                exchange_side=exchange_side,
            )
            for (
                event_id,
                market_id,
                selection_id,
                locked_odds,
                sport,
                exchange_side,
            ) in leg_fingerprints
        )
        _validate_open_ticket_economics_for_analysis(
            ticket_id,
            stake,
            detached_legs,
        )
        snapshots.append(
            _paper_ticket_type(
                ticket_id=ticket_id,
                stake=stake,
                legs=detached_legs,
                placed_at=placed_at,
                status=_open_status,
                provider_source_ids=tuple(provider_source_ids),
            )
        )

    current_tickets = tuple(tickets)
    if (
        len(current_tickets) != len(source_tickets)
        or any(
            current is not source
            for current, source in zip(current_tickets, source_tickets)
        )
    ):
        raise ValueError("portfolio ticket set changed during snapshot")

    for ticket, fingerprint in zip(source_tickets, captured):
        if _analysis_ticket_fingerprint(ticket) != fingerprint:
            raise ValueError("portfolio ticket changed during snapshot")

    return snapshots


def _normalize_resolution_keys_for_analysis(
    values: object,
    label: str,
    *,
    _normalizer=PaperBook._normalize_resolution_keys,
) -> set[str]:
    return _normalizer(values, label)


def _settlement_result_for_analysis(
    ticket: PaperTicket,
    balance: Decimal,
    winning_quote_keys: set[str],
    void_quote_keys: set[str],
    *,
    _settlement_result=PaperBook._settlement_result,
) -> tuple[TicketStatus, Decimal, Decimal]:
    return _settlement_result(
        ticket,
        balance,
        winning_quote_keys,
        void_quote_keys,
    )


def _scenario_profit_in_context(
    tickets: list[PaperTicket],
    winning_quote_keys: set[str],
) -> Decimal:
    """Calculate one scenario while the canonical local context is active."""

    total = _canonical_decimal("0")
    for ticket in tickets:
        if not _is_open_ticket_status(ticket.status):
            continue
        stake = _require_finite_decimal(
            ticket.stake,
            f"portfolio ticket {ticket.ticket_id} stake",
        )
        combined_odds = _canonical_decimal("1")
        for leg in ticket.legs:
            odds = _require_finite_decimal(
                leg.locked_odds,
                f"portfolio ticket {ticket.ticket_id} locked_odds",
            )
            combined_odds *= odds
        if all(leg.quote_key in winning_quote_keys for leg in ticket.legs):
            scenario_value = stake * combined_odds - stake
        else:
            scenario_value = stake.copy_negate()
        if not scenario_value.is_finite():
            raise ValueError(
                f"portfolio ticket {ticket.ticket_id} scenario profit must be finite"
            )
        total += scenario_value
    if not total.is_finite():
        raise ValueError("portfolio scenario profit must be finite")
    return total


@dataclass(frozen=True, slots=True)
class PortfolioReport:
    mode: str
    scenario_count: int
    worst_case: Decimal
    best_case: Decimal
    mean_case: Decimal


class PortfolioEngine:
    """Scenario P&L engine with bounded exact enumeration and deterministic sampling.

    ``exclusive_groups`` is an explicit mutual-exclusivity contract supplied by the
    caller. It does *not* prove that the listed quote keys exhaust every terminal
    outcome of the underlying market. To avoid false exact/worst-case claims, every
    supplied group therefore includes a conservative ``none of the listed quotes``
    state and reports are truth-labeled as conservative.
    """

    def __init__(self, max_exact_states: int = 100_000, sample_count: int = 20_000, seed: int = 7) -> None:
        if isinstance(max_exact_states, bool) or not isinstance(max_exact_states, int) or max_exact_states < 1:
            raise ValueError("max_exact_states must be a positive integer")
        if isinstance(sample_count, bool) or not isinstance(sample_count, int) or sample_count < 1:
            raise ValueError("sample_count must be a positive integer")
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ValueError("seed must be an integer")
        self.max_exact_states = max_exact_states
        self.sample_count = sample_count
        self.seed = seed

    @staticmethod
    def affected_tickets(tickets: list[PaperTicket], quote_key: str) -> list[str]:
        if (
            type(quote_key) is not str
            or not quote_key
            or quote_key != quote_key.strip()
        ):
            raise ValueError("quote_key must be canonical non-empty text")
        ticket_snapshot = _snapshot_open_tickets_for_analysis(tickets)
        return [
            ticket.ticket_id
            for ticket in ticket_snapshot
            if any(leg.quote_key == quote_key for leg in ticket.legs)
        ]

    @staticmethod
    def scenario_profit(tickets: list[PaperTicket], winning_quote_keys: set[str]) -> Decimal:
        winning_snapshot = _normalize_resolution_keys_for_analysis(
            winning_quote_keys,
            "winning_quote_keys",
        )
        ticket_snapshot = _snapshot_open_tickets_for_analysis(tickets)
        try:
            with localcontext(_PORTFOLIO_DECIMAL_CONTEXT):
                return _scenario_profit_in_context(ticket_snapshot, winning_snapshot)
        except DecimalException as exc:
            raise _portfolio_arithmetic_error(exc) from exc

    @staticmethod
    def scenario_profit_settlements(
        tickets: list[PaperTicket],
        settlement_by_quote: dict[str, str],
    ) -> Decimal:
        """Evaluate one fully specified settlement state using PaperBook economics.

        The mapping is scenario evidence only; it grants no exhaustiveness authority.
        Callers that need complete-state truth must obtain the mapping from the
        authoritative market-outcome contract.
        """
        if type(settlement_by_quote) is not dict:
            raise ValueError("settlement_by_quote must be an exact dict")
        snapshot = settlement_by_quote.copy()
        allowed = frozenset({"win", "loss", "void"})
        for quote_key, result in snapshot.items():
            if (
                type(quote_key) is not str
                or not quote_key
                or quote_key != quote_key.strip()
            ):
                raise ValueError(
                    "settlement_by_quote keys must be non-empty canonical strings"
                )
            if type(result) is not str or result not in allowed:
                raise ValueError(
                    "settlement_by_quote values must be win, loss, or void"
                )

        ticket_snapshot = _snapshot_open_tickets_for_analysis(tickets)

        try:
            with localcontext(_PORTFOLIO_DECIMAL_CONTEXT):
                total = _canonical_decimal("0")
                for ticket in ticket_snapshot:
                    if not _is_open_ticket_status(ticket.status):
                        continue
                    stake = _require_finite_decimal(
                        ticket.stake,
                        f"portfolio ticket {ticket.ticket_id} stake",
                    )
                    leg_keys = {leg.quote_key for leg in ticket.legs}
                    missing = leg_keys.difference(snapshot)
                    if missing:
                        raise ValueError(
                            "portfolio ticket is missing terminal settlement evidence"
                        )
                    winners = {
                        quote_key
                        for quote_key in leg_keys
                        if snapshot[quote_key] == "win"
                    }
                    voids = {
                        quote_key
                        for quote_key in leg_keys
                        if snapshot[quote_key] == "void"
                    }
                    _status, payout, _balance = _settlement_result_for_analysis(
                        ticket,
                        _canonical_decimal("0"),
                        winners,
                        voids,
                    )
                    scenario_value = payout - stake
                    if not scenario_value.is_finite():
                        raise ValueError(
                            f"portfolio ticket {ticket.ticket_id} scenario profit must be finite"
                        )
                    total += scenario_value
                if not total.is_finite():
                    raise ValueError("portfolio scenario profit must be finite")
                return total
        except DecimalException as exc:
            raise _portfolio_arithmetic_error(exc) from exc

    def analyse(self, tickets: list[PaperTicket], exclusive_groups: list[set[str]] | None = None) -> PortfolioReport:
        groups = [set(group) for group in (exclusive_groups or [])]
        seen: set[str] = set()
        for group in groups:
            if not group:
                raise ValueError("exclusive groups must not be empty")
            if seen.intersection(group):
                raise ValueError("exclusive groups must be disjoint")
            seen.update(group)
        groups.sort(key=lambda group: tuple(sorted(group)))

        open_tickets = _snapshot_open_tickets_for_analysis(tickets)
        all_keys = {leg.quote_key for ticket in open_tickets for leg in ticket.legs}
        grouped = set().union(*groups) if groups else set()
        if not grouped.issubset(all_keys):
            raise ValueError("exclusive group contains quote not present in portfolio")
        if not open_tickets:
            zero = _canonical_decimal("0")
            return PortfolioReport("exact", 1, zero, zero, zero)
        ungrouped = sorted(all_keys - grouped)
        state_count = 2 ** len(ungrouped)
        for group in groups:
            # A set supplied by the caller proves mutual exclusivity only. It does
            # not prove that one of its members must win, so preserve the possible
            # terminal state where none of the listed quote keys wins.
            state_count *= len(group) + 1
        try:
            with localcontext(_PORTFOLIO_DECIMAL_CONTEXT):
                if state_count <= self.max_exact_states:
                    profits = list(self._exact_profits(open_tickets, groups, ungrouped))
                    mode = "conservative-enumeration" if groups else "exact"
                else:
                    profits = list(self._sample_profits(open_tickets, groups, ungrouped))
                    mode = "conservative-approximate" if groups else "approximate"
                mean_case = sum(profits, _canonical_decimal("0")) / _canonical_decimal(len(profits))
                if not mean_case.is_finite():
                    raise ValueError("portfolio mean scenario profit must be finite")
        except DecimalException as exc:
            raise _portfolio_arithmetic_error(exc) from exc
        return PortfolioReport(
            mode,
            len(profits),
            min(profits),
            max(profits),
            mean_case,
        )

    def _exact_profits(self, tickets, groups, ungrouped):
        group_choices = [tuple(sorted(group)) + (None,) for group in groups]
        group_product = itertools.product(*group_choices) if group_choices else [()]
        for selected_group_outcomes in group_product:
            base = {selection for selection in selected_group_outcomes if selection is not None}
            for mask in range(2 ** len(ungrouped)):
                winners = set(base)
                winners.update(selection for index, selection in enumerate(ungrouped) if mask & (1 << index))
                yield _scenario_profit_in_context(tickets, winners)

    def _sample_profits(self, tickets, groups, ungrouped):
        rng = random.Random(self.seed)
        group_choices = [tuple(sorted(group)) + (None,) for group in groups]
        for _ in range(self.sample_count):
            winners: set[str] = set()
            for group in group_choices:
                selected = rng.choice(group)
                if selected is not None:
                    winners.add(selected)
            winners.update(selection for selection in ungrouped if rng.random() < 0.5)
            yield _scenario_profit_in_context(tickets, winners)
