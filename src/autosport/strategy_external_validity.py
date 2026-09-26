"""Strategy-specific descriptive external-validity view over EvaluationUniverse.

Consumer only: no denominator, baseline, execution, issuance, promotion, provider-write,
or real-money authority is created here.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Sequence

from .evaluation_universe import EvaluationUniverseLedger, FunnelStage, SlotState
from .external_validity_baseline import (
    EvaluationContractFamily,
    FrozenBaselineProtocol,
    PolicyEvaluation,
    build_external_validity_report,
)
from .opportunity import StrategyClass


class StrategyExternalValidityError(ValueError):
    pass


class StrategyEvidenceGrade(StrEnum):
    INSUFFICIENT = "INSUFFICIENT"
    THEORETICAL = "THEORETICAL"
    PAPER_MODEL = "PAPER_MODEL"


_SUPPORTED = {
    StrategyClass.LIVE_PRICE_MOVEMENT: EvaluationContractFamily.LIVE_PRICE_EXECUTION,
    StrategyClass.ARBITRAGE: EvaluationContractFamily.ARBITRAGE_EXECUTION,
    StrategyClass.DUTCHING: EvaluationContractFamily.DUTCHING_EXECUTION,
    StrategyClass.HEDGE_REBALANCE: EvaluationContractFamily.HEDGE_PORTFOLIO_RISK,
}
_UNRESOLVED = {
    FunnelStage.ATTEMPTED,
    FunnelStage.UNKNOWN,
    FunnelStage.PENDING,
    FunnelStage.MISSING,
}
_PAPER_OUTCOME_STAGES = {
    FunnelStage.ACCEPTED,
    FunnelStage.PARTIAL,
    FunnelStage.REJECTED,
    FunnelStage.RECONCILED,
    FunnelStage.SETTLED,
    FunnelStage.VOID,
}


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class StrategyExternalValidityReport:
    strategy_class: StrategyClass
    evaluation_contract_family: EvaluationContractFamily
    strategy_version_id: str
    config_sha256: str
    model_version_ids: tuple[str, ...]
    research_protocol_id: str
    research_protocol_sha256: str
    primary_metric: str
    uncertainty_method: str
    portfolio_before_ids: tuple[str, ...]
    cost_contract_sha256s: tuple[str, ...]
    execution_model_ids: tuple[str, ...]
    universe_id: str
    universe_sha256: str
    membership_sha256: str
    ledger_sha256: str
    baseline_protocol_sha256: str
    baseline_report_sha256: str
    candidate_evaluation_sha256: str
    evaluated_row_count: int
    stage_counts: tuple[tuple[str, int], ...]
    attrition_counts: tuple[tuple[str, int], ...]
    dependence_cluster_keys: tuple[str, ...]
    evidence_grade: StrategyEvidenceGrade
    external_evidence_gaps: tuple[str, ...]

    @property
    def report_sha256(self) -> str:
        return _digest(self.to_payload(include_digest=False))

    def to_payload(self, *, include_digest: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": 1,
            "kind": "autosport_strategy_external_validity_report",
            "strategy_class": self.strategy_class.value,
            "evaluation_contract_family": self.evaluation_contract_family.value,
            "strategy_version_id": self.strategy_version_id,
            "config_sha256": self.config_sha256,
            "model_version_ids": list(self.model_version_ids),
            "research_protocol_id": self.research_protocol_id,
            "research_protocol_sha256": self.research_protocol_sha256,
            "primary_metric": self.primary_metric,
            "uncertainty_method": self.uncertainty_method,
            "portfolio_before_ids": list(self.portfolio_before_ids),
            "cost_contract_sha256s": list(self.cost_contract_sha256s),
            "execution_model_ids": list(self.execution_model_ids),
            "universe_id": self.universe_id,
            "universe_sha256": self.universe_sha256,
            "membership_sha256": self.membership_sha256,
            "ledger_sha256": self.ledger_sha256,
            "baseline_protocol_sha256": self.baseline_protocol_sha256,
            "baseline_report_sha256": self.baseline_report_sha256,
            "candidate_evaluation_sha256": self.candidate_evaluation_sha256,
            "evaluated_row_count": self.evaluated_row_count,
            "stage_counts": [list(x) for x in self.stage_counts],
            "attrition_counts": [list(x) for x in self.attrition_counts],
            "dependence_cluster_keys": list(self.dependence_cluster_keys),
            "evidence_grade": self.evidence_grade.value,
            "external_evidence_gaps": list(self.external_evidence_gaps),
            "truth": {
                "complete_frozen_denominator_preserved": True,
                "same_universe_baseline_membership": True,
                "product_issued_policy_evaluation_verified": False,
                "promotion_authority": False,
                "provider_write_authority": False,
                "real_money_execution": False,
            },
        }
        if include_digest:
            payload["report_sha256"] = self.report_sha256
        return payload


def evaluate_strategy_external_validity(
    ledger: EvaluationUniverseLedger,
    protocol: FrozenBaselineProtocol,
    candidate: PolicyEvaluation,
    baseline_results: Sequence[PolicyEvaluation],
) -> StrategyExternalValidityReport:
    """Derive a fail-closed descriptive view from canonical existing authorities."""
    if type(ledger) is not EvaluationUniverseLedger:
        raise StrategyExternalValidityError("ledger must be exact EvaluationUniverseLedger")
    if type(protocol) is not FrozenBaselineProtocol:
        raise StrategyExternalValidityError("protocol must be exact FrozenBaselineProtocol")
    if type(candidate) is not PolicyEvaluation:
        raise StrategyExternalValidityError("candidate must be exact PolicyEvaluation")

    family = _SUPPORTED.get(protocol.strategy_class)
    if family is None:
        raise StrategyExternalValidityError(
            "supports LIVE_PRICE_MOVEMENT, ARBITRAGE, DUTCHING, and HEDGE_REBALANCE only"
        )
    if protocol.evaluation_contract_family is not family:
        raise StrategyExternalValidityError("strategy/evaluation-contract mismatch")

    cohort = ledger.cohort()
    if protocol.evidence_scope.cohort_keys != cohort.row_ids:
        raise StrategyExternalValidityError(
            "baseline cohort must equal exact frozen EvaluationUniverse row_ids"
        )
    baseline_report = build_external_validity_report(protocol, candidate, baseline_results)

    rows = ledger.universe.rows
    strategy_ids = tuple(sorted({row.strategy_version_id for row in rows}))
    config_ids = tuple(sorted({row.config_sha256 for row in rows}))
    if len(strategy_ids) != 1:
        raise StrategyExternalValidityError("frozen denominator mixes strategy_version_id values")
    if protocol.candidate_id != strategy_ids[0]:
        raise StrategyExternalValidityError(
            "baseline candidate_id does not match frozen strategy_version_id"
        )
    if len(config_ids) != 1:
        raise StrategyExternalValidityError("frozen denominator mixes config_sha256 values")

    candidate_rows = tuple(row for row in rows if row.slot_state is SlotState.CANDIDATE)
    stages = tuple(ledger.current_stage(row.row_id) for row in candidate_rows)
    gaps = {
        "generic_product_evaluation_issuance_not_verified",
        "real_provider_execution_authority_not_substituted",
    }
    grade = StrategyEvidenceGrade.INSUFFICIENT
    if candidate_rows:
        grade = StrategyEvidenceGrade.THEORETICAL
        if protocol.strategy_class in {StrategyClass.ARBITRAGE, StrategyClass.DUTCHING} and any(
            row.terminal_space_proof_id is None for row in candidate_rows
        ):
            gaps.add("terminal_space_authority_incomplete")
            grade = StrategyEvidenceGrade.INSUFFICIENT
        if any(stage in _UNRESOLVED for stage in stages):
            gaps.add("material_funnel_state_unresolved")
            grade = StrategyEvidenceGrade.INSUFFICIENT
        elif (
            grade is not StrategyEvidenceGrade.INSUFFICIENT
            and any(stage in _PAPER_OUTCOME_STAGES for stage in stages)
        ):
            # EvaluationUniverse validates these stages against canonical #623
            # PAPER attempt evidence. That proves a generic execution outcome,
            # not the strategy-specific external-validity contract required by
            # #634 (later-quote, complete multi-leg economics, or same-trajectory
            # counterfactual evidence). Stay descriptive until that proof is
            # explicitly composed and re-resolved here.
            gaps.add("strategy_specific_paper_proof_not_verified")

    if protocol.strategy_class is StrategyClass.LIVE_PRICE_MOVEMENT:
        gaps.add("external_later_quote_execution_evidence_not_verified")
    elif protocol.strategy_class in {StrategyClass.ARBITRAGE, StrategyClass.DUTCHING}:
        gaps.add("external_multi_leg_acceptance_and_settlement_not_verified")
    else:
        gaps.add("external_same_trajectory_counterfactual_execution_not_verified")

    return StrategyExternalValidityReport(
        strategy_class=protocol.strategy_class,
        evaluation_contract_family=protocol.evaluation_contract_family,
        strategy_version_id=strategy_ids[0],
        config_sha256=config_ids[0],
        model_version_ids=tuple(sorted({
            row.model_version_id for row in rows if row.model_version_id is not None
        })),
        research_protocol_id=ledger.universe.research_protocol_id,
        research_protocol_sha256=ledger.universe.protocol_sha256,
        primary_metric=protocol.primary_metric,
        uncertainty_method=protocol.uncertainty_method,
        portfolio_before_ids=tuple(sorted({row.portfolio_before_id for row in rows})),
        cost_contract_sha256s=tuple(sorted({row.cost_contract_sha256 for row in rows})),
        execution_model_ids=tuple(sorted({
            row.execution_model_id for row in rows if row.execution_model_id is not None
        })),
        universe_id=ledger.universe.universe_id,
        universe_sha256=ledger.universe.universe_sha256,
        membership_sha256=ledger.universe.membership_sha256,
        ledger_sha256=ledger.ledger_sha256,
        baseline_protocol_sha256=protocol.identity_sha256,
        baseline_report_sha256=baseline_report.identity_sha256,
        candidate_evaluation_sha256=candidate.identity_sha256,
        evaluated_row_count=len(rows),
        stage_counts=cohort.stage_counts,
        attrition_counts=cohort.attrition_counts,
        dependence_cluster_keys=tuple(sorted({k for row in rows for k in row.dependence_cluster_keys})),
        evidence_grade=grade,
        external_evidence_gaps=tuple(sorted(gaps)),
    )


__all__ = [
    "StrategyEvidenceGrade",
    "StrategyExternalValidityError",
    "StrategyExternalValidityReport",
    "evaluate_strategy_external_validity",
]
