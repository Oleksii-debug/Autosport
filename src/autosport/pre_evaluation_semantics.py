"""Canonical pre-freeze semantics for complete evaluation-board members.

The denominator implementation intentionally lives elsewhere.  This module owns the
semantic evidence that a denominator consumer may use before freeze.  It accepts
canonical product objects (OpportunityIntent, PaperRiskPolicy, PaperBook and
PortfolioDependencyGraph) and derives row semantics; callers never supply a
``slot_state``, funnel stage, attrition reason, quote-set digest, risk-policy identity,
portfolio identity, economic-goal identity, cost-contract digest or dependency cluster.

The authority is deliberately conservative.  It can prove pre-execution eligibility,
but it never fabricates PAPER execution identity.  A slot that is otherwise eligible is
therefore emitted as ``ELIGIBLE/THEORETICAL_ONLY`` until a separately canonical
execution authority is consumed by the downstream evaluation-universe lineage.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Iterable, Mapping

from .economic_goal_provenance import provenance_for
from .opportunity import OpportunityDecision, QuoteRef
from .paper import PaperBook
from .portfolio_plan import OpportunityIntent, PortfolioDependencyGraph
from .pre_evaluation_binding import (
    BoundPreEvaluationSession,
    ProviderMemberIdentity,
)
from .risk import PaperRiskPolicy


SCHEMA_VERSION = 1
AUTHORITY_FAMILY = "research.pre-evaluation-row-semantics-v1"
_HEX = frozenset("0123456789abcdef")


class SemanticSlotState(StrEnum):
    CANDIDATE = "CANDIDATE"
    NO_EVENT = "NO_EVENT"
    NO_CANDIDATE = "NO_CANDIDATE"
    WAIT_ZERO = "WAIT_ZERO"


class SemanticFunnelStage(StrEnum):
    OBSERVED_SLOT = "OBSERVED_SLOT"
    DETECTED = "DETECTED"
    ELIGIBLE = "ELIGIBLE"


class SemanticAttritionReason(StrEnum):
    NO_EVENT = "NO_EVENT"
    NO_CANDIDATE = "NO_CANDIDATE"
    WAIT_ZERO = "WAIT_ZERO"
    INVALID_EVIDENCE = "INVALID_EVIDENCE"
    STALE_QUOTE = "STALE_QUOTE"
    RISK_REJECTED = "RISK_REJECTED"
    LIMIT_REJECTED = "LIMIT_REJECTED"
    BUDGET_REJECTED = "BUDGET_REJECTED"
    THEORETICAL_ONLY = "THEORETICAL_ONLY"
    INCOMPLETE_EVIDENCE = "INCOMPLETE_EVIDENCE"


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError("pre-evaluation semantic payload must be finite canonical JSON") from exc


def _digest(value: object) -> str:
    return sha256(_canonical_json(value)).hexdigest()


def _text(name: str, value: object) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be non-empty canonical text")
    value.encode("utf-8")
    return value


def _optional_text(name: str, value: object) -> str | None:
    return None if value is None else _text(name, value)


def _sha(name: str, value: object) -> str:
    raw = _text(name, value)
    if len(raw) != 64 or raw != raw.lower() or any(ch not in _HEX for ch in raw):
        raise ValueError(f"{name} must be canonical lowercase SHA-256 hex")
    return raw


def _optional_sha(name: str, value: object) -> str | None:
    return None if value is None else _sha(name, value)


def _nonnegative_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _instant(name: str, value: object) -> datetime:
    raw = _text(name, value)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _timestamp(name: str, value: object) -> str:
    return _instant(name, value).isoformat().replace("+00:00", "Z")


def _optional_timestamp(name: str, value: object) -> str | None:
    return None if value is None else _timestamp(name, value)


def _clusters(values: object) -> tuple[str, ...]:
    if type(values) is not tuple or not values:
        raise ValueError("dependence_cluster_keys must be a non-empty tuple")
    result = tuple(_text("dependence_cluster_key", value) for value in values)
    if result != tuple(sorted(result)) or len(result) != len(set(result)):
        raise ValueError("dependence_cluster_keys must be sorted and unique")
    return result


@dataclass(frozen=True, slots=True)
class PreEvaluationCostContract:
    """Versioned product cost ceiling used by the pre-evaluation authority."""

    contract_id: str
    max_cost_micros: int

    def __post_init__(self) -> None:
        _text("contract_id", self.contract_id)
        _nonnegative_int("max_cost_micros", self.max_cost_micros)

    @property
    def contract_sha256(self) -> str:
        return _digest(
            {
                "schema": "autosport.pre_evaluation_cost_contract",
                "schema_version": 1,
                "contract_id": self.contract_id,
                "max_cost_micros": self.max_cost_micros,
            }
        )


@dataclass(frozen=True, slots=True)
class ProviderSelectionBinding:
    """Exact provider-member identity plus the selection identity #638 can recheck.

    ``member_sha256`` and ``row_key`` are the authoritative complete-board handles.
    The downstream provider consumer must compare the remaining fields with the
    matching CompleteBoardMemberSpec before constructing an EvaluationRow.
    """

    row_key: str
    member_sha256: str
    event_id: str | None
    market_id: str | None
    selection_id: str | None
    source_id: str
    source_at: str

    def __post_init__(self) -> None:
        _text("row_key", self.row_key)
        object.__setattr__(
            self, "member_sha256", _sha("member_sha256", self.member_sha256)
        )
        _text("source_id", self.source_id)
        object.__setattr__(self, "source_at", _timestamp("source_at", self.source_at))
        selection = (self.event_id, self.market_id, self.selection_id)
        if all(value is None for value in selection):
            return
        if any(value is None for value in selection):
            raise ValueError("provider selection identity must be complete or entirely empty")
        for name, value in zip(
            ("event_id", "market_id", "selection_id"), selection, strict=True
        ):
            _text(name, value)

    @property
    def identity(self) -> ProviderMemberIdentity:
        return ProviderMemberIdentity(
            row_key=self.row_key,
            member_sha256=self.member_sha256,
        )

    def authority_payload(self) -> dict[str, object]:
        """Fields allowed to influence semantic authority.

        source_at is retained as observational metadata for downstream comparison,
        but it is not allowed to mint a new semantic authority.
        """
        return {
            "row_key": self.row_key,
            "member_sha256": self.member_sha256,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "source_id": self.source_id,
        }

    @property
    def binding_sha256(self) -> str:
        return _digest(self.authority_payload())

    def to_payload(self) -> dict[str, object]:
        return {
            "row_key": self.row_key,
            "member_sha256": self.member_sha256,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "source_id": self.source_id,
            "source_at": self.source_at,
        }


@dataclass(frozen=True, slots=True)
class PreEvaluationSlotSemanticEvidence:
    """Immutable pre-freeze semantic evidence for one provider member."""

    row_key: str
    member_sha256: str
    provider_selection_sha256: str
    source_slot_evidence_digest: str
    slot_state: SemanticSlotState
    decision_stage: SemanticFunnelStage
    attrition_reason: SemanticAttritionReason
    detection_at: str | None
    decision_at: str | None
    quote_set_sha256: str | None
    freshness_policy_sha256: str
    strategy_version_id: str
    model_version_id: str | None
    config_sha256: str
    portfolio_before_id: str
    economic_goal_id: str
    risk_policy_id: str
    cost_contract_sha256: str
    dependence_cluster_keys: tuple[str, ...]
    opportunity_intent_sha256: str | None
    quote_identity_sha256: str | None

    def __post_init__(self) -> None:
        for name in (
            "row_key",
            "portfolio_before_id",
            "economic_goal_id",
            "risk_policy_id",
            "strategy_version_id",
        ):
            _text(name, getattr(self, name))
        for name in (
            "member_sha256",
            "provider_selection_sha256",
            "source_slot_evidence_digest",
            "freshness_policy_sha256",
            "config_sha256",
            "cost_contract_sha256",
        ):
            object.__setattr__(self, name, _sha(name, getattr(self, name)))
        object.__setattr__(
            self,
            "quote_set_sha256",
            _optional_sha("quote_set_sha256", self.quote_set_sha256),
        )
        object.__setattr__(
            self,
            "opportunity_intent_sha256",
            _optional_sha("opportunity_intent_sha256", self.opportunity_intent_sha256),
        )
        object.__setattr__(
            self,
            "quote_identity_sha256",
            _optional_sha("quote_identity_sha256", self.quote_identity_sha256),
        )
        _optional_text("model_version_id", self.model_version_id)
        object.__setattr__(
            self,
            "dependence_cluster_keys",
            _clusters(self.dependence_cluster_keys),
        )
        if not isinstance(self.slot_state, SemanticSlotState):
            raise TypeError("slot_state must be SemanticSlotState")
        if not isinstance(self.decision_stage, SemanticFunnelStage):
            raise TypeError("decision_stage must be SemanticFunnelStage")
        if not isinstance(self.attrition_reason, SemanticAttritionReason):
            raise TypeError("attrition_reason must be SemanticAttritionReason")
        detection = (
            None if self.detection_at is None else _instant("detection_at", self.detection_at)
        )
        decision = (
            None if self.decision_at is None else _instant("decision_at", self.decision_at)
        )
        if decision is not None and (detection is None or decision < detection):
            raise ValueError("decision_at requires detection_at and cannot precede it")
        object.__setattr__(
            self, "detection_at", _optional_timestamp("detection_at", self.detection_at)
        )
        object.__setattr__(
            self, "decision_at", _optional_timestamp("decision_at", self.decision_at)
        )

        if self.slot_state is SemanticSlotState.CANDIDATE:
            if self.decision_stage is SemanticFunnelStage.OBSERVED_SLOT:
                raise ValueError("candidate semantics must reach DETECTED or ELIGIBLE")
            if self.quote_set_sha256 is None or self.opportunity_intent_sha256 is None:
                raise ValueError("candidate semantics require canonical quote and intent evidence")
            if self.quote_identity_sha256 is None or detection is None:
                raise ValueError("candidate semantics require quote identity and detection time")
            if self.decision_stage is SemanticFunnelStage.ELIGIBLE and decision is None:
                raise ValueError("eligible candidate semantics require decision_at")
        else:
            if self.decision_stage is not SemanticFunnelStage.OBSERVED_SLOT:
                raise ValueError("coverage/zero semantics must remain at OBSERVED_SLOT")
            expected = {
                SemanticSlotState.NO_EVENT: SemanticAttritionReason.NO_EVENT,
                SemanticSlotState.NO_CANDIDATE: SemanticAttritionReason.NO_CANDIDATE,
                SemanticSlotState.WAIT_ZERO: SemanticAttritionReason.WAIT_ZERO,
            }[self.slot_state]
            if self.attrition_reason is not expected:
                raise ValueError(f"{self.slot_state.value} requires {expected.value}")
            if detection is not None or decision is not None:
                raise ValueError("coverage/zero semantics cannot claim detection/decision time")

    @property
    def evidence_digest(self) -> str:
        return _digest(self.to_payload())

    def to_payload(self) -> dict[str, object]:
        return {
            "row_key": self.row_key,
            "member_sha256": self.member_sha256,
            "provider_selection_sha256": self.provider_selection_sha256,
            "source_slot_evidence_digest": self.source_slot_evidence_digest,
            "slot_state": self.slot_state.value,
            "decision_stage": self.decision_stage.value,
            "attrition_reason": self.attrition_reason.value,
            "detection_at": self.detection_at,
            "decision_at": self.decision_at,
            "quote_set_sha256": self.quote_set_sha256,
            "freshness_policy_sha256": self.freshness_policy_sha256,
            "strategy_version_id": self.strategy_version_id,
            "model_version_id": self.model_version_id,
            "config_sha256": self.config_sha256,
            "portfolio_before_id": self.portfolio_before_id,
            "economic_goal_id": self.economic_goal_id,
            "risk_policy_id": self.risk_policy_id,
            "cost_contract_sha256": self.cost_contract_sha256,
            "dependence_cluster_keys": list(self.dependence_cluster_keys),
            "opportunity_intent_sha256": self.opportunity_intent_sha256,
            "quote_identity_sha256": self.quote_identity_sha256,
        }

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, object]
    ) -> "PreEvaluationSlotSemanticEvidence":
        expected = {
            "row_key",
            "member_sha256",
            "provider_selection_sha256",
            "source_slot_evidence_digest",
            "slot_state",
            "decision_stage",
            "attrition_reason",
            "detection_at",
            "decision_at",
            "quote_set_sha256",
            "freshness_policy_sha256",
            "strategy_version_id",
            "model_version_id",
            "config_sha256",
            "portfolio_before_id",
            "economic_goal_id",
            "risk_policy_id",
            "cost_contract_sha256",
            "dependence_cluster_keys",
            "opportunity_intent_sha256",
            "quote_identity_sha256",
        }
        if set(payload) != expected:
            raise ValueError("semantic slot payload has unexpected fields")
        clusters = payload["dependence_cluster_keys"]
        if type(clusters) is not list:
            raise ValueError("dependence_cluster_keys must be a list")
        try:
            return cls(
                row_key=_text("row_key", payload["row_key"]),
                member_sha256=_sha("member_sha256", payload["member_sha256"]),
                provider_selection_sha256=_sha(
                    "provider_selection_sha256", payload["provider_selection_sha256"]
                ),
                source_slot_evidence_digest=_sha(
                    "source_slot_evidence_digest", payload["source_slot_evidence_digest"]
                ),
                slot_state=SemanticSlotState(payload["slot_state"]),
                decision_stage=SemanticFunnelStage(payload["decision_stage"]),
                attrition_reason=SemanticAttritionReason(payload["attrition_reason"]),
                detection_at=_optional_text("detection_at", payload["detection_at"]),
                decision_at=_optional_text("decision_at", payload["decision_at"]),
                quote_set_sha256=_optional_sha(
                    "quote_set_sha256", payload["quote_set_sha256"]
                ),
                freshness_policy_sha256=_sha(
                    "freshness_policy_sha256", payload["freshness_policy_sha256"]
                ),
                strategy_version_id=_text(
                    "strategy_version_id", payload["strategy_version_id"]
                ),
                model_version_id=_optional_text(
                    "model_version_id", payload["model_version_id"]
                ),
                config_sha256=_sha("config_sha256", payload["config_sha256"]),
                portfolio_before_id=_text(
                    "portfolio_before_id", payload["portfolio_before_id"]
                ),
                economic_goal_id=_text("economic_goal_id", payload["economic_goal_id"]),
                risk_policy_id=_text("risk_policy_id", payload["risk_policy_id"]),
                cost_contract_sha256=_sha(
                    "cost_contract_sha256", payload["cost_contract_sha256"]
                ),
                dependence_cluster_keys=tuple(clusters),
                opportunity_intent_sha256=_optional_sha(
                    "opportunity_intent_sha256", payload["opportunity_intent_sha256"]
                ),
                quote_identity_sha256=_optional_sha(
                    "quote_identity_sha256", payload["quote_identity_sha256"]
                ),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("semantic slot payload is invalid") from exc


@dataclass(frozen=True, slots=True)
class PreEvaluationSemanticSession:
    """Root authority covering the exact denominator context and member set."""

    bound_authority_digest: str
    denominator_context_digest: str
    portfolio_sha256: str
    risk_policy_sha256: str
    economic_goal_identity: str
    cost_contract_sha256: str
    dependency_graph_sha256: str
    provider_bindings_sha256: str
    slots: tuple[PreEvaluationSlotSemanticEvidence, ...]

    def __post_init__(self) -> None:
        for name in (
            "bound_authority_digest",
            "denominator_context_digest",
            "portfolio_sha256",
            "risk_policy_sha256",
            "cost_contract_sha256",
            "dependency_graph_sha256",
            "provider_bindings_sha256",
        ):
            object.__setattr__(self, name, _sha(name, getattr(self, name)))
        _text("economic_goal_identity", self.economic_goal_identity)
        row_keys = tuple(slot.row_key for slot in self.slots)
        if row_keys != tuple(sorted(row_keys)):
            raise ValueError("semantic slots must be sorted by row_key")
        if len(row_keys) != len(set(row_keys)):
            raise ValueError("duplicate row_key in semantic session")

    @property
    def authority_digest(self) -> str:
        return _digest(self.to_payload())

    @property
    def authority_id(self) -> str:
        return f"pre-evaluation-semantics:{self.authority_digest[:32]}"

    def resolve_slot(self, row_key: str) -> PreEvaluationSlotSemanticEvidence:
        _text("row_key", row_key)
        for slot in self.slots:
            if slot.row_key == row_key:
                return slot
        raise KeyError(row_key)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "authority_family": AUTHORITY_FAMILY,
            "bound_authority_digest": self.bound_authority_digest,
            "denominator_context_digest": self.denominator_context_digest,
            "portfolio_sha256": self.portfolio_sha256,
            "risk_policy_sha256": self.risk_policy_sha256,
            "economic_goal_identity": self.economic_goal_identity,
            "cost_contract_sha256": self.cost_contract_sha256,
            "dependency_graph_sha256": self.dependency_graph_sha256,
            "provider_bindings_sha256": self.provider_bindings_sha256,
            "slots": [
                {**slot.to_payload(), "evidence_digest": slot.evidence_digest}
                for slot in self.slots
            ],
        }


class PreEvaluationSemanticAuthority:
    """Derive downstream row semantics only from typed canonical product evidence."""

    def __init__(self, cost_contract: PreEvaluationCostContract):
        if not isinstance(cost_contract, PreEvaluationCostContract):
            raise TypeError("cost_contract must be PreEvaluationCostContract")
        self._cost_contract = cost_contract

    @property
    def cost_contract(self) -> PreEvaluationCostContract:
        return self._cost_contract

    def derive_session(
        self,
        *,
        bound: BoundPreEvaluationSession,
        provider_selections: Iterable[ProviderSelectionBinding],
        intents: tuple[OpportunityIntent, ...],
        risk_policy: PaperRiskPolicy,
        book: PaperBook,
        dependency_graph: PortfolioDependencyGraph,
    ) -> PreEvaluationSemanticSession:
        if not isinstance(bound, BoundPreEvaluationSession):
            raise TypeError("bound must be BoundPreEvaluationSession")
        if type(intents) is not tuple or any(
            not isinstance(intent, OpportunityIntent) for intent in intents
        ):
            raise TypeError("intents must be a tuple of OpportunityIntent values")
        if len({intent.intent_sha256 for intent in intents}) != len(intents):
            raise ValueError("intents must have unique canonical intent identities")
        if not isinstance(risk_policy, PaperRiskPolicy):
            raise TypeError("risk_policy must be PaperRiskPolicy")
        if not isinstance(book, PaperBook):
            raise TypeError("book must be PaperBook")
        if not isinstance(dependency_graph, PortfolioDependencyGraph):
            raise TypeError("dependency_graph must be PortfolioDependencyGraph")

        portfolio_sha256 = PaperRiskPolicy.risk_of_ruin_portfolio_sha256(book)
        if portfolio_sha256 is None:
            raise ValueError("canonical portfolio identity cannot be proven")
        if dependency_graph.portfolio_sha256 != portfolio_sha256:
            raise ValueError("dependency graph does not bind the exact portfolio")
        if dependency_graph.intent_sha256s != tuple(
            intent.intent_sha256 for intent in intents
        ):
            raise ValueError("dependency graph does not bind the exact intent vector")
        if dependency_graph.candidate_sha256s != tuple(
            intent.candidate_sha256 for intent in intents
        ):
            raise ValueError("dependency graph does not bind the exact candidate vector")

        provider_by_key: dict[str, ProviderSelectionBinding] = {}
        for provider in provider_selections:
            if not isinstance(provider, ProviderSelectionBinding):
                raise TypeError("provider_selections must contain ProviderSelectionBinding")
            if provider.row_key in provider_by_key:
                raise ValueError(f"duplicate provider row_key: {provider.row_key}")
            provider_by_key[provider.row_key] = provider
        expected_members = {
            member.row_key: member.member_sha256 for member in bound.members
        }
        actual_members = {
            row_key: provider.member_sha256
            for row_key, provider in provider_by_key.items()
        }
        if actual_members != expected_members:
            raise ValueError(
                "provider semantic bindings must exactly equal denominator-bound member identities"
            )

        provider_bindings_sha256 = _digest(
            [
                provider_by_key[row_key].authority_payload()
                for row_key in sorted(provider_by_key)
            ]
        )
        risk_policy_sha256 = risk_policy.provenance_sha256
        goal = risk_policy.economic_goal
        if goal is None:
            economic_goal_identity = f"none:{risk_policy_sha256}"
        else:
            economic_goal_identity = provenance_for(goal).decision_identity

        slots: list[PreEvaluationSlotSemanticEvidence] = []
        for row_key in sorted(provider_by_key):
            provider = provider_by_key[row_key]
            # The legacy slot/facts layer is membership/replay evidence only.
            # It is deliberately not authority for config, freshness, risk, cost,
            # provider metadata, or positive-intent semantics.
            bound.resolve_slot(row_key)
            matching = self._matching_intents(provider, intents)
            if len(matching) > 1:
                raise ValueError(
                    f"provider member {row_key} maps to multiple canonical intents"
                )
            intent = None if not matching else matching[0]
            slots.append(
                self._derive_slot(
                    bound=bound,
                    provider=provider,
                    intent=intent,
                    risk_policy=risk_policy,
                    book=book,
                    dependency_graph=dependency_graph,
                    portfolio_sha256=portfolio_sha256,
                    economic_goal_identity=economic_goal_identity,
                )
            )

        return PreEvaluationSemanticSession(
            bound_authority_digest=self._semantic_bound_authority_digest(bound),
            denominator_context_digest=bound.context.digest,
            portfolio_sha256=portfolio_sha256,
            risk_policy_sha256=risk_policy_sha256,
            economic_goal_identity=economic_goal_identity,
            cost_contract_sha256=self._cost_contract.contract_sha256,
            dependency_graph_sha256=dependency_graph.graph_sha256,
            provider_bindings_sha256=provider_bindings_sha256,
            slots=tuple(slots),
        )

    @staticmethod
    def _matching_intents(
        provider: ProviderSelectionBinding,
        intents: tuple[OpportunityIntent, ...],
    ) -> tuple[OpportunityIntent, ...]:
        if provider.event_id is None:
            return ()
        target = (
            provider.event_id,
            provider.market_id,
            provider.selection_id,
            provider.source_id,
        )
        matched: list[OpportunityIntent] = []
        for intent in intents:
            quote_matches = [
                quote
                for quote in intent.opportunity.quotes
                if (
                    quote.event_id,
                    quote.market_id,
                    quote.selection_id,
                    quote.source_id,
                )
                == target
            ]
            if quote_matches:
                if len(quote_matches) != 1:
                    raise ValueError("canonical intent has ambiguous provider selection identity")
                matched.append(intent)
        return tuple(matched)

    def _derive_slot(
        self,
        *,
        bound: BoundPreEvaluationSession,
        provider: ProviderSelectionBinding,
        intent: OpportunityIntent | None,
        risk_policy: PaperRiskPolicy,
        book: PaperBook,
        dependency_graph: PortfolioDependencyGraph,
        portfolio_sha256: str,
        economic_goal_identity: str,
    ) -> PreEvaluationSlotSemanticEvidence:
        bound.resolve_slot(provider.row_key)
        source_slot_digest = self._semantic_source_digest(
            bound=bound,
            provider=provider,
            intent=intent,
        )
        freshness_policy_sha256 = self._freshness_policy_sha256(risk_policy)
        coverage_config_sha256 = self._coverage_config_sha256(bound)
        session_strategy = "pre-evaluation-coverage:v1"
        portfolio_before_id = f"paper-book:{portfolio_sha256}"
        risk_policy_id = f"paper-risk-policy:{risk_policy.provenance_sha256}"

        if provider.event_id is None:
            return PreEvaluationSlotSemanticEvidence(
                row_key=provider.row_key,
                member_sha256=provider.member_sha256,
                provider_selection_sha256=provider.binding_sha256,
                source_slot_evidence_digest=source_slot_digest,
                slot_state=SemanticSlotState.NO_EVENT,
                decision_stage=SemanticFunnelStage.OBSERVED_SLOT,
                attrition_reason=SemanticAttritionReason.NO_EVENT,
                detection_at=None,
                decision_at=None,
                quote_set_sha256=None,
                freshness_policy_sha256=freshness_policy_sha256,
                strategy_version_id=session_strategy,
                model_version_id=None,
                config_sha256=coverage_config_sha256,
                portfolio_before_id=portfolio_before_id,
                economic_goal_id=economic_goal_identity,
                risk_policy_id=risk_policy_id,
                cost_contract_sha256=self._cost_contract.contract_sha256,
                dependence_cluster_keys=(self._coverage_cluster(provider),),
                opportunity_intent_sha256=None,
                quote_identity_sha256=None,
            )

        if intent is None:
            return PreEvaluationSlotSemanticEvidence(
                row_key=provider.row_key,
                member_sha256=provider.member_sha256,
                provider_selection_sha256=provider.binding_sha256,
                source_slot_evidence_digest=source_slot_digest,
                slot_state=SemanticSlotState.NO_CANDIDATE,
                decision_stage=SemanticFunnelStage.OBSERVED_SLOT,
                attrition_reason=SemanticAttritionReason.NO_CANDIDATE,
                detection_at=None,
                decision_at=None,
                quote_set_sha256=None,
                freshness_policy_sha256=freshness_policy_sha256,
                strategy_version_id=session_strategy,
                model_version_id=None,
                config_sha256=coverage_config_sha256,
                portfolio_before_id=portfolio_before_id,
                economic_goal_id=economic_goal_identity,
                risk_policy_id=risk_policy_id,
                cost_contract_sha256=self._cost_contract.contract_sha256,
                dependence_cluster_keys=(self._coverage_cluster(provider),),
                opportunity_intent_sha256=None,
                quote_identity_sha256=None,
            )

        quote_set_sha256 = self._quote_set_sha256(intent)
        quote_identity_sha256 = self._quote_identity_sha256(intent)
        cluster = self._intent_cluster(intent, dependency_graph)
        if intent.opportunity.decision in {
            OpportunityDecision.WAIT,
            OpportunityDecision.ZERO,
        }:
            return PreEvaluationSlotSemanticEvidence(
                row_key=provider.row_key,
                member_sha256=provider.member_sha256,
                provider_selection_sha256=provider.binding_sha256,
                source_slot_evidence_digest=source_slot_digest,
                slot_state=SemanticSlotState.WAIT_ZERO,
                decision_stage=SemanticFunnelStage.OBSERVED_SLOT,
                attrition_reason=SemanticAttritionReason.WAIT_ZERO,
                detection_at=None,
                decision_at=None,
                quote_set_sha256=quote_set_sha256,
                freshness_policy_sha256=freshness_policy_sha256,
                strategy_version_id=intent.strategy_id,
                model_version_id=intent.model_id,
                config_sha256=intent.config_sha256,
                portfolio_before_id=portfolio_before_id,
                economic_goal_id=economic_goal_identity,
                risk_policy_id=risk_policy_id,
                cost_contract_sha256=self._cost_contract.contract_sha256,
                dependence_cluster_keys=(cluster,),
                opportunity_intent_sha256=intent.intent_sha256,
                quote_identity_sha256=quote_identity_sha256,
            )

        if intent.opportunity.decision is not OpportunityDecision.ACTIONABLE:
            raise ValueError("unsupported OpportunityDecision")
        detection_at = _timestamp("opportunity observed_at", intent.evidence.observed_at)
        proposal_ts = intent.risk_context.proposal_ts
        if proposal_ts is None:
            return self._candidate_attrition(
                bound=bound,
                provider=provider,
                source_slot_digest=source_slot_digest,
                intent=intent,
                portfolio_before_id=portfolio_before_id,
                economic_goal_identity=economic_goal_identity,
                risk_policy_id=risk_policy_id,
                cluster=cluster,
                quote_set_sha256=quote_set_sha256,
                quote_identity_sha256=quote_identity_sha256,
                detection_at=detection_at,
                freshness_policy_sha256=freshness_policy_sha256,
                reason=SemanticAttritionReason.INCOMPLETE_EVIDENCE,
            )
        decision_at = _timestamp("proposal_ts", proposal_ts)
        if _instant("proposal_ts", decision_at) < _instant("detection_at", detection_at):
            return self._candidate_attrition(
                bound=bound,
                provider=provider,
                source_slot_digest=source_slot_digest,
                intent=intent,
                portfolio_before_id=portfolio_before_id,
                economic_goal_identity=economic_goal_identity,
                risk_policy_id=risk_policy_id,
                cluster=cluster,
                quote_set_sha256=quote_set_sha256,
                quote_identity_sha256=quote_identity_sha256,
                detection_at=detection_at,
                freshness_policy_sha256=freshness_policy_sha256,
                reason=SemanticAttritionReason.INVALID_EVIDENCE,
            )

        reason = self._canonical_freshness_reason(intent, risk_policy)
        if reason is not None:
            return self._candidate_attrition(
                bound=bound,
                provider=provider,
                source_slot_digest=source_slot_digest,
                intent=intent,
                portfolio_before_id=portfolio_before_id,
                economic_goal_identity=economic_goal_identity,
                risk_policy_id=risk_policy_id,
                cluster=cluster,
                quote_set_sha256=quote_set_sha256,
                quote_identity_sha256=quote_identity_sha256,
                detection_at=detection_at,
                freshness_policy_sha256=freshness_policy_sha256,
                reason=reason,
            )

        if risk_policy.economic_goal is None:
            risk_reason = SemanticAttritionReason.INCOMPLETE_EVIDENCE
        else:
            stake = risk_policy.derive_goal_stake(
                book,
                intent.signal_strength,
                context=intent.risk_context,
            )
            if stake is None:
                risk_reason = SemanticAttritionReason.RISK_REJECTED
            else:
                decision = risk_policy.evaluate(
                    book,
                    stake,
                    context=intent.risk_context,
                )
                risk_reason = (
                    None
                    if decision.allowed
                    else SemanticAttritionReason.RISK_REJECTED
                )
        if risk_reason is not None:
            return self._candidate_attrition(
                bound=bound,
                provider=provider,
                source_slot_digest=source_slot_digest,
                intent=intent,
                portfolio_before_id=portfolio_before_id,
                economic_goal_identity=economic_goal_identity,
                risk_policy_id=risk_policy_id,
                cluster=cluster,
                quote_set_sha256=quote_set_sha256,
                quote_identity_sha256=quote_identity_sha256,
                detection_at=detection_at,
                freshness_policy_sha256=freshness_policy_sha256,
                reason=risk_reason,
            )

        return PreEvaluationSlotSemanticEvidence(
            row_key=provider.row_key,
            member_sha256=provider.member_sha256,
            provider_selection_sha256=provider.binding_sha256,
            source_slot_evidence_digest=source_slot_digest,
            slot_state=SemanticSlotState.CANDIDATE,
            decision_stage=SemanticFunnelStage.ELIGIBLE,
            attrition_reason=SemanticAttritionReason.THEORETICAL_ONLY,
            detection_at=detection_at,
            decision_at=decision_at,
            quote_set_sha256=quote_set_sha256,
            freshness_policy_sha256=freshness_policy_sha256,
            strategy_version_id=intent.strategy_id,
            model_version_id=intent.model_id,
            config_sha256=intent.config_sha256,
            portfolio_before_id=portfolio_before_id,
            economic_goal_id=economic_goal_identity,
            risk_policy_id=risk_policy_id,
            cost_contract_sha256=self._cost_contract.contract_sha256,
            dependence_cluster_keys=(cluster,),
            opportunity_intent_sha256=intent.intent_sha256,
            quote_identity_sha256=quote_identity_sha256,
        )

    def _candidate_attrition(
        self,
        *,
        bound: BoundPreEvaluationSession,
        provider: ProviderSelectionBinding,
        source_slot_digest: str,
        intent: OpportunityIntent,
        portfolio_before_id: str,
        economic_goal_identity: str,
        risk_policy_id: str,
        cluster: str,
        quote_set_sha256: str,
        quote_identity_sha256: str,
        detection_at: str,
        freshness_policy_sha256: str,
        reason: SemanticAttritionReason,
    ) -> PreEvaluationSlotSemanticEvidence:
        return PreEvaluationSlotSemanticEvidence(
            row_key=provider.row_key,
            member_sha256=provider.member_sha256,
            provider_selection_sha256=provider.binding_sha256,
            source_slot_evidence_digest=source_slot_digest,
            slot_state=SemanticSlotState.CANDIDATE,
            decision_stage=SemanticFunnelStage.DETECTED,
            attrition_reason=reason,
            detection_at=detection_at,
            decision_at=None,
            quote_set_sha256=quote_set_sha256,
            freshness_policy_sha256=freshness_policy_sha256,
            strategy_version_id=intent.strategy_id,
            model_version_id=intent.model_id,
            config_sha256=intent.config_sha256,
            portfolio_before_id=portfolio_before_id,
            economic_goal_id=economic_goal_identity,
            risk_policy_id=risk_policy_id,
            cost_contract_sha256=self._cost_contract.contract_sha256,
            dependence_cluster_keys=(cluster,),
            opportunity_intent_sha256=intent.intent_sha256,
            quote_identity_sha256=quote_identity_sha256,
        )

    @staticmethod
    def _semantic_bound_authority_digest(bound: BoundPreEvaluationSession) -> str:
        return _digest(
            {
                "schema": "autosport.pre_evaluation_semantic_bound_projection",
                "schema_version": 1,
                "context_digest": bound.context.digest,
                "members": [
                    {
                        "row_key": member.row_key,
                        "member_sha256": member.member_sha256,
                    }
                    for member in bound.members
                ],
            }
        )

    @staticmethod
    def _semantic_source_digest(
        *,
        bound: BoundPreEvaluationSession,
        provider: ProviderSelectionBinding,
        intent: OpportunityIntent | None,
    ) -> str:
        return _digest(
            {
                "schema": "autosport.pre_evaluation_semantic_source",
                "schema_version": 1,
                "context_digest": bound.context.digest,
                "row_key": provider.row_key,
                "member_sha256": provider.member_sha256,
                "provider_selection_sha256": provider.binding_sha256,
                "opportunity_intent_sha256": (
                    None if intent is None else intent.intent_sha256
                ),
            }
        )

    @staticmethod
    def _coverage_config_sha256(bound: BoundPreEvaluationSession) -> str:
        return _digest(
            {
                "schema": "autosport.pre_evaluation_coverage_config",
                "schema_version": 1,
                "research_protocol_id": bound.context.research_protocol_id,
                "protocol_sha256": bound.context.protocol_sha256,
            }
        )

    @staticmethod
    def _freshness_policy_sha256(risk_policy: PaperRiskPolicy) -> str:
        goal = risk_policy.economic_goal
        return _digest(
            {
                "schema": "autosport.pre_evaluation_freshness_policy",
                "schema_version": 1,
                "risk_policy_sha256": risk_policy.provenance_sha256,
                "max_quote_age_seconds": (
                    None if goal is None else str(goal.max_quote_age_seconds)
                ),
            }
        )

    @staticmethod
    def _canonical_freshness_reason(
        intent: OpportunityIntent,
        risk_policy: PaperRiskPolicy,
    ) -> SemanticAttritionReason | None:
        goal = risk_policy.economic_goal
        if goal is None:
            return SemanticAttritionReason.INCOMPLETE_EVIDENCE
        proposal_ts = intent.risk_context.proposal_ts
        if proposal_ts is None:
            return SemanticAttritionReason.INCOMPLETE_EVIDENCE
        proposal_time = _instant("proposal_ts", proposal_ts)
        for quote in intent.risk_context.quotes:
            quote_ts = quote.source_ts if quote.source_ts is not None else quote.observed_ts
            quote_time = _instant("quote timestamp", quote_ts)
            delta = proposal_time - quote_time
            if delta.days < 0:
                return SemanticAttritionReason.INVALID_EVIDENCE
            age_seconds = Decimal(delta.days * 86400 + delta.seconds) + (
                Decimal(delta.microseconds) / Decimal("1000000")
            )
            if age_seconds > goal.max_quote_age_seconds:
                return SemanticAttritionReason.STALE_QUOTE
        return None

    @staticmethod
    def _quote_set_sha256(intent: OpportunityIntent) -> str:
        return _digest(
            [quote.to_dict() for quote in intent.opportunity.quotes]
        )

    @staticmethod
    def _quote_identity_sha256(intent: OpportunityIntent) -> str:
        return _digest(
            [
                {
                    "event_id": quote.event_id,
                    "market_id": quote.market_id,
                    "selection_id": quote.selection_id,
                    "source_id": quote.source_id,
                    "sequence": quote.sequence,
                    "market_event_hash": quote.market_event_hash,
                    "market_snapshot_hash": quote.market_snapshot_hash,
                }
                for quote in intent.opportunity.quotes
            ]
        )

    @staticmethod
    def _coverage_cluster(provider: ProviderSelectionBinding) -> str:
        return f"coverage:{_digest({'member_sha256': provider.member_sha256, 'row_key': provider.row_key})}"

    @staticmethod
    def _intent_cluster(
        intent: OpportunityIntent,
        graph: PortfolioDependencyGraph,
    ) -> str:
        candidate = intent.candidate_sha256
        if candidate not in graph.candidate_sha256s:
            raise ValueError("intent candidate is absent from dependency graph")
        adjacency: dict[str, set[str]] = {
            item: set() for item in graph.candidate_sha256s
        }
        for left, right in graph.dependency_edges:
            adjacency[left].add(right)
            adjacency[right].add(left)
        pending = [candidate]
        connected: set[str] = set()
        while pending:
            current = pending.pop()
            if current in connected:
                continue
            connected.add(current)
            pending.extend(adjacency[current] - connected)
        return f"dependency:{_digest({'graph_sha256': graph.graph_sha256, 'members': sorted(connected)})}"


class PreEvaluationSemanticStore:
    """Immutable store; restart must replay the exact semantic root."""

    def __init__(self, path: str | os.PathLike[str]):
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def save(self, session: PreEvaluationSemanticSession) -> None:
        if not isinstance(session, PreEvaluationSemanticSession):
            raise TypeError("session must be PreEvaluationSemanticSession")
        if self._path.exists():
            current = self.load()
            if current.authority_digest == session.authority_digest:
                return
            raise ValueError("semantic evidence already exists with conflicting authority")
        envelope = {
            "schema_version": SCHEMA_VERSION,
            "authority_family": AUTHORITY_FAMILY,
            "authority_id": session.authority_id,
            "authority_digest": session.authority_digest,
            "payload": session.to_payload(),
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f".{self._path.name}.tmp")
        with temporary.open("wb") as handle:
            handle.write(_canonical_json(envelope) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self._path)

    def load(self) -> PreEvaluationSemanticSession:
        try:
            envelope = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("invalid pre-evaluation semantic evidence file") from exc
        if type(envelope) is not dict or set(envelope) != {
            "schema_version",
            "authority_family",
            "authority_id",
            "authority_digest",
            "payload",
        }:
            raise ValueError("semantic evidence envelope has unexpected fields")
        if envelope["schema_version"] != SCHEMA_VERSION:
            raise ValueError("unsupported semantic evidence schema")
        if envelope["authority_family"] != AUTHORITY_FAMILY:
            raise ValueError("unexpected semantic evidence authority family")
        payload = envelope["payload"]
        if type(payload) is not dict:
            raise ValueError("semantic evidence payload must be an object")
        session = self._from_payload(payload)
        if session.to_payload() != payload:
            raise ValueError("semantic evidence replay mismatch")
        if envelope["authority_digest"] != session.authority_digest:
            raise ValueError("semantic evidence digest mismatch")
        if envelope["authority_id"] != session.authority_id:
            raise ValueError("semantic evidence authority id mismatch")
        return session

    def load_expected(
        self, expected: PreEvaluationSemanticSession
    ) -> PreEvaluationSemanticSession:
        if not isinstance(expected, PreEvaluationSemanticSession):
            raise TypeError("expected must be PreEvaluationSemanticSession")
        current = self.load()
        if current.authority_digest != expected.authority_digest:
            raise ValueError("durable semantic evidence does not match re-resolved authority")
        return current

    @staticmethod
    def _from_payload(payload: Mapping[str, object]) -> PreEvaluationSemanticSession:
        expected = {
            "schema_version",
            "authority_family",
            "bound_authority_digest",
            "denominator_context_digest",
            "portfolio_sha256",
            "risk_policy_sha256",
            "economic_goal_identity",
            "cost_contract_sha256",
            "dependency_graph_sha256",
            "provider_bindings_sha256",
            "slots",
        }
        if set(payload) != expected:
            raise ValueError("semantic session payload has unexpected fields")
        if payload["schema_version"] != SCHEMA_VERSION:
            raise ValueError("unsupported semantic session schema")
        if payload["authority_family"] != AUTHORITY_FAMILY:
            raise ValueError("unexpected semantic session authority family")
        raw_slots = payload["slots"]
        if type(raw_slots) is not list:
            raise ValueError("semantic slots must be a list")
        slots: list[PreEvaluationSlotSemanticEvidence] = []
        for raw in raw_slots:
            if type(raw) is not dict or "evidence_digest" not in raw:
                raise ValueError("semantic slot entry is invalid")
            item = dict(raw)
            claimed_digest = item.pop("evidence_digest")
            slot = PreEvaluationSlotSemanticEvidence.from_payload(item)
            if claimed_digest != slot.evidence_digest:
                raise ValueError("semantic slot evidence digest mismatch")
            slots.append(slot)
        return PreEvaluationSemanticSession(
            bound_authority_digest=_sha(
                "bound_authority_digest", payload["bound_authority_digest"]
            ),
            denominator_context_digest=_sha(
                "denominator_context_digest", payload["denominator_context_digest"]
            ),
            portfolio_sha256=_sha("portfolio_sha256", payload["portfolio_sha256"]),
            risk_policy_sha256=_sha(
                "risk_policy_sha256", payload["risk_policy_sha256"]
            ),
            economic_goal_identity=_text(
                "economic_goal_identity", payload["economic_goal_identity"]
            ),
            cost_contract_sha256=_sha(
                "cost_contract_sha256", payload["cost_contract_sha256"]
            ),
            dependency_graph_sha256=_sha(
                "dependency_graph_sha256", payload["dependency_graph_sha256"]
            ),
            provider_bindings_sha256=_sha(
                "provider_bindings_sha256", payload["provider_bindings_sha256"]
            ),
            slots=tuple(slots),
        )
