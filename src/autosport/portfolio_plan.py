from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum

from .decision_ledger import (
    ECONOMIC_DECISION_KIND,
    MATERIAL_ACTION_ID_PAYLOAD_KEY,
    DecisionLedgerIntegrityError,
    DecisionRecord,
    EconomicDecisionAuthority,
    JsonlDecisionLedger,
)
from .domain import PaperTicket, TicketStatus
from .market_outcomes import MarketSettlementOutcomeAuthority
from .economic_goal_provenance import provenance_for
from .opportunity import Opportunity, OpportunityDecision, QuoteRef, StrategyClass
from .paper import PaperBook
from .risk import PaperRiskPolicy, ProposedTicketRiskContext, RiskOfRuinVectorEvidence
from .scenario_search import ScenarioGroup, ScenarioOutcome, ScenarioSearchEngine


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


def _decimal_from_serialized(name: str, value: object) -> Decimal:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{name} must be a canonical finite Decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be a canonical finite Decimal string") from exc
    if not parsed.is_finite() or str(parsed) != value:
        raise ValueError(f"{name} must be a canonical finite Decimal string")
    return parsed


def _canonical_json_payload(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_payload(payload: object) -> str:
    return hashlib.sha256(_canonical_json_payload(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class OpportunityEvidence:
    evidence_id: str
    observed_at: str
    causal_cutoff: str
    reproducibility_sha256: str
    truth: EvidenceTruth = EvidenceTruth.EXACT
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
                "outcome_space_complete": self.outcome_space_complete,
                "terminal_state_space_sha256": self.terminal_state_space_sha256,
                "execution_assumptions_sha256": self.execution_assumptions_sha256,
                "execution_feasible": self.execution_feasible,
            }
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "autosport.opportunity_evidence",
            "schema_version": 1,
            "evidence_id": self.evidence_id,
            "observed_at": self.observed_at,
            "causal_cutoff": self.causal_cutoff,
            "reproducibility_sha256": self.reproducibility_sha256,
            "truth": self.truth.value,
            "outcome_space_complete": self.outcome_space_complete,
            "terminal_state_space_sha256": self.terminal_state_space_sha256,
            "execution_assumptions_sha256": self.execution_assumptions_sha256,
            "execution_feasible": self.execution_feasible,
        }

    @classmethod
    def from_dict(cls, raw: object) -> "OpportunityEvidence":
        expected = {
            "schema",
            "schema_version",
            "evidence_id",
            "observed_at",
            "causal_cutoff",
            "reproducibility_sha256",
            "truth",
            "outcome_space_complete",
            "terminal_state_space_sha256",
            "execution_assumptions_sha256",
            "execution_feasible",
        }
        if type(raw) is not dict or set(raw) != expected:
            raise ValueError("serialized opportunity evidence must contain canonical fields")
        if raw["schema"] != "autosport.opportunity_evidence" or raw["schema_version"] != 1:
            raise ValueError("unsupported opportunity evidence schema")
        try:
            return cls(
                evidence_id=raw["evidence_id"],
                observed_at=raw["observed_at"],
                causal_cutoff=raw["causal_cutoff"],
                reproducibility_sha256=raw["reproducibility_sha256"],
                truth=EvidenceTruth(raw["truth"]),
                outcome_space_complete=raw["outcome_space_complete"],
                terminal_state_space_sha256=raw["terminal_state_space_sha256"],
                execution_assumptions_sha256=raw["execution_assumptions_sha256"],
                execution_feasible=raw["execution_feasible"],
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("serialized opportunity evidence is invalid") from exc


@dataclass(frozen=True, slots=True)
class OpportunityIntent:
    """Executable intent that consumes, but never redefines, canonical Opportunity truth."""

    intent_id: str
    opportunity: Opportunity
    evidence: OpportunityEvidence
    risk_context: ProposedTicketRiskContext
    signal_strength: Decimal
    strategy_id: str
    config_sha256: str
    model_id: str | None = None

    def __post_init__(self) -> None:
        _canonical_text("intent_id", self.intent_id)
        if not isinstance(self.opportunity, Opportunity):
            raise TypeError("opportunity must be canonical Opportunity")
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

        opportunity_quotes = {quote.quote_key: quote for quote in self.opportunity.quotes}
        context_quotes = {quote.quote_key: quote for quote in self.risk_context.quotes}
        if set(opportunity_quotes) != set(context_quotes):
            raise ValueError(
                "risk_context quote identity must exactly match canonical opportunity quotes"
            )
        for quote_key, event in context_quotes.items():
            reference = opportunity_quotes[quote_key]
            rebound = QuoteRef.from_market_event(
                event,
                market_snapshot_hash=reference.market_snapshot_hash,
            )
            if rebound != reference:
                raise ValueError(
                    "risk_context quote snapshot must exactly match canonical opportunity evidence"
                )

        strategy_class = self.opportunity.strategy_class
        if strategy_class in {
            StrategyClass.PREDICTIVE_EDGE,
            StrategyClass.HYBRID,
        } and self.model_id is None:
            raise ValueError(
                f"{strategy_class.value} requires model_id identity"
            )
        if (
            strategy_class not in {
                StrategyClass.PREDICTIVE_EDGE,
                StrategyClass.HYBRID,
            }
            and self.model_id is not None
        ):
            raise ValueError(
                "model_id is valid only for forecast-dependent strategy classes"
            )

    @property
    def opportunity_class(self) -> StrategyClass:
        """Compatibility name backed by the single canonical StrategyClass authority."""
        return self.opportunity.strategy_class

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
                "schema_version": 2,
                "intent_id": self.intent_id,
                "opportunity_id": self.opportunity.opportunity_id,
                "opportunity_class": self.opportunity.strategy_class.value,
                "opportunity_decision": self.opportunity.decision.value,
                "evidence_sha256": self.evidence.evidence_sha256,
                "candidate_sha256": self.candidate_sha256,
                "signal_strength": str(self.signal_strength),
                "strategy_id": self.strategy_id,
                "model_id": self.model_id,
                "config_sha256": self.config_sha256,
            }
        )

    def audit_payload(self) -> dict[str, object]:
        return {
            "schema": "autosport.opportunity_intent_evidence",
            "schema_version": 1,
            "intent_id": self.intent_id,
            "intent_sha256": self.intent_sha256,
            "opportunity_id": self.opportunity.opportunity_id,
            "opportunity": self.opportunity.to_dict(),
            "evidence": self.evidence.to_dict(),
            "evidence_sha256": self.evidence.evidence_sha256,
            "candidate_sha256": self.candidate_sha256,
            "signal_strength": str(self.signal_strength),
            "strategy_id": self.strategy_id,
            "model_id": self.model_id,
            "config_sha256": self.config_sha256,
            "risk_context": {
                "provider_accounts": [
                    list(binding) for binding in self.risk_context.provider_accounts
                ],
                "bankroll_id": self.risk_context.bankroll_id,
                "currency": self.risk_context.currency,
                "measurement_window_start": self.risk_context.measurement_window_start,
                "measurement_window_end": self.risk_context.measurement_window_end,
                "proposal_ts": self.risk_context.proposal_ts,
            },
        }


