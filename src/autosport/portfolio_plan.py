from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum

from .economic_goal_provenance import provenance_for
from .paper import PaperBook
from .risk import PaperRiskPolicy, ProposedTicketRiskContext, RiskOfRuinVectorEvidence


class OpportunityClass(str, Enum):
    PREDICTIVE_EDGE = "predictive_edge"
    LIVE_PRICE_MOVEMENT = "live_price_movement"
    ARBITRAGE = "arbitrage"
    DUTCHING = "dutching"
    HEDGE_REBALANCE = "hedge_rebalance"
    HYBRID = "hybrid"


class EvidenceTruth(str, Enum):
    EXACT = "exact"
    APPROXIMATE = "approximate"


class PortfolioAction(str, Enum):
    STAKE_VECTOR = "stake_vector"
    HEDGE_REBALANCE = "hedge_rebalance"
    PAPER_PLAN = "paper_plan"
    WAIT = "wait"
    ZERO = "zero"


def _canonical_text(name: str, value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty canonical string")
    if "\x00" in value:
        raise ValueError(f"{name} must not contain NUL")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must be valid UTF-8 text") from exc
    return value


def _canonical_timestamp(name: str, value: object) -> tuple[str, datetime]:
    raw = _canonical_text(name, value)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware ISO-8601")
    return raw, parsed


def _canonical_sha256(name: str, value: object) -> str:
    digest = _canonical_text(name, value)
    if (
        len(digest) != 64
        or digest != digest.lower()
        or any(ch not in "0123456789abcdef" for ch in digest)
    ):
        raise ValueError(f"{name} must be a lowercase 64-character SHA-256 hex digest")
    return digest


def _optional_sha256(name: str, value: object) -> str | None:
    if value is None:
        return None
    return _canonical_sha256(name, value)


def _sha256_payload(payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True, slots=True)
class OpportunityEvidence:
    evidence_id: str
    observed_at: str
    causal_cutoff: str
    reproducibility_sha256: str
    truth: EvidenceTruth = EvidenceTruth.EXACT
    forecast_sha256: str | None = None
    outcome_space_complete: bool = False
    terminal_state_space_sha256: str | None = None
    execution_assumptions_sha256: str | None = None
    execution_feasible: bool = True

    def __post_init__(self) -> None:
        _canonical_text("evidence_id", self.evidence_id)
        _, observed = _canonical_timestamp("observed_at", self.observed_at)
        _, cutoff = _canonical_timestamp("causal_cutoff", self.causal_cutoff)
        if cutoff > observed:
            raise ValueError("causal_cutoff must not be after observed_at")
        _canonical_sha256("reproducibility_sha256", self.reproducibility_sha256)
        _optional_sha256("forecast_sha256", self.forecast_sha256)
        _optional_sha256(
            "terminal_state_space_sha256", self.terminal_state_space_sha256
        )
        _optional_sha256(
            "execution_assumptions_sha256", self.execution_assumptions_sha256
        )
        if not isinstance(self.truth, EvidenceTruth):
            raise ValueError("truth must be an EvidenceTruth")
        if type(self.outcome_space_complete) is not bool:
            raise ValueError("outcome_space_complete must be a bool")
        if type(self.execution_feasible) is not bool:
            raise ValueError("execution_feasible must be a bool")
        if self.outcome_space_complete and self.terminal_state_space_sha256 is None:
            raise ValueError(
                "complete outcome-space evidence requires terminal_state_space_sha256"
            )

    @property
    def evidence_sha256(self) -> str:
        return _sha256_payload(
            {
                "schema": "autosport.opportunity_evidence",
                "schema_version": 1,
                "evidence_id": self.evidence_id,
                "observed_at": self.observed_at,
                "causal_cutoff": self.causal_cutoff,
                "reproducibility_sha256": self.reproducibility_sha256,
                "truth": self.truth.value,
                "forecast_sha256": self.forecast_sha256,
                "outcome_space_complete": self.outcome_space_complete,
                "terminal_state_space_sha256": self.terminal_state_space_sha256,
                "execution_assumptions_sha256": self.execution_assumptions_sha256,
                "execution_feasible": self.execution_feasible,
            }
        )


@dataclass(frozen=True, slots=True)
class OpportunityIntent:
    intent_id: str
    opportunity_class: OpportunityClass
    evidence: OpportunityEvidence
    risk_context: ProposedTicketRiskContext
    signal_strength: Decimal
    strategy_id: str
    config_sha256: str
    model_id: str | None = None

    def __post_init__(self) -> None:
        _canonical_text("intent_id", self.intent_id)
        if not isinstance(self.opportunity_class, OpportunityClass):
            raise ValueError("opportunity_class must be an OpportunityClass")
        if not isinstance(self.evidence, OpportunityEvidence):
            raise TypeError("evidence must be OpportunityEvidence")
        if not isinstance(self.risk_context, ProposedTicketRiskContext):
            raise TypeError("risk_context must be ProposedTicketRiskContext")
        if (
            not isinstance(self.signal_strength, Decimal)
            or not self.signal_strength.is_finite()
        ):
            raise ValueError("signal_strength must be a finite exact Decimal")
        _canonical_text("strategy_id", self.strategy_id)
        _canonical_sha256("config_sha256", self.config_sha256)
        if self.model_id is not None:
            _canonical_text("model_id", self.model_id)
        if PaperRiskPolicy.risk_of_ruin_candidate_sha256(self.risk_context) is None:
            raise ValueError("risk_context cannot produce canonical candidate identity")
        if self.opportunity_class in {
            OpportunityClass.PREDICTIVE_EDGE,
            OpportunityClass.HYBRID,
        }:
            if self.evidence.forecast_sha256 is None:
                raise ValueError(
                    f"{self.opportunity_class.value} requires forecast_sha256 evidence"
                )
            if self.model_id is None:
                raise ValueError(
                    f"{self.opportunity_class.value} requires model_id identity"
                )

    @property
    def candidate_sha256(self) -> str:
        digest = PaperRiskPolicy.risk_of_ruin_candidate_sha256(self.risk_context)
        if digest is None:
            raise RuntimeError("validated opportunity lost canonical candidate identity")
        return digest

    @property
    def intent_sha256(self) -> str:
        return _sha256_payload(
            {
                "schema": "autosport.opportunity_intent",
                "schema_version": 1,
                "intent_id": self.intent_id,
                "opportunity_class": self.opportunity_class.value,
                "evidence_sha256": self.evidence.evidence_sha256,
                "candidate_sha256": self.candidate_sha256,
                "signal_strength": str(self.signal_strength),
                "strategy_id": self.strategy_id,
                "model_id": self.model_id,
                "config_sha256": self.config_sha256,
            }
        )


@dataclass(frozen=True, slots=True)
class PortfolioPlan:
    decision_ts: str
    action: PortfolioAction
    stakes: tuple[Decimal, ...]
    intent_ids: tuple[str, ...]
    intent_sha256s: tuple[str, ...]
    opportunity_classes: tuple[str, ...]
    portfolio_sha256: str | None
    dependency_graph_sha256: str | None
    economic_goal_contract_sha256: str | None
    risk_policy_sha256: str
    portfolio_truth: EvidenceTruth
    reason: str

    def __post_init__(self) -> None:
        _canonical_timestamp("decision_ts", self.decision_ts)
        if not isinstance(self.action, PortfolioAction):
            raise ValueError("action must be a PortfolioAction")
        if type(self.stakes) is not tuple:
            raise ValueError("stakes must be a tuple")
        for stake in self.stakes:
            if (
                not isinstance(stake, Decimal)
                or not stake.is_finite()
                or stake < Decimal("0")
            ):
                raise ValueError(
                    "stakes must contain non-negative finite exact Decimals"
                )
        if type(self.intent_ids) is not tuple or type(self.intent_sha256s) is not tuple:
            raise ValueError("intent identity vectors must be tuples")
        if len(self.stakes) != len(self.intent_ids) or len(self.intent_ids) != len(
            self.intent_sha256s
        ):
            raise ValueError("plan vectors must have matching cardinality")
        if self.action is not PortfolioAction.WAIT and len(self.intent_ids) != len(
            set(self.intent_ids)
        ):
            raise ValueError("non-WAIT plan intent_ids must be unique")
        for intent_id in self.intent_ids:
            _canonical_text("intent_id", intent_id)
        for digest in self.intent_sha256s:
            _canonical_sha256("intent_sha256", digest)
        if type(self.opportunity_classes) is not tuple or len(
            self.opportunity_classes
        ) != len(self.intent_ids):
            raise ValueError("opportunity_classes must match intent cardinality")
        for opportunity_class in self.opportunity_classes:
            OpportunityClass(opportunity_class)
        _optional_sha256("portfolio_sha256", self.portfolio_sha256)
        _optional_sha256("dependency_graph_sha256", self.dependency_graph_sha256)
        _optional_sha256(
            "economic_goal_contract_sha256", self.economic_goal_contract_sha256
        )
        _canonical_sha256("risk_policy_sha256", self.risk_policy_sha256)
        if not isinstance(self.portfolio_truth, EvidenceTruth):
            raise ValueError("portfolio_truth must be an EvidenceTruth")
        _canonical_text("reason", self.reason)
        positive = any(stake > 0 for stake in self.stakes)
        if self.action is PortfolioAction.STAKE_VECTOR and not positive:
            raise ValueError("STAKE_VECTOR requires at least one positive stake")
        if self.action in {PortfolioAction.WAIT, PortfolioAction.ZERO} and positive:
            raise ValueError("WAIT/ZERO plans must not carry positive stakes")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "autosport.portfolio_plan",
            "schema_version": 1,
            "decision_ts": self.decision_ts,
            "action": self.action.value,
            "stakes": [str(value) for value in self.stakes],
            "intent_ids": list(self.intent_ids),
            "intent_sha256s": list(self.intent_sha256s),
            "opportunity_classes": list(self.opportunity_classes),
            "portfolio_sha256": self.portfolio_sha256,
            "dependency_graph_sha256": self.dependency_graph_sha256,
            "economic_goal_contract_sha256": self.economic_goal_contract_sha256,
            "risk_policy_sha256": self.risk_policy_sha256,
            "portfolio_truth": self.portfolio_truth.value,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, raw: object) -> "PortfolioPlan":
        if type(raw) is not dict:
            raise ValueError("serialized portfolio plan must be a JSON object")
        if raw.get("schema") != "autosport.portfolio_plan":
            raise ValueError("unsupported portfolio plan schema")
        if raw.get("schema_version") != 1:
            raise ValueError("unsupported portfolio plan schema_version")
        try:
            stakes_raw = raw["stakes"]
            intent_ids_raw = raw["intent_ids"]
            intent_sha256s_raw = raw["intent_sha256s"]
            classes_raw = raw["opportunity_classes"]
            if any(
                type(value) is not list
                for value in (
                    stakes_raw,
                    intent_ids_raw,
                    intent_sha256s_raw,
                    classes_raw,
                )
            ):
                raise ValueError("serialized portfolio plan vectors must be lists")
            return cls(
                decision_ts=raw["decision_ts"],
                action=PortfolioAction(raw["action"]),
                stakes=tuple(Decimal(value) for value in stakes_raw),
                intent_ids=tuple(intent_ids_raw),
                intent_sha256s=tuple(intent_sha256s_raw),
                opportunity_classes=tuple(classes_raw),
                portfolio_sha256=raw.get("portfolio_sha256"),
                dependency_graph_sha256=raw.get("dependency_graph_sha256"),
                economic_goal_contract_sha256=raw.get(
                    "economic_goal_contract_sha256"
                ),
                risk_policy_sha256=raw["risk_policy_sha256"],
                portfolio_truth=EvidenceTruth(raw["portfolio_truth"]),
                reason=raw["reason"],
            )
        except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
            raise ValueError("serialized portfolio plan is invalid") from exc

    @property
    def plan_sha256(self) -> str:
        return _sha256_payload(self.to_dict())


