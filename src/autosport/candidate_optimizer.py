from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, DecimalException, Overflow, Underflow, localcontext

from .candidate_search import (
    _CANDIDATE_DECIMAL_CONTEXT,
    BeamParlayCandidateSearch,
    CandidateLeg,
    ParlayCandidate,
)
from .domain import PaperTicket, TicketLeg, TicketStatus
from .portfolio import (
    _PORTFOLIO_DECIMAL_CONTEXT,
    _snapshot_open_tickets_for_analysis,
)
from .scenario_search import (
    ScenarioGroup,
    ScenarioSearchEngine,
    ScenarioSearchReport,
    _scenario_conservative_bounds,
)


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
    scenario_reports_authoritative: bool
    worst_case_change_proven: bool
    best_case_change_proven: bool
    ranking_risk_change: Decimal
    ranking_risk_truth: str
    standalone_expected_profit: Decimal

    @property
    def scenario_worst_case_change_proven(self) -> bool:
        """True only for exact extrema inside the supplied scenario model."""
        return (
            self.scenario_reports_authoritative
            and self.base_report.worst_proven
            and self.with_candidate_report.worst_proven
        )

    @property
    def scenario_best_case_change_proven(self) -> bool:
        """True only for exact extrema inside the supplied scenario model."""
        return (
            self.scenario_reports_authoritative
            and self.base_report.best_proven
            and self.with_candidate_report.best_proven
        )

    @property
    def exact_marginal_extrema(self) -> bool:
        """Terminal-space exactness; incomplete caller scenario models never qualify."""
        return self.worst_case_change_proven and self.best_case_change_proven


