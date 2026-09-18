from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Iterable

from .candidate_optimizer import CandidatePortfolioImpact, PortfolioAwareCandidateOptimizer
from .candidate_search import ParlayCandidate
from .decision_ledger import (
    ECONOMIC_DECISION_KIND,
    GENERAL_DECISION_KIND,
    MATERIAL_ACTION_ID_PAYLOAD_KEY,
    DecisionRecord,
    JsonlDecisionLedger,
    bind_economic_goal,
)
from .domain import MarketEvent, PaperTicket, TicketLeg
from .forecasting import ForecastRecord, parse_iso_timestamp
from .paper import PaperBook
from .price_truth import paper_quote_rejection_reason
from .risk import (
    PaperRiskPolicy,
    ProposedTicketRiskContext,
    RiskDecision,
    RiskOfRuinEvidence,
)
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

_RESEARCH_MATERIAL_ACTION_SCHEMA = "autosport.research.open-ticket.v1"
_RESEARCH_MATERIAL_ACTION_MARKER = "material_action_id="
_RESEARCH_MATERIAL_ACTION_NAME = "OPEN_PAPER_RESEARCH_TICKET"
_RESEARCH_MATERIAL_ACTION_INTENT_SCHEMA = "autosport.research.material-action-intent.v1"
_RESEARCH_MATERIAL_ACTION_INTENT_PAYLOAD_KEY = "material_action_intent_sha256"
_RESEARCH_DECISION_AGENT = "research-decision-pipeline"


class ResearchDecisionReconciliationRequired(RuntimeError):
    """Raised when durable paper and decision evidence disagree for one research action."""


class ResearchDecisionAlreadyCommitted(RuntimeError):
    """Signals an exact previously committed research material action on retry/restart."""

    def __init__(self, material_action_id: str, ticket_id: str) -> None:
        super().__init__(
            "research material action is already durably committed: "
            f"{material_action_id}"
        )
        self.material_action_id = material_action_id
        self.ticket_id = ticket_id


def _validate_sha256(value: str, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{label} must be a 64-character SHA-256 hex string")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{label} must be hexadecimal") from exc
    return value.lower()