@dataclass(frozen=True, slots=True)
class PortfolioDependencyGraph:
    """Canonical dependency/correlation evidence bound to exact plan inputs."""

    portfolio_sha256: str
    intent_sha256s: tuple[str, ...]
    candidate_sha256s: tuple[str, ...]
    dependency_edges: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        _canonical_sha256("dependency portfolio_sha256", self.portfolio_sha256)
        if type(self.intent_sha256s) is not tuple or type(self.candidate_sha256s) is not tuple:
            raise ValueError("dependency identity vectors must be tuples")
        if len(self.intent_sha256s) != len(self.candidate_sha256s):
            raise ValueError("dependency identity vectors must have matching cardinality")
        for digest in self.intent_sha256s:
            _canonical_sha256("dependency intent_sha256", digest)
        for digest in self.candidate_sha256s:
            _canonical_sha256("dependency candidate_sha256", digest)
        if len(self.intent_sha256s) != len(set(self.intent_sha256s)):
            raise ValueError("dependency intent identities must be unique")
        if len(self.candidate_sha256s) != len(set(self.candidate_sha256s)):
            raise ValueError("dependency candidate identities must be unique")
        if type(self.dependency_edges) is not tuple:
            raise ValueError("dependency_edges must be a canonical tuple")
        candidates = set(self.candidate_sha256s)
        validated_edges: list[tuple[str, str]] = []
        for edge in self.dependency_edges:
            if type(edge) is not tuple or len(edge) != 2:
                raise ValueError("dependency edge must contain exactly two candidate hashes")
            left = _canonical_sha256("dependency edge candidate", edge[0])
            right = _canonical_sha256("dependency edge candidate", edge[1])
            if left == right:
                raise ValueError("dependency edge cannot self-reference a candidate")
            canonical_edge = tuple(sorted((left, right)))
            if edge != canonical_edge:
                raise ValueError("dependency edges must use canonical endpoint order")
            if left not in candidates or right not in candidates:
                raise ValueError("dependency edge must reference the exact candidate set")
            validated_edges.append(canonical_edge)
        if tuple(validated_edges) != tuple(sorted(validated_edges)):
            raise ValueError("dependency_edges must be sorted")
        if len(validated_edges) != len(set(validated_edges)):
            raise ValueError("dependency_edges must be unique")

    @classmethod
    def for_inputs(
        cls,
        book: PaperBook,
        intents: tuple[OpportunityIntent, ...],
        *,
        dependency_edges: tuple[tuple[str, str], ...] = (),
    ) -> "PortfolioDependencyGraph":
        if not isinstance(book, PaperBook):
            raise TypeError("book must be PaperBook")
        if type(intents) is not tuple or any(
            not isinstance(intent, OpportunityIntent) for intent in intents
        ):
            raise TypeError("intents must be a tuple of OpportunityIntent values")
        portfolio_sha256 = PaperRiskPolicy.risk_of_ruin_portfolio_sha256(book)
        if portfolio_sha256 is None:
            raise ValueError("canonical current portfolio identity cannot be proven")
        return cls(
            portfolio_sha256=portfolio_sha256,
            intent_sha256s=tuple(intent.intent_sha256 for intent in intents),
            candidate_sha256s=tuple(intent.candidate_sha256 for intent in intents),
            dependency_edges=dependency_edges,
        )

    @property
    def graph_sha256(self) -> str:
        return _sha256_payload(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "autosport.portfolio_dependency_graph",
            "schema_version": 1,
            "portfolio_sha256": self.portfolio_sha256,
            "intent_sha256s": list(self.intent_sha256s),
            "candidate_sha256s": list(self.candidate_sha256s),
            "dependency_edges": [list(edge) for edge in self.dependency_edges],
        }

    @classmethod
    def from_dict(cls, raw: object) -> "PortfolioDependencyGraph":
        expected = {
            "schema",
            "schema_version",
            "portfolio_sha256",
            "intent_sha256s",
            "candidate_sha256s",
            "dependency_edges",
        }
        if type(raw) is not dict or set(raw) != expected:
            raise ValueError("serialized dependency graph must contain canonical fields")
        if raw["schema"] != "autosport.portfolio_dependency_graph":
            raise ValueError("unsupported dependency graph schema")
        if raw["schema_version"] != 1:
            raise ValueError("unsupported dependency graph schema_version")
        intent_sha256s = raw["intent_sha256s"]
        candidate_sha256s = raw["candidate_sha256s"]
        edges_raw = raw["dependency_edges"]
        if type(intent_sha256s) is not list or type(candidate_sha256s) is not list:
            raise ValueError("serialized dependency identity vectors must be lists")
        if type(edges_raw) is not list:
            raise ValueError("serialized dependency edges must be a list")
        edges: list[tuple[str, str]] = []
        for edge in edges_raw:
            if type(edge) is not list or len(edge) != 2:
                raise ValueError("serialized dependency edge must contain two endpoints")
            edges.append((edge[0], edge[1]))
        return cls(
            portfolio_sha256=raw["portfolio_sha256"],
            intent_sha256s=tuple(intent_sha256s),
            candidate_sha256s=tuple(candidate_sha256s),
            dependency_edges=tuple(edges),
        )


_REQUIRED_TERMINAL_EXECUTION_CHECKS = frozenset(
    {
        "market_quote_freshness_status",
        "paper_risk_policy",
        "routing_feasibility",
        "settlement_rule_scope",
    }
)


