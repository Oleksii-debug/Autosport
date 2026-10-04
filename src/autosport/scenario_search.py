from __future__ import annotations

import itertools
import math
import random
from dataclasses import dataclass
from datetime import datetime
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from fractions import Fraction

from .domain import PaperTicket, TicketStatus
from .market_outcomes import (
    MarketSettlementOutcomeAuthority,
    assert_market_settlement_outcome_authoritative,
)
from .portfolio import (
    PortfolioEngine,
    _PORTFOLIO_DECIMAL_CONTEXT,
    _snapshot_open_tickets_for_analysis,
)


_MARKET_OUTCOME_AUTHORITY_TYPE = MarketSettlementOutcomeAuthority
_ASSERT_MARKET_OUTCOME_AUTHORITY = assert_market_settlement_outcome_authoritative
_OUTCOME_ASSERT_AVAILABLE = _MARKET_OUTCOME_AUTHORITY_TYPE.assert_available_as_of

_MAX_SCENARIO_GROUPS = 256
_MAX_SCENARIO_OUTCOMES_PER_GROUP = 1024
_MAX_SCENARIO_PROBABILITY_COEFFICIENT_DIGITS = 4096
_MAX_SCENARIO_PROBABILITY_ABS_EXPONENT = 4096
_SCENARIO_DECIMAL_CONTEXT = Context(
    prec=28,
    rounding=ROUND_HALF_EVEN,
    Emin=-999999,
    Emax=999999,
    capitals=1,
    clamp=0,
)


def _scenario_conservative_bounds(
    tickets: list[PaperTicket],
) -> tuple[Decimal, Decimal]:
    """Derive fail-closed portfolio bounds under one canonical Decimal context."""
    with localcontext(_PORTFOLIO_DECIMAL_CONTEXT):
        floor = -sum((ticket.stake for ticket in tickets), Decimal("0"))
        ceiling = sum(
            (
                ticket.stake * ticket.combined_odds - ticket.stake
                for ticket in tickets
            ),
            Decimal("0"),
        )
    if not floor.is_finite() or not ceiling.is_finite():
        raise ValueError("scenario conservative bounds must be finite")
    return floor, ceiling


