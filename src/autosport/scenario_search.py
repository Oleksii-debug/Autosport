from __future__ import annotations

import itertools
import math
import random
from dataclasses import dataclass
from decimal import Decimal

from .domain import PaperTicket, TicketStatus
from .market_outcomes import MarketSettlementOutcomeAuthority
from .portfolio import PortfolioEngine


@dataclass(frozen=True, slots=True)
class ScenarioOutcome:
    quote_key: str
    probability: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ScenarioGroup:
    group_id: str
    outcomes: tuple[ScenarioOutcome, ...]

    def __post_init__(self) -> None:
        if len(self.outcomes) < 2:
            raise ValueError("scenario group requires at least two outcomes")
        keys = [item.quote_key for item in self.outcomes]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate outcome quote_key")
        probabilities = [item.probability for item in self.outcomes]
        if any(value is not None for value in probabilities):
            if any(value is None for value in probabilities):
                raise ValueError("either all or no outcome probabilities must be supplied")
            total = sum((value for value in probabilities if value is not None), Decimal("0"))
            if abs(total - Decimal("1")) > Decimal("0.000000001"):
                raise ValueError("scenario group probabilities must sum to 1")
            if any(value is not None and (value < 0 or value > 1) for value in probabilities):
                raise ValueError("invalid outcome probability")


@dataclass(frozen=True, slots=True)
class ScenarioSearchReport:
    mode: str
    total_states: int
    nodes_explored: int
    observed_worst: Decimal
    observed_best: Decimal
    conservative_floor: Decimal
    conservative_ceiling: Decimal
    worst_proven: bool
    best_proven: bool
    expected_case: Decimal | None
    expected_mode: str | None
    outcome_space_exhaustive: bool = False
    outcome_authority_sha256s: tuple[str, ...] = ()


class PortfolioDependencyIndex:
    def __init__(self, tickets: list[PaperTicket]) -> None:
        self.quote_to_tickets: dict[str, set[str]] = {}
        self.ticket_by_id: dict[str, PaperTicket] = {}
        for ticket in tickets:
            if ticket.status is not TicketStatus.OPEN:
                continue
            self.ticket_by_id[ticket.ticket_id] = ticket
            for leg in ticket.legs:
                self.quote_to_tickets.setdefault(leg.quote_key, set()).add(ticket.ticket_id)

    def affected_by(self, quote_keys: set[str]) -> set[str]:
        affected: set[str] = set()
        for key in quote_keys:
            affected.update(self.quote_to_tickets.get(key, set()))
        return affected


def _require_integer(value: int, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field} must be a non-boolean integer")
    return value


