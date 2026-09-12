from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal

from .candidate_search import BeamParlayCandidateSearch, CandidateLeg, ParlayCandidate
from .domain import PaperTicket, TicketLeg, TicketStatus
from .scenario_search import ScenarioGroup, ScenarioSearchEngine, ScenarioSearchReport


@dataclass(frozen=True, slots=True)
class CandidatePortfolioImpact:
    """Truth-labeled change in portfolio risk metrics after adding one synthetic paper candidate."""

    candidate: ParlayCandidate
    stake: Decimal
    dependent_existing_ticket_ids: tuple[str, ...]
    base_report: ScenarioSearchReport
    with_candidate_report: ScenarioSearchReport
    observed_worst_case_change: Decimal
    conservative_floor_change: Decimal
    observed_best_case_change: Decimal
    conservative_ceiling_change: Decimal
    expected_case_change: Decimal | None
    expected_change_mode: str | None
    worst_case_change_proven: bool
    best_case_change_proven: bool
    ranking_risk_change: Decimal
    ranking_risk_truth: str
    standalone_expected_profit: Decimal

    @property
    def exact_marginal_extrema(self) -> bool:
        return self.worst_case_change_proven and self.best_case_change_proven


class PortfolioAwareCandidateOptimizer:
    """
    Generate bounded candidates, then rerank them by their change to the whole paper portfolio.

    The beam generator's independent-probability EV is only a screening/tie-break signal. Final risk
    ranking comes from ScenarioSearchEngine on the supplied canonical scenario space.
    """

    def __init__(
        self,
        *,
        generator: BeamParlayCandidateSearch | None = None,
        scenario_engine: ScenarioSearchEngine | None = None,
        result_limit: int = 50,
    ) -> None:
        if result_limit <= 0:
            raise ValueError("result_limit must be positive")
        self.generator = generator or BeamParlayCandidateSearch()
        self.scenario_engine = scenario_engine or ScenarioSearchEngine()
        self.result_limit = result_limit

    def optimize(
        self,
        existing_tickets: list[PaperTicket],
        candidate_legs: list[CandidateLeg],
        groups: list[ScenarioGroup],
        *,
        stake: Decimal | str,
        minimum_legs: int = 2,
    ) -> list[CandidatePortfolioImpact]:
        generated = self.generator.search(candidate_legs, minimum_legs=minimum_legs)
        return self.evaluate_candidates(existing_tickets, generated, groups, stake=stake)

    def evaluate_candidates(
        self,
        existing_tickets: list[PaperTicket],
        candidates: list[ParlayCandidate],
        groups: list[ScenarioGroup],
        *,
        stake: Decimal | str,
    ) -> list[CandidatePortfolioImpact]:
        amount = Decimal(str(stake))
        if amount <= 0:
            raise ValueError("stake must be positive")
        if not groups:
            raise ValueError("scenario groups required for portfolio-aware candidate evaluation")

        quote_to_group = _quote_group_map(groups)
        open_existing = [ticket for ticket in existing_tickets if ticket.status is TicketStatus.OPEN]
        # Analyse the base once. ScenarioSearchEngine itself verifies that every open portfolio leg
        # belongs to the supplied scenario space.
        base_report = self.scenario_engine.analyse(open_existing, groups)
        ranked: list[CandidatePortfolioImpact] = []
        for candidate in candidates:
            synthetic = _candidate_ticket(candidate, amount, quote_to_group)
            touched_groups = {quote_to_group[leg.quote_key] for leg in synthetic.legs}
            dependent = _dependent_existing_ticket_ids(open_existing, touched_groups, quote_to_group)
            with_report = self.scenario_engine.analyse(open_existing + [synthetic], groups)

            worst_proven = base_report.worst_proven and with_report.worst_proven
            best_proven = base_report.best_proven and with_report.best_proven
            observed_worst_change = with_report.observed_worst - base_report.observed_worst
            conservative_floor_change = with_report.conservative_floor - base_report.conservative_floor
            observed_best_change = with_report.observed_best - base_report.observed_best
            conservative_ceiling_change = with_report.conservative_ceiling - base_report.conservative_ceiling

            expected_change: Decimal | None = None
            expected_mode: str | None = None
            if base_report.expected_case is not None and with_report.expected_case is not None:
                expected_change = with_report.expected_case - base_report.expected_case
                expected_mode = _expected_change_mode(base_report.expected_mode, with_report.expected_mode)

            # Never rank an unproven observed minimum as though it were an exact floor. When extrema
            # are not proven, use the conservative floor delta for the risk component and keep the
            # observed change as explicitly labeled evidence only.
            if worst_proven:
                ranking_risk_change = observed_worst_change
                ranking_risk_truth = "exact-worst-case-change"
            else:
                ranking_risk_change = conservative_floor_change
                ranking_risk_truth = "conservative-floor-change"

            ranked.append(
                CandidatePortfolioImpact(
                    candidate=candidate,
                    stake=amount,
                    dependent_existing_ticket_ids=dependent,
                    base_report=base_report,
                    with_candidate_report=with_report,
                    observed_worst_case_change=observed_worst_change,
                    conservative_floor_change=conservative_floor_change,
                    observed_best_case_change=observed_best_change,
                    conservative_ceiling_change=conservative_ceiling_change,
                    expected_case_change=expected_change,
                    expected_change_mode=expected_mode,
                    worst_case_change_proven=worst_proven,
                    best_case_change_proven=best_proven,
                    ranking_risk_change=ranking_risk_change,
                    ranking_risk_truth=ranking_risk_truth,
                    standalone_expected_profit=candidate.expected_profit_per_unit * amount,
                )
            )

        ranked.sort(key=_ranking_key, reverse=True)
        return ranked[: self.result_limit]