@dataclass(frozen=True, slots=True)
class TerminalStateCompletenessEvidence:
    """Typed external witness for an exhaustive paper terminal-state model.

    This value does not invent market/provider settlement rules. It binds one
    externally verified completeness/reproducibility claim to the exact current
    portfolio, intent/candidate vector, dependency graph and canonical ScenarioGroup
    model. build_portfolio_plan independently re-runs ScenarioSearchEngine over
    current+proposed paper tickets before the witness can authorize any plan.
    """

    evidence_id: str
    verifier_identity: str
    verification_protocol_sha256: str
    reproducibility_bundle_sha256: str
    causal_cutoff: str
    evaluated_at: str
    portfolio_sha256: str
    dependency_graph_sha256: str
    intent_sha256s: tuple[str, ...]
    candidate_sha256s: tuple[str, ...]
    scenario_groups: tuple[ScenarioGroup, ...]
    execution_check_sha256s: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        _canonical_text("terminal completeness evidence_id", self.evidence_id)
        _canonical_text("terminal completeness verifier_identity", self.verifier_identity)
        _canonical_sha256(
            "terminal completeness verification_protocol_sha256",
            self.verification_protocol_sha256,
        )
        _canonical_sha256(
            "terminal completeness reproducibility_bundle_sha256",
            self.reproducibility_bundle_sha256,
        )
        _, cutoff = _canonical_timestamp(
            "terminal completeness causal_cutoff", self.causal_cutoff
        )
        _, evaluated = _canonical_timestamp(
            "terminal completeness evaluated_at", self.evaluated_at
        )
        if cutoff > evaluated:
            raise ValueError(
                "terminal completeness causal cutoff must not be after evaluation"
            )
        _canonical_sha256(
            "terminal completeness portfolio_sha256", self.portfolio_sha256
        )
        _canonical_sha256(
            "terminal completeness dependency_graph_sha256",
            self.dependency_graph_sha256,
        )
        if type(self.execution_check_sha256s) is not tuple:
            raise ValueError(
                "terminal execution checks must be a canonical tuple"
            )
        validated_execution_checks: list[tuple[str, str]] = []
        for item in self.execution_check_sha256s:
            if type(item) is not tuple or len(item) != 2:
                raise ValueError(
                    "terminal execution checks must contain (check, sha256) tuples"
                )
            check_name = _canonical_text(
                "terminal execution check", item[0]
            )
            digest = _canonical_sha256(
                "terminal execution check sha256", item[1]
            )
            validated_execution_checks.append((check_name, digest))
        canonical_execution_checks = tuple(validated_execution_checks)
        if (
            canonical_execution_checks
            != tuple(sorted(canonical_execution_checks))
            or len(canonical_execution_checks)
            != len(set(canonical_execution_checks))
        ):
            raise ValueError(
                "terminal execution checks must be sorted and unique"
            )
        check_names = {name for name, _ in canonical_execution_checks}
        if not _REQUIRED_TERMINAL_EXECUTION_CHECKS.issubset(check_names):
            raise ValueError(
                "terminal execution witness lacks required canonical control evidence"
            )
        if (
            type(self.intent_sha256s) is not tuple
            or type(self.candidate_sha256s) is not tuple
        ):
            raise ValueError("terminal completeness identity vectors must be tuples")
        if len(self.intent_sha256s) != len(self.candidate_sha256s):
            raise ValueError("terminal completeness identity vectors must match")
        if len(self.intent_sha256s) != len(set(self.intent_sha256s)):
            raise ValueError("terminal completeness intent identities must be unique")
        if len(self.candidate_sha256s) != len(set(self.candidate_sha256s)):
            raise ValueError("terminal completeness candidate identities must be unique")
        for digest in self.intent_sha256s:
            _canonical_sha256("terminal completeness intent_sha256", digest)
        for digest in self.candidate_sha256s:
            _canonical_sha256("terminal completeness candidate_sha256", digest)

        if type(self.scenario_groups) is not tuple or not self.scenario_groups:
            raise ValueError("terminal completeness requires canonical scenario groups")
        if any(not isinstance(group, ScenarioGroup) for group in self.scenario_groups):
            raise ValueError("terminal completeness contains invalid scenario group")
        group_ids = tuple(group.group_id for group in self.scenario_groups)
        for group_id in group_ids:
            _canonical_text("terminal scenario group_id", group_id)
        if (
            group_ids != tuple(sorted(group_ids))
            or len(group_ids) != len(set(group_ids))
        ):
            raise ValueError(
                "terminal scenario groups must be sorted and uniquely identified"
            )
        seen_quotes: set[str] = set()
        for group in self.scenario_groups:
            keys = tuple(outcome.quote_key for outcome in group.outcomes)
            if keys != tuple(sorted(keys)):
                raise ValueError(
                    "terminal scenario outcomes must use canonical quote-key order"
                )
            for outcome in group.outcomes:
                _canonical_text("terminal scenario quote_key", outcome.quote_key)
                if outcome.quote_key in seen_quotes:
                    raise ValueError(
                        "terminal scenario quote_key may appear in only one group"
                    )
                seen_quotes.add(outcome.quote_key)
                if outcome.probability is not None and (
                    not isinstance(outcome.probability, Decimal)
                    or not outcome.probability.is_finite()
                ):
                    raise ValueError(
                        "terminal scenario probability must be a finite exact Decimal"
                    )

    @staticmethod
    def _groups_payload(
        groups: tuple[ScenarioGroup, ...],
    ) -> list[dict[str, object]]:
        return [
            {
                "group_id": group.group_id,
                "outcomes": [
                    {
                        "quote_key": outcome.quote_key,
                        "probability": (
                            None
                            if outcome.probability is None
                            else str(outcome.probability)
                        ),
                    }
                    for outcome in group.outcomes
                ],
            }
            for group in groups
        ]

    @classmethod
    def state_space_sha256_for(
        cls, groups: tuple[ScenarioGroup, ...]
    ) -> str:
        if type(groups) is not tuple or not groups:
            raise ValueError("terminal state-space hash requires scenario groups")
        return _sha256_payload(
            {
                "schema": "autosport.terminal_state_space",
                "schema_version": 1,
                "scenario_groups": cls._groups_payload(groups),
            }
        )

    @property
    def terminal_state_space_sha256(self) -> str:
        return self.state_space_sha256_for(self.scenario_groups)

    @staticmethod
    def execution_assumptions_sha256_for(
        *,
        verifier_identity: str,
        verification_protocol_sha256: str,
        reproducibility_bundle_sha256: str,
        execution_check_sha256s: tuple[tuple[str, str], ...],
    ) -> str:
        return _sha256_payload(
            {
                "schema": "autosport.terminal_execution_assumptions",
                "schema_version": 1,
                "verifier_identity": verifier_identity,
                "verification_protocol_sha256": verification_protocol_sha256,
                "reproducibility_bundle_sha256": reproducibility_bundle_sha256,
                "execution_check_sha256s": [
                    [name, digest]
                    for name, digest in execution_check_sha256s
                ],
            }
        )

    @property
    def execution_assumptions_sha256(self) -> str:
        return self.execution_assumptions_sha256_for(
            verifier_identity=self.verifier_identity,
            verification_protocol_sha256=self.verification_protocol_sha256,
            reproducibility_bundle_sha256=self.reproducibility_bundle_sha256,
            execution_check_sha256s=self.execution_check_sha256s,
        )

    def _identity_payload(self) -> dict[str, object]:
        return {
            "schema": "autosport.terminal_state_completeness_evidence",
            "schema_version": 1,
            "evidence_id": self.evidence_id,
            "verifier_identity": self.verifier_identity,
            "verification_protocol_sha256": self.verification_protocol_sha256,
            "reproducibility_bundle_sha256": self.reproducibility_bundle_sha256,
            "causal_cutoff": self.causal_cutoff,
            "evaluated_at": self.evaluated_at,
            "portfolio_sha256": self.portfolio_sha256,
            "dependency_graph_sha256": self.dependency_graph_sha256,
            "intent_sha256s": list(self.intent_sha256s),
            "candidate_sha256s": list(self.candidate_sha256s),
            "scenario_groups": self._groups_payload(self.scenario_groups),
            "execution_check_sha256s": [
                [name, digest]
                for name, digest in self.execution_check_sha256s
            ],
            "terminal_state_space_sha256": self.terminal_state_space_sha256,
            "execution_assumptions_sha256": self.execution_assumptions_sha256,
        }

    @property
    def evidence_sha256(self) -> str:
        return _sha256_payload(self._identity_payload())

    def to_dict(self) -> dict[str, object]:
        return {
            **self._identity_payload(),
            "evidence_sha256": self.evidence_sha256,
        }

    @classmethod
    def from_dict(cls, raw: object) -> "TerminalStateCompletenessEvidence":
        expected = {
            "schema",
            "schema_version",
            "evidence_id",
            "verifier_identity",
            "verification_protocol_sha256",
            "reproducibility_bundle_sha256",
            "causal_cutoff",
            "evaluated_at",
            "portfolio_sha256",
            "dependency_graph_sha256",
            "intent_sha256s",
            "candidate_sha256s",
            "scenario_groups",
            "terminal_state_space_sha256",
            "execution_check_sha256s",
            "execution_assumptions_sha256",
            "evidence_sha256",
        }
        if type(raw) is not dict or set(raw) != expected:
            raise ValueError(
                "serialized terminal completeness evidence must contain canonical fields"
            )
        if (
            raw["schema"] != "autosport.terminal_state_completeness_evidence"
            or raw["schema_version"] != 1
        ):
            raise ValueError("unsupported terminal completeness evidence schema")
        try:
            intents_raw = raw["intent_sha256s"]
            candidates_raw = raw["candidate_sha256s"]
            groups_raw = raw["scenario_groups"]
            if (
                type(intents_raw) is not list
                or type(candidates_raw) is not list
                or type(groups_raw) is not list
            ):
                raise ValueError(
                    "serialized terminal completeness vectors must be lists"
                )
            groups: list[ScenarioGroup] = []
            for group_raw in groups_raw:
                if (
                    type(group_raw) is not dict
                    or set(group_raw) != {"group_id", "outcomes"}
                ):
                    raise ValueError("serialized terminal scenario group is invalid")
                outcomes_raw = group_raw["outcomes"]
                if type(outcomes_raw) is not list:
                    raise ValueError(
                        "serialized terminal scenario outcomes must be a list"
                    )
                outcomes: list[ScenarioOutcome] = []
                for outcome_raw in outcomes_raw:
                    if (
                        type(outcome_raw) is not dict
                        or set(outcome_raw) != {"quote_key", "probability"}
                    ):
                        raise ValueError(
                            "serialized terminal scenario outcome is invalid"
                        )
                    probability_raw = outcome_raw["probability"]
                    probability = (
                        None
                        if probability_raw is None
                        else _decimal_from_serialized(
                            "terminal scenario probability", probability_raw
                        )
                    )
                    outcomes.append(
                        ScenarioOutcome(
                            quote_key=outcome_raw["quote_key"],
                            probability=probability,
                        )
                    )
                groups.append(
                    ScenarioGroup(
                        group_id=group_raw["group_id"],
                        outcomes=tuple(outcomes),
                    )
                )
            execution_checks_raw = raw["execution_check_sha256s"]
            if type(execution_checks_raw) is not list:
                raise ValueError(
                    "serialized terminal execution checks must be a list"
                )
            execution_checks: list[tuple[str, str]] = []
            for item in execution_checks_raw:
                if type(item) is not list or len(item) != 2:
                    raise ValueError(
                        "serialized terminal execution check must contain two fields"
                    )
                execution_checks.append((item[0], item[1]))
            evidence = cls(
                evidence_id=raw["evidence_id"],
                verifier_identity=raw["verifier_identity"],
                verification_protocol_sha256=raw[
                    "verification_protocol_sha256"
                ],
                reproducibility_bundle_sha256=raw[
                    "reproducibility_bundle_sha256"
                ],
                causal_cutoff=raw["causal_cutoff"],
                evaluated_at=raw["evaluated_at"],
                portfolio_sha256=raw["portfolio_sha256"],
                dependency_graph_sha256=raw["dependency_graph_sha256"],
                intent_sha256s=tuple(intents_raw),
                candidate_sha256s=tuple(candidates_raw),
                scenario_groups=tuple(groups),
                execution_check_sha256s=tuple(execution_checks),
            )
            if (
                raw["terminal_state_space_sha256"]
                != evidence.terminal_state_space_sha256
            ):
                raise ValueError(
                    "serialized terminal state-space digest does not match groups"
                )
            if (
                raw["execution_assumptions_sha256"]
                != evidence.execution_assumptions_sha256
            ):
                raise ValueError(
                    "serialized terminal execution-assumption digest does not match typed checks"
                )
            serialized_evidence_sha256 = _canonical_sha256(
                "serialized terminal completeness evidence_sha256",
                raw["evidence_sha256"],
            )
            if serialized_evidence_sha256 != evidence.evidence_sha256:
                raise ValueError(
                    "serialized terminal completeness identity does not match"
                )
            return evidence
        except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "serialized terminal completeness evidence is invalid"
            ) from exc


_VERIFIED_TERMINAL_AUTHORITY_TOKEN = object()


