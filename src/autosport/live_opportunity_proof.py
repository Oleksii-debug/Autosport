from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum

from .opportunity import OpportunityDecision, StrategyClass
from .portfolio_plan import EvidenceTruth, OpportunityIntent
from .scenario_search import ScenarioSearchReport


class OpportunityProofError(ValueError):
    """Raised when strategy-proof inputs are non-canonical or authority-ambiguous."""


class OpportunityProofClassification(str, Enum):
    OUTCOME_INDEPENDENT_POSITIVE = "outcome_independent_positive"
    THEORETICAL_ARBITRAGE_ONLY = "theoretical_arbitrage_only"
    EXECUTION_RISK_PRESENT = "execution_risk_present"
    PARTIAL_COVERAGE = "partial_coverage"
    HEDGED_BUT_NOT_GUARANTEED = "hedged_but_not_guaranteed"
    RISKED_PORTFOLIO = "risked_portfolio"


def _canonical_text(name: str, value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise OpportunityProofError(f"{name} must be a non-empty canonical string")
    if "\x00" in value:
        raise OpportunityProofError(f"{name} must not contain NUL")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise OpportunityProofError(f"{name} must be valid UTF-8 text") from exc
    return value


def _canonical_sha256(name: str, value: object) -> str:
    digest = _canonical_text(name, value)
    if (
        len(digest) != 64
        or digest != digest.lower()
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise OpportunityProofError(
            f"{name} must be a lowercase 64-character SHA-256 digest"
        )
    return digest


def _finite_nonnegative_decimal(name: str, value: object) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise OpportunityProofError(f"{name} must be an exact finite Decimal")
    if value < 0 or (value.is_zero() and value.is_signed()):
        raise OpportunityProofError(f"{name} must be non-negative")
    return value


def _decimal_string(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise OpportunityProofError("proof Decimal must be finite")
    sign, digits, exponent = value.as_tuple()
    if not digits or all(digit == 0 for digit in digits):
        return "0"
    canonical_digits = list(digits)
    canonical_exponent = exponent
    while canonical_digits[-1] == 0:
        canonical_digits.pop()
        canonical_exponent += 1
    return str(Decimal((sign, tuple(canonical_digits), canonical_exponent)))


def _parse_timestamp(name: str, value: object) -> tuple[str, datetime]:
    raw = _canonical_text(name, value)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OpportunityProofError(f"{name} must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OpportunityProofError(f"{name} must be timezone-aware")
    normalized = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return normalized, parsed


def _sha256_payload(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ApplicableCostCoverage:
    """Source-owned cost witness consumed only as a proof downgrade fence.

    This type does not resolve or mint economic costs.  Upstream product authority must
    derive the amount and evidence digest.  Incomplete coverage can only prevent a
    guaranteed classification; it can never upgrade one.
    """

    intent_sha256: str
    total_applicable_cost: Decimal
    coverage_complete: bool
    evidence_sha256: str

    def __post_init__(self) -> None:
        _canonical_sha256("cost intent_sha256", self.intent_sha256)
        _finite_nonnegative_decimal(
            "total_applicable_cost", self.total_applicable_cost
        )
        if type(self.coverage_complete) is not bool:
            raise OpportunityProofError("coverage_complete must be a bool")
        _canonical_sha256("cost evidence_sha256", self.evidence_sha256)

    @property
    def coverage_sha256(self) -> str:
        return _sha256_payload(
            {
                "schema": "autosport.applicable_cost_coverage",
                "schema_version": 1,
                "intent_sha256": self.intent_sha256,
                "total_applicable_cost": _decimal_string(self.total_applicable_cost),
                "coverage_complete": self.coverage_complete,
                "evidence_sha256": self.evidence_sha256,
            }
        )


@dataclass(frozen=True, slots=True)
class LiveFreshnessFence:
    """Externally resolved quote-freshness window for the exact opportunity intent."""

    intent_sha256: str
    valid_until: str
    policy_sha256: str

    def __post_init__(self) -> None:
        _canonical_sha256("freshness intent_sha256", self.intent_sha256)
        _parse_timestamp("freshness valid_until", self.valid_until)
        _canonical_sha256("freshness policy_sha256", self.policy_sha256)

    @property
    def freshness_sha256(self) -> str:
        normalized, _ = _parse_timestamp("freshness valid_until", self.valid_until)
        return _sha256_payload(
            {
                "schema": "autosport.live_freshness_fence",
                "schema_version": 1,
                "intent_sha256": self.intent_sha256,
                "valid_until": normalized,
                "policy_sha256": self.policy_sha256,
            }
        )


@dataclass(frozen=True, slots=True)
class ExecutableOpportunityProof:
    intent_sha256: str
    classification: OpportunityProofClassification
    decision_at: str
    gross_min_pnl: Decimal
    after_cost_min_pnl: Decimal
    cost_coverage_sha256: str
    freshness_sha256: str
    outcome_authority_sha256s: tuple[str, ...]
    reason: str

    def __post_init__(self) -> None:
        _canonical_sha256("proof intent_sha256", self.intent_sha256)
        if not isinstance(self.classification, OpportunityProofClassification):
            raise OpportunityProofError(
                "classification must be OpportunityProofClassification"
            )
        _parse_timestamp("proof decision_at", self.decision_at)
        if not isinstance(self.gross_min_pnl, Decimal) or not self.gross_min_pnl.is_finite():
            raise OpportunityProofError("gross_min_pnl must be a finite Decimal")
        if (
            not isinstance(self.after_cost_min_pnl, Decimal)
            or not self.after_cost_min_pnl.is_finite()
        ):
            raise OpportunityProofError("after_cost_min_pnl must be a finite Decimal")
        _canonical_sha256("cost_coverage_sha256", self.cost_coverage_sha256)
        _canonical_sha256("freshness_sha256", self.freshness_sha256)
        if type(self.outcome_authority_sha256s) is not tuple:
            raise OpportunityProofError("outcome_authority_sha256s must be a tuple")
        for digest in self.outcome_authority_sha256s:
            _canonical_sha256("outcome authority sha256", digest)
        if tuple(sorted(self.outcome_authority_sha256s)) != self.outcome_authority_sha256s:
            raise OpportunityProofError("outcome authority hashes must be sorted")
        if len(set(self.outcome_authority_sha256s)) != len(
            self.outcome_authority_sha256s
        ):
            raise OpportunityProofError("outcome authority hashes must be unique")
        _canonical_text("proof reason", self.reason)

    @property
    def proof_sha256(self) -> str:
        decision_at, _ = _parse_timestamp("proof decision_at", self.decision_at)
        return _sha256_payload(
            {
                "schema": "autosport.executable_opportunity_proof",
                "schema_version": 1,
                "intent_sha256": self.intent_sha256,
                "classification": self.classification.value,
                "decision_at": decision_at,
                "gross_min_pnl": _decimal_string(self.gross_min_pnl),
                "after_cost_min_pnl": _decimal_string(self.after_cost_min_pnl),
                "cost_coverage_sha256": self.cost_coverage_sha256,
                "freshness_sha256": self.freshness_sha256,
                "outcome_authority_sha256s": list(self.outcome_authority_sha256s),
                "reason": self.reason,
            }
        )


def classify_executable_opportunity(
    intent: OpportunityIntent,
    scenario: ScenarioSearchReport,
    *,
    cost_coverage: ApplicableCostCoverage,
    freshness: LiveFreshnessFence,
    decision_at: str,
) -> ExecutableOpportunityProof:
    """Classify one existing opportunity without creating execution authority.

    The positive outcome-independent label is deliberately narrower than a positive
    mathematical screen.  It is available only to exact ARBITRAGE/DUTCHING evidence
    after complete costs, exact terminal-space proof, freshness and explicit execution
    feasibility are all present.  Every missing predicate downgrades the result.
    """

    if type(intent) is not OpportunityIntent:
        raise OpportunityProofError("intent must be exact canonical OpportunityIntent")
    if type(scenario) is not ScenarioSearchReport:
        raise OpportunityProofError("scenario must be exact canonical ScenarioSearchReport")
    if type(cost_coverage) is not ApplicableCostCoverage:
        raise OpportunityProofError(
            "cost_coverage must be exact ApplicableCostCoverage"
        )
    if type(freshness) is not LiveFreshnessFence:
        raise OpportunityProofError("freshness must be exact LiveFreshnessFence")

    if cost_coverage.intent_sha256 != intent.intent_sha256:
        raise OpportunityProofError("cost evidence is bound to a different intent")
    if freshness.intent_sha256 != intent.intent_sha256:
        raise OpportunityProofError("freshness evidence is bound to a different intent")

    decision_normalized, decision_time = _parse_timestamp("decision_at", decision_at)
    _, observed_time = _parse_timestamp(
        "opportunity evidence observed_at", intent.evidence.observed_at
    )
    _, cutoff_time = _parse_timestamp(
        "opportunity evidence causal_cutoff", intent.evidence.causal_cutoff
    )
    _, fresh_until = _parse_timestamp("freshness valid_until", freshness.valid_until)
    if decision_time < cutoff_time or decision_time < observed_time:
        raise OpportunityProofError(
            "decision_at cannot precede causal cutoff or observed opportunity evidence"
        )
    if fresh_until < observed_time:
        raise OpportunityProofError(
            "freshness validity cannot expire before opportunity observation"
        )

    if not isinstance(scenario.observed_worst, Decimal) or not scenario.observed_worst.is_finite():
        raise OpportunityProofError("scenario observed_worst must be a finite Decimal")
    if type(scenario.outcome_authority_sha256s) is not tuple:
        raise OpportunityProofError("scenario authority hashes must be a tuple")
    authority_hashes = tuple(sorted(scenario.outcome_authority_sha256s))
    if len(set(authority_hashes)) != len(authority_hashes):
        raise OpportunityProofError("scenario authority hashes must be unique")
    for digest in authority_hashes:
        _canonical_sha256("scenario outcome authority sha256", digest)

    gross_min = scenario.observed_worst
    after_cost = gross_min - cost_coverage.total_applicable_cost
    strategy = intent.opportunity.strategy_class
    actionable = intent.opportunity.decision is OpportunityDecision.ACTIONABLE

    complete = bool(
        intent.evidence.outcome_space_complete
        and scenario.outcome_space_exhaustive
    )
    exact_terminal = bool(
        complete
        and scenario.outcome_space_exact
        and scenario.worst_proven
        and authority_hashes
        and intent.evidence.truth is EvidenceTruth.EXACT
    )
    execution_bound = intent.evidence.execution_assumptions_sha256 is not None
    execution_feasible = bool(
        execution_bound and intent.evidence.execution_feasible
    )
    fresh = decision_time <= fresh_until

    if not actionable:
        classification = OpportunityProofClassification.RISKED_PORTFOLIO
        reason = "opportunity decision is WAIT/ZERO rather than actionable"
    elif not complete:
        classification = OpportunityProofClassification.PARTIAL_COVERAGE
        reason = "complete terminal outcome coverage is not proven"
    elif strategy is StrategyClass.HEDGE_REBALANCE:
        classification = OpportunityProofClassification.HEDGED_BUT_NOT_GUARANTEED
        reason = "hedge/rebalance remains a weaker non-guarantee classification"
    elif strategy not in {StrategyClass.ARBITRAGE, StrategyClass.DUTCHING}:
        classification = OpportunityProofClassification.RISKED_PORTFOLIO
        reason = "strategy class is not an exact price-structure guarantee candidate"
    elif not exact_terminal:
        classification = OpportunityProofClassification.THEORETICAL_ARBITRAGE_ONLY
        reason = "terminal-space or evidence truth is not exact enough for a guarantee"
    elif not cost_coverage.coverage_complete:
        classification = OpportunityProofClassification.THEORETICAL_ARBITRAGE_ONLY
        reason = "applicable economic cost coverage is incomplete"
    elif after_cost <= 0:
        if gross_min > 0:
            classification = OpportunityProofClassification.THEORETICAL_ARBITRAGE_ONLY
            reason = "gross minimum P&L is positive but complete costs remove the positive floor"
        else:
            classification = OpportunityProofClassification.RISKED_PORTFOLIO
            reason = "exact minimum terminal P&L is not positive"
    elif not execution_bound or not execution_feasible or not fresh:
        classification = OpportunityProofClassification.EXECUTION_RISK_PRESENT
        if not execution_bound:
            reason = "exact positive economics lacks bound execution assumptions"
        elif not execution_feasible:
            reason = "exact positive economics is not execution-feasible"
        else:
            reason = "exact positive economics is stale at the decision timestamp"
    else:
        classification = OpportunityProofClassification.OUTCOME_INDEPENDENT_POSITIVE
        reason = "complete exact after-cost terminal P&L is positive and execution evidence is current"

    return ExecutableOpportunityProof(
        intent_sha256=intent.intent_sha256,
        classification=classification,
        decision_at=decision_normalized,
        gross_min_pnl=gross_min,
        after_cost_min_pnl=after_cost,
        cost_coverage_sha256=cost_coverage.coverage_sha256,
        freshness_sha256=freshness.freshness_sha256,
        outcome_authority_sha256s=authority_hashes,
        reason=reason,
    )
