from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Iterable

from .candidate_optimizer import CandidatePortfolioImpact, PortfolioAwareCandidateOptimizer
from .candidate_search import ParlayCandidate
from .decision_ledger import DecisionRecord, JsonlDecisionLedger
from .domain import PaperTicket, TicketLeg
from .forecasting import ForecastRecord, parse_iso_timestamp
from .paper import PaperBook
from .risk import PaperRiskPolicy, RiskDecision
from .scenario_search import ScenarioGroup


_DEFAULT_BLOCKED_QUALITY_FLAGS = frozenset(
    {
        "STALE_SOURCE",
        "FUTURE_CLOCK_SKEW",
        "SOURCE_TIME_REGRESSION",
        "TRUNCATED_BATCH",
        "GAP_DETECTED",
    }
)


def _validate_sha256(value: str, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{label} must be a 64-character SHA-256 hex string")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{label} must be hexadecimal") from exc
    return value.lower()


def _validate_canonical_string(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or not value.strip()
        or value != value.strip()
    ):
        raise ValueError(f"{label} must be a non-empty canonical string")
    return value


def _finite_decimal(value: object, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{label} must be a finite decimal")
    return parsed


@dataclass(frozen=True, slots=True)
class ResearchEvidence:
    """One causal market evidence item made available before a research decision."""

    evidence_id: str
    quote_key: str
    source_id: str
    observed_at: str
    available_at: str
    decimal_odds: Decimal
    content_sha256: str
    quality_flags: tuple[str, ...] = ()
    market_snapshot_hash: str | None = None

    def __post_init__(self) -> None:
        for label, value in (
            ("evidence_id", self.evidence_id),
            ("quote_key", self.quote_key),
            ("source_id", self.source_id),
            ("observed_at", self.observed_at),
            ("available_at", self.available_at),
        ):
            _validate_canonical_string(value, label)
        odds = _finite_decimal(self.decimal_odds, "evidence decimal odds")
        if odds <= 1:
            raise ValueError("evidence decimal odds must be greater than 1")
        object.__setattr__(self, "decimal_odds", odds)
        observed = parse_iso_timestamp(self.observed_at)
        available = parse_iso_timestamp(self.available_at)
        if available < observed:
            raise ValueError("evidence cannot be available before it was observed")
        object.__setattr__(
            self, "content_sha256", _validate_sha256(self.content_sha256, "content_sha256")
        )
        if self.market_snapshot_hash is not None:
            object.__setattr__(
                self,
                "market_snapshot_hash",
                _validate_sha256(self.market_snapshot_hash, "market_snapshot_hash"),
            )
        if not isinstance(self.quality_flags, (tuple, list)):
            raise ValueError("evidence quality flags must be a tuple or list")
        flags = tuple(self.quality_flags)
        if any(
            not isinstance(flag, str) or not flag.strip() or flag != flag.strip()
            for flag in flags
        ):
            raise ValueError("evidence quality flags must be non-empty canonical strings")
        if len(flags) != len(set(flags)):
            raise ValueError("duplicate evidence quality flag")
        object.__setattr__(self, "quality_flags", flags)


@dataclass(frozen=True, slots=True)
class ResearchDecisionPolicy:
    """Deterministic critic/data-quality policy. No LLM or network call is made here."""

    max_forecast_uncertainty: Decimal = Decimal("0.35")
    minimum_evidence_per_leg: int = 1
    blocked_quality_flags: frozenset[str] = field(
        default_factory=lambda: _DEFAULT_BLOCKED_QUALITY_FLAGS
    )
    require_market_snapshot_hash: bool = True
    require_worst_case_proof: bool = False
    minimum_ranking_risk_change: Decimal | None = None
    minimum_standalone_expected_profit_per_unit: Decimal | None = None

    def __post_init__(self) -> None:
        uncertainty = _finite_decimal(
            self.max_forecast_uncertainty,
            "max_forecast_uncertainty",
        )
        if uncertainty < 0 or uncertainty > 1:
            raise ValueError("max_forecast_uncertainty must be between 0 and 1")
        object.__setattr__(self, "max_forecast_uncertainty", uncertainty)
        if (
            isinstance(self.minimum_evidence_per_leg, bool)
            or not isinstance(self.minimum_evidence_per_leg, int)
            or self.minimum_evidence_per_leg < 1
        ):
            raise ValueError("minimum_evidence_per_leg must be a positive integer")
        if not isinstance(self.blocked_quality_flags, (tuple, list, set, frozenset)):
            raise ValueError(
                "blocked_quality_flags must be a tuple, list, set, or frozenset"
            )
        blocked_flags = tuple(self.blocked_quality_flags)
        if any(
            not isinstance(flag, str) or not flag.strip() or flag != flag.strip()
            for flag in blocked_flags
        ):
            raise ValueError(
                "blocked_quality_flags must contain non-empty canonical strings"
            )
        object.__setattr__(
            self,
            "blocked_quality_flags",
            frozenset(blocked_flags),
        )
        if self.minimum_ranking_risk_change is not None:
            object.__setattr__(
                self,
                "minimum_ranking_risk_change",
                _finite_decimal(
                    self.minimum_ranking_risk_change,
                    "minimum_ranking_risk_change",
                ),
            )
        if self.minimum_standalone_expected_profit_per_unit is not None:
            object.__setattr__(
                self,
                "minimum_standalone_expected_profit_per_unit",
                _finite_decimal(
                    self.minimum_standalone_expected_profit_per_unit,
                    "minimum_standalone_expected_profit_per_unit",
                ),
            )


@dataclass(frozen=True, slots=True)
class ResearchLegReview:
    quote_key: str
    approved: bool
    reasons: tuple[str, ...]
    forecast_id: str | None
    forecast_hash: str | None
    evidence_ids: tuple[str, ...]
    evidence_hashes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CriticVerdict:
    approved: bool
    reasons: tuple[str, ...]
    leg_reviews: tuple[ResearchLegReview, ...]


@dataclass(frozen=True, slots=True)
class ResearchDecision:
    approved: bool
    reasons: tuple[str, ...]
    decision_ts: str
    critic: CriticVerdict
    portfolio_impact: CandidatePortfolioImpact
    risk: RiskDecision
    ticket_id: str | None
    audit_sha256: str


class DeterministicResearchCritic:
    """Causal evidence/forecast critic that only evaluates typed local inputs."""

    name = "deterministic-research-critic"

    def __init__(self, policy: ResearchDecisionPolicy | None = None) -> None:
        self.policy = policy or ResearchDecisionPolicy()

    def review(
        self,
        candidate: ParlayCandidate,
        forecasts: dict[str, ForecastRecord],
        evidence: Iterable[ResearchEvidence],
        *,
        decision_ts: str,
    ) -> CriticVerdict:
        decision_time = parse_iso_timestamp(decision_ts)
        by_quote: dict[str, list[ResearchEvidence]] = {}
        for item in evidence:
            if parse_iso_timestamp(item.available_at) <= decision_time:
                by_quote.setdefault(item.quote_key, []).append(item)

        leg_reviews: list[ResearchLegReview] = []
        all_reasons: list[str] = []
        for leg in candidate.legs:
            reasons: list[str] = []
            forecast = forecasts.get(leg.quote_key)
            available = sorted(
                by_quote.get(leg.quote_key, []),
                key=lambda item: (parse_iso_timestamp(item.available_at), item.evidence_id),
            )
            selected_ids: tuple[str, ...] = ()
            selected_hashes: tuple[str, ...] = ()

            if forecast is None:
                reasons.append("missing ForecastRecord")
            else:
                if forecast.quote_key != leg.quote_key:
                    reasons.append("forecast quote_key mismatch")
                if parse_iso_timestamp(forecast.generated_at) > decision_time:
                    reasons.append("forecast was generated after decision time")
                if parse_iso_timestamp(forecast.input_cutoff_ts) > decision_time:
                    reasons.append("forecast input cutoff is after decision time")
                if forecast.probability != leg.probability:
                    reasons.append("candidate probability does not match ForecastRecord")
                if forecast.uncertainty > self.policy.max_forecast_uncertainty:
                    reasons.append("forecast uncertainty exceeds policy")

                forecast_evidence_hashes = {
                    str(value).lower() for value in forecast.evidence_hashes
                }
                included = [
                    item
                    for item in available
                    if parse_iso_timestamp(item.available_at)
                    <= parse_iso_timestamp(forecast.input_cutoff_ts)
                    and item.content_sha256 in forecast_evidence_hashes
                ]
                selected_ids = tuple(item.evidence_id for item in included)
                selected_hashes = tuple(item.content_sha256 for item in included)
                if len(included) < self.policy.minimum_evidence_per_leg:
                    reasons.append("insufficient causal evidence linked by forecast hash")

                if not available:
                    reasons.append("no evidence was available by decision time")
                else:
                    latest = available[-1]
                    if parse_iso_timestamp(latest.available_at) > parse_iso_timestamp(
                        forecast.input_cutoff_ts
                    ):
                        reasons.append("forecast does not cover latest available evidence")
                    if latest.content_sha256 not in forecast_evidence_hashes:
                        reasons.append("latest evidence hash is absent from ForecastRecord")
                    if latest.decimal_odds != leg.decimal_odds:
                        reasons.append("candidate odds do not match latest evidence")
                    blocked = sorted(
                        set(latest.quality_flags).intersection(
                            self.policy.blocked_quality_flags
                        )
                    )
                    if blocked:
                        reasons.append(
                            "blocked data-quality flags: " + ",".join(blocked)
                        )
                    if self.policy.require_market_snapshot_hash:
                        if forecast.market_snapshot_hash is None:
                            reasons.append("ForecastRecord lacks market snapshot hash")
                        elif latest.market_snapshot_hash is None:
                            reasons.append("latest evidence lacks market snapshot hash")
                        elif (
                            forecast.market_snapshot_hash.lower()
                            != latest.market_snapshot_hash.lower()
                        ):
                            reasons.append("market snapshot hash mismatch")

            review = ResearchLegReview(
                quote_key=leg.quote_key,
                approved=not reasons,
                reasons=tuple(reasons),
                forecast_id=forecast.forecast_id if forecast else None,
                forecast_hash=forecast.canonical_hash if forecast else None,
                evidence_ids=selected_ids,
                evidence_hashes=selected_hashes,
            )
            leg_reviews.append(review)
            all_reasons.extend(f"{leg.quote_key}: {reason}" for reason in reasons)

        return CriticVerdict(
            approved=not all_reasons,
            reasons=tuple(all_reasons),
            leg_reviews=tuple(leg_reviews),
        )


class ResearchDecisionPipeline:
    """
    Paper-only research decision path:
    evidence -> ForecastRecord -> critic -> portfolio impact -> risk -> PaperBook + audit.
    """

    def __init__(
        self,
        *,
        optimizer: PortfolioAwareCandidateOptimizer | None = None,
        critic: DeterministicResearchCritic | None = None,
        risk_policy: PaperRiskPolicy | None = None,
    ) -> None:
        self.optimizer = optimizer or PortfolioAwareCandidateOptimizer()
        self.critic = critic or DeterministicResearchCritic()
        self.risk_policy = risk_policy or PaperRiskPolicy()

    def decide_and_open(
        self,
        *,
        book: PaperBook,
        candidate: ParlayCandidate,
        groups: list[ScenarioGroup],
        forecasts: dict[str, ForecastRecord],
        evidence: Iterable[ResearchEvidence],
        stake: Decimal | str,
        decision_ts: str,
        decision_ledger: JsonlDecisionLedger,
        replay_run_id: str,
    ) -> ResearchDecision:
        _validate_canonical_string(replay_run_id, "replay_run_id")
        _validate_canonical_string(decision_ts, "decision_ts")
        parse_iso_timestamp(decision_ts)
        amount = _finite_decimal(stake, "stake")
        if amount <= 0:
            raise ValueError("stake must be positive")

        evidence_items = tuple(evidence)
        context_hash = _research_context_hash(
            book=book,
            candidate=candidate,
            groups=groups,
            forecasts=forecasts,
            evidence=evidence_items,
            decision_ts=decision_ts,
        )

        impact = self.optimizer.evaluate_candidates(
            list(book.tickets.values()),
            [candidate],
            groups,
            stake=amount,
        )[0]
        critic_verdict = self.critic.review(
            candidate,
            forecasts,
            evidence_items,
            decision_ts=decision_ts,
        )
        risk = self.risk_policy.evaluate(book, amount)

        reasons: list[str] = list(critic_verdict.reasons)
        policy = self.critic.policy
        if not risk.allowed:
            reasons.append("risk policy: " + risk.reason)
        if policy.require_worst_case_proof and not impact.worst_case_change_proven:
            reasons.append("portfolio worst-case change is not proven exact")
        if (
            policy.minimum_ranking_risk_change is not None
            and impact.ranking_risk_change < policy.minimum_ranking_risk_change
        ):
            reasons.append("portfolio ranking risk change is below policy minimum")
        if (
            policy.minimum_standalone_expected_profit_per_unit is not None
            and candidate.expected_profit_per_unit
            < policy.minimum_standalone_expected_profit_per_unit
        ):
            reasons.append("standalone expected profit per unit is below policy minimum")

        reasons = list(dict.fromkeys(reasons))
        approved = not reasons
        ticket: PaperTicket | None = None
        if approved:
            ticket = book.open_ticket(
                _ticket_legs(candidate),
                amount,
                reason=(
                    "paper research decision; "
                    f"portfolio_truth={impact.ranking_risk_truth}; "
                    f"critic={self.critic.name}"
                ),
                placed_at=decision_ts,
            )

        action = (
            "OPEN_PAPER_RESEARCH_TICKET"
            if approved
            else "REJECT_PAPER_RESEARCH_CANDIDATE"
        )
        payload = _audit_payload(
            candidate=candidate,
            amount=amount,
            critic=critic_verdict,
            impact=impact,
            risk=risk,
            approved=approved,
            reasons=tuple(reasons),
            ticket_id=ticket.ticket_id if ticket else None,
            forecasts=forecasts,
            evidence=evidence_items,
        )
        audit_sha = decision_ledger.append(
            DecisionRecord(
                replay_run_id=replay_run_id,
                agent="research-decision-pipeline",
                observed_ts=decision_ts,
                action=action,
                payload=payload,
                context_hash=context_hash,
            )
        )
        return ResearchDecision(
            approved=approved,
            reasons=tuple(reasons),
            decision_ts=decision_ts,
            critic=critic_verdict,
            portfolio_impact=impact,
            risk=risk,
            ticket_id=ticket.ticket_id if ticket else None,
            audit_sha256=audit_sha,
        )


def _candidate_identity_payload(leg) -> dict[str, str]:
    event_id, market_id, selection_id = leg.ticket_identity()
    return {
        "quote_key": leg.quote_key,
        "event_id": event_id,
        "market_id": market_id,
        "selection_id": selection_id,
    }


def _ticket_legs(candidate: ParlayCandidate) -> list[TicketLeg]:
    legs: list[TicketLeg] = []
    for item in candidate.legs:
        event_id, market_id, selection_id = item.ticket_identity()
        legs.append(TicketLeg(event_id, market_id, selection_id, item.decimal_odds))
    return legs


def _audit_payload(
    *,
    candidate: ParlayCandidate,
    amount: Decimal,
    critic: CriticVerdict,
    impact: CandidatePortfolioImpact,
    risk: RiskDecision,
    approved: bool,
    reasons: tuple[str, ...],
    ticket_id: str | None,
    forecasts: dict[str, ForecastRecord],
    evidence: tuple[ResearchEvidence, ...],
) -> dict:
    candidate_keys = {leg.quote_key for leg in candidate.legs}
    relevant_evidence = sorted(
        (item for item in evidence if item.quote_key in candidate_keys),
        key=lambda item: (item.quote_key, item.available_at, item.evidence_id),
    )
    return {
        "approved": approved,
        "reasons": list(reasons),
        "ticket_id": ticket_id,
        "stake": str(amount),
        "candidate_quote_keys": [leg.quote_key for leg in candidate.legs],
        "candidate_ticket_identities": [
            _candidate_identity_payload(leg) for leg in candidate.legs
        ],
        "candidate_combined_odds": str(candidate.combined_odds),
        "candidate_independent_probability": str(candidate.independent_probability),
        "candidate_expected_profit_per_unit": str(candidate.expected_profit_per_unit),
        "critic": {
            "approved": critic.approved,
            "reasons": list(critic.reasons),
            "legs": [
                {
                    "quote_key": review.quote_key,
                    "approved": review.approved,
                    "reasons": list(review.reasons),
                    "forecast_id": review.forecast_id,
                    "forecast_hash": review.forecast_hash,
                    "evidence_ids": list(review.evidence_ids),
                    "evidence_hashes": list(review.evidence_hashes),
                }
                for review in critic.leg_reviews
            ],
        },
        "portfolio": {
            "ranking_risk_change": str(impact.ranking_risk_change),
            "ranking_risk_truth": impact.ranking_risk_truth,
            "observed_worst_case_change": str(impact.observed_worst_case_change),
            "conservative_floor_change": str(impact.conservative_floor_change),
            "worst_case_change_proven": impact.worst_case_change_proven,
            "observed_best_case_change": str(impact.observed_best_case_change),
            "conservative_ceiling_change": str(impact.conservative_ceiling_change),
            "best_case_change_proven": impact.best_case_change_proven,
            "expected_case_change": (
                str(impact.expected_case_change)
                if impact.expected_case_change is not None
                else None
            ),
            "expected_change_mode": impact.expected_change_mode,
            "dependent_existing_ticket_ids": list(impact.dependent_existing_ticket_ids),
        },
        "risk": {"allowed": risk.allowed, "reason": risk.reason},
        "forecasts": [
            {
                "quote_key": key,
                "forecast_id": forecasts[key].forecast_id,
                "forecast_hash": forecasts[key].canonical_hash,
                "model_id": forecasts[key].model_id,
                "model_version": forecasts[key].model_version,
                "strategy_version": forecasts[key].strategy_version,
                "input_cutoff_ts": forecasts[key].input_cutoff_ts,
                "generated_at": forecasts[key].generated_at,
                "uncertainty": str(forecasts[key].uncertainty),
            }
            for key in sorted(candidate_keys.intersection(forecasts))
        ],
        "evidence": [
            {
                "evidence_id": item.evidence_id,
                "quote_key": item.quote_key,
                "source_id": item.source_id,
                "observed_at": item.observed_at,
                "available_at": item.available_at,
                "decimal_odds": str(item.decimal_odds),
                "content_sha256": item.content_sha256,
                "market_snapshot_hash": item.market_snapshot_hash,
                "quality_flags": list(item.quality_flags),
            }
            for item in relevant_evidence
        ],
        "real_money_execution": False,
    }


def _research_context_hash(
    *,
    book: PaperBook,
    candidate: ParlayCandidate,
    groups: list[ScenarioGroup],
    forecasts: dict[str, ForecastRecord],
    evidence: tuple[ResearchEvidence, ...],
    decision_ts: str,
) -> str:
    candidate_keys = {leg.quote_key for leg in candidate.legs}
    payload = {
        "decision_ts": decision_ts,
        "book": {
            "initial_bankroll": str(book.initial_bankroll),
            "balance": str(book.balance),
            "tickets": [
                {
                    "ticket_id": ticket.ticket_id,
                    "stake": str(ticket.stake),
                    "status": ticket.status.value,
                    "payout": str(ticket.payout),
                    "legs": [
                        {"quote_key": leg.quote_key, "locked_odds": str(leg.locked_odds)}
                        for leg in ticket.legs
                    ],
                }
                for ticket in sorted(book.tickets.values(), key=lambda item: item.ticket_id)
            ],
        },
        "candidate": [
            {
                **_candidate_identity_payload(leg),
                "decimal_odds": str(leg.decimal_odds),
                "probability": str(leg.probability),
            }
            for leg in candidate.legs
        ],
        "groups": [
            {
                "group_id": group.group_id,
                "outcomes": [
                    {
                        "quote_key": outcome.quote_key,
                        "probability": str(outcome.probability) if outcome.probability is not None else None,
                    }
                    for outcome in group.outcomes
                ],
            }
            for group in groups
        ],
        "forecasts": [
            {"quote_key": key, "forecast_hash": forecasts[key].canonical_hash}
            for key in sorted(candidate_keys.intersection(forecasts))
        ],
        "evidence": [
            {
                "evidence_id": item.evidence_id,
                "quote_key": item.quote_key,
                "content_sha256": item.content_sha256,
                "available_at": item.available_at,
                "decimal_odds": str(item.decimal_odds),
                "quality_flags": list(item.quality_flags),
            }
            for item in sorted(
                (item for item in evidence if item.quote_key in candidate_keys),
                key=lambda item: (item.quote_key, item.available_at, item.evidence_id),
            )
        ],
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