def _canonical_verified_outcome_authorities(
    authorities: tuple[MarketSettlementOutcomeAuthority, ...],
    *,
    decision_as_of: datetime | None = None,
) -> tuple[MarketSettlementOutcomeAuthority, ...]:
    if type(authorities) is not tuple:
        raise TypeError("verified_outcome_authorities must be a tuple")
    if any(
        not isinstance(authority, MarketSettlementOutcomeAuthority)
        for authority in authorities
    ):
        raise TypeError(
            "verified_outcome_authorities must contain MarketSettlementOutcomeAuthority values"
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
    authority_sha256s = tuple(
        authority.authority_sha256 for authority in ordered
    )
    if len(authority_sha256s) != len(set(authority_sha256s)):
        raise ValueError("verified outcome authorities must be unique")
    if decision_as_of is not None:
        for authority in ordered:
            authority.assert_available_as_of(decision_as_of)
    return ordered


@dataclass(frozen=True, slots=True)
class VerifiedTerminalEconomics:
    """Canonical terminal-economics result bound to completeness/control evidence."""

    completeness_evidence: TerminalStateCompletenessEvidence
    report_mode: str
    total_states: int
    worst_terminal_profit: Decimal
    best_terminal_profit: Decimal
    worst_proven: bool
    best_proven: bool
    outcome_space_exhaustive: bool = False
    outcome_space_exact: bool = False
    outcome_authority_sha256s: tuple[str, ...] = ()
    _authority_verification_token: object | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(
            self.completeness_evidence, TerminalStateCompletenessEvidence
        ):
            raise ValueError(
                "terminal economics requires TerminalStateCompletenessEvidence"
            )
        _canonical_text("terminal economics report_mode", self.report_mode)
        if type(self.total_states) is not int or self.total_states < 1:
            raise ValueError(
                "terminal economics total_states must be a positive integer"
            )
        for label, value in (
            ("worst_terminal_profit", self.worst_terminal_profit),
            ("best_terminal_profit", self.best_terminal_profit),
        ):
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"{label} must be a finite exact Decimal")
        if (
            type(self.worst_proven) is not bool
            or type(self.best_proven) is not bool
            or type(self.outcome_space_exhaustive) is not bool
            or type(self.outcome_space_exact) is not bool
        ):
            raise ValueError("terminal economics proof flags must be bools")
        if self.worst_terminal_profit > self.best_terminal_profit:
            raise ValueError(
                "terminal economics worst profit cannot exceed best profit"
            )
        if type(self.outcome_authority_sha256s) is not tuple:
            raise ValueError(
                "terminal economics outcome_authority_sha256s must be a tuple"
            )
        for digest in self.outcome_authority_sha256s:
            _canonical_sha256("terminal outcome authority sha256", digest)
        if len(self.outcome_authority_sha256s) != len(
            set(self.outcome_authority_sha256s)
        ):
            raise ValueError(
                "terminal outcome authority identities must be unique"
            )
        if self.outcome_space_exact and not self.outcome_space_exhaustive:
            raise ValueError(
                "exact terminal outcome space must also be exhaustive"
            )
        if self.outcome_authority_sha256s:
            if self._authority_verification_token is not _VERIFIED_TERMINAL_AUTHORITY_TOKEN:
                raise TypeError(
                    "authoritative terminal economics requires separately verified market authority"
                )
            if not self.outcome_space_exhaustive:
                raise ValueError(
                    "authoritative terminal economics must prove exhaustive outcome coverage"
                )
        elif self.outcome_space_exhaustive or self.outcome_space_exact:
            raise ValueError(
                "terminal outcome truth cannot be authoritative without verified authority identities"
            )
        if self.outcome_space_exact and (
            not self.worst_proven or not self.best_proven
        ):
            raise ValueError(
                "exact authoritative terminal space requires exact extrema proofs"
            )

    def _identity_payload(self) -> dict[str, object]:
        return {
            "schema": "autosport.verified_terminal_economics",
            "schema_version": 2,
            "completeness_evidence": self.completeness_evidence.to_dict(),
            "report_mode": self.report_mode,
            "total_states": self.total_states,
            "worst_terminal_profit": str(self.worst_terminal_profit),
            "best_terminal_profit": str(self.best_terminal_profit),
            "worst_proven": self.worst_proven,
            "best_proven": self.best_proven,
            "outcome_space_exhaustive": self.outcome_space_exhaustive,
            "outcome_space_exact": self.outcome_space_exact,
            "outcome_authority_sha256s": list(self.outcome_authority_sha256s),
        }

    @property
    def proof_sha256(self) -> str:
        return _sha256_payload(self._identity_payload())

    def to_dict(self) -> dict[str, object]:
        return {
            **self._identity_payload(),
            "proof_sha256": self.proof_sha256,
        }

    @classmethod
    def from_dict(
        cls,
        raw: object,
        *,
        verified_outcome_authorities: tuple[
            MarketSettlementOutcomeAuthority, ...
        ] = (),
        decision_as_of: datetime | None = None,
    ) -> "VerifiedTerminalEconomics":
        expected = {
            "schema",
            "schema_version",
            "completeness_evidence",
            "report_mode",
            "total_states",
            "worst_terminal_profit",
            "best_terminal_profit",
            "worst_proven",
            "best_proven",
            "outcome_space_exhaustive",
            "outcome_space_exact",
            "outcome_authority_sha256s",
            "proof_sha256",
        }
        if type(raw) is not dict or set(raw) != expected:
            raise ValueError(
                "serialized terminal economics must contain canonical fields"
            )
        if (
            raw["schema"] != "autosport.verified_terminal_economics"
            or raw["schema_version"] != 2
        ):
            raise ValueError("unsupported terminal economics schema")
        try:
            authority_sha256s_raw = raw["outcome_authority_sha256s"]
            if type(authority_sha256s_raw) is not list:
                raise ValueError(
                    "serialized outcome authority identities must be a list"
                )
            authority_sha256s = tuple(authority_sha256s_raw)
            token: object | None = None
            if authority_sha256s:
                ordered = _canonical_verified_outcome_authorities(
                    verified_outcome_authorities,
                    decision_as_of=decision_as_of,
                )
                verified_sha256s = tuple(
                    authority.authority_sha256 for authority in ordered
                )
                if verified_sha256s != authority_sha256s:
                    raise ValueError(
                        "serialized terminal economics authority identities do not match separately verified source evidence"
                    )
                expected_exact = all(
                    authority.terminal_space_exact for authority in ordered
                )
                if raw["outcome_space_exact"] is not expected_exact:
                    raise ValueError(
                        "serialized terminal exactness does not match verified authorities"
                    )
                token = _VERIFIED_TERMINAL_AUTHORITY_TOKEN
            proof = cls(
                completeness_evidence=TerminalStateCompletenessEvidence.from_dict(
                    raw["completeness_evidence"]
                ),
                report_mode=raw["report_mode"],
                total_states=raw["total_states"],
                worst_terminal_profit=_decimal_from_serialized(
                    "serialized worst_terminal_profit",
                    raw["worst_terminal_profit"],
                ),
                best_terminal_profit=_decimal_from_serialized(
                    "serialized best_terminal_profit",
                    raw["best_terminal_profit"],
                ),
                worst_proven=raw["worst_proven"],
                best_proven=raw["best_proven"],
                outcome_space_exhaustive=raw["outcome_space_exhaustive"],
                outcome_space_exact=raw["outcome_space_exact"],
                outcome_authority_sha256s=authority_sha256s,
                _authority_verification_token=token,
            )
            serialized_proof_sha256 = _canonical_sha256(
                "serialized terminal economics proof_sha256",
                raw["proof_sha256"],
            )
            if serialized_proof_sha256 != proof.proof_sha256:
                raise ValueError(
                    "serialized terminal economics identity does not match"
                )
            return proof
        except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
            raise ValueError("serialized terminal economics is invalid") from exc