def _canonical_scenario_text(value: object, *, field: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ValueError(f"{field} must be a non-empty canonical string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field} must be valid UTF-8 text") from exc
    return value


def _validate_scenario_probability(value: object) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise ValueError("scenario outcome probability must be an exact finite Decimal")
    if value < 0 or value > 1:
        raise ValueError("scenario outcome probability must be between 0 and 1")
    parts = value.as_tuple()
    if not isinstance(parts.exponent, int):
        raise ValueError("scenario outcome probability exponent must be an integer")
    if len(parts.digits) > _MAX_SCENARIO_PROBABILITY_COEFFICIENT_DIGITS:
        raise ValueError("scenario outcome probability coefficient exceeds resource limit")
    if abs(parts.exponent) > _MAX_SCENARIO_PROBABILITY_ABS_EXPONENT:
        raise ValueError("scenario outcome probability exponent exceeds resource limit")
    return value


@dataclass(frozen=True, slots=True)
class ScenarioOutcome:
    quote_key: str
    probability: Decimal | None = None

    def __post_init__(self) -> None:
        _canonical_scenario_text(self.quote_key, field="scenario outcome quote_key")
        if self.probability is not None:
            _validate_scenario_probability(self.probability)


@dataclass(frozen=True, slots=True)
class ScenarioGroup:
    group_id: str
    outcomes: tuple[ScenarioOutcome, ...]

    def __post_init__(self) -> None:
        _canonical_scenario_text(self.group_id, field="scenario group_id")
        if type(self.outcomes) is not tuple or len(self.outcomes) < 2:
            raise ValueError("scenario group outcomes must be a tuple with at least two outcomes")
        if len(self.outcomes) > _MAX_SCENARIO_OUTCOMES_PER_GROUP:
            raise ValueError("scenario group outcomes exceeds resource limit")
        if any(type(item) is not ScenarioOutcome for item in self.outcomes):
            raise ValueError("scenario group outcomes must contain exact ScenarioOutcome values")
        for item in self.outcomes:
            item.__post_init__()
        keys = [item.quote_key for item in self.outcomes]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate outcome quote_key")
        probabilities = [item.probability for item in self.outcomes]
        if any(value is not None for value in probabilities):
            if any(value is None for value in probabilities):
                raise ValueError("either all or no outcome probabilities must be supplied")
            typed = tuple(_validate_scenario_probability(value) for value in probabilities)
            if sum((Fraction(value) for value in typed), Fraction(0, 1)) != Fraction(1, 1):
                raise ValueError("scenario group probabilities must sum exactly to 1")


def _scenario_group_fingerprint(
    group: ScenarioGroup,
) -> tuple[str, tuple[tuple[str, Decimal | None], ...]]:
    if type(group) is not ScenarioGroup:
        raise ValueError("scenario groups must contain exact ScenarioGroup values")
    group.__post_init__()
    return (
        group.group_id,
        tuple((outcome.quote_key, outcome.probability) for outcome in group.outcomes),
    )


def _scenario_group_sort_key(group: ScenarioGroup) -> tuple[str, tuple[str, ...]]:
    return group.group_id, tuple(outcome.quote_key for outcome in group.outcomes)


def _snapshot_scenario_groups(
    groups: list[ScenarioGroup] | tuple[ScenarioGroup, ...],
) -> tuple[ScenarioGroup, ...]:
    if type(groups) not in (list, tuple):
        raise ValueError("scenario groups must be a list or tuple")
    source_groups = tuple(groups)
    if len(source_groups) > _MAX_SCENARIO_GROUPS:
        raise ValueError("scenario groups exceeds resource limit")
    captured: list[tuple[str, tuple[tuple[str, Decimal | None], ...]]] = []
    snapshots: list[ScenarioGroup] = []
    for group in source_groups:
        fingerprint = _scenario_group_fingerprint(group)
        captured.append(fingerprint)
        group_id, outcome_values = fingerprint
        snapshots.append(
            ScenarioGroup(
                group_id,
                tuple(
                    ScenarioOutcome(quote_key, probability)
                    for quote_key, probability in sorted(outcome_values, key=lambda item: item[0])
                ),
            )
        )

    current_groups = tuple(groups)
    if (
        len(current_groups) != len(source_groups)
        or any(current is not source for current, source in zip(current_groups, source_groups))
    ):
        raise ValueError("scenario group set changed during snapshot")
    for group, fingerprint in zip(source_groups, captured):
        if _scenario_group_fingerprint(group) != fingerprint:
            raise ValueError("scenario group changed during snapshot")

    ordered = tuple(sorted(snapshots, key=_scenario_group_sort_key))
    group_ids = tuple(group.group_id for group in ordered)
    if len(group_ids) != len(set(group_ids)):
        raise ValueError("scenario group_id values must be unique")
    return ordered


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
    outcome_space_exact: bool = False
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
        open_tickets = _snapshot_open_tickets_for_analysis(tickets)
        canonical_groups = _snapshot_scenario_groups(groups)
        if not open_tickets:
            zero = Decimal("0")
            return ScenarioSearchReport("exact", 1, 1, zero, zero, zero, zero, True, True, zero, "exact")
        mapping = self._validate_and_map(open_tickets, canonical_groups)
        total_states = math.prod(len(group.outcomes) for group in canonical_groups)
        floor, ceiling = _scenario_conservative_bounds(open_tickets)
        if total_states <= self.exact_state_limit:
            profits, weighted = self._enumerate(open_tickets, canonical_groups)
            expected = weighted if weighted is not None else None
            return ScenarioSearchReport(
                "exact-enumeration", total_states, total_states, min(profits), max(profits), floor, ceiling, True, True,
                expected, "exact-independent-groups" if expected is not None else None,
            )

        min_result = self._branch_bound(open_tickets, canonical_groups, mapping, minimize=True)
        max_result = self._branch_bound(open_tickets, canonical_groups, mapping, minimize=False)
        if min_result[2] and max_result[2]:
            expected, expected_mode = self._sample_expected(open_tickets, canonical_groups)
            return ScenarioSearchReport(
                "branch-and-bound-exact-extrema", total_states, min_result[1] + max_result[1], min_result[0], max_result[0],
                floor, ceiling, True, True, expected, expected_mode,
            )

        sample_worst, sample_best, expected = self._sample(open_tickets, canonical_groups)
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
        *,
        decision_as_of: datetime,
    ) -> ScenarioSearchReport:
        """Evaluate a complete authority-derived terminal-state cover.

        Authority evidence is provider-bound and causally fenced at decision_as_of.
        A conservative terminal superset is exhaustive but not exact; the report
        labels those two truths separately and never samples an oversized space.
        """
        if type(authorities) not in (list, tuple) or not authorities:
            raise ValueError(
                "authoritative outcome analysis requires market authorities"
            )
        source_authorities = tuple(authorities)
        if any(
            type(authority) is not _MARKET_OUTCOME_AUTHORITY_TYPE
            for authority in source_authorities
        ):
            raise ValueError(
                "authoritative outcome analysis requires canonical market authorities"
            )
        for authority in source_authorities:
            _ASSERT_MARKET_OUTCOME_AUTHORITY(authority)
        current_authorities = tuple(authorities)
        if (
            len(current_authorities) != len(source_authorities)
            or any(
                current is not source
                for current, source in zip(current_authorities, source_authorities)
            )
        ):
            raise ValueError("market outcome authority set changed during snapshot")

        ordered = tuple(
            sorted(
                source_authorities,
                key=lambda authority: (
                    authority.identity.identity_key,
                    authority.authority_sha256,
                ),
            )
        )
        for authority in ordered:
            _OUTCOME_ASSERT_AVAILABLE(authority, decision_as_of)
            _ASSERT_MARKET_OUTCOME_AUTHORITY(authority)

        current_authorities = tuple(authorities)
        if (
            len(current_authorities) != len(source_authorities)
            or any(
                current is not source
                for current, source in zip(current_authorities, source_authorities)
            )
        ):
            raise ValueError("market outcome authority set changed during analysis boundary")

        open_tickets = _snapshot_open_tickets_for_analysis(tickets)
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
                outcome_space_exact=True,
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
        state_authorities: list[MarketSettlementOutcomeAuthority] = []
        state_counts: list[int] = []

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
                if (
                    authority.settlement_rules_sha256
                    != baseline.settlement_rules_sha256
                ):
                    raise ValueError(
                        "provider authorities disagree on canonical settlement rules"
                    )

            bound_sources = frozenset(provider_sources)
            for quote_key in baseline.quote_keys:
                if quote_key in coverage:
                    raise ValueError(
                        "authoritative outcome universes overlap on quote identity"
                    )
                coverage[quote_key] = (market_key, bound_sources)
            state_authorities.append(baseline)
            state_counts.append(baseline.terminal_state_count)

        missing = ticket_quote_keys.difference(coverage)
        if missing:
            raise ValueError(
                "ticket leg missing from authoritative outcome universe"
            )

        relevant_market_keys = {
            coverage[quote_key][0] for quote_key in ticket_quote_keys
        }
        authority_market_keys = {
            authority.identity.market_key for authority in state_authorities
        }
        unrelated = authority_market_keys.difference(relevant_market_keys)
        if unrelated:
            raise ValueError(
                "authoritative outcome universe is unrelated to the open portfolio"
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

        total_states = math.prod(state_counts)
        if total_states > self.exact_state_limit:
            raise ValueError(
                "authoritative terminal outcome space exceeds exact_state_limit; "
                "complete-state truth cannot be approximated"
            )

        state_spaces = [
            authority.terminal_states for authority in state_authorities
        ]
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

        floor, ceiling = _scenario_conservative_bounds(open_tickets)
        outcome_space_exact = all(
            authority.terminal_space_exact for authority in state_authorities
        )
        return ScenarioSearchReport(
            mode=(
                "authoritative-exact-enumeration"
                if outcome_space_exact
                else "authoritative-conservative-enumeration"
            ),
            total_states=total_states,
            nodes_explored=total_states,
            observed_worst=min(profits),
            observed_best=max(profits),
            conservative_floor=floor,
            conservative_ceiling=ceiling,
            worst_proven=outcome_space_exact,
            best_proven=outcome_space_exact,
            expected_case=None,
            expected_mode=None,
            outcome_space_exhaustive=True,
            outcome_space_exact=outcome_space_exact,
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
        expected_fraction = Fraction(0, 1) if can_weight else None
        for combination in itertools.product(*(group.outcomes for group in groups)):
            winners = {outcome.quote_key for outcome in combination}
            profit = PortfolioEngine.scenario_profit(tickets, winners)
            profits.append(profit)
            if expected_fraction is not None:
                probability = Fraction(1, 1)
                for outcome in combination:
                    if outcome.probability is None:
                        raise RuntimeError("weighted enumeration lost canonical probability")
                    probability *= Fraction(outcome.probability)
                expected_fraction += probability * Fraction(profit)
        expected = (
            _fraction_to_exact_decimal(expected_fraction)
            if expected_fraction is not None
            else None
        )
        return profits, expected

    def _partial_bounds(
        self,
        tickets: list[PaperTicket],
        mapping: dict[str, int],
        assignments: dict[int, str],
    ) -> tuple[Decimal, Decimal]:
        with localcontext(_PORTFOLIO_DECIMAL_CONTEXT):
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
        if not lower.is_finite() or not upper.is_finite():
            raise ValueError("scenario branch bounds must be finite")
        return lower, upper

    def _ordered_groups(self, tickets: list[PaperTicket], groups: list[ScenarioGroup], mapping: dict[str, int]) -> list[int]:
        with localcontext(_PORTFOLIO_DECIMAL_CONTEXT):
            impact = [Decimal("0") for _ in groups]
            for ticket in tickets:
                potential = ticket.stake * ticket.combined_odds
                touched = {mapping[leg.quote_key] for leg in ticket.legs}
                for group_index in touched:
                    impact[group_index] += potential
        if any(not value.is_finite() for value in impact):
            raise ValueError("scenario group impact must be finite")
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
        floor, ceiling = _scenario_conservative_bounds(tickets)
        return floor if minimize else ceiling

    def _sample(self, tickets, groups) -> tuple[Decimal, Decimal, Decimal | None]:
        rng = random.Random(self.seed)
        worst = Decimal("Infinity")
        best = Decimal("-Infinity")
        can_weight = all(all(outcome.probability is not None for outcome in group.outcomes) for group in groups)
        weighted_tables = (
            tuple(_integer_probability_weights(group.outcomes) for group in groups)
            if can_weight
            else ()
        )
        total = Fraction(0, 1)
        for _ in range(self.sample_count):
            winners: set[str] = set()
            for index, group in enumerate(groups):
                if can_weight:
                    selected = _weighted_choice(
                        rng,
                        group.outcomes,
                        weighted_tables[index],
                    )
                else:
                    selected = rng.choice(group.outcomes)
                winners.add(selected.quote_key)
            value = PortfolioEngine.scenario_profit(tickets, winners)
            worst = min(worst, value)
            best = max(best, value)
            total += Fraction(value)
        expected = (
            _fraction_to_sampled_decimal(total / self.sample_count)
            if can_weight
            else None
        )
        return worst, best, expected

    def _sample_expected(self, tickets, groups) -> tuple[Decimal | None, str | None]:
        if not all(all(outcome.probability is not None for outcome in group.outcomes) for group in groups):
            return None, None
        _worst, _best, expected = self._sample(tickets, groups)
        return expected, "sampled-independent-groups"


def _fraction_to_exact_decimal(value: Fraction) -> Decimal:
    denominator = value.denominator
    twos = 0
    fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:
        raise ValueError("exact scenario expectation is not Decimal-representable")
    scale = max(twos, fives)
    coefficient = value.numerator
    if twos < scale:
        coefficient *= 2 ** (scale - twos)
    if fives < scale:
        coefficient *= 5 ** (scale - fives)
    if coefficient == 0:
        return Decimal("0")
    coefficient_decimal = Decimal(coefficient)
    parts = coefficient_decimal.as_tuple()
    return Decimal((parts.sign, parts.digits, parts.exponent - scale))


def _fraction_to_sampled_decimal(value: Fraction) -> Decimal:
    with localcontext(_SCENARIO_DECIMAL_CONTEXT):
        result = Decimal(value.numerator) / Decimal(value.denominator)
    if not result.is_finite():
        raise ValueError("sampled scenario expectation must be finite")
    return result


def _decimal_probability_coefficient(value: Decimal) -> tuple[int, int]:
    validated = _validate_scenario_probability(value)
    parts = validated.as_tuple()
    coefficient = 0
    for digit in parts.digits:
        coefficient = coefficient * 10 + digit
    if parts.sign:
        coefficient = -coefficient
    return coefficient, parts.exponent


def _integer_probability_weights(
    outcomes: tuple[ScenarioOutcome, ...],
) -> tuple[int, ...]:
    if type(outcomes) is not tuple or not outcomes:
        raise ValueError("weighted scenario outcomes must be a non-empty tuple")
    probability_parts: list[tuple[int, int]] = []
    for outcome in outcomes:
        if type(outcome) is not ScenarioOutcome or outcome.probability is None:
            raise ValueError("weighted scenario outcomes require exact probabilities")
        probability_parts.append(_decimal_probability_coefficient(outcome.probability))
    minimum_exponent = min(exponent for _coefficient, exponent in probability_parts)
    weights = tuple(
        coefficient * (10 ** (exponent - minimum_exponent))
        for coefficient, exponent in probability_parts
    )
    if any(weight < 0 for weight in weights) or sum(weights) <= 0:
        raise ValueError("weighted scenario probabilities are invalid")
    common_factor = 0
    for weight in weights:
        common_factor = math.gcd(common_factor, weight)
    if common_factor <= 0:
        raise ValueError("weighted scenario probabilities have no positive mass")
    return tuple(weight // common_factor for weight in weights)


def _weighted_choice(
    rng: random.Random,
    outcomes: tuple[ScenarioOutcome, ...],
    weights: tuple[int, ...] | None = None,
) -> ScenarioOutcome:
    if weights is None:
        weights = _integer_probability_weights(outcomes)
    if len(weights) != len(outcomes):
        raise ValueError("weighted scenario table does not match outcomes")
    total_weight = sum(weights)
    if total_weight <= 0:
        raise ValueError("weighted scenario probability mass must be positive")
    target = rng.randrange(total_weight)
    cumulative = 0
    for outcome, weight in zip(outcomes, weights):
        cumulative += weight
        if target < cumulative:
            return outcome
    raise RuntimeError("weighted scenario selection failed despite exact probability mass")