def _require_positive_integer(value: int, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{field} must be a positive non-boolean integer")
    return value


class ScenarioSearchEngine:
    """Portfolio scenario search with exact enumeration, bounded branch-and-bound and explicitly labeled approximation."""

    def __init__(
        self,
        exact_state_limit: int = 250_000,
        branch_node_limit: int = 500_000,
        sample_count: int = 50_000,
        seed: int = 17,
    ) -> None:
        self.exact_state_limit = _require_positive_integer(exact_state_limit, field="exact_state_limit")
        self.branch_node_limit = _require_positive_integer(branch_node_limit, field="branch_node_limit")
        self.sample_count = _require_positive_integer(sample_count, field="sample_count")
        self.seed = _require_integer(seed, field="seed")

    def analyse(self, tickets: list[PaperTicket], groups: list[ScenarioGroup]) -> ScenarioSearchReport:
        open_tickets = [ticket for ticket in tickets if ticket.status is TicketStatus.OPEN]
        if not open_tickets:
            zero = Decimal("0")
            return ScenarioSearchReport("exact", 1, 1, zero, zero, zero, zero, True, True, zero, "exact")
        mapping = self._validate_and_map(open_tickets, groups)
        total_states = math.prod(len(group.outcomes) for group in groups)
        floor = -sum((ticket.stake for ticket in open_tickets), Decimal("0"))
        ceiling = sum((ticket.stake * ticket.combined_odds - ticket.stake for ticket in open_tickets), Decimal("0"))
        if total_states <= self.exact_state_limit:
            profits, weighted = self._enumerate(open_tickets, groups)
            expected = weighted if weighted is not None else None
            return ScenarioSearchReport(
                "exact-enumeration", total_states, total_states, min(profits), max(profits), floor, ceiling, True, True,
                expected, "exact-independent-groups" if expected is not None else None,
            )

        min_result = self._branch_bound(open_tickets, groups, mapping, minimize=True)
        max_result = self._branch_bound(open_tickets, groups, mapping, minimize=False)
        if min_result[2] and max_result[2]:
            expected, expected_mode = self._sample_expected(open_tickets, groups)
            return ScenarioSearchReport(
                "branch-and-bound-exact-extrema", total_states, min_result[1] + max_result[1], min_result[0], max_result[0],
                floor, ceiling, True, True, expected, expected_mode,
            )

        sample_worst, sample_best, expected = self._sample(open_tickets, groups)
        observed_worst = min(sample_worst, min_result[0])
        observed_best = max(sample_best, max_result[0])
        return ScenarioSearchReport(
            "bounded-approximation", total_states, min_result[1] + max_result[1] + self.sample_count,
            observed_worst, observed_best, floor, ceiling, min_result[2], max_result[2], expected,
            "sampled-independent-groups" if expected is not None else None,
        )

    def analyse_authoritative(
        self,
        tickets: list[PaperTicket],
        authorities: list[MarketSettlementOutcomeAuthority]
        | tuple[MarketSettlementOutcomeAuthority, ...],
    ) -> ScenarioSearchReport:
        """Evaluate only terminal states derived from canonical exhaustive authority.

        Unlike analyse(), this path never accepts caller-supplied ScenarioGroup values
        as completeness evidence and never samples an oversized terminal space.
        """
        if type(authorities) not in (list, tuple) or not authorities:
            raise ValueError(
                "authoritative outcome analysis requires market authorities"
            )
        if any(
            not isinstance(authority, MarketSettlementOutcomeAuthority)
            for authority in authorities
        ):
            raise ValueError(
                "authoritative outcome analysis requires canonical market authorities"
            )

        ordered = tuple(
            sorted(
                authorities,
                key=lambda authority: (
                    authority.identity.identity_key,
                    authority.authority_sha256,
                ),
            )
        )
        open_tickets = [
            ticket for ticket in tickets if ticket.status is TicketStatus.OPEN
        ]
        if not open_tickets:
            zero = Decimal("0")
            return ScenarioSearchReport(
                mode="authoritative-exact-enumeration",
                total_states=1,
                nodes_explored=1,
                observed_worst=zero,
                observed_best=zero,
                conservative_floor=zero,
                conservative_ceiling=zero,
                worst_proven=True,
                best_proven=True,
                expected_case=None,
                expected_mode=None,
                outcome_space_exhaustive=True,
                outcome_authority_sha256s=tuple(
                    authority.authority_sha256 for authority in ordered
                ),
            )

        market_groups: dict[
            tuple[str, str, str, str],
            list[MarketSettlementOutcomeAuthority],
        ] = {}
        for authority in ordered:
            market_groups.setdefault(
                authority.identity.market_key,
                [],
            ).append(authority)

        ticket_quote_keys = {
            leg.quote_key
            for ticket in open_tickets
            for leg in ticket.legs
        }
        coverage: dict[
            str,
            tuple[
                tuple[str, str, str, str],
                frozenset[str],
            ],
        ] = {}
        state_spaces: list[tuple[object, ...]] = []
        state_authorities: list[MarketSettlementOutcomeAuthority] = []

        for market_key in sorted(market_groups):
            provider_authorities = market_groups[market_key]
            baseline = provider_authorities[0]
            provider_sources: set[str] = set()
            for authority in provider_authorities:
                source_id = authority.identity.source_id
                if source_id in provider_sources:
                    raise ValueError(
                        "duplicate provider authority for canonical market identity"
                    )
                provider_sources.add(source_id)
                if (
                    authority.selection_ids != baseline.selection_ids
                    or authority.settlement_semantics
                    is not baseline.settlement_semantics
                ):
                    raise ValueError(
                        "provider authorities disagree on canonical market terminal states"
                    )

            authority_quote_keys = set(baseline.quote_keys)
            if not ticket_quote_keys.intersection(authority_quote_keys):
                raise ValueError(
                    "authoritative outcome universe is unrelated to the open portfolio"
                )
            bound_sources = frozenset(provider_sources)
            for quote_key in baseline.quote_keys:
                if quote_key in coverage:
                    raise ValueError(
                        "authoritative outcome universes overlap on quote identity"
                    )
                coverage[quote_key] = (market_key, bound_sources)
            state_spaces.append(baseline.terminal_states)
            state_authorities.append(baseline)

        missing = ticket_quote_keys.difference(coverage)
        if missing:
            raise ValueError(
                "ticket leg missing from authoritative outcome universe"
            )

        for ticket in open_tickets:
            if len(ticket.provider_source_ids) != 1:
                raise ValueError(
                    "authoritative outcome analysis requires exactly one provider "
                    "source identity per open ticket"
                )
            ticket_source = ticket.provider_source_ids[0]
            for leg in ticket.legs:
                _market_key, allowed_sources = coverage[leg.quote_key]
                if ticket_source not in allowed_sources:
                    raise ValueError(
                        "ticket provider source does not match authoritative "
                        "market outcome evidence"
                    )

        total_states = math.prod(len(states) for states in state_spaces)
        if total_states > self.exact_state_limit:
            raise ValueError(
                "authoritative terminal outcome space exceeds exact_state_limit; "
                "complete-state truth cannot be approximated"
            )

        profits: list[Decimal] = []
        for combination in itertools.product(*state_spaces):
            settlement_by_quote: dict[str, str] = {}
            for authority, state in zip(state_authorities, combination):
                state_settlement = authority.settlement_by_quote(state)
                if settlement_by_quote.keys() & state_settlement.keys():
                    raise ValueError(
                        "authoritative terminal states overlap on quote identity"
                    )
                settlement_by_quote.update(state_settlement)
            profits.append(
                PortfolioEngine.scenario_profit_settlements(
                    open_tickets,
                    settlement_by_quote,
                )
            )

        floor = -sum(
            (ticket.stake for ticket in open_tickets),
            Decimal("0"),
        )
        ceiling = sum(
            (
                ticket.stake * ticket.combined_odds - ticket.stake
                for ticket in open_tickets
            ),
            Decimal("0"),
        )
        return ScenarioSearchReport(
            mode="authoritative-exact-enumeration",
            total_states=total_states,
            nodes_explored=total_states,
            observed_worst=min(profits),
            observed_best=max(profits),
            conservative_floor=floor,
            conservative_ceiling=ceiling,
            worst_proven=True,
            best_proven=True,
            expected_case=None,
            expected_mode=None,
            outcome_space_exhaustive=True,
            outcome_authority_sha256s=tuple(
                authority.authority_sha256 for authority in ordered
            ),
        )

    def _validate_and_map(self, tickets: list[PaperTicket], groups: list[ScenarioGroup]) -> dict[str, int]:
        if not groups:
            raise ValueError("scenario groups required")
        mapping: dict[str, int] = {}
        for index, group in enumerate(groups):
            for outcome in group.outcomes:
                if outcome.quote_key in mapping:
                    raise ValueError("quote_key appears in multiple scenario groups")
                mapping[outcome.quote_key] = index
        for ticket in tickets:
            for leg in ticket.legs:
                if leg.quote_key not in mapping:
                    raise ValueError(f"ticket leg missing from scenario space: {leg.quote_key}")
        return mapping

    def _enumerate(self, tickets: list[PaperTicket], groups: list[ScenarioGroup]) -> tuple[list[Decimal], Decimal | None]:
        profits: list[Decimal] = []
        can_weight = all(all(outcome.probability is not None for outcome in group.outcomes) for group in groups)
        expected = Decimal("0") if can_weight else None
        for combination in itertools.product(*(group.outcomes for group in groups)):
            winners = {outcome.quote_key for outcome in combination}
            profit = PortfolioEngine.scenario_profit(tickets, winners)
            profits.append(profit)
            if expected is not None:
                probability = Decimal("1")
                for outcome in combination:
                    probability *= outcome.probability or Decimal("0")
                expected += probability * profit
        return profits, expected

    def _partial_bounds(
        self,
        tickets: list[PaperTicket],
        mapping: dict[str, int],
        assignments: dict[int, str],
    ) -> tuple[Decimal, Decimal]:
        lower = Decimal("0")
        upper = Decimal("0")
        for ticket in tickets:
            lost = False
            undecided = False
            for leg in ticket.legs:
                group_index = mapping[leg.quote_key]
                selected = assignments.get(group_index)
                if selected is None:
                    undecided = True
                elif selected != leg.quote_key:
                    lost = True
                    break
            if lost:
                lower -= ticket.stake
                upper -= ticket.stake
            elif undecided:
                lower -= ticket.stake
                upper += ticket.stake * ticket.combined_odds - ticket.stake
            else:
                profit = ticket.stake * ticket.combined_odds - ticket.stake
                lower += profit
                upper += profit
        return lower, upper

    def _ordered_groups(self, tickets: list[PaperTicket], groups: list[ScenarioGroup], mapping: dict[str, int]) -> list[int]:
        impact = [Decimal("0") for _ in groups]
        for ticket in tickets:
            potential = ticket.stake * ticket.combined_odds
            touched = {mapping[leg.quote_key] for leg in ticket.legs}
            for group_index in touched:
                impact[group_index] += potential
        return sorted(range(len(groups)), key=lambda index: impact[index], reverse=True)

    def _branch_bound(self, tickets, groups, mapping, minimize: bool) -> tuple[Decimal, int, bool]:
        order = self._ordered_groups(tickets, groups, mapping)
        assignments: dict[int, str] = {}
        nodes = 0
        complete = True
        best = Decimal("Infinity") if minimize else Decimal("-Infinity")

        def dfs(depth: int) -> None:
            nonlocal nodes, complete, best
            if nodes >= self.branch_node_limit:
                complete = False
                return
            nodes += 1
            lower, upper = self._partial_bounds(tickets, mapping, assignments)
            if minimize and best.is_finite() and lower >= best:
                return
            if not minimize and best.is_finite() and upper <= best:
                return
            if depth == len(order):
                value = lower
                if minimize:
                    best = min(best, value)
                else:
                    best = max(best, value)
                return
            group_index = order[depth]
            group = groups[group_index]
            for outcome in group.outcomes:
                assignments[group_index] = outcome.quote_key
                dfs(depth + 1)
                if not complete:
                    break
            assignments.pop(group_index, None)

        dfs(0)
        if not best.is_finite():
            best = self._conservative_fallback(tickets, minimize)
        return best, nodes, complete

    @staticmethod
    def _conservative_fallback(tickets: list[PaperTicket], minimize: bool) -> Decimal:
        if minimize:
            return -sum((ticket.stake for ticket in tickets), Decimal("0"))
        return sum((ticket.stake * ticket.combined_odds - ticket.stake for ticket in tickets), Decimal("0"))

    def _sample(self, tickets, groups) -> tuple[Decimal, Decimal, Decimal | None]:
        rng = random.Random(self.seed)
        worst = Decimal("Infinity")
        best = Decimal("-Infinity")
        can_weight = all(all(outcome.probability is not None for outcome in group.outcomes) for group in groups)
        total = Decimal("0")
        for _ in range(self.sample_count):
            winners: set[str] = set()
            for group in groups:
                if can_weight:
                    selected = _weighted_choice(rng, group.outcomes)
                else:
                    selected = rng.choice(group.outcomes)
                winners.add(selected.quote_key)
            value = PortfolioEngine.scenario_profit(tickets, winners)
            worst = min(worst, value)
            best = max(best, value)
            total += value
        expected = total / Decimal(self.sample_count) if can_weight else None
        return worst, best, expected

    def _sample_expected(self, tickets, groups) -> tuple[Decimal | None, str | None]:
        if not all(all(outcome.probability is not None for outcome in group.outcomes) for group in groups):
            return None, None
        _worst, _best, expected = self._sample(tickets, groups)
        return expected, "sampled-independent-groups"


def _weighted_choice(rng: random.Random, outcomes: tuple[ScenarioOutcome, ...]) -> ScenarioOutcome:
    threshold = rng.random()
    cumulative = 0.0
    for outcome in outcomes:
        cumulative += float(outcome.probability or Decimal("0"))
        if threshold <= cumulative:
            return outcome
    return outcomes[-1]