@dataclass(frozen=True, slots=True)
class PortfolioPlan:
    decision_ts: str
    action: PortfolioAction
    stakes: tuple[Decimal, ...]
    intent_ids: tuple[str, ...]
    intent_sha256s: tuple[str, ...]
    opportunity_classes: tuple[str, ...]
    portfolio_sha256: str | None
    dependency_graph: PortfolioDependencyGraph | None
    terminal_economics: VerifiedTerminalEconomics | None
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
            StrategyClass(opportunity_class)
        _optional_sha256("portfolio_sha256", self.portfolio_sha256)
        if self.dependency_graph is not None:
            if not isinstance(self.dependency_graph, PortfolioDependencyGraph):
                raise ValueError("dependency_graph must be PortfolioDependencyGraph")
            if self.portfolio_sha256 != self.dependency_graph.portfolio_sha256:
                raise ValueError("dependency graph must bind the exact portfolio identity")
            if self.intent_sha256s != self.dependency_graph.intent_sha256s:
                raise ValueError("dependency graph must bind the exact intent vector")
        if self.terminal_economics is not None:
            if not isinstance(self.terminal_economics, VerifiedTerminalEconomics):
                raise ValueError(
                    "terminal_economics must be VerifiedTerminalEconomics"
                )
            terminal_evidence = self.terminal_economics.completeness_evidence
            if self.portfolio_sha256 != terminal_evidence.portfolio_sha256:
                raise ValueError(
                    "terminal economics must bind exact portfolio identity"
                )
            if self.dependency_graph is None:
                raise ValueError(
                    "terminal economics requires a bound dependency graph"
                )
            if (
                self.dependency_graph.graph_sha256
                != terminal_evidence.dependency_graph_sha256
            ):
                raise ValueError(
                    "terminal economics must bind exact dependency graph"
                )
            if self.intent_sha256s != terminal_evidence.intent_sha256s:
                raise ValueError(
                    "terminal economics must bind exact intent vector"
                )
        _optional_sha256(
            "economic_goal_contract_sha256", self.economic_goal_contract_sha256
        )
        _canonical_sha256("risk_policy_sha256", self.risk_policy_sha256)
        if not isinstance(self.portfolio_truth, EvidenceTruth):
            raise ValueError("portfolio_truth must be an EvidenceTruth")
        _canonical_text("reason", self.reason)
        positive = any(stake > 0 for stake in self.stakes)
        if self.action in {
            PortfolioAction.STAKE_VECTOR,
            PortfolioAction.HEDGE_REBALANCE,
            PortfolioAction.PAPER_PLAN,
        } and not positive:
            raise ValueError("positive portfolio action requires at least one positive stake")
        if self.action in {PortfolioAction.WAIT, PortfolioAction.ZERO} and positive:
            raise ValueError("WAIT/ZERO plans must not carry positive stakes")
        if positive:
            if self.portfolio_truth is not EvidenceTruth.EXACT:
                raise ValueError("positive portfolio action requires exact portfolio truth")
            if self.portfolio_sha256 is None:
                raise ValueError("positive portfolio action requires portfolio identity")
            if self.dependency_graph is None:
                raise ValueError("positive portfolio action requires bound dependency graph")
            if self.economic_goal_contract_sha256 is None:
                raise ValueError("positive portfolio action requires economic-goal identity")
            outcome_independent_positive = any(
                stake > 0
                and StrategyClass(opportunity_class)
                in {
                    StrategyClass.ARBITRAGE,
                    StrategyClass.DUTCHING,
                    StrategyClass.HEDGE_REBALANCE,
                }
                for stake, opportunity_class in zip(
                    self.stakes,
                    self.opportunity_classes,
                    strict=True,
                )
            )
            if outcome_independent_positive:
                proof = self.terminal_economics
                if (
                    proof is None
                    or not proof.outcome_authority_sha256s
                    or not proof.outcome_space_exhaustive
                    or not proof.outcome_space_exact
                    or not proof.worst_proven
                    or proof.worst_terminal_profit <= Decimal("0")
                ):
                    raise ValueError(
                        "positive outcome-independent action requires authoritative "
                        "exact exhaustive market-outcome semantics with positive "
                        "minimum terminal net P&L"
                    )

    @property
    def dependency_graph_sha256(self) -> str | None:
        return (
            None
            if self.dependency_graph is None
            else self.dependency_graph.graph_sha256
        )

    def _identity_payload(self) -> dict[str, object]:
        return {
            "schema": "autosport.portfolio_plan",
            "schema_version": 4,
            "decision_ts": self.decision_ts,
            "action": self.action.value,
            "stakes": [str(value) for value in self.stakes],
            "intent_ids": list(self.intent_ids),
            "intent_sha256s": list(self.intent_sha256s),
            "opportunity_classes": list(self.opportunity_classes),
            "portfolio_sha256": self.portfolio_sha256,
            "dependency_graph": (
                None if self.dependency_graph is None else self.dependency_graph.to_dict()
            ),
            "dependency_graph_sha256": self.dependency_graph_sha256,
            "terminal_economics": (
                None
                if self.terminal_economics is None
                else self.terminal_economics.to_dict()
            ),
            "economic_goal_contract_sha256": self.economic_goal_contract_sha256,
            "risk_policy_sha256": self.risk_policy_sha256,
            "portfolio_truth": self.portfolio_truth.value,
            "reason": self.reason,
        }

    @property
    def plan_sha256(self) -> str:
        return _sha256_payload(self._identity_payload())

    def to_dict(self) -> dict[str, object]:
        return {**self._identity_payload(), "plan_sha256": self.plan_sha256}

    @classmethod
    def from_dict(
        cls,
        raw: object,
        *,
        verified_outcome_authorities: tuple[
            MarketSettlementOutcomeAuthority, ...
        ] = (),
    ) -> "PortfolioPlan":
        expected = {
            "schema",
            "schema_version",
            "decision_ts",
            "action",
            "stakes",
            "intent_ids",
            "intent_sha256s",
            "opportunity_classes",
            "portfolio_sha256",
            "dependency_graph",
            "dependency_graph_sha256",
            "terminal_economics",
            "economic_goal_contract_sha256",
            "risk_policy_sha256",
            "portfolio_truth",
            "reason",
            "plan_sha256",
        }
        if type(raw) is not dict or set(raw) != expected:
            raise ValueError("serialized portfolio plan must contain canonical fields")
        if raw["schema"] != "autosport.portfolio_plan":
            raise ValueError("unsupported portfolio plan schema")
        if raw["schema_version"] != 4:
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
            graph_raw = raw["dependency_graph"]
            graph = (
                None
                if graph_raw is None
                else PortfolioDependencyGraph.from_dict(graph_raw)
            )
            terminal_raw = raw["terminal_economics"]
            _, decision_time = _canonical_timestamp(
                "serialized portfolio decision_ts",
                raw["decision_ts"],
            )
            terminal_economics = (
                None
                if terminal_raw is None
                else VerifiedTerminalEconomics.from_dict(
                    terminal_raw,
                    verified_outcome_authorities=verified_outcome_authorities,
                    decision_as_of=decision_time,
                )
            )
            plan = cls(
                decision_ts=raw["decision_ts"],
                action=PortfolioAction(raw["action"]),
                stakes=tuple(
                    _decimal_from_serialized("serialized portfolio stake", value)
                    for value in stakes_raw
                ),
                intent_ids=tuple(intent_ids_raw),
                intent_sha256s=tuple(intent_sha256s_raw),
                opportunity_classes=tuple(classes_raw),
                portfolio_sha256=raw["portfolio_sha256"],
                dependency_graph=graph,
                terminal_economics=terminal_economics,
                economic_goal_contract_sha256=raw["economic_goal_contract_sha256"],
                risk_policy_sha256=raw["risk_policy_sha256"],
                portfolio_truth=EvidenceTruth(raw["portfolio_truth"]),
                reason=raw["reason"],
            )
            serialized_graph_sha256 = raw["dependency_graph_sha256"]
            if serialized_graph_sha256 != plan.dependency_graph_sha256:
                raise ValueError("serialized dependency graph digest does not match graph")
            serialized_plan_sha256 = _canonical_sha256(
                "serialized plan_sha256", raw["plan_sha256"]
            )
            if serialized_plan_sha256 != plan.plan_sha256:
                raise ValueError("serialized plan identity does not match plan contents")
            return plan
        except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
            raise ValueError("serialized portfolio plan is invalid") from exc


_PORTFOLIO_PLAN_DECISION_AGENT = "portfolio-plan"
_PORTFOLIO_PLAN_DECISION_ACTION = "RECORD_PORTFOLIO_PLAN"
_PORTFOLIO_PLAN_JSON_PAYLOAD_KEY = "portfolio_plan_json"
_PORTFOLIO_PLAN_SHA256_PAYLOAD_KEY = "portfolio_plan_sha256"
_PORTFOLIO_INTENT_EVIDENCE_JSON_PAYLOAD_KEY = "portfolio_intent_evidence_json"


class PortfolioPlanReconciliationRequired(RuntimeError):
    """One durable material-action identity disagrees with current PortfolioPlan truth."""


