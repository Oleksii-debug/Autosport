from __future__ import annotations

import hashlib
import json
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
        if (
            not isinstance(result_limit, int)
            or isinstance(result_limit, bool)
            or result_limit <= 0
        ):
            raise ValueError("result_limit must be a positive non-boolean integer")
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
        if not amount.is_finite():
            raise ValueError("stake must be finite")
        if amount <= 0:
            raise ValueError("stake must be positive")
        if not groups:
            raise ValueError("scenario groups required for portfolio-aware candidate evaluation")

        quote_to_group = _quote_group_map(groups)
        open_existing = [ticket for ticket in existing_tickets if ticket.status is TicketStatus.OPEN]
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
    used_event_ids: set[str] = set()
    recomputed_odds = Decimal("1")
    recomputed_probability = Decimal("1")
    for leg in candidate.legs:
        if leg.quote_key not in quote_to_group:
            raise ValueError(f"candidate quote missing from scenario space: {leg.quote_key}")
        group_index = quote_to_group[leg.quote_key]
        if group_index in touched_groups:
            raise ValueError("candidate contains mutually exclusive outcomes from one scenario group")
        if leg.event_id in used_event_ids:
            raise ValueError(
                "candidate contains multiple legs from one event; "
                "canonical research candidates require event isolation"
            )
        touched_groups.add(group_index)
        used_event_ids.add(leg.event_id)
        if not leg.probability.is_finite():
            raise ValueError("candidate leg probability must be finite")
        if leg.probability < 0 or leg.probability > 1:
            raise ValueError("candidate leg probability must be between 0 and 1")
        ticket_leg = _ticket_leg_from_candidate(leg)
        ticket_legs.append(ticket_leg)
        recomputed_odds *= leg.decimal_odds
        recomputed_probability *= leg.probability

    recomputed_ev = recomputed_probability * recomputed_odds - Decimal("1")
    if candidate.combined_odds != recomputed_odds:
        raise ValueError("candidate combined_odds does not match its legs")
    if candidate.independent_probability != recomputed_probability:
        raise ValueError("candidate independent_probability does not match its legs")
    if candidate.expected_profit_per_unit != recomputed_ev:
        raise ValueError("candidate expected_profit_per_unit does not match its legs")

    identity = json.dumps(
        {
            "legs": [list(leg.ticket_identity()) for leg in candidate.legs],
            "stake": str(stake),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
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
    event_id, market_id, selection_id = leg.ticket_identity()
    if any("|" in component for component in (event_id, market_id, selection_id)):
        raise ValueError(
            "candidate structured identity cannot enter quote-key scenario risk while an identity component contains '|'"
        )
    if not leg.decimal_odds.is_finite():
        raise ValueError("candidate decimal odds must be finite")
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
    if base == "exact":
        return with_candidate
    return f"base:{base};with-candidate:{with_candidate}"


def _candidate_identity_key(
    candidate: ParlayCandidate,
) -> tuple[tuple[str, str, str, str, str], ...]:
    """Canonical deterministic tie-break independent of caller candidate order."""

    return tuple(
        sorted(
            (
                *leg.ticket_identity(),
                str(leg.decimal_odds),
                str(leg.probability),
            )
            for leg in candidate.legs
        )
    )


def _ranking_key(
    impact: CandidatePortfolioImpact,
) -> tuple[
    int,
    Decimal,
    int,
    Decimal,
    int,
    Decimal,
    tuple[tuple[str, str, str, str, str], ...],
]:
    proof_tier = 1 if impact.worst_case_change_proven else 0
    expected_available = 1 if impact.expected_case_change is not None else 0
    expected_change = impact.expected_case_change if impact.expected_case_change is not None else Decimal("-Infinity")
    dependency_preference = -len(impact.dependent_existing_ticket_ids)
    return (
        proof_tier,
        impact.ranking_risk_change,
        expected_available,
        expected_change,
        dependency_preference,
        impact.standalone_expected_profit,
        _candidate_identity_key(impact.candidate),
    )