def _terminal_plan(
    *,
    decision_ts: str,
    action: PortfolioAction,
    reason: str,
    intents: tuple[OpportunityIntent, ...],
    portfolio_sha256: str | None,
    dependency_graph_sha256: str | None,
    policy: PaperRiskPolicy,
    portfolio_truth: EvidenceTruth,
) -> PortfolioPlan:
    goal = policy.economic_goal
    return PortfolioPlan(
        decision_ts=decision_ts,
        action=action,
        stakes=tuple(Decimal("0") for _ in intents),
        intent_ids=tuple(intent.intent_id for intent in intents),
        intent_sha256s=tuple(intent.intent_sha256 for intent in intents),
        opportunity_classes=tuple(
            intent.opportunity_class.value for intent in intents
        ),
        portfolio_sha256=portfolio_sha256,
        dependency_graph_sha256=dependency_graph_sha256,
        economic_goal_contract_sha256=(
            provenance_for(goal).contract_sha256 if goal is not None else None
        ),
        risk_policy_sha256=policy.provenance_sha256,
        portfolio_truth=portfolio_truth,
        reason=reason,
    )


def _intent_preflight_reason(
    intent: OpportunityIntent,
    decision_time: datetime,
) -> str | None:
    evidence = intent.evidence
    _, observed = _canonical_timestamp("observed_at", evidence.observed_at)
    _, cutoff = _canonical_timestamp("causal_cutoff", evidence.causal_cutoff)
    if cutoff > decision_time or observed > decision_time:
        return "opportunity evidence is from the future relative to decision time"

    proposal_ts = intent.risk_context.proposal_ts
    if proposal_ts is not None:
        _, proposal_time = _canonical_timestamp("proposal_ts", proposal_ts)
        if proposal_time > decision_time:
            return "proposal timestamp is from the future relative to decision time"

    if intent.signal_strength <= 0:
        return None
    if not evidence.execution_feasible:
        return "opportunity execution is not proven feasible"
    if intent.opportunity_class in {
        OpportunityClass.ARBITRAGE,
        OpportunityClass.DUTCHING,
    }:
        if evidence.truth is not EvidenceTruth.EXACT:
            return "outcome-independent opportunity evidence is not exact"
        if (
            not evidence.outcome_space_complete
            or evidence.terminal_state_space_sha256 is None
        ):
            return (
                "outcome-independent opportunity lacks complete terminal-state evidence"
            )
        if evidence.execution_assumptions_sha256 is None:
            return (
                "outcome-independent opportunity lacks execution-assumption evidence"
            )
    return None