class PortfolioAwareCandidateOptimizer:
    """
    Generate bounded candidates, then rerank them by their change to the whole paper portfolio.

    The beam generator's independent-probability EV is only a screening/tie-break signal.
    This generic API accepts caller-supplied scenario models, so their exact extrema remain
    secondary ranking evidence and never become terminal-space proof. Positive terminal-risk
    authority requires a separate product-owned authoritative outcome composition.
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
        # Financial/economic ingress must not silently promote binary floats,
        # bools, integers, Decimal subclasses, or arbitrary __str__ providers into
        # stake authority. Preserve the established exact Decimal/text API only.
        if type(stake) is Decimal:
            amount = stake
        elif type(stake) is str:
            try:
                amount = Decimal(stake)
            except DecimalException as exc:
                raise ValueError(
                    "stake must be an exact Decimal or decimal text"
                ) from exc
        else:
            raise ValueError("stake must be an exact Decimal or decimal text")
        if not amount.is_finite():
            raise ValueError("stake must be finite")
        if amount <= 0:
            raise ValueError("stake must be positive")

        candidate_snapshot = tuple(candidates)
        scenario_groups = list(groups)
        if not scenario_groups:
            raise ValueError("scenario groups required for portfolio-aware candidate evaluation")

        quote_to_group = _quote_group_map(scenario_groups)
        open_existing = _snapshot_open_tickets_for_analysis(existing_tickets)
        base_floor, base_ceiling = _scenario_conservative_bounds(open_existing)
        base_report, scenario_reports_authoritative = _analyse_scenario_engine(
            self.scenario_engine,
            _snapshot_open_tickets_for_analysis(open_existing),
            list(scenario_groups),
        )
        _validate_scenario_report(
            base_report,
            canonical_floor=base_floor,
            canonical_ceiling=base_ceiling,
        )
        ranked: list[CandidatePortfolioImpact] = []
        seen_candidate_identities: set[
            tuple[tuple[str, str, str, str, str, str], ...]
        ] = set()
        for candidate in candidate_snapshot:
            canonical_candidate = _canonical_candidate(candidate)
            candidate_identity = _candidate_identity_key(canonical_candidate)
            if candidate_identity in seen_candidate_identities:
                continue
            seen_candidate_identities.add(candidate_identity)

            synthetic = _candidate_ticket(canonical_candidate, amount, quote_to_group)
            touched_groups = {quote_to_group[leg.quote_key] for leg in synthetic.legs}
            dependent = _dependent_existing_ticket_ids(open_existing, touched_groups, quote_to_group)
            with_candidate_tickets = open_existing + [synthetic]
            with_floor, with_ceiling = _scenario_conservative_bounds(with_candidate_tickets)
            with_report, with_report_authoritative = _analyse_scenario_engine(
                self.scenario_engine,
                _snapshot_open_tickets_for_analysis(with_candidate_tickets),
                list(scenario_groups),
            )
            if with_report_authoritative is not scenario_reports_authoritative:
                raise ValueError("scenario engine authority changed during evaluation")
            _validate_scenario_report(
                with_report,
                canonical_floor=with_floor,
                canonical_ceiling=with_ceiling,
            )

            # This API accepts caller-supplied ScenarioGroup models, not product-owned
            # MarketSettlementOutcomeAuthority evidence. Report flags returned by an
            # injected/subclassed scenario engine therefore cannot mint terminal-space
            # authority. Generic scenario extrema remain useful secondary evidence only.
            worst_proven = False
            best_proven = False
            try:
                with localcontext(_PORTFOLIO_DECIMAL_CONTEXT):
                    observed_worst_change = (
                        with_report.observed_worst - base_report.observed_worst
                    )
                    conservative_floor_change = with_floor - base_floor
                    observed_best_change = (
                        with_report.observed_best - base_report.observed_best
                    )
                    conservative_ceiling_change = with_ceiling - base_ceiling

                    expected_change: Decimal | None = None
                    if (
                        scenario_reports_authoritative
                        and base_report.expected_case is not None
                        and with_report.expected_case is not None
                    ):
                        expected_change = (
                            with_report.expected_case - base_report.expected_case
                        )
            except DecimalException as exc:
                raise ValueError(
                    "candidate portfolio impact exceeds canonical Decimal range"
                ) from exc

            impact_values = (
                observed_worst_change,
                conservative_floor_change,
                observed_best_change,
                conservative_ceiling_change,
            )
            if any(not value.is_finite() for value in impact_values) or (
                expected_change is not None and not expected_change.is_finite()
            ):
                raise ValueError(
                    "candidate portfolio impact must be finite"
                )

            expected_mode: str | None = None
            if expected_change is not None:
                expected_mode = _expected_change_mode(
                    base_report.expected_mode,
                    with_report.expected_mode,
                )

            if worst_proven:
                ranking_risk_change = observed_worst_change
                ranking_risk_truth = "exact-worst-case-change"
            else:
                ranking_risk_change = conservative_floor_change
                ranking_risk_truth = "conservative-floor-change"

            ranked.append(
                CandidatePortfolioImpact(
                    candidate=canonical_candidate,
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
                    scenario_reports_authoritative=scenario_reports_authoritative,
                    worst_case_change_proven=worst_proven,
                    best_case_change_proven=best_proven,
                    ranking_risk_change=ranking_risk_change,
                    ranking_risk_truth=ranking_risk_truth,
                    standalone_expected_profit=_scale_standalone_expected_profit(
                        canonical_candidate.expected_profit_per_unit,
                        amount,
                    ),
                )
            )

        ranked.sort(key=_ranking_key, reverse=True)
        return ranked[: self.result_limit]


def _scale_standalone_expected_profit(
    expected_profit_per_unit: Decimal,
    stake: Decimal,
) -> Decimal:
    """Scale canonical candidate EV without inheriting caller Decimal context."""

    try:
        with localcontext(_CANDIDATE_DECIMAL_CONTEXT) as context:
            context.clear_flags()
            value = expected_profit_per_unit * stake
            range_lost = context.flags[Overflow] or context.flags[Underflow]
    except DecimalException as exc:
        raise ValueError("candidate standalone expected profit exceeds Decimal range") from exc
    if range_lost or not value.is_finite():
        raise ValueError("candidate standalone expected profit exceeds Decimal range")
    return value


def _quote_group_map(groups: list[ScenarioGroup]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for group_index, group in enumerate(groups):
        for outcome in group.outcomes:
            if outcome.quote_key in mapping:
                raise ValueError(f"quote_key appears in multiple scenario groups: {outcome.quote_key}")
            mapping[outcome.quote_key] = group_index
    return mapping


def _decimal_identity_key(value: Decimal) -> str:
    """Exact context-independent numeric identity bounded by stored coefficient digits."""

    if not value.is_finite():
        raise ValueError("decimal identity requires a finite value")
    if value.is_zero():
        return "0:0:0"

    parts = value.as_tuple()
    exponent = parts.exponent
    if not isinstance(exponent, int):
        raise ValueError("decimal identity requires a finite value")
    digits = list(parts.digits)
    while digits[-1] == 0:
        digits.pop()
        exponent += 1
    coefficient = "".join(str(digit) for digit in digits)
    return f"{parts.sign}:{coefficient}:{exponent}"


def _candidate_leg_identity_key(
    leg: CandidateLeg,
) -> tuple[str, str, str, str, str, str]:
    return (
        leg.sport or "",
        *leg.ticket_identity(),
        _decimal_identity_key(leg.decimal_odds),
        _decimal_identity_key(leg.probability),
    )


def _canonical_candidate(candidate: ParlayCandidate) -> ParlayCandidate:
    # Candidate economics are an authority boundary.  Do not dispatch through
    # caller subclasses/proxies before accepting the canonical DTO shape.
    if type(candidate) is not ParlayCandidate:
        raise ValueError("candidate must be the exact canonical ParlayCandidate type")
    if type(candidate.legs) is not tuple or not candidate.legs:
        raise ValueError("candidate requires a canonical non-empty leg tuple")

    for leg in candidate.legs:
        if type(leg) is not CandidateLeg:
            raise ValueError("candidate leg must be the exact canonical CandidateLeg type")
        if type(leg.decimal_odds) is not Decimal:
            raise ValueError("candidate decimal odds must be an exact Decimal")
        if not leg.decimal_odds.is_finite():
            raise ValueError("candidate decimal odds must be finite")
        if leg.decimal_odds <= 1:
            raise ValueError("candidate decimal odds must be greater than 1")
        if type(leg.probability) is not Decimal:
            raise ValueError("candidate leg probability must be an exact Decimal")
        if not leg.probability.is_finite():
            raise ValueError("candidate leg probability must be finite")
        if leg.probability < 0 or leg.probability > 1:
            raise ValueError("candidate leg probability must be between 0 and 1")

    canonical_legs = tuple(sorted(candidate.legs, key=_candidate_leg_identity_key))

    # Reuse the generator's canonical arithmetic authority instead of rebuilding
    # candidate economics under mutable caller Decimal context. This also gives
    # semantically equivalent leg permutations one canonical candidate form.
    canonical = BeamParlayCandidateSearch._to_candidate(canonical_legs)
    if candidate.combined_odds != canonical.combined_odds:
        raise ValueError("candidate combined_odds does not match its legs")
    if candidate.independent_probability != canonical.independent_probability:
        raise ValueError("candidate independent_probability does not match its legs")
    if candidate.expected_profit_per_unit != canonical.expected_profit_per_unit:
        raise ValueError("candidate expected_profit_per_unit does not match its legs")
    return canonical


def _candidate_ticket(
    candidate: ParlayCandidate,
    stake: Decimal,
    quote_to_group: dict[str, int],
) -> PaperTicket:
    candidate = _canonical_candidate(candidate)
    ticket_legs: list[TicketLeg] = []
    touched_groups: set[int] = set()
    used_event_ids: set[tuple[str | None, str]] = set()
    for leg in candidate.legs:
        if leg.quote_key not in quote_to_group:
            raise ValueError(f"candidate quote missing from scenario space: {leg.quote_key}")
        group_index = quote_to_group[leg.quote_key]
        if group_index in touched_groups:
            raise ValueError("candidate contains mutually exclusive outcomes from one scenario group")
        event_identity = (leg.sport, leg.event_id)
        if event_identity in used_event_ids:
            raise ValueError(
                "candidate contains multiple legs from one event within the same "
                "sport-qualified identity; canonical research candidates require event isolation"
            )
        touched_groups.add(group_index)
        used_event_ids.add(event_identity)
        if not leg.probability.is_finite():
            raise ValueError("candidate leg probability must be finite")
        if leg.probability < 0 or leg.probability > 1:
            raise ValueError("candidate leg probability must be between 0 and 1")
        ticket_legs.append(_ticket_leg_from_candidate(leg))

    identity = json.dumps(
        {
            "legs": [list(part) for part in _candidate_identity_key(candidate)],
            "stake": _decimal_identity_key(stake),
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
    return TicketLeg(
        event_id,
        market_id,
        selection_id,
        leg.decimal_odds,
        sport=leg.sport,
    )


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


def _analyse_scenario_engine(
    engine: ScenarioSearchEngine,
    tickets: list[PaperTicket],
    groups: list[ScenarioGroup],
    _engine_type=ScenarioSearchEngine,
    _canonical_analyse=ScenarioSearchEngine.analyse,
) -> tuple[ScenarioSearchReport, bool]:
    """Return one report plus authority derived from the captured canonical engine type."""

    if type(engine) is _engine_type:
        canonical = _engine_type(
            exact_state_limit=engine.exact_state_limit,
            branch_node_limit=engine.branch_node_limit,
            sample_count=engine.sample_count,
            seed=engine.seed,
        )
        return _canonical_analyse(canonical, tickets, groups), True
    return engine.analyse(tickets, groups), False


def _validate_scenario_report(
    report: ScenarioSearchReport,
    *,
    canonical_floor: Decimal,
    canonical_ceiling: Decimal,
) -> None:
    """Validate report shape and independently derived conservative bounds."""

    if type(report) is not ScenarioSearchReport:
        raise ValueError("scenario engine must return exact ScenarioSearchReport")
    if (
        type(report.total_states) is not int
        or report.total_states <= 0
        or type(report.nodes_explored) is not int
        or report.nodes_explored < 0
    ):
        raise ValueError("scenario report state counts must be bounded integers")
    if type(report.mode) is not str or not report.mode or report.mode != report.mode.strip():
        raise ValueError("scenario report mode must be canonical text")
    if type(report.worst_proven) is not bool or type(report.best_proven) is not bool:
        raise ValueError("scenario report proof flags must be bool")
    if (
        type(report.outcome_space_exhaustive) is not bool
        or type(report.outcome_space_exact) is not bool
    ):
        raise ValueError("scenario report outcome-space flags must be bool")

    numeric = (
        report.observed_worst,
        report.observed_best,
        report.conservative_floor,
        report.conservative_ceiling,
    )
    if any(type(value) is not Decimal or not value.is_finite() for value in numeric):
        raise ValueError("scenario report risk values must be exact finite Decimal")
    if (
        report.conservative_floor != canonical_floor
        or report.conservative_ceiling != canonical_ceiling
    ):
        raise ValueError("scenario report conservative bounds mismatch")
    if not (
        canonical_floor
        <= report.observed_worst
        <= report.observed_best
        <= canonical_ceiling
    ):
        raise ValueError("scenario report observed extrema exceed conservative bounds")

    if report.expected_case is None:
        if report.expected_mode is not None:
            raise ValueError("scenario expected mode requires expected_case")
    else:
        if (
            type(report.expected_case) is not Decimal
            or not report.expected_case.is_finite()
            or report.expected_case < canonical_floor
            or report.expected_case > canonical_ceiling
        ):
            raise ValueError("scenario expected_case exceeds conservative bounds")
        if (
            type(report.expected_mode) is not str
            or not report.expected_mode
            or report.expected_mode != report.expected_mode.strip()
        ):
            raise ValueError("scenario expected mode must be canonical text")


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
) -> tuple[tuple[str, str, str, str, str, str], ...]:
    """Canonical deterministic tie-break independent of caller candidate order."""

    return tuple(sorted(_candidate_leg_identity_key(leg) for leg in candidate.legs))


def _ranking_key(
    impact: CandidatePortfolioImpact,
) -> tuple[
    int,
    Decimal,
    int,
    Decimal,
    int,
    Decimal,
    int,
    Decimal,
    tuple[tuple[str, str, str, str, str, str], ...],
]:
    terminal_proof_tier = 1 if impact.worst_case_change_proven else 0
    scenario_proof_tier = 1 if impact.scenario_worst_case_change_proven else 0
    scenario_worst_change = (
        impact.observed_worst_case_change
        if impact.scenario_worst_case_change_proven
        else Decimal("-Infinity")
    )
    expected_available = 1 if impact.expected_case_change is not None else 0
    expected_change = impact.expected_case_change if impact.expected_case_change is not None else Decimal("-Infinity")
    dependency_preference = -len(impact.dependent_existing_ticket_ids)
    return (
        terminal_proof_tier,
        impact.ranking_risk_change,
        scenario_proof_tier,
        scenario_worst_change,
        expected_available,
        expected_change,
        dependency_preference,
        impact.standalone_expected_profit,
        _candidate_identity_key(impact.candidate),
    )
