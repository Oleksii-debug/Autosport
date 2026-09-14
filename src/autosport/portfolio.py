from __future__ import annotations

import itertools
import random
from dataclasses import dataclass
from decimal import Decimal

from .domain import PaperTicket, TicketStatus


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
        return [ticket.ticket_id for ticket in tickets if ticket.status is TicketStatus.OPEN and any(leg.quote_key == quote_key for leg in ticket.legs)]

    @staticmethod
    def scenario_profit(tickets: list[PaperTicket], winning_quote_keys: set[str]) -> Decimal:
        total = Decimal("0")
        for ticket in tickets:
            if ticket.status is not TicketStatus.OPEN:
                continue
            if all(leg.quote_key in winning_quote_keys for leg in ticket.legs):
                total += ticket.stake * ticket.combined_odds - ticket.stake
            else:
                total -= ticket.stake
        return total

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

        open_tickets = [ticket for ticket in tickets if ticket.status is TicketStatus.OPEN]
        all_keys = {leg.quote_key for ticket in open_tickets for leg in ticket.legs}
        grouped = set().union(*groups) if groups else set()
        if not grouped.issubset(all_keys):
            raise ValueError("exclusive group contains quote not present in portfolio")
        if not open_tickets:
            zero = Decimal("0")
            return PortfolioReport("exact", 1, zero, zero, zero)
        ungrouped = sorted(all_keys - grouped)
        state_count = 2 ** len(ungrouped)
        for group in groups:
            # A set supplied by the caller proves mutual exclusivity only. It does
            # not prove that one of its members must win, so preserve the possible
            # terminal state where none of the listed quote keys wins.
            state_count *= len(group) + 1
        if state_count <= self.max_exact_states:
            profits = list(self._exact_profits(open_tickets, groups, ungrouped))
            mode = "conservative-enumeration" if groups else "exact"
        else:
            profits = list(self._sample_profits(open_tickets, groups, ungrouped))
            mode = "conservative-approximate" if groups else "approximate"
        return PortfolioReport(mode, len(profits), min(profits), max(profits), sum(profits, Decimal("0")) / Decimal(len(profits)))

    def _exact_profits(self, tickets, groups, ungrouped):
        group_choices = [tuple(sorted(group)) + (None,) for group in groups]
        group_product = itertools.product(*group_choices) if group_choices else [()]
        for selected_group_outcomes in group_product:
            base = {selection for selection in selected_group_outcomes if selection is not None}
            for mask in range(2 ** len(ungrouped)):
                winners = set(base)
                winners.update(selection for index, selection in enumerate(ungrouped) if mask & (1 << index))
                yield self.scenario_profit(tickets, winners)

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
            yield self.scenario_profit(tickets, winners)