def build_portfolio_plan(
    book: PaperBook,
    intents: tuple[OpportunityIntent, ...],
    risk_policy: PaperRiskPolicy,
    decision_ts: str,
    *,
    portfolio_truth: EvidenceTruth = EvidenceTruth.EXACT,
    dependency_graph_sha256: str | None,
    risk_of_ruin_vector_evidence: RiskOfRuinVectorEvidence | None = None,
) -> PortfolioPlan:
    """Build one pure whole-portfolio paper plan under canonical RiskPolicy authority."""

    if not isinstance(book, PaperBook):
        raise TypeError("book must be PaperBook")
    if type(intents) is not tuple:
        raise TypeError("intents must be a tuple")
    if any(not isinstance(intent, OpportunityIntent) for intent in intents):
        raise TypeError("intents must contain OpportunityIntent values")
    if not isinstance(risk_policy, PaperRiskPolicy):
        raise TypeError("risk_policy must be PaperRiskPolicy")
    if not isinstance(portfolio_truth, EvidenceTruth):
        raise TypeError("portfolio_truth must be EvidenceTruth")
    decision_ts, decision_time = _canonical_timestamp("decision_ts", decision_ts)
    if dependency_graph_sha256 is not None:
        _canonical_sha256("dependency_graph_sha256", dependency_graph_sha256)

    portfolio_sha256 = risk_policy.risk_of_ruin_portfolio_sha256(book)
    if portfolio_sha256 is None:
        return _terminal_plan(
            decision_ts=decision_ts,
            action=PortfolioAction.WAIT,
            reason="canonical current portfolio identity cannot be proven",
            intents=intents,
            portfolio_sha256=None,
            dependency_graph_sha256=dependency_graph_sha256,
            policy=risk_policy,
            portfolio_truth=portfolio_truth,
        )
    if not intents:
        return _terminal_plan(
            decision_ts=decision_ts,
            action=PortfolioAction.ZERO,
            reason="opportunity set is empty",
            intents=intents,
            portfolio_sha256=portfolio_sha256,
            dependency_graph_sha256=dependency_graph_sha256,
            policy=risk_policy,
            portfolio_truth=portfolio_truth,
        )

    intent_sha256s = tuple(intent.intent_sha256 for intent in intents)
    if len(intent_sha256s) != len(set(intent_sha256s)):
        return _terminal_plan(
            decision_ts=decision_ts,
            action=PortfolioAction.WAIT,
            reason="opportunity set contains duplicate immutable intent identity",
            intents=intents,
            portfolio_sha256=portfolio_sha256,
            dependency_graph_sha256=dependency_graph_sha256,
            policy=risk_policy,
            portfolio_truth=portfolio_truth,
        )

    positive = tuple(intent for intent in intents if intent.signal_strength > 0)
    if positive and portfolio_truth is not EvidenceTruth.EXACT:
        return _terminal_plan(
            decision_ts=decision_ts,
            action=PortfolioAction.WAIT,
            reason="positive portfolio action requires exact portfolio completeness truth",
            intents=intents,
            portfolio_sha256=portfolio_sha256,
            dependency_graph_sha256=dependency_graph_sha256,
            policy=risk_policy,
            portfolio_truth=portfolio_truth,
        )
    if positive and dependency_graph_sha256 is None:
        return _terminal_plan(
            decision_ts=decision_ts,
            action=PortfolioAction.WAIT,
            reason="positive portfolio action requires dependency-graph evidence",
            intents=intents,
            portfolio_sha256=portfolio_sha256,
            dependency_graph_sha256=None,
            policy=risk_policy,
            portfolio_truth=portfolio_truth,
        )

    for intent in intents:
        reason = _intent_preflight_reason(intent, decision_time)
        if reason is not None:
            return _terminal_plan(
                decision_ts=decision_ts,
                action=PortfolioAction.WAIT,
                reason=reason,
                intents=intents,
                portfolio_sha256=portfolio_sha256,
                dependency_graph_sha256=dependency_graph_sha256,
                policy=risk_policy,
                portfolio_truth=portfolio_truth,
            )

    allocation = risk_policy.derive_goal_stake_vector(
        book,
        tuple(intent.signal_strength for intent in intents),
        contexts=tuple(intent.risk_context for intent in intents),
        risk_of_ruin_vector_evidence=risk_of_ruin_vector_evidence,
    )
    action = {
        "STAKE_VECTOR": PortfolioAction.STAKE_VECTOR,
        "WAIT": PortfolioAction.WAIT,
        "ZERO": PortfolioAction.ZERO,
    }[allocation.action]
    goal = risk_policy.economic_goal
    return PortfolioPlan(
        decision_ts=decision_ts,
        action=action,
        stakes=allocation.stakes,
        intent_ids=tuple(intent.intent_id for intent in intents),
        intent_sha256s=intent_sha256s,
        opportunity_classes=tuple(
            intent.opportunity_class.value for intent in intents
        ),
        portfolio_sha256=portfolio_sha256,
        dependency_graph_sha256=dependency_graph_sha256,
        economic_goal_contract_sha256=(
            provenance_for(goal).contract_sha256 if goal is not None else None
        ),
        risk_policy_sha256=risk_policy.provenance_sha256,
        portfolio_truth=portfolio_truth,
        reason=allocation.reason,
    )
