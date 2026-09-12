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
    """Scenario P&L engine with exact enumeration when bounded and deterministic sampling otherwise."""

    def __init__(self, max_exact_states: int = 100_000, sample_count: int = 20_000, seed: int = 7) -> None:
        self.max_exact_states = max_exact_states
        self.sample_count = sample_count
        self.seed = seed

    @staticmethod
    def affected_tickets(tickets: list[PaperTicket], selection_id: str) -> list[str]:
        return [
            ticket.ticket_id
            for ticket in tickets
            if ticket.status is TicketStatus.OPEN and any(leg.selection_id == selection_id for leg in ticket.legs)
        ]

    @staticmethod
    def scenario_profit(tickets: list[PaperTicket], winning_selection_ids: set[str]) -> Decimal:
        total = Decimal("0")
        for ticket in tickets:
            if ticket.status is not TicketStatus.OPEN:
                continue
            if all(leg.selection_id in winning_selection_ids for leg in ticket.legs):
                total += ticket.stake * ticket.combined_odds - ticket.stake
            else:
                total -= ticket.stake
        return total

    def analyse(
        self,
        tickets: list[PaperTicket],
        exclusive_groups: list[set[str]] | None = None,
    ) -> PortfolioReport:
        open_tickets = [t for t in tickets if t.status is TicketStatus.OPEN]
        if not open_tickets:
            zero = Decimal("0")
            return PortfolioReport("exact", 1, zero, zero, zero)
        all_selections = {leg.selection_id for t in open_tickets for leg in t.legs}
        groups = [set(group) for group in (exclusive_groups or [])]
        grouped = set().union(*groups) if groups else set()
        ungrouped = sorted(all_selections - grouped)
        state_count = (2 ** len(ungrouped))
        for group in groups:
            state_count *= max(1, len(group))
        if state_count <= self.max_exact_states:
            profits = list(self._exact_profits(open_tickets, groups, ungrouped))
            mode = "exact"
        else:
            profits = list(self._sample_profits(open_tickets, groups, ungrouped))
            mode = "approximate"
        return PortfolioReport(
            mode=mode,
            scenario_count=len(profits),
            worst_case=min(profits),
            best_case=max(profits),
            mean_case=sum(profits, Decimal("0")) / Decimal(len(profits)),
        )

    def _exact_profits(self, tickets, groups, ungrouped):
        group_choices = [sorted(group) for group in groups]
        group_product = itertools.product(*group_choices) if group_choices else [()]
        for selected_group_outcomes in group_product:
            base = set(selected_group_outcomes)
            for mask in range(2 ** len(ungrouped)):
                winners = set(base)
                for idx, selection in enumerate(ungrouped):
                    if mask & (1 << idx):
                        winners.add(selection)
                yield self.scenario_profit(tickets, winners)

    def _sample_profits(self, tickets, groups, ungrouped):
        rng = random.Random(self.seed)
        for _ in range(self.sample_count):
            winners = {rng.choice(tuple(group)) for group in groups if group}
            winners.update(selection for selection in ungrouped if rng.random() < 0.5)
            yield self.scenario_profit(tickets, winners)
