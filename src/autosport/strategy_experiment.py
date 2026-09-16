from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from enum import StrEnum
from typing import Any, Iterable

from .strategy_comparison import StrategyRunEvidence


class ExperimentDecision(StrEnum):
    CHALLENGER_ELIGIBLE = "CHALLENGER_ELIGIBLE"
    RETAIN_CHAMPION = "RETAIN_CHAMPION"


_SUPPORTED_METRICS = {"net_profit", "roi", "final_balance"}


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _require_sha256(value: Any, field: str) -> str:
    text = _require_text(value, field).lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError(f"{field} must be a SHA-256 hex digest")
    return text


def _require_decimal(value: Any, field: str, *, minimum: Decimal | None = None) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite Decimal")
    try:
        decimal = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{field} must be a finite Decimal") from exc
    if not decimal.is_finite():
        raise ValueError(f"{field} must be a finite Decimal")
    if minimum is not None and decimal < minimum:
        raise ValueError(f"{field} must be >= {minimum}")
    return decimal


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    case_id: str
    dataset_name: str
    sport: str
    dataset_schema_version: int
    market_sha256: str
    sealed_results_sha256: str
    historical_import_identity: str | None
    replay_dataset_hash: str
    event_count: int
    initial_bankroll: Decimal

    def __post_init__(self) -> None:
        _require_text(self.case_id, "case_id")
        _require_text(self.dataset_name, "dataset_name")
        _require_text(self.sport, "sport")
        if type(self.dataset_schema_version) is not int or self.dataset_schema_version < 1:
            raise ValueError("dataset_schema_version must be an integer >= 1")
        _require_sha256(self.market_sha256, "market_sha256")
        _require_sha256(self.sealed_results_sha256, "sealed_results_sha256")
        if self.historical_import_identity is not None:
            _require_sha256(self.historical_import_identity, "historical_import_identity")
        _require_sha256(self.replay_dataset_hash, "replay_dataset_hash")
        if type(self.event_count) is not int or self.event_count < 1:
            raise ValueError("event_count must be an integer >= 1")
        _require_decimal(self.initial_bankroll, "initial_bankroll")

    @property
    def identity(self) -> tuple[Any, ...]:
        return (
            self.dataset_name,
            self.sport,
            self.dataset_schema_version,
            self.market_sha256.lower(),
            self.sealed_results_sha256.lower(),
            self.historical_import_identity.lower() if self.historical_import_identity else None,
            self.replay_dataset_hash.lower(),
            self.event_count,
            self.initial_bankroll,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "dataset_name": self.dataset_name,
            "sport": self.sport,
            "dataset_schema_version": self.dataset_schema_version,
            "market_sha256": self.market_sha256.lower(),
            "sealed_results_sha256": self.sealed_results_sha256.lower(),
            "historical_import_identity": self.historical_import_identity.lower()
            if self.historical_import_identity
            else None,
            "replay_dataset_hash": self.replay_dataset_hash.lower(),
            "event_count": self.event_count,
            "initial_bankroll": str(self.initial_bankroll),
        }


@dataclass(frozen=True, slots=True)
class CandidateRef:
    candidate_id: str
    canonical_strategy_id: str
    runtime_ref: str
    authority_fingerprint: str
    agent_composition_sha256: str
    research_plan_sha256: str | None = None

    def __post_init__(self) -> None:
        for field in ("candidate_id", "canonical_strategy_id", "runtime_ref", "authority_fingerprint"):
            _require_text(getattr(self, field), field)
        _require_sha256(self.agent_composition_sha256, "agent_composition_sha256")
        if self.research_plan_sha256 is not None:
            _require_sha256(self.research_plan_sha256, "research_plan_sha256")

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "canonical_strategy_id": self.canonical_strategy_id,
            "runtime_ref": self.runtime_ref,
            "authority_fingerprint": self.authority_fingerprint,
            "agent_composition_sha256": self.agent_composition_sha256.lower(),
            "research_plan_sha256": self.research_plan_sha256.lower()
            if self.research_plan_sha256
            else None,
        }