def _validate_canonical_string(
    value: object,
    label: str,
    *,
    canonical_error: str | None = None,
) -> str:
    if (
        type(value) is not str
        or not value
        or not value.strip()
        or value != value.strip()
    ):
        raise ValueError(
            canonical_error or f"{label} must be a non-empty canonical string"
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8 text") from exc
    return value


def _finite_decimal(value: object, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{label} must be a finite decimal")
    return parsed


def _research_material_action_id(
    *,
    replay_run_id: str,
    decision_ts: str,
    candidate: ParlayCandidate,
    supplied: str | None,
) -> str:
    if supplied is not None:
        return _validate_canonical_string(supplied, "material_action_id")
    identity = {
        "schema": _RESEARCH_MATERIAL_ACTION_SCHEMA,
        "replay_run_id": replay_run_id,
        "agent": _RESEARCH_DECISION_AGENT,
        "action": _RESEARCH_MATERIAL_ACTION_NAME,
        "decision_ts": decision_ts,
        "candidate": [
            {
                "quote_key": leg.quote_key,
                "event_id": leg.ticket_identity()[0],
                "market_id": leg.ticket_identity()[1],
                "selection_id": leg.ticket_identity()[2],
                "decimal_odds": str(leg.decimal_odds),
                "probability": str(leg.probability),
            }
            for leg in candidate.legs
        ],
    }
    canonical = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _risk_of_ruin_evidence_payload(
    evidence: RiskOfRuinEvidence | None,
) -> dict[str, str] | None:
    if evidence is None:
        return None
    return {
        "evidence_id": evidence.evidence_id,
        "research_protocol_sha256": evidence.research_protocol_sha256,
        "reproducibility_bundle_sha256": evidence.reproducibility_bundle_sha256,
        "producer_identity": evidence.producer_identity,
        "causal_cutoff": evidence.causal_cutoff,
        "evaluated_at": evidence.evaluated_at,
        "bankroll_id": evidence.bankroll_id,
        "currency": evidence.currency,
        "base_portfolio_sha256": evidence.base_portfolio_sha256,
        "candidate_sha256": evidence.candidate_sha256,
        "evaluated_stake": str(evidence.evaluated_stake),
        "upper_bound": str(evidence.upper_bound),
    }


def _research_material_action_intent_sha256(
    *,
    replay_run_id: str,
    material_action_id: str,
    decision_ts: str,
    candidate: ParlayCandidate,
    groups: list[ScenarioGroup],
    forecasts: dict[str, ForecastRecord],
    evidence: tuple[ResearchEvidence, ...],
    provider_accounts: tuple[tuple[str, str], ...] = (),
    risk_of_ruin_evidence: RiskOfRuinEvidence | None = None,
) -> str:
    """Bind one caller idempotence key to the immutable research decision intent."""

    candidate_keys = {leg.quote_key for leg in candidate.legs}
    payload = {
        "schema": _RESEARCH_MATERIAL_ACTION_INTENT_SCHEMA,
        "replay_run_id": replay_run_id,
        "material_action_id": material_action_id,
        "decision_ts": decision_ts,
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
                        "probability": (
                            str(outcome.probability)
                            if outcome.probability is not None
                            else None
                        ),
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
        "provider_accounts": [
            {"source_id": source_id, "account_id": account_id}
            for source_id, account_id in provider_accounts
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
            for item in sorted(
                (item for item in evidence if item.quote_key in candidate_keys),
                key=lambda item: (item.quote_key, item.available_at, item.evidence_id),
            )
        ],
    }
    if risk_of_ruin_evidence is not None:
        payload["risk_of_ruin_evidence"] = _risk_of_ruin_evidence_payload(
            risk_of_ruin_evidence
        )
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _research_material_action_ticket(
    book: PaperBook,
    material_action_id: str,
) -> PaperTicket | None:
    marker = f"{_RESEARCH_MATERIAL_ACTION_MARKER}{material_action_id}"
    matches = [
        ticket
        for ticket in book.tickets.values()
        if ticket.strategy_reason.endswith(f"; {marker}")
    ]
    if len(matches) > 1:
        raise ResearchDecisionReconciliationRequired(
            "PaperBook contains duplicate tickets for one research material_action_id"
        )
    return matches[0] if matches else None


def _ticket_matches_candidate(
    ticket: PaperTicket,
    candidate: ParlayCandidate,
    *,
    decision_ts: str,
    provider_source_ids: tuple[str, ...],
    provider_accounts: tuple[tuple[str, str], ...],
    bankroll_id: str,
    currency: str,
) -> bool:
    return (
        isinstance(ticket.stake, Decimal)
        and ticket.stake.is_finite()
        and ticket.stake > 0
        and ticket.placed_at == decision_ts
        and tuple(ticket.legs) == tuple(_ticket_legs(candidate))
        and ticket.provider_source_ids == provider_source_ids
        and ticket.provider_accounts == provider_accounts
        and ticket.bankroll_id == bankroll_id
        and ticket.currency == currency
    )


def _rollback_uncommitted_ticket(
    book: PaperBook,
    ticket: PaperTicket,
    *,
    balance_before: Decimal,
    lifecycle_len_before: int,
) -> None:
    if book.tickets.get(ticket.ticket_id) is not ticket:
        raise ResearchDecisionReconciliationRequired(
            "research decision rollback cannot prove ticket identity"
        )
    expected_lifecycle = ("open", ticket.ticket_id, (), ())
    actual_lifecycle = book._lifecycle[-1] if book._lifecycle else None
    if (
        len(book._lifecycle) != lifecycle_len_before + 1
        or not isinstance(actual_lifecycle, tuple)
        or tuple(actual_lifecycle[:4]) != expected_lifecycle
    ):
        raise ResearchDecisionReconciliationRequired(
            "research decision rollback cannot prove lifecycle boundary"
        )
    del book.tickets[ticket.ticket_id]
    book.balance = balance_before
    del book._lifecycle[lifecycle_len_before:]


def _verified_decision_sha256(
    ledger: JsonlDecisionLedger,
    record: DecisionRecord,
    goal,
    risk_policy: PaperRiskPolicy | None = None,
) -> str | None:
    try:
        if goal is None:
            persisted = next(
                (
                    item
                    for item in ledger.verified_records()
                    if item.decision_id == record.decision_id
                ),
                None,
            )
            expected = record
        else:
            persisted = ledger.verified_economic_decision(
                record.decision_id,
                goal,
                risk_policy=risk_policy,
            )
            expected = bind_economic_goal(record, goal, risk_policy)
        if persisted != expected:
            return None
        snapshot = ledger.verified_snapshot()
        for line in snapshot.payload.decode("utf-8").splitlines():
            envelope = json.loads(line)
            persisted_record = envelope.get("record")
            if (
                isinstance(persisted_record, dict)
                and persisted_record.get("decision_id") == record.decision_id
            ):
                digest = envelope.get("sha256")
                if isinstance(digest, str) and len(digest) == 64:
                    return digest
        return None
    except Exception:
        return None


def _reconcile_existing_economic_action(
    *,
    book: PaperBook,
    ledger: JsonlDecisionLedger,
    goal,
    risk_policy: PaperRiskPolicy,
    material_action_id: str,
    intent_sha256: str,
    replay_run_id: str,
    decision_ts: str,
    candidate: ParlayCandidate,
    provider_source_ids: tuple[str, ...],
    provider_accounts: tuple[tuple[str, str], ...],
) -> None:
    if getattr(ledger, "path", None) is not None and not ledger.path.exists():
        persisted = None
    else:
        persisted = ledger.verified_economic_decision_for_material_action(
            material_action_id,
            goal,
            risk_policy=risk_policy,
        )
    ticket = _research_material_action_ticket(book, material_action_id)

    if persisted is None and ticket is None:
        return
    if persisted is None:
        raise ResearchDecisionReconciliationRequired(
            "PaperBook research material action exists without a durable Decision Ledger record"
        )
    if ticket is None:
        raise ResearchDecisionReconciliationRequired(
            "Decision Ledger research material action exists without a durable PaperBook ticket"
        )

    payload = persisted.payload
    if payload.get(_RESEARCH_MATERIAL_ACTION_INTENT_PAYLOAD_KEY) != intent_sha256:
        raise ResearchDecisionReconciliationRequired(
            "durable research material action does not match current decision intent"
        )
    expected_quote_keys = tuple(leg.quote_key for leg in candidate.legs)
    if (
        persisted.replay_run_id != replay_run_id
        or persisted.agent != _RESEARCH_DECISION_AGENT
        or persisted.action != _RESEARCH_MATERIAL_ACTION_NAME
        or persisted.observed_ts != decision_ts
        or payload.get(MATERIAL_ACTION_ID_PAYLOAD_KEY) != material_action_id
        or payload.get("ticket_id") != ticket.ticket_id
        or payload.get("stake") != str(ticket.stake)
        or tuple(payload.get("candidate_quote_keys", ())) != expected_quote_keys
        or not _ticket_matches_candidate(
            ticket,
            candidate,
            decision_ts=decision_ts,
            provider_source_ids=provider_source_ids,
            provider_accounts=provider_accounts,
            bankroll_id=goal.bankroll_id,
            currency=goal.currency,
        )
    ):
        raise ResearchDecisionReconciliationRequired(
            "PaperBook and Decision Ledger research material-action evidence do not match exactly"
        )

    raise ResearchDecisionAlreadyCommitted(material_action_id, ticket.ticket_id)


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
        for flag in flags:
            _validate_canonical_string(
                flag,
                "evidence quality flag",
                canonical_error="evidence quality flags must be non-empty canonical strings",
            )
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
            type(self.minimum_evidence_per_leg) is not int
            or self.minimum_evidence_per_leg < 1
        ):
            raise ValueError("minimum_evidence_per_leg must be a positive integer")
        if not isinstance(self.blocked_quality_flags, (tuple, list, set, frozenset)):
            raise ValueError(
                "blocked_quality_flags must be a tuple, list, set, or frozenset"
            )
        blocked_flags = tuple(self.blocked_quality_flags)
        for flag in blocked_flags:
            _validate_canonical_string(
                flag,
                "blocked_quality_flags item",
                canonical_error="blocked_quality_flags must contain non-empty canonical strings",
            )
        object.__setattr__(
            self,
            "blocked_quality_flags",
            frozenset(blocked_flags),
        )
        for label, value in (
            ("require_market_snapshot_hash", self.require_market_snapshot_hash),
            ("require_worst_case_proof", self.require_worst_case_proof),
        ):
            if not isinstance(value, bool):
                raise ValueError(f"{label} must be boolean")
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
    portfolio_impact: CandidatePortfolioImpact | None
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
        stake: Decimal | str | None,
        decision_ts: str,
        market_quotes: Iterable[MarketEvent] | None = None,
        provider_accounts: tuple[tuple[str, str], ...] = (),
        risk_of_ruin_evidence: RiskOfRuinEvidence | None = None,
        decision_ledger: JsonlDecisionLedger,
        replay_run_id: str,
        material_action_id: str | None = None,
    ) -> ResearchDecision:
        _validate_canonical_string(replay_run_id, "replay_run_id")
        _validate_canonical_string(decision_ts, "decision_ts")
        parse_iso_timestamp(decision_ts)
        evidence_items = tuple(evidence)
        goal = self.risk_policy.economic_goal
        quote_items = tuple(market_quotes or ())
        proposal_context: ProposedTicketRiskContext | None = None
        if goal is not None:
            proposal_context = ProposedTicketRiskContext(
                legs=tuple(_ticket_legs(candidate)),
                quotes=quote_items,
                provider_accounts=provider_accounts,
                bankroll_id=goal.bankroll_id,
                currency=goal.currency,
                proposal_ts=decision_ts,
                risk_of_ruin_evidence=risk_of_ruin_evidence,
            )

        resolved_material_action_id: str | None = None
        resolved_material_action_intent_sha256: str | None = None
        if goal is not None:
            assert proposal_context is not None
            resolved_material_action_id = _research_material_action_id(
                replay_run_id=replay_run_id,
                decision_ts=decision_ts,
                candidate=candidate,
                supplied=material_action_id,
            )
            resolved_material_action_intent_sha256 = (
                _research_material_action_intent_sha256(
                    replay_run_id=replay_run_id,
                    material_action_id=resolved_material_action_id,
                    decision_ts=decision_ts,
                    candidate=candidate,
                    groups=groups,
                    forecasts=forecasts,
                    evidence=evidence_items,
                    provider_accounts=proposal_context.provider_accounts,
                    risk_of_ruin_evidence=risk_of_ruin_evidence,
                )
            )
            _reconcile_existing_economic_action(
                book=book,
                ledger=decision_ledger,
                goal=goal,
                risk_policy=self.risk_policy,
                material_action_id=resolved_material_action_id,
                intent_sha256=resolved_material_action_intent_sha256,
                replay_run_id=replay_run_id,
                decision_ts=decision_ts,
                candidate=candidate,
                provider_source_ids=tuple(sorted(proposal_context.source_ids)),
                provider_accounts=proposal_context.provider_accounts,
            )

        if goal is None:
            amount = _finite_decimal(stake, "stake")
            if amount <= 0:
                raise ValueError("stake must be positive")
            stake_source = "legacy-caller-fixed"
        else:
            # Research-plan stake is compatibility metadata only under an active
            # owner EconomicGoalContract; it has no monetary authority.
            derived = self.risk_policy.derive_goal_stake(
                book,
                candidate.expected_profit_per_unit,
                context=proposal_context,
            )
            amount = derived if derived is not None else Decimal("0")
            stake_source = "economic-goal-derived"

        context_hash = _research_context_hash(
            book=book,
            candidate=candidate,
            groups=groups,
            forecasts=forecasts,
            evidence=evidence_items,
            decision_ts=decision_ts,
            provider_accounts=(
                proposal_context.provider_accounts
                if proposal_context is not None
                else ()
            ),
            risk_of_ruin_evidence=risk_of_ruin_evidence,
        )

        impact = (
            self.optimizer.evaluate_candidates(
                list(book.tickets.values()),
                [candidate],
                groups,
                stake=amount,
            )[0]
            if amount > 0
            else None
        )
        critic_verdict = self.critic.review(
            candidate,
            forecasts,
            evidence_items,
            decision_ts=decision_ts,
        )

        if amount <= 0:
            risk = RiskDecision(
                False,
                "economic goal produced ZERO stake under the current authority envelope",
            )
        else:
            quote_rejection: str | None = None
            candidate_quote_keys = {leg.quote_key for leg in candidate.legs}
            for quote in quote_items:
                if quote.quote_key not in candidate_quote_keys:
                    continue
                rejection = paper_quote_rejection_reason(quote, amount)
                if rejection is not None:
                    quote_rejection = f"{quote.quote_key}: {rejection}"
                    break

            if quote_rejection is not None:
                risk = RiskDecision(False, "paper quote: " + quote_rejection)
            else:
                risk = self.risk_policy.evaluate(
                    book,
                    amount,
                    context=proposal_context,
                )

        reasons: list[str] = list(critic_verdict.reasons)
        policy = self.critic.policy
        if not risk.allowed:
            reasons.append("risk policy: " + risk.reason)
        if (
            policy.require_worst_case_proof
            and (impact is None or not impact.worst_case_change_proven)
        ):
            reasons.append("portfolio worst-case change is not proven exact")
        if (
            impact is not None
            and policy.minimum_ranking_risk_change is not None
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
        approved = not reasons and impact is not None
        ticket: PaperTicket | None = None
        balance_before = book.balance
        lifecycle_len_before = len(book._lifecycle)
        if approved:
            assert impact is not None
            strategy_reason = (
                "paper research decision; "
                f"portfolio_truth={impact.ranking_risk_truth}; "
                f"critic={self.critic.name}"
            )
            if resolved_material_action_id is not None:
                strategy_reason += (
                    f"; {_RESEARCH_MATERIAL_ACTION_MARKER}"
                    f"{resolved_material_action_id}"
                )
            ticket = book.open_ticket(
                _ticket_legs(candidate),
                amount,
                reason=strategy_reason,
                placed_at=decision_ts,
                provider_source_ids=(
                    tuple(sorted(proposal_context.source_ids))
                    if proposal_context is not None
                    else ()
                ),
                provider_accounts=(
                    proposal_context.provider_accounts
                    if proposal_context is not None
                    else ()
                ),
                bankroll_id=goal.bankroll_id if goal is not None else None,
                currency=goal.currency if goal is not None else None,
            )

        record: DecisionRecord | None = None
        try:
            action = (
                _RESEARCH_MATERIAL_ACTION_NAME
                if approved
                else "REJECT_PAPER_RESEARCH_CANDIDATE"
            )
            payload = _audit_payload(
                candidate=candidate,
                amount=amount,
                stake_source=stake_source,
                critic=critic_verdict,
                impact=impact,
                risk=risk,
                approved=approved,
                reasons=tuple(reasons),
                ticket_id=ticket.ticket_id if ticket else None,
                forecasts=forecasts,
                evidence=evidence_items,
                provider_accounts=(
                    proposal_context.provider_accounts
                    if proposal_context is not None
                    else ()
                ),
                risk_of_ruin_evidence=risk_of_ruin_evidence,
            )
            if approved and resolved_material_action_id is not None:
                assert resolved_material_action_intent_sha256 is not None
                payload[MATERIAL_ACTION_ID_PAYLOAD_KEY] = resolved_material_action_id
                payload[_RESEARCH_MATERIAL_ACTION_INTENT_PAYLOAD_KEY] = (
                    resolved_material_action_intent_sha256
                )
            record = DecisionRecord(
                replay_run_id=replay_run_id,
                agent=_RESEARCH_DECISION_AGENT,
                observed_ts=decision_ts,
                action=action,
                payload=payload,
                context_hash=context_hash,
                decision_kind=(
                    ECONOMIC_DECISION_KIND
                    if goal is not None
                    else GENERAL_DECISION_KIND
                ),
            )
            if goal is None:
                audit_sha = decision_ledger.append(record)
            else:
                audit_sha = decision_ledger.append_economic(
                    record,
                    goal,
                    risk_policy=self.risk_policy,
                )
        except Exception:
            durable_sha = (
                _verified_decision_sha256(
                    decision_ledger,
                    record,
                    goal,
                    self.risk_policy if goal is not None else None,
                )
                if record is not None
                else None
            )
            if durable_sha is not None:
                return ResearchDecision(
                    approved=approved,
                    reasons=tuple(reasons),
                    decision_ts=decision_ts,
                    critic=critic_verdict,
                    portfolio_impact=impact,
                    risk=risk,
                    ticket_id=ticket.ticket_id if ticket else None,
                    audit_sha256=durable_sha,
                )
            if ticket is not None:
                _rollback_uncommitted_ticket(
                    book,
                    ticket,
                    balance_before=balance_before,
                    lifecycle_len_before=lifecycle_len_before,
                )
            raise

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
    stake_source: str,
    critic: CriticVerdict,
    impact: CandidatePortfolioImpact | None,
    risk: RiskDecision,
    approved: bool,
    reasons: tuple[str, ...],
    ticket_id: str | None,
    forecasts: dict[str, ForecastRecord],
    evidence: tuple[ResearchEvidence, ...],
    provider_accounts: tuple[tuple[str, str], ...] = (),
    risk_of_ruin_evidence: RiskOfRuinEvidence | None = None,
) -> dict:
    candidate_keys = {leg.quote_key for leg in candidate.legs}
    relevant_evidence = sorted(
        (item for item in evidence if item.quote_key in candidate_keys),
        key=lambda item: (item.quote_key, item.available_at, item.evidence_id),
    )
    payload = {
        "approved": approved,
        "reasons": list(reasons),
        "ticket_id": ticket_id,
        "stake": str(amount),
        "stake_source": stake_source,
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
        "portfolio": (
            None
            if impact is None
            else {
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
            }
        ),
        "risk": {"allowed": risk.allowed, "reason": risk.reason},
        "provider_accounts": [
            {"source_id": source_id, "account_id": account_id}
            for source_id, account_id in provider_accounts
        ],
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
    if risk_of_ruin_evidence is not None:
        payload["risk_of_ruin_evidence"] = _risk_of_ruin_evidence_payload(
            risk_of_ruin_evidence
        )
    return payload


def _research_context_hash(
    *,
    book: PaperBook,
    candidate: ParlayCandidate,
    groups: list[ScenarioGroup],
    forecasts: dict[str, ForecastRecord],
    evidence: tuple[ResearchEvidence, ...],
    decision_ts: str,
    provider_accounts: tuple[tuple[str, str], ...] = (),
    risk_of_ruin_evidence: RiskOfRuinEvidence | None = None,
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
                    "provider_source_ids": list(ticket.provider_source_ids),
                    "provider_accounts": [
                        {"source_id": source_id, "account_id": account_id}
                        for source_id, account_id in ticket.provider_accounts
                    ],
                    "bankroll_id": ticket.bankroll_id,
                    "currency": ticket.currency,
                    "legs": [
                        {"quote_key": leg.quote_key, "locked_odds": str(leg.locked_odds)}
                        for leg in ticket.legs
                    ],
                }
                for ticket in sorted(book.tickets.values(), key=lambda item: item.ticket_id)
            ],
        },
        "provider_accounts": [
            {"source_id": source_id, "account_id": account_id}
            for source_id, account_id in provider_accounts
        ],
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
    if risk_of_ruin_evidence is not None:
        payload["risk_of_ruin_evidence"] = _risk_of_ruin_evidence_payload(
            risk_of_ruin_evidence
        )
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
