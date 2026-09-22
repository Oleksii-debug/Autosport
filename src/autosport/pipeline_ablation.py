from __future__ import annotations

import hashlib
import itertools
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from enum import StrEnum
from math import factorial
from typing import Iterable, Mapping, Sequence

_HEX = frozenset("0123456789abcdef")


class PipelineComponent(StrEnum):
    DATA = "DATA"
    MODEL = "MODEL"
    THRESHOLD_SELECTION = "THRESHOLD_SELECTION"
    SIZING = "SIZING"
    EXECUTION = "EXECUTION"


class IdentifiabilityTier(StrEnum):
    FACTUAL_MECHANICAL = "FACTUAL_MECHANICAL"
    FROZEN_REPLAY_COUNTERFACTUAL = "FROZEN_REPLAY_COUNTERFACTUAL"
    SIMULATED_COUNTERFACTUAL = "SIMULATED_COUNTERFACTUAL"
    FORWARD_RANDOMIZED_OR_PAIRED = "FORWARD_RANDOMIZED_OR_PAIRED"
    NOT_IDENTIFIABLE = "NOT_IDENTIFIABLE"


class ClaimKind(StrEnum):
    FORECAST = "FORECAST"
    DECISION = "DECISION"
    ECONOMIC = "ECONOMIC"
    EXECUTION = "EXECUTION"


class MetricDirection(StrEnum):
    MAXIMIZE = "MAXIMIZE"
    MINIMIZE = "MINIMIZE"


class MetricSemantics(StrEnum):
    PROPER_FORECAST_SCORE = "PROPER_FORECAST_SCORE"
    CALIBRATION = "CALIBRATION"
    DECISION_QUALITY = "DECISION_QUALITY"
    ECONOMIC_UTILITY = "ECONOMIC_UTILITY"
    EXECUTION_DRAG = "EXECUTION_DRAG"


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty canonical string")
    value.encode("utf-8")
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(char not in _HEX for char in text):
        raise ValueError(f"{name} must be a canonical SHA-256 hex string")
    return text


def _opt_sha(value: object | None, name: str) -> str | None:
    return None if value is None else _sha(value, name)


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    if not text.endswith("Z"):
        raise ValueError(f"{name} must use canonical UTC-Z encoding")
    try:
        return datetime.fromisoformat(text[:-1] + "+00:00").astimezone(timezone.utc)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc


def _dec(value: object, name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")
    return value


def _dec_text(value: Decimal) -> str:
    text = format(_dec(value, "decimal"), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _digest(payload: Mapping[str, object]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ComponentBinding:
    component: PipelineComponent
    candidate_sha256: str
    baseline_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.component, PipelineComponent):
            raise ValueError("component must be PipelineComponent")
        object.__setattr__(self, "candidate_sha256", _sha(self.candidate_sha256, "candidate_sha256"))
        object.__setattr__(self, "baseline_sha256", _sha(self.baseline_sha256, "baseline_sha256"))
        if self.candidate_sha256 == self.baseline_sha256:
            raise ValueError("candidate and baseline component identities must differ")

    def canonical_payload(self) -> dict[str, object]:
        return {"component": self.component.value, "candidate_sha256": self.candidate_sha256, "baseline_sha256": self.baseline_sha256}


@dataclass(frozen=True, slots=True)
class AblationProtocol:
    protocol_id: str
    research_question_id: str
    hypothesis_id: str
    source_sha256: str
    candidate_id: str
    champion_id: str
    dataset_manifest_sha256: str
    holdout_access_sha256: str
    causal_cutoff: str
    evaluation_as_of: str
    scope_id: str
    component_bindings: tuple[ComponentBinding, ...]
    claim_kind: ClaimKind
    primary_metric: str
    metric_semantics: MetricSemantics
    metric_direction: MetricDirection
    practical_threshold: Decimal
    guardrail_sha256s: tuple[str, ...]
    uncertainty_method: str
    missing_outcome_policy: str
    multiplicity_rule: str
    created_at: str

    def __post_init__(self) -> None:
        for name in ("protocol_id", "research_question_id", "hypothesis_id", "candidate_id", "champion_id", "scope_id", "primary_metric", "uncertainty_method", "missing_outcome_policy", "multiplicity_rule"):
            _text(getattr(self, name), name)
        for name in ("source_sha256", "dataset_manifest_sha256", "holdout_access_sha256"):
            object.__setattr__(self, name, _sha(getattr(self, name), name))
        if _instant(self.causal_cutoff, "causal_cutoff") > _instant(self.evaluation_as_of, "evaluation_as_of"):
            raise ValueError("causal_cutoff must not follow evaluation_as_of")
        if _instant(self.created_at, "created_at") > _instant(self.evaluation_as_of, "evaluation_as_of"):
            raise ValueError("protocol created_at must not follow evaluation_as_of")
        if not isinstance(self.claim_kind, ClaimKind) or not isinstance(self.metric_semantics, MetricSemantics):
            raise ValueError("claim_kind and metric_semantics must use canonical enums")
        allowed = {
            ClaimKind.FORECAST: {MetricSemantics.PROPER_FORECAST_SCORE, MetricSemantics.CALIBRATION},
            ClaimKind.DECISION: {MetricSemantics.DECISION_QUALITY},
            ClaimKind.ECONOMIC: {MetricSemantics.ECONOMIC_UTILITY},
            ClaimKind.EXECUTION: {MetricSemantics.EXECUTION_DRAG},
        }
        if self.metric_semantics not in allowed[self.claim_kind]:
            raise ValueError("metric_semantics is incompatible with claim_kind")
        if not isinstance(self.metric_direction, MetricDirection):
            raise ValueError("metric_direction must be MetricDirection")
        _dec(self.practical_threshold, "practical_threshold")
        if type(self.component_bindings) is not tuple or not self.component_bindings or len(self.component_bindings) > 5:
            raise ValueError("component_bindings must contain one to five components")
        if any(not isinstance(item, ComponentBinding) for item in self.component_bindings):
            raise ValueError("component_bindings must contain ComponentBinding values")
        names = tuple(item.component.value for item in self.component_bindings)
        if names != tuple(sorted(names)) or len(names) != len(set(names)):
            raise ValueError("component_bindings must be sorted and unique")
        if type(self.guardrail_sha256s) is not tuple:
            raise ValueError("guardrail_sha256s must be a tuple")
        guardrails = tuple(_sha(value, "guardrail_sha256") for value in self.guardrail_sha256s)
        if guardrails != tuple(sorted(guardrails)) or len(guardrails) != len(set(guardrails)):
            raise ValueError("guardrail_sha256s must be sorted and unique")
        object.__setattr__(self, "guardrail_sha256s", guardrails)

    @property
    def components(self) -> tuple[PipelineComponent, ...]:
        return tuple(item.component for item in self.component_bindings)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1, "kind": "autosport-pipeline-ablation-protocol-v1", "protocol_id": self.protocol_id,
            "research_question_id": self.research_question_id, "hypothesis_id": self.hypothesis_id, "source_sha256": self.source_sha256,
            "candidate_id": self.candidate_id, "champion_id": self.champion_id, "dataset_manifest_sha256": self.dataset_manifest_sha256,
            "holdout_access_sha256": self.holdout_access_sha256, "causal_cutoff": self.causal_cutoff, "evaluation_as_of": self.evaluation_as_of,
            "scope_id": self.scope_id, "component_bindings": [item.canonical_payload() for item in self.component_bindings],
            "claim_kind": self.claim_kind.value, "primary_metric": self.primary_metric, "metric_semantics": self.metric_semantics.value,
            "metric_direction": self.metric_direction.value, "practical_threshold": _dec_text(self.practical_threshold),
            "guardrail_sha256s": list(self.guardrail_sha256s), "uncertainty_method": self.uncertainty_method,
            "missing_outcome_policy": self.missing_outcome_policy, "multiplicity_rule": self.multiplicity_rule, "created_at": self.created_at,
        }

    @property
    def protocol_sha256(self) -> str:
        return _digest(self.canonical_payload())


@dataclass(frozen=True, slots=True)
class AblationObservation:
    enabled_components: tuple[PipelineComponent, ...]
    metric_value: Decimal | None
    uncertainty_low: Decimal | None
    uncertainty_high: Decimal | None
    evidence_sha256: str
    factual_evidence_sha256: str | None
    evidence_available_at: str
    eligible_count: int
    selected_count: int
    resolved_count: int
    pending_count: int
    void_count: int
    missing_count: int
    applicable_costs_complete: bool = False
    execution_receipt_sha256: str | None = None
    simulator_sha256: str | None = None
    assignment_evidence_sha256: str | None = None
    replay_authority_sha256: str | None = None
    sizing_linearity_proven: bool = False
    zero_action_semantics_sha256: str | None = None
    assumptions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.enabled_components) is not tuple or any(not isinstance(item, PipelineComponent) for item in self.enabled_components):
            raise ValueError("enabled_components must be a tuple of PipelineComponent values")
        names = tuple(item.value for item in self.enabled_components)
        if names != tuple(sorted(names)) or len(names) != len(set(names)):
            raise ValueError("enabled_components must be sorted and unique")
        if self.metric_value is not None:
            _dec(self.metric_value, "metric_value")
        if (self.uncertainty_low is None) != (self.uncertainty_high is None) or (self.metric_value is not None and self.uncertainty_low is None):
            raise ValueError("evaluated metric requires complete uncertainty bounds")
        if self.uncertainty_low is not None:
            low, high = _dec(self.uncertainty_low, "uncertainty_low"), _dec(self.uncertainty_high, "uncertainty_high")
            if low > high or self.metric_value is None or not low <= self.metric_value <= high:
                raise ValueError("metric_value must lie inside a valid uncertainty interval")
        object.__setattr__(self, "evidence_sha256", _sha(self.evidence_sha256, "evidence_sha256"))
        _instant(self.evidence_available_at, "evidence_available_at")
        for name in ("eligible_count", "selected_count", "resolved_count", "pending_count", "void_count", "missing_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.selected_count > self.eligible_count or self.resolved_count + self.pending_count + self.void_count + self.missing_count != self.selected_count:
            raise ValueError("selected outcome counts must exactly cover selected_count within eligible_count")
        for name in ("factual_evidence_sha256", "execution_receipt_sha256", "simulator_sha256", "assignment_evidence_sha256", "replay_authority_sha256", "zero_action_semantics_sha256"):
            object.__setattr__(self, name, _opt_sha(getattr(self, name), name))
        if type(self.assumptions) is not tuple:
            raise ValueError("assumptions must be a tuple")
        assumptions = tuple(_text(item, "assumption") for item in self.assumptions)
        if assumptions != tuple(sorted(assumptions)) or len(assumptions) != len(set(assumptions)):
            raise ValueError("assumptions must be sorted and unique")
        object.__setattr__(self, "assumptions", assumptions)
        authorities = tuple(value for value in (self.factual_evidence_sha256, self.replay_authority_sha256, self.simulator_sha256, self.assignment_evidence_sha256) if value is not None)
        incomplete = self.pending_count > 0 or self.missing_count > 0
        if incomplete and (self.metric_value is not None or authorities):
            raise ValueError("pending/missing outcomes require no metric and no identifiability authority")
        if not incomplete and self.metric_value is not None and len(authorities) != 1:
            raise ValueError("evaluated observation requires exactly one identifiability authority")
        if self.metric_value is None and authorities:
            raise ValueError("observation without a metric cannot claim identifiability authority")
        if self.simulator_sha256 is not None and not self.assumptions:
            raise ValueError("simulated counterfactual requires explicit assumptions")

    @property
    def identifiability_tier(self) -> IdentifiabilityTier:
        if self.metric_value is None:
            return IdentifiabilityTier.NOT_IDENTIFIABLE
        if self.simulator_sha256 is not None:
            return IdentifiabilityTier.SIMULATED_COUNTERFACTUAL
        if self.replay_authority_sha256 is not None:
            return IdentifiabilityTier.FROZEN_REPLAY_COUNTERFACTUAL
        if self.assignment_evidence_sha256 is not None:
            return IdentifiabilityTier.FORWARD_RANDOMIZED_OR_PAIRED
        return IdentifiabilityTier.FACTUAL_MECHANICAL if self.factual_evidence_sha256 is not None else IdentifiabilityTier.NOT_IDENTIFIABLE

    def canonical_payload(self) -> dict[str, object]:
        return {
            "enabled_components": [item.value for item in self.enabled_components], "metric_value": None if self.metric_value is None else _dec_text(self.metric_value),
            "uncertainty_low": None if self.uncertainty_low is None else _dec_text(self.uncertainty_low), "uncertainty_high": None if self.uncertainty_high is None else _dec_text(self.uncertainty_high),
            "identifiability_tier": self.identifiability_tier.value, "evidence_sha256": self.evidence_sha256, "factual_evidence_sha256": self.factual_evidence_sha256,
            "evidence_available_at": self.evidence_available_at, "eligible_count": self.eligible_count, "selected_count": self.selected_count,
            "resolved_count": self.resolved_count, "pending_count": self.pending_count, "void_count": self.void_count, "missing_count": self.missing_count,
            "applicable_costs_complete": self.applicable_costs_complete, "execution_receipt_sha256": self.execution_receipt_sha256,
            "simulator_sha256": self.simulator_sha256, "assignment_evidence_sha256": self.assignment_evidence_sha256,
            "replay_authority_sha256": self.replay_authority_sha256, "sizing_linearity_proven": self.sizing_linearity_proven,
            "zero_action_semantics_sha256": self.zero_action_semantics_sha256, "assumptions": list(self.assumptions),
        }


@dataclass(frozen=True, slots=True)
class ComponentFinding:
    component: PipelineComponent
    identifiability_tier: IdentifiabilityTier
    contribution: Decimal | None
    contribution_low: Decimal | None
    contribution_high: Decimal | None
    reason: str
    evidence_sha256s: tuple[str, ...]

    def canonical_payload(self) -> dict[str, object]:
        return {"component": self.component.value, "identifiability_tier": self.identifiability_tier.value,
                "contribution": None if self.contribution is None else _dec_text(self.contribution),
                "contribution_low": None if self.contribution_low is None else _dec_text(self.contribution_low),
                "contribution_high": None if self.contribution_high is None else _dec_text(self.contribution_high),
                "reason": self.reason, "evidence_sha256s": list(self.evidence_sha256s)}


@dataclass(frozen=True, slots=True)
class AblationEvaluationEvidence:
    protocol_sha256: str
    complete_factorial: bool
    observations: tuple[AblationObservation, ...]
    findings: tuple[ComponentFinding, ...]
    missing_coalitions: tuple[tuple[PipelineComponent, ...], ...]
    total_effect: Decimal | None
    total_effect_low: Decimal | None
    total_effect_high: Decimal | None
    interaction_residual: Decimal | None

    def canonical_payload(self) -> dict[str, object]:
        return {"schema_version": 1, "kind": "autosport-pipeline-ablation-evaluation-v1", "protocol_sha256": self.protocol_sha256,
                "complete_factorial": self.complete_factorial, "observations": [item.canonical_payload() for item in self.observations],
                "findings": [item.canonical_payload() for item in self.findings],
                "missing_coalitions": [[component.value for component in coalition] for coalition in self.missing_coalitions],
                "total_effect": None if self.total_effect is None else _dec_text(self.total_effect),
                "total_effect_low": None if self.total_effect_low is None else _dec_text(self.total_effect_low),
                "total_effect_high": None if self.total_effect_high is None else _dec_text(self.total_effect_high),
                "interaction_residual": None if self.interaction_residual is None else _dec_text(self.interaction_residual),
                "truth": {"promotion_authority": False, "real_money_execution": False, "simulation_is_factual": False}}

    @property
    def evidence_sha256(self) -> str:
        return _digest(self.canonical_payload())


def _coalitions(components: Sequence[PipelineComponent]) -> tuple[tuple[PipelineComponent, ...], ...]:
    return tuple(tuple(items) for size in range(len(components) + 1) for items in itertools.combinations(components, size))


def _utility(protocol: AblationProtocol, value: Decimal) -> Decimal:
    return value if protocol.metric_direction is MetricDirection.MAXIMIZE else -value


def _interval(protocol: AblationProtocol, low: Decimal, high: Decimal) -> tuple[Decimal, Decimal]:
    return (low, high) if protocol.metric_direction is MetricDirection.MAXIMIZE else (-high, -low)


def _tier(tiers: Iterable[IdentifiabilityTier]) -> IdentifiabilityTier:
    values = frozenset(tiers)
    if not values or IdentifiabilityTier.NOT_IDENTIFIABLE in values:
        return IdentifiabilityTier.NOT_IDENTIFIABLE
    if len(values) == 1:
        return next(iter(values))
    if IdentifiabilityTier.SIMULATED_COUNTERFACTUAL in values:
        return IdentifiabilityTier.SIMULATED_COUNTERFACTUAL
    if IdentifiabilityTier.FROZEN_REPLAY_COUNTERFACTUAL in values:
        return IdentifiabilityTier.FROZEN_REPLAY_COUNTERFACTUAL
    return IdentifiabilityTier.NOT_IDENTIFIABLE


def evaluate_pipeline_ablation(protocol: AblationProtocol, observations: Sequence[AblationObservation]) -> AblationEvaluationEvidence:
    if not isinstance(protocol, AblationProtocol) or not observations:
        raise ValueError("ablation evaluation requires a canonical protocol and observations")
    allowed, by_enabled = frozenset(protocol.components), {}
    as_of = _instant(protocol.evaluation_as_of, "evaluation_as_of")
    for item in observations:
        if not isinstance(item, AblationObservation):
            raise TypeError("observations must contain AblationObservation values")
        if not set(item.enabled_components) <= allowed or item.enabled_components in by_enabled:
            raise ValueError("observation coalition is outside protocol or duplicated")
        if _instant(item.evidence_available_at, "evidence_available_at") > as_of:
            raise ValueError("future evidence cannot enter the frozen evaluation")
        if protocol.claim_kind is ClaimKind.ECONOMIC and item.metric_value is not None and not item.applicable_costs_complete:
            raise ValueError("economic metric requires complete applicable-cost evidence")
        if item.identifiability_tier is IdentifiabilityTier.FACTUAL_MECHANICAL and {PipelineComponent.DATA, PipelineComponent.MODEL, PipelineComponent.THRESHOLD_SELECTION}.intersection(item.enabled_components):
            raise ValueError("data/model/threshold interventions require replay, simulation, or prospective assignment authority")
        if protocol.claim_kind is ClaimKind.EXECUTION and item.identifiability_tier is IdentifiabilityTier.FACTUAL_MECHANICAL and item.metric_value is not None and item.execution_receipt_sha256 is None:
            raise ValueError("factual execution metric requires exact execution receipt")
        if PipelineComponent.SIZING in item.enabled_components and item.identifiability_tier is IdentifiabilityTier.FACTUAL_MECHANICAL and item.metric_value is not None and not item.sizing_linearity_proven:
            raise ValueError("factual sizing attribution requires proven linearity")
        if item.selected_count == 0 and item.metric_value is not None and item.zero_action_semantics_sha256 is None:
            raise ValueError("zero-action metric requires frozen WAIT/NO_BET semantics")
        by_enabled[item.enabled_components] = item

    expected = _coalitions(protocol.components)
    missing = tuple(key for key in expected if key not in by_enabled)
    complete = not missing and all(by_enabled[key].metric_value is not None for key in expected)
    ordered = tuple(by_enabled[key] for key in sorted(by_enabled, key=lambda key: tuple(item.value for item in key)))
    if complete and len({by_enabled[key].eligible_count for key in expected}) != 1:
        raise ValueError(
            "complete factorial requires one eligible universe denominator across all arms"
        )
    if not complete:
        evidence = tuple(sorted(item.evidence_sha256 for item in ordered))
        findings = tuple(ComponentFinding(component, IdentifiabilityTier.NOT_IDENTIFIABLE, None, None, None,
                                          "complete frozen factorial evidence is unavailable", evidence)
                         for component in protocol.components)
        return AblationEvaluationEvidence(protocol.protocol_sha256, False, ordered, findings, missing, None, None, None, None)

    utility = {key: _utility(protocol, by_enabled[key].metric_value) for key in expected}  # type: ignore[arg-type]
    intervals = {key: _interval(protocol, by_enabled[key].uncertainty_low, by_enabled[key].uncertainty_high) for key in expected}  # type: ignore[arg-type]
    empty, full = (), protocol.components
    total = utility[full] - utility[empty]
    total_low, total_high = intervals[full][0] - intervals[empty][1], intervals[full][1] - intervals[empty][0]
    n, findings = len(full), []
    with localcontext() as context:
        context.prec = 50
        denominator = Decimal(factorial(n))
        for component in full:
            value = low = high = Decimal(0)
            tiers, evidence = [], set()
            others = tuple(item for item in full if item != component)
            for coalition in _coalitions(others):
                with_component = tuple(sorted((*coalition, component), key=lambda item: item.value))
                weight = Decimal(factorial(len(coalition)) * factorial(n - len(coalition) - 1)) / denominator
                value += weight * (utility[with_component] - utility[coalition])
                low += weight * (intervals[with_component][0] - intervals[coalition][1])
                high += weight * (intervals[with_component][1] - intervals[coalition][0])
                tiers += [by_enabled[coalition].identifiability_tier, by_enabled[with_component].identifiability_tier]
                evidence.update((by_enabled[coalition].evidence_sha256, by_enabled[with_component].evidence_sha256))
            tier = _tier(tiers)
            identifiable = tier is not IdentifiabilityTier.NOT_IDENTIFIABLE
            findings.append(ComponentFinding(component, tier, value if identifiable else None, low if identifiable else None, high if identifiable else None,
                "Shapley allocation over the complete frozen factorial value function; allocation is protocol-defined and is not stronger than the weakest supporting evidence tier" if identifiable else "mixed evidence tiers prevent one unambiguous identifiability label",
                tuple(sorted(evidence))))
        one_at_a_time = sum((utility[full] - utility[tuple(item for item in full if item != component)] for component in full), Decimal(0))
    return AblationEvaluationEvidence(protocol.protocol_sha256, True, ordered, tuple(sorted(findings, key=lambda item: item.component.value)), (), total, total_low, total_high, total - one_at_a_time)


__all__ = ["AblationEvaluationEvidence", "AblationObservation", "AblationProtocol", "ClaimKind", "ComponentBinding", "ComponentFinding", "IdentifiabilityTier", "MetricDirection", "MetricSemantics", "PipelineComponent", "evaluate_pipeline_ablation"]