@dataclass(frozen=True, slots=True)
class GuardrailRule:
    metric: str
    higher_is_better: bool = True
    max_regression: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if self.metric not in _SUPPORTED_METRICS:
            raise ValueError(f"unsupported guardrail metric: {self.metric}")
        _require_decimal(self.max_regression, "max_regression", minimum=Decimal("0"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "higher_is_better": self.higher_is_better,
            "max_regression": str(self.max_regression),
        }


@dataclass(frozen=True, slots=True)
class ChampionChallengerProtocol:
    experiment_id: str
    champion: CandidateRef
    challengers: tuple[CandidateRef, ...]
    cases: tuple[EvaluationCase, ...]
    primary_metric: str
    minimum_total_improvement: Decimal = Decimal("0")
    primary_higher_is_better: bool = True
    guardrails: tuple[GuardrailRule, ...] = ()
    protocol_schema_version: int = 1

    def __post_init__(self) -> None:
        _require_text(self.experiment_id, "experiment_id")
        if type(self.protocol_schema_version) is not int or self.protocol_schema_version != 1:
            raise ValueError("protocol_schema_version must be 1")
        if not self.challengers:
            raise ValueError("at least one challenger is required")
        if not self.cases:
            raise ValueError("at least one evaluation case is required")
        if self.primary_metric not in _SUPPORTED_METRICS:
            raise ValueError(f"unsupported primary metric: {self.primary_metric}")
        _require_decimal(self.minimum_total_improvement, "minimum_total_improvement", minimum=Decimal("0"))

        candidates = (self.champion, *self.challengers)
        candidate_ids = [candidate.candidate_id for candidate in candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("candidate IDs must be unique")
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("evaluation case IDs must be unique")

        fingerprints = {candidate.authority_fingerprint for candidate in candidates}
        if len(fingerprints) != 1:
            raise PermissionError("experiment candidates may not widen or alter authority")

        guardrail_metrics = [rule.metric for rule in self.guardrails]
        if len(guardrail_metrics) != len(set(guardrail_metrics)):
            raise ValueError("guardrail metrics must be unique")
        if self.primary_metric in guardrail_metrics:
            raise ValueError("primary metric must not also be a guardrail")

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "protocol_schema_version": self.protocol_schema_version,
            "experiment_id": self.experiment_id,
            "champion": self.champion.to_dict(),
            "challengers": [candidate.to_dict() for candidate in self.challengers],
            "cases": [case.to_dict() for case in self.cases],
            "primary_metric": self.primary_metric,
            "minimum_total_improvement": str(self.minimum_total_improvement),
            "primary_higher_is_better": self.primary_higher_is_better,
            "guardrails": [rule.to_dict() for rule in self.guardrails],
        }

    @property
    def protocol_sha256(self) -> str:
        canonical = json.dumps(
            self.canonical_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ExperimentRunCell:
    case_id: str
    candidate_id: str
    evidence: StrategyRunEvidence

    def __post_init__(self) -> None:
        _require_text(self.case_id, "case_id")
        _require_text(self.candidate_id, "candidate_id")
        if self.evidence.strategy_id != self.candidate_id:
            raise ValueError(
                "strategy run evidence candidate mismatch: "
                f"expected={self.candidate_id} actual={self.evidence.strategy_id}"
            )


@dataclass(frozen=True, slots=True)
class ExperimentDecisionReport:
    experiment_id: str
    protocol_sha256: str
    decision: ExperimentDecision
    selected_candidate_id: str
    previous_champion_id: str
    eligible_challenger_ids: tuple[str, ...]
    aggregate_primary_improvements: dict[str, Decimal]
    case_metrics: tuple[dict[str, Any], ...]
    evidence_sha256s: tuple[str, ...]
    donor_provenance: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "autosport_champion_challenger_decision",
            "experiment_id": self.experiment_id,
            "protocol_sha256": self.protocol_sha256,
            "decision": self.decision.value,
            "selected_candidate_id": self.selected_candidate_id,
            "previous_champion_id": self.previous_champion_id,
            "eligible_challenger_ids": list(self.eligible_challenger_ids),
            "aggregate_primary_improvements": {
                candidate: str(value)
                for candidate, value in sorted(self.aggregate_primary_improvements.items())
            },
            "case_metrics": list(self.case_metrics),
            "evidence_sha256s": list(self.evidence_sha256s),
            "donor_provenance": dict(self.donor_provenance),
            "truth": {
                "recommendation_only": True,
                "active_strategy_mutation": False,
                "real_money_execution": False,
                "human_tested": False,
                "nvda_verified": False,
                "profitability_claim": False,
                "predictive_superiority_claim": False,
            },
        }


def evaluate_champion_challenger(
    protocol: ChampionChallengerProtocol,
    cells: Iterable[ExperimentRunCell],
    *,
    donor_repository: str = "Oleksii-debug/Nika-Core",
    donor_sha: str = "2f7be3389109d7dd6fb3bae40540fe0cf2eba695",
) -> ExperimentDecisionReport:
    """Evaluate a frozen, complete, recommendation-only champion/challenger matrix.

    The function consumes already-validated canonical ``StrategyRunEvidence``.
    It never persists state and never activates a strategy. Every declared
    candidate/case cell must appear exactly once, and every summary artifact
    must be unique within the experiment.
    """

    cell_values = tuple(cells)
    expected = {
        (candidate.candidate_id, case.case_id)
        for candidate in (protocol.champion, *protocol.challengers)
        for case in protocol.cases
    }
    actual = {(cell.candidate_id, cell.case_id) for cell in cell_values}
    if len(cell_values) != len(actual):
        raise ValueError("experiment matrix contains duplicate candidate/case cells")
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(f"experiment matrix is incomplete or unexpected: missing={missing} extra={extra}")

    candidates = {candidate.candidate_id: candidate for candidate in (protocol.champion, *protocol.challengers)}
    cases = {case.case_id: case for case in protocol.cases}
    by_key = {(cell.candidate_id, cell.case_id): cell.evidence for cell in cell_values}

    seen_run_ids: set[str] = set()
    seen_summary_hashes: set[str] = set()
    for (candidate_id, case_id), evidence in by_key.items():
        candidate = candidates[candidate_id]
        case = cases[case_id]
        if evidence.comparison_identity != (
            case.dataset_name,
            case.sport,
            case.dataset_schema_version,
            case.market_sha256.lower(),
            case.sealed_results_sha256.lower(),
            case.historical_import_identity.lower() if case.historical_import_identity else None,
            case.replay_dataset_hash.lower(),
            case.event_count,
            evidence.price_semantics,
            evidence.executable_quote_verified,
            evidence.paper_fill_fidelity_verified,
            case.initial_bankroll,
        ):
            raise ValueError(f"evidence dataset/price identity mismatch for {candidate_id}/{case_id}")
        if evidence.canonical_strategy_id != candidate.canonical_strategy_id:
            raise ValueError(f"canonical strategy identity mismatch for {candidate_id}/{case_id}")
        if evidence.agent_composition_sha256 != candidate.agent_composition_sha256.lower():
            raise ValueError(f"agent composition identity mismatch for {candidate_id}/{case_id}")
        if evidence.research_plan_sha256 != candidate.research_plan_sha256:
            raise ValueError(f"research-plan identity mismatch for {candidate_id}/{case_id}")
        if evidence.run_id in seen_run_ids:
            raise ValueError(f"run_id reused across experiment matrix: {evidence.run_id}")
        if evidence.source_sha256 in seen_summary_hashes:
            raise ValueError(f"summary evidence reused across experiment matrix: {evidence.source_sha256}")
        seen_run_ids.add(evidence.run_id)
        seen_summary_hashes.add(evidence.source_sha256)

    def metric(evidence: StrategyRunEvidence, name: str) -> Decimal:
        value = getattr(evidence, name)
        return _require_decimal(value, f"metric {name}")

    champion_id = protocol.champion.candidate_id
    eligible: list[tuple[str, Decimal]] = []
    aggregate_improvements: dict[str, Decimal] = {}
    case_metrics: list[dict[str, Any]] = []

    for case in protocol.cases:
        champion = by_key[(champion_id, case.case_id)]
        for challenger in protocol.challengers:
            candidate = by_key[(challenger.candidate_id, case.case_id)]
            case_metrics.append(
                {
                    "case_id": case.case_id,
                    "candidate_id": challenger.candidate_id,
                    "primary_metric": protocol.primary_metric,
                    "champion_value": str(metric(champion, protocol.primary_metric)),
                    "challenger_value": str(metric(candidate, protocol.primary_metric)),
                    "delta": str(
                        metric(candidate, protocol.primary_metric)
                        - metric(champion, protocol.primary_metric)
                    ),
                    "guardrails": {
                        rule.metric: {
                            "champion": str(metric(champion, rule.metric)),
                            "challenger": str(metric(candidate, rule.metric)),
                            "allowed_regression": str(rule.max_regression),
                            "passed": _guardrail_pass(rule, champion, candidate),
                        }
                        for rule in protocol.guardrails
                    },
                }
            )

    for challenger in protocol.challengers:
        total = Decimal("0")
        guardrails_ok = True
        for case in protocol.cases:
            champion = by_key[(champion_id, case.case_id)]
            candidate = by_key[(challenger.candidate_id, case.case_id)]
            challenger_value = metric(candidate, protocol.primary_metric)
            champion_value = metric(champion, protocol.primary_metric)
            delta = challenger_value - champion_value
            total += delta if protocol.primary_higher_is_better else -delta
            if not all(
                _guardrail_pass(rule, champion, candidate) for rule in protocol.guardrails
            ):
                guardrails_ok = False
        aggregate_improvements[challenger.candidate_id] = total
        if total >= protocol.minimum_total_improvement and guardrails_ok:
            eligible.append((challenger.candidate_id, total))

    eligible.sort(key=lambda item: (-item[1], item[0]))
    selected = eligible[0][0] if eligible else champion_id
    decision = ExperimentDecision.CHALLENGER_ELIGIBLE if eligible else ExperimentDecision.RETAIN_CHAMPION

    with localcontext() as context:
        context.prec = 80
        # Re-check aggregate values under a deterministic high-precision Decimal context.
        for candidate_id, value in aggregate_improvements.items():
            _require_decimal(value, f"aggregate improvement for {candidate_id}")

    return ExperimentDecisionReport(
        experiment_id=protocol.experiment_id,
        protocol_sha256=protocol.protocol_sha256,
        decision=decision,
        selected_candidate_id=selected,
        previous_champion_id=champion_id,
        eligible_challenger_ids=tuple(candidate_id for candidate_id, _ in eligible),
        aggregate_primary_improvements=aggregate_improvements,
        case_metrics=tuple(case_metrics),
        evidence_sha256s=tuple(sorted(seen_summary_hashes)),
        donor_provenance={"repository": donor_repository, "reviewed_sha": donor_sha},
    )


def _guardrail_pass(
    rule: GuardrailRule,
    champion: StrategyRunEvidence,
    challenger: StrategyRunEvidence,
) -> bool:
    champion_value = _require_decimal(getattr(champion, rule.metric), f"champion {rule.metric}")
    challenger_value = _require_decimal(getattr(challenger, rule.metric), f"challenger {rule.metric}")
    regression = champion_value - challenger_value if rule.higher_is_better else challenger_value - champion_value
    return regression <= rule.max_regression


__all__ = [
    "CandidateRef",
    "ChampionChallengerProtocol",
    "EvaluationCase",
    "ExperimentDecision",
    "ExperimentDecisionReport",
    "ExperimentRunCell",
    "GuardrailRule",
    "evaluate_champion_challenger",
]