def _quote_group_map(groups: list[ScenarioGroup]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for group_index, group in enumerate(groups):
        for outcome in group.outcomes:
            if outcome.quote_key in mapping:
                raise ValueError(f"quote_key appears in multiple scenario groups: {outcome.quote_key}")
            mapping[outcome.quote_key] = group_index
    return mapping


def _candidate_ticket(
    candidate: ParlayCandidate,
    stake: Decimal,
    quote_to_group: dict[str, int],
) -> PaperTicket:
    if not candidate.legs:
        raise ValueError("candidate requires at least one leg")
    ticket_legs: list[TicketLeg] = []
    touched_groups: set[int] = set()
    for leg in candidate.legs:
        if leg.quote_key not in quote_to_group:
            raise ValueError(f"candidate quote missing from scenario space: {leg.quote_key}")
        group_index = quote_to_group[leg.quote_key]
        if group_index in touched_groups:
            raise ValueError("candidate contains mutually exclusive outcomes from one scenario group")
        touched_groups.add(group_index)
        ticket_leg = _ticket_leg_from_candidate(leg)
        ticket_legs.append(ticket_leg)

    identity = "|".join(item.quote_key for item in candidate.legs) + f"|stake={stake}"
    ticket_id = "optimizer:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return PaperTicket(
        ticket_id=ticket_id,
        stake=stake,
        legs=tuple(ticket_legs),
        placed_at="portfolio-aware-optimizer",
        status=TicketStatus.OPEN,
        strategy_reason="synthetic candidate for portfolio impact evaluation",
    )


def _ticket_leg_from_candidate(leg: CandidateLeg) -> TicketLeg:
    parts = leg.quote_key.split("|", 2)
    if len(parts) != 3 or not all(parts):
        raise ValueError(f"candidate quote_key is not canonical event|market|selection: {leg.quote_key}")
    event_id, market_id, selection_id = parts
    if event_id != leg.event_id:
        raise ValueError("candidate event_id does not match quote_key")
    if leg.decimal_odds <= 1:
        raise ValueError("candidate decimal odds must be greater than 1")
    return TicketLeg(event_id, market_id, selection_id, leg.decimal_odds)


def _dependent_existing_ticket_ids(
    tickets: list[PaperTicket],
    touched_groups: set[int],
    quote_to_group: dict[str, int],
) -> tuple[str, ...]:
    dependent: list[str] = []
    for ticket in tickets:
        ticket_groups: set[int] = set()
        for leg in ticket.legs:
            if leg.quote_key not in quote_to_group:
                raise ValueError(f"existing ticket leg missing from scenario space: {leg.quote_key}")
            ticket_groups.add(quote_to_group[leg.quote_key])
        if ticket_groups.intersection(touched_groups):
            dependent.append(ticket.ticket_id)
    return tuple(sorted(dependent))


def _expected_change_mode(base: str | None, with_candidate: str | None) -> str | None:
    if base is None or with_candidate is None:
        return None
    if base == with_candidate:
        return base
    return f"base:{base};with-candidate:{with_candidate}"


def _ranking_key(impact: CandidatePortfolioImpact) -> tuple[int, Decimal, int, Decimal, int, Decimal]:
    proof_tier = 1 if impact.worst_case_change_proven else 0
    expected_available = 1 if impact.expected_case_change is not None else 0
    expected_change = impact.expected_case_change if impact.expected_case_change is not None else Decimal("-Infinity")
    # Smaller dependency footprint is a deterministic tie-break after actual portfolio risk/expected impact.
    dependency_preference = -len(impact.dependent_existing_ticket_ids)
    return (
        proof_tier,
        impact.ranking_risk_change,
        expected_available,
        expected_change,
        dependency_preference,
        impact.standalone_expected_profit,
    )