def _portfolio_intent_evidence_json(
    plan: PortfolioPlan,
    intents: tuple[OpportunityIntent, ...],
) -> str:
    if type(intents) is not tuple or any(
        not isinstance(intent, OpportunityIntent) for intent in intents
    ):
        raise TypeError("intents must be a tuple of OpportunityIntent values")
    if tuple(intent.intent_id for intent in intents) != plan.intent_ids:
        raise ValueError("durable intent ids do not match PortfolioPlan")
    if tuple(intent.intent_sha256 for intent in intents) != plan.intent_sha256s:
        raise ValueError("durable intent hashes do not match PortfolioPlan")
    if tuple(intent.opportunity_class.value for intent in intents) != plan.opportunity_classes:
        raise ValueError("durable strategy classes do not match PortfolioPlan")
    return _canonical_json_payload(
        {
            "schema": "autosport.portfolio_plan_intent_evidence",
            "schema_version": 1,
            "intents": [intent.audit_payload() for intent in intents],
        }
    )


def persist_portfolio_plan_decision(
    ledger: JsonlDecisionLedger,
    plan: PortfolioPlan,
    intents: tuple[OpportunityIntent, ...],
    risk_policy: PaperRiskPolicy,
    *,
    initialize_ledger: bool,
    replay_run_id: str,
    material_action_id: str,
    verified_outcome_authorities: tuple[
        MarketSettlementOutcomeAuthority, ...
    ] = (),
) -> DecisionRecord:
    """Fsync one exact plan to the canonical Decision Ledger, idempotently across restart."""

    if not isinstance(ledger, JsonlDecisionLedger):
        raise TypeError("ledger must be JsonlDecisionLedger")
    if not isinstance(plan, PortfolioPlan):
        raise TypeError("plan must be PortfolioPlan")
    if not isinstance(risk_policy, PaperRiskPolicy):
        raise TypeError("risk_policy must be PaperRiskPolicy")
    if type(initialize_ledger) is not bool:
        raise TypeError("initialize_ledger must be a bool")
    replay_run_id = _canonical_text("replay_run_id", replay_run_id)
    material_action_id = _canonical_text("material_action_id", material_action_id)

    goal = risk_policy.economic_goal
    if goal is None:
        raise ValueError("durable PortfolioPlan decision requires canonical EconomicGoal")
    if plan.economic_goal_contract_sha256 != provenance_for(goal).contract_sha256:
        raise ValueError("PortfolioPlan EconomicGoal provenance does not match risk authority")
    if plan.risk_policy_sha256 != risk_policy.provenance_sha256:
        raise ValueError("PortfolioPlan RiskPolicy provenance does not match risk authority")

    plan_json = _canonical_json_payload(plan.to_dict())
    intent_evidence_json = _portfolio_intent_evidence_json(plan, intents)
    context_hash = _sha256_payload(
        {
            "schema": "autosport.portfolio_plan_decision_context",
            "schema_version": 1,
            "plan_sha256": plan.plan_sha256,
            "intent_evidence_sha256": hashlib.sha256(
                intent_evidence_json.encode("utf-8")
            ).hexdigest(),
        }
    )
    payload = {
        MATERIAL_ACTION_ID_PAYLOAD_KEY: material_action_id,
        _PORTFOLIO_PLAN_SHA256_PAYLOAD_KEY: plan.plan_sha256,
        _PORTFOLIO_PLAN_JSON_PAYLOAD_KEY: plan_json,
        _PORTFOLIO_INTENT_EVIDENCE_JSON_PAYLOAD_KEY: intent_evidence_json,
    }

    ledger_exists = ledger.path.exists()
    if initialize_ledger:
        if ledger_exists:
            raise DecisionLedgerIntegrityError(
                "PortfolioPlan ledger initialization requires a genuinely absent ledger"
            )
        existing = None
    else:
        if not ledger_exists:
            raise DecisionLedgerIntegrityError(
                "Decision Ledger file is missing or unreadable"
            )
        existing = ledger.verified_economic_decision_for_material_action(
            material_action_id,
            goal,
            risk_policy=risk_policy,
        )
    if existing is not None:
        if (
            existing.replay_run_id != replay_run_id
            or existing.agent != _PORTFOLIO_PLAN_DECISION_AGENT
            or existing.observed_ts != plan.decision_ts
            or existing.action != _PORTFOLIO_PLAN_DECISION_ACTION
            or existing.context_hash != context_hash
            or existing.payload.get(_PORTFOLIO_PLAN_SHA256_PAYLOAD_KEY)
            != plan.plan_sha256
            or existing.payload.get(_PORTFOLIO_PLAN_JSON_PAYLOAD_KEY) != plan_json
            or existing.payload.get(_PORTFOLIO_INTENT_EVIDENCE_JSON_PAYLOAD_KEY)
            != intent_evidence_json
        ):
            raise PortfolioPlanReconciliationRequired(
                "durable PortfolioPlan material action conflicts with current decision intent"
            )
        try:
            restored = PortfolioPlan.from_dict(
                json.loads(existing.payload[_PORTFOLIO_PLAN_JSON_PAYLOAD_KEY]),
                verified_outcome_authorities=verified_outcome_authorities,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PortfolioPlanReconciliationRequired(
                "durable PortfolioPlan evidence cannot be reconstructed"
            ) from exc
        if restored != plan:
            raise PortfolioPlanReconciliationRequired(
                "durable PortfolioPlan evidence does not match current plan"
            )
        return existing

    record = DecisionRecord(
        replay_run_id=replay_run_id,
        agent=_PORTFOLIO_PLAN_DECISION_AGENT,
        observed_ts=plan.decision_ts,
        action=_PORTFOLIO_PLAN_DECISION_ACTION,
        payload=payload,
        context_hash=context_hash,
        decision_kind=ECONOMIC_DECISION_KIND,
    )
    ledger.append_economic(
        record,
        EconomicDecisionAuthority(goal, risk_policy),
    )
    persisted = ledger.verified_economic_decision_for_material_action(
        material_action_id,
        goal,
        risk_policy=risk_policy,
    )
    if persisted is None:
        raise PortfolioPlanReconciliationRequired(
            "PortfolioPlan decision was not durably recoverable after append"
        )
    return persisted


def _terminal_plan(
    *,
    decision_ts: str,
    action: PortfolioAction,
    reason: str,
    intents: tuple[OpportunityIntent, ...],
    portfolio_sha256: str | None,
    dependency_graph: PortfolioDependencyGraph | None,
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
        dependency_graph=dependency_graph,
        terminal_economics=None,
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
    *,
    authoritative_terminal_model: bool = False,
) -> str | None:
    evidence = intent.evidence
    if intent.opportunity.decision is OpportunityDecision.WAIT:
        return "canonical opportunity decision is WAIT"
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
        StrategyClass.ARBITRAGE,
        StrategyClass.DUTCHING,
        StrategyClass.HEDGE_REBALANCE,
    }:
        if evidence.truth is not EvidenceTruth.EXACT:
            return "outcome-independent opportunity evidence is not exact"
        if not authoritative_terminal_model:
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


def _terminal_state_bindings_reason(
    evidence: TerminalStateCompletenessEvidence,
    *,
    portfolio_sha256: str,
    dependency_graph: PortfolioDependencyGraph,
    intents: tuple[OpportunityIntent, ...],
    decision_time: datetime,
) -> str | None:
    if evidence.portfolio_sha256 != portfolio_sha256:
        return "terminal completeness evidence does not bind current portfolio"
    if evidence.dependency_graph_sha256 != dependency_graph.graph_sha256:
        return "terminal completeness evidence does not bind current dependency graph"
    if evidence.intent_sha256s != tuple(
        intent.intent_sha256 for intent in intents
    ):
        return "terminal completeness evidence does not bind current intent vector"
    if evidence.candidate_sha256s != tuple(
        intent.candidate_sha256 for intent in intents
    ):
        return "terminal completeness evidence does not bind current candidate vector"
    _, cutoff = _canonical_timestamp(
        "terminal completeness causal_cutoff", evidence.causal_cutoff
    )
    _, evaluated = _canonical_timestamp(
        "terminal completeness evaluated_at", evidence.evaluated_at
    )
    if cutoff > decision_time or evaluated > decision_time:
        return (
            "terminal completeness evidence is from the future relative to decision time"
        )
    return None


def _proposed_terminal_ticket(
    intent: OpportunityIntent,
    stake: Decimal,
    decision_ts: str,
) -> PaperTicket:
    context = intent.risk_context
    return PaperTicket(
        ticket_id=f"proposal-{intent.candidate_sha256}",
        stake=stake,
        legs=context.legs,
        placed_at=decision_ts,
        status=TicketStatus.OPEN,
        payout=Decimal("0"),
        strategy_reason=f"portfolio-plan:{intent.intent_id}",
        provider_source_ids=tuple(sorted(context.source_ids)),
        provider_accounts=context.provider_accounts,
        bankroll_id=context.bankroll_id,
        currency=context.currency,
    )


def _verify_terminal_economics(
    book: PaperBook,
    intents: tuple[OpportunityIntent, ...],
    stakes: tuple[Decimal, ...],
    dependency_graph: PortfolioDependencyGraph,
    evidence: TerminalStateCompletenessEvidence,
    *,
    portfolio_sha256: str,
    decision_ts: str,
    decision_time: datetime,
    market_outcome_authorities: tuple[
        MarketSettlementOutcomeAuthority, ...
    ] = (),
) -> tuple[VerifiedTerminalEconomics | None, str | None]:
    binding_reason = _terminal_state_bindings_reason(
        evidence,
        portfolio_sha256=portfolio_sha256,
        dependency_graph=dependency_graph,
        intents=intents,
        decision_time=decision_time,
    )
    if binding_reason is not None:
        return None, binding_reason

    positive_pairs = tuple(
        (intent, stake)
        for intent, stake in zip(intents, stakes, strict=True)
        if stake > 0
    )
    if not positive_pairs:
        return (
            None,
            "terminal economics requires at least one positive proposed stake",
        )

    outcome_independent = tuple(
        intent
        for intent, _ in positive_pairs
        if intent.opportunity_class
        in {
            StrategyClass.ARBITRAGE,
            StrategyClass.DUTCHING,
            StrategyClass.HEDGE_REBALANCE,
        }
    )
    if not market_outcome_authorities:
        for intent in outcome_independent:
            if (
                intent.evidence.terminal_state_space_sha256
                != evidence.terminal_state_space_sha256
            ):
                return (
                    None,
                    "outcome-independent evidence terminal-state identity does not match verified completeness",
                )
            if (
                intent.evidence.execution_assumptions_sha256
                != evidence.execution_assumptions_sha256
            ):
                return (
                    None,
                    "outcome-independent execution assumptions do not match verified completeness",
                )

    tickets = [
        ticket
        for ticket in book.tickets.values()
        if ticket.status is TicketStatus.OPEN
    ]
    tickets.extend(
        _proposed_terminal_ticket(intent, stake, decision_ts)
        for intent, stake in positive_pairs
    )

    if market_outcome_authorities:
        try:
            ordered_authorities = _canonical_verified_outcome_authorities(
                market_outcome_authorities,
                decision_as_of=decision_time,
            )
            report = ScenarioSearchEngine().analyse_authoritative(
                tickets,
                ordered_authorities,
                decision_as_of=decision_time,
            )
        except (ArithmeticError, TypeError, ValueError) as exc:
            return (
                None,
                f"authoritative terminal-state model is not executable: {exc}",
            )
        proof = VerifiedTerminalEconomics(
            completeness_evidence=evidence,
            report_mode=report.mode,
            total_states=report.total_states,
            worst_terminal_profit=report.observed_worst,
            best_terminal_profit=report.observed_best,
            worst_proven=report.worst_proven,
            best_proven=report.best_proven,
            outcome_space_exhaustive=report.outcome_space_exhaustive,
            outcome_space_exact=report.outcome_space_exact,
            outcome_authority_sha256s=report.outcome_authority_sha256s,
            _authority_verification_token=_VERIFIED_TERMINAL_AUTHORITY_TOKEN,
        )
        if not proof.outcome_space_exhaustive:
            return (
                None,
                "authoritative terminal outcome space is not exhaustive",
            )
        if outcome_independent:
            if not proof.outcome_space_exact:
                return (
                    None,
                    "authoritative terminal outcome space is exhaustive but not exact",
                )
            if not proof.worst_proven:
                return (
                    None,
                    "outcome-independent terminal minimum is not exactly proven",
                )
            if proof.worst_terminal_profit <= Decimal("0"):
                return (
                    None,
                    "outcome-independent exact minimum terminal net P&L is not positive",
                )
        return proof, None

    try:
        report = ScenarioSearchEngine().analyse(
            tickets,
            list(evidence.scenario_groups),
        )
    except (ArithmeticError, TypeError, ValueError) as exc:
        return (
            None,
            f"verified terminal-state model is not executable: {exc}",
        )

    proof = VerifiedTerminalEconomics(
        completeness_evidence=evidence,
        report_mode=report.mode,
        total_states=report.total_states,
        worst_terminal_profit=report.observed_worst,
        best_terminal_profit=report.observed_best,
        worst_proven=report.worst_proven,
        best_proven=report.best_proven,
    )
    if outcome_independent:
        if not proof.worst_proven:
            return (
                None,
                "outcome-independent terminal minimum is not exactly proven",
            )
        if proof.worst_terminal_profit <= Decimal("0"):
            return (
                None,
                "outcome-independent exact minimum terminal net P&L is not positive",
            )
    return proof, None


def build_portfolio_plan(
    book: PaperBook,
    intents: tuple[OpportunityIntent, ...],
    risk_policy: PaperRiskPolicy,
    decision_ts: str,
    *,
    portfolio_truth: EvidenceTruth = EvidenceTruth.EXACT,
    dependency_graph: PortfolioDependencyGraph | None,
    risk_of_ruin_vector_evidence: RiskOfRuinVectorEvidence | None = None,
    terminal_state_evidence: TerminalStateCompletenessEvidence | None = None,
    market_outcome_authorities: tuple[
        MarketSettlementOutcomeAuthority, ...
    ] = (),
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
    if dependency_graph is not None and not isinstance(
        dependency_graph, PortfolioDependencyGraph
    ):
        raise TypeError("dependency_graph must be PortfolioDependencyGraph")
    if terminal_state_evidence is not None and not isinstance(
        terminal_state_evidence, TerminalStateCompletenessEvidence
    ):
        raise TypeError(
            "terminal_state_evidence must be TerminalStateCompletenessEvidence"
        )
    if type(market_outcome_authorities) is not tuple:
        raise TypeError("market_outcome_authorities must be a tuple")
    if any(
        not isinstance(authority, MarketSettlementOutcomeAuthority)
        for authority in market_outcome_authorities
    ):
        raise TypeError(
            "market_outcome_authorities must contain MarketSettlementOutcomeAuthority values"
        )
    decision_ts, decision_time = _canonical_timestamp("decision_ts", decision_ts)

    portfolio_sha256 = risk_policy.risk_of_ruin_portfolio_sha256(book)
    if portfolio_sha256 is None:
        return _terminal_plan(
            decision_ts=decision_ts,
            action=PortfolioAction.WAIT,
            reason="canonical current portfolio identity cannot be proven",
            intents=intents,
            portfolio_sha256=None,
            dependency_graph=None,
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
            dependency_graph=None,
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
            dependency_graph=None,
            policy=risk_policy,
            portfolio_truth=portfolio_truth,
        )

    potential_positive = tuple(
        intent
        for intent in intents
        if intent.signal_strength > 0
        and intent.opportunity.decision is OpportunityDecision.ACTIONABLE
    )
    if potential_positive and portfolio_truth is not EvidenceTruth.EXACT:
        return _terminal_plan(
            decision_ts=decision_ts,
            action=PortfolioAction.WAIT,
            reason="positive portfolio action requires exact portfolio completeness truth",
            intents=intents,
            portfolio_sha256=portfolio_sha256,
            dependency_graph=None,
            policy=risk_policy,
            portfolio_truth=portfolio_truth,
        )
    if potential_positive and dependency_graph is None:
        return _terminal_plan(
            decision_ts=decision_ts,
            action=PortfolioAction.WAIT,
            reason="positive portfolio action requires dependency-graph evidence",
            intents=intents,
            portfolio_sha256=portfolio_sha256,
            dependency_graph=None,
            policy=risk_policy,
            portfolio_truth=portfolio_truth,
        )

    if dependency_graph is not None:
        expected_candidates = tuple(intent.candidate_sha256 for intent in intents)
        if (
            dependency_graph.portfolio_sha256 != portfolio_sha256
            or dependency_graph.intent_sha256s != intent_sha256s
            or dependency_graph.candidate_sha256s != expected_candidates
        ):
            return _terminal_plan(
                decision_ts=decision_ts,
                action=PortfolioAction.WAIT,
                reason="dependency graph does not bind exact portfolio and candidate inputs",
                intents=intents,
                portfolio_sha256=portfolio_sha256,
                dependency_graph=None,
                policy=risk_policy,
                portfolio_truth=portfolio_truth,
            )

    allocation_signals: list[Decimal] = []
    rejected: list[tuple[str, str]] = []
    has_canonical_zero = False
    for intent in intents:
        if intent.opportunity.decision is OpportunityDecision.ZERO:
            has_canonical_zero = True
            allocation_signals.append(Decimal("0"))
            rejected.append((intent.intent_id, "canonical opportunity decision is ZERO"))
            continue
        reason = _intent_preflight_reason(
            intent,
            decision_time,
            authoritative_terminal_model=bool(market_outcome_authorities),
        )
        if reason is not None:
            allocation_signals.append(Decimal("0"))
            rejected.append((intent.intent_id, reason))
            continue
        allocation_signals.append(
            intent.signal_strength
            if intent.opportunity.decision is OpportunityDecision.ACTIONABLE
            else Decimal("0")
        )

    if not any(signal > 0 for signal in allocation_signals):
        if rejected:
            if has_canonical_zero and all(
                intent.opportunity.decision is OpportunityDecision.ZERO
                or intent.signal_strength <= 0
                for intent in intents
            ):
                action = PortfolioAction.ZERO
                reason = "canonical opportunity decision is ZERO"
            else:
                action = PortfolioAction.WAIT
                reason = "no safe actionable opportunity remains after preflight: " + "; ".join(
                    f"{intent_id}={rejection}" for intent_id, rejection in rejected
                )
            return _terminal_plan(
                decision_ts=decision_ts,
                action=action,
                reason=reason,
                intents=intents,
                portfolio_sha256=portfolio_sha256,
                dependency_graph=dependency_graph,
                policy=risk_policy,
                portfolio_truth=portfolio_truth,
            )

    positive_candidates = {
        intent.candidate_sha256
        for intent, signal in zip(intents, allocation_signals, strict=True)
        if signal > 0
    }
    # The current canonical RiskPolicy has no authority that proves an exhaustive
    # dependency/correlation graph.  A caller-supplied edge list therefore cannot
    # prove *absence* of an omitted dependency.  Until the canonical scenario/dependency
    # authority is bound here, fail closed for every joint-positive vector and for
    # every new positive candidate evaluated alongside an already-open position.
    # This makes omission non-authoritative rather than treating an empty edge list
    # as evidence of independence.
    if len(positive_candidates) > 1 and terminal_state_evidence is None:
        return _terminal_plan(
            decision_ts=decision_ts,
            action=PortfolioAction.WAIT,
            reason=(
                "multiple positive candidates require complete canonical joint-dependency "
                "proof; caller-supplied dependency edges cannot prove omitted correlations absent"
            ),
            intents=intents,
            portfolio_sha256=portfolio_sha256,
            dependency_graph=dependency_graph,
            policy=risk_policy,
            portfolio_truth=portfolio_truth,
        )
    if (
        positive_candidates
        and terminal_state_evidence is None
        and any(
            ticket.status is TicketStatus.OPEN
            for ticket in book.tickets.values()
        )
    ):
        return _terminal_plan(
            decision_ts=decision_ts,
            action=PortfolioAction.WAIT,
            reason=(
                "positive candidate with existing open positions requires complete canonical "
                "current+proposed dependency proof"
            ),
            intents=intents,
            portfolio_sha256=portfolio_sha256,
            dependency_graph=dependency_graph,
            policy=risk_policy,
            portfolio_truth=portfolio_truth,
        )

    allocation = risk_policy.derive_goal_stake_vector(
        book,
        tuple(allocation_signals),
        contexts=tuple(intent.risk_context for intent in intents),
        risk_of_ruin_vector_evidence=risk_of_ruin_vector_evidence,
    )
    terminal_economics: VerifiedTerminalEconomics | None = None
    if allocation.action == "STAKE_VECTOR":
        actual_positive = tuple(
            intent
            for intent, stake in zip(
                intents,
                allocation.stakes,
                strict=True,
            )
            if stake > 0
        )
        needs_complete_dependency = (
            len(actual_positive) > 1
            or (
                bool(actual_positive)
                and any(
                    ticket.status is TicketStatus.OPEN
                    for ticket in book.tickets.values()
                )
            )
        )
        needs_terminal_economics = any(
            intent.opportunity_class
            in {
                StrategyClass.ARBITRAGE,
                StrategyClass.DUTCHING,
                StrategyClass.HEDGE_REBALANCE,
            }
            for intent in actual_positive
        )
        if needs_complete_dependency or needs_terminal_economics:
            if terminal_state_evidence is None:
                return _terminal_plan(
                    decision_ts=decision_ts,
                    action=PortfolioAction.WAIT,
                    reason=(
                        "positive portfolio vector requires typed verified terminal-state economics "
                        "and completeness for current+proposed positions"
                    ),
                    intents=intents,
                    portfolio_sha256=portfolio_sha256,
                    dependency_graph=dependency_graph,
                    policy=risk_policy,
                    portfolio_truth=portfolio_truth,
                )
            if dependency_graph is None:
                return _terminal_plan(
                    decision_ts=decision_ts,
                    action=PortfolioAction.WAIT,
                    reason=(
                        "verified terminal-state economics requires dependency graph"
                    ),
                    intents=intents,
                    portfolio_sha256=portfolio_sha256,
                    dependency_graph=None,
                    policy=risk_policy,
                    portfolio_truth=portfolio_truth,
                )
            terminal_economics, terminal_reason = _verify_terminal_economics(
                book,
                intents,
                allocation.stakes,
                dependency_graph,
                terminal_state_evidence,
                portfolio_sha256=portfolio_sha256,
                decision_ts=decision_ts,
                decision_time=decision_time,
                market_outcome_authorities=market_outcome_authorities,
            )
            if terminal_reason is not None:
                return _terminal_plan(
                    decision_ts=decision_ts,
                    action=PortfolioAction.WAIT,
                    reason=terminal_reason,
                    intents=intents,
                    portfolio_sha256=portfolio_sha256,
                    dependency_graph=dependency_graph,
                    policy=risk_policy,
                    portfolio_truth=portfolio_truth,
                )
            if not market_outcome_authorities:
                # External ScenarioGroups remain diagnostic only.  Positive
                # current+proposed decisions require canonical provider-scoped
                # market/settlement authority from market_outcomes.py.
                return _terminal_plan(
                    decision_ts=decision_ts,
                    action=PortfolioAction.WAIT,
                    reason=(
                        "positive whole-position or outcome-independent action requires "
                        "authoritative exhaustive market-outcome semantics; external "
                        "scenario groups cannot prove omitted real outcomes absent"
                    ),
                    intents=intents,
                    portfolio_sha256=portfolio_sha256,
                    dependency_graph=dependency_graph,
                    policy=risk_policy,
                    portfolio_truth=portfolio_truth,
                )
        positive_strategy_classes = {
            intent.opportunity.strategy_class
            for intent, stake in zip(intents, allocation.stakes, strict=True)
            if stake > 0
        }
        if StrategyClass.HEDGE_REBALANCE in positive_strategy_classes:
            action = PortfolioAction.HEDGE_REBALANCE
        elif positive_strategy_classes.intersection(
            {StrategyClass.ARBITRAGE, StrategyClass.DUTCHING}
        ):
            # Terminal economics here are canonical paper proof.  Even a typed
            # external execution witness does not grant irreversible bookmaker
            # authority, so arbitrage/dutching remains explicitly a PaperPlan seam.
            action = PortfolioAction.PAPER_PLAN
        else:
            action = PortfolioAction.STAKE_VECTOR
    else:
        action = {
            "WAIT": PortfolioAction.WAIT,
            "ZERO": PortfolioAction.ZERO,
        }[allocation.action]
    goal = risk_policy.economic_goal
    reason = allocation.reason
    if action in {
        PortfolioAction.STAKE_VECTOR,
        PortfolioAction.HEDGE_REBALANCE,
        PortfolioAction.PAPER_PLAN,
    } and rejected:
        reason += "; ineligible intents zeroed: " + "; ".join(
            f"{intent_id}={rejection}" for intent_id, rejection in rejected
        )
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
        dependency_graph=dependency_graph,
        terminal_economics=terminal_economics,
        economic_goal_contract_sha256=(
            provenance_for(goal).contract_sha256 if goal is not None else None
        ),
        risk_policy_sha256=risk_policy.provenance_sha256,
        portfolio_truth=portfolio_truth,
        reason=(
            reason
            + (
                ""
                if terminal_economics is None
                else (
                    "; canonical terminal economics verified with "
                    f"{terminal_economics.report_mode} worst-case P&L="
                    f"{terminal_economics.worst_terminal_profit}"
                    + (
                        ""
                        if not terminal_economics.outcome_authority_sha256s
                        else (
                            "; provider outcome space exhaustive="
                            f"{terminal_economics.outcome_space_exhaustive}, exact="
                            f"{terminal_economics.outcome_space_exact}"
                        )
                    )
                )
            )
            + (
                ""
                if action is not PortfolioAction.PAPER_PLAN
                else (
                    "; paper-only outcome-independent plan: no real-money or "
                    "irreversible bookmaker execution authority"
                )
            )
        ),
    )
