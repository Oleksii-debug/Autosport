from __future__ import annotations

from decimal import Decimal, localcontext

from autosport.strategy_comparison import StrategyRunEvidence
from autosport.strategy_experiment import (
    CandidateRef,
    ChampionChallengerProtocol,
    EvaluationCase,
    ExperimentDecision,
    ExperimentRunCell,
    ScientificProtocolBinding,
    evaluate_champion_challenger,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


def _scientific() -> ScientificProtocolBinding:
    return ScientificProtocolBinding(
        research_protocol_id="rp-decimal-context-v1",
        research_question_id="rq-decimal-context",
        research_question_sha256=SHA_B,
        hypothesis_id="hyp-decimal-context",
        hypothesis_sha256=SHA_C,
        inclusion_criteria="frozen governed paper case",
        exclusion_criteria="exclude unsealed or future-leaking evidence",
        lawful_source_requirements="recorded entitlement and provenance required",
        causal_cutoff="decision-time availability only",
        evaluation_design="frozen temporal holdout",
        feature_set_version="features-v1",
        uncertainty_method="predeclared deterministic exact paired delta",
        multiple_comparison_control="single challenger; no post-hoc family expansion",
        robustness_checks=("high-significance Decimal threshold boundary",),
        random_seed_policy="deterministic; no stochastic component",
        stopping_rule="evaluate frozen matrix once",
        promotion_rule="eligible only at or above exact frozen threshold",
        expected_artifacts=("run summaries", "decision report", "protocol hash"),
        code_config_sha256=SHA_D,
        frozen_at_utc="2026-09-16T16:00:00Z",
    )


def _case() -> EvaluationCase:
    return EvaluationCase(
        case_id="case-1",
        dataset_name="paper-case",
        sport="football",
        dataset_schema_version=2,
        market_sha256=SHA_A,
        sealed_results_sha256=SHA_B,
        historical_import_identity=SHA_C,
        replay_dataset_hash=SHA_D,
        event_count=1,
        price_semantics="executable",
        executable_quote_verified=True,
        paper_fill_fidelity_verified=True,
        price_source_ids=("source-1",),
        initial_bankroll=Decimal("1000"),
    )


def _evidence(
    *,
    run_id: str,
    strategy_id: str,
    source_sha: str,
    net_profit: Decimal,
    research_plan_sha256: str,
) -> StrategyRunEvidence:
    return StrategyRunEvidence(
        source_path=f"/tmp/{run_id}.json",
        source_sha256=source_sha,
        run_id=run_id,
        dataset_name="paper-case",
        sport="football",
        dataset_schema_version=2,
        market_sha256=SHA_A,
        sealed_results_sha256=SHA_B,
        historical_import_identity=SHA_C,
        replay_dataset_hash=SHA_D,
        event_count=1,
        strategy_id=strategy_id,
        canonical_strategy_id=strategy_id,
        agent_names=("market",),
        agent_composition_sha256=SHA_A,
        research_plan_sha256=research_plan_sha256,
        price_semantics="executable",
        executable_quote_verified=True,
        paper_fill_fidelity_verified=True,
        price_source_ids=("source-1",),
        initial_bankroll=Decimal("1000"),
        final_balance=Decimal("1000"),
        committed_stake=Decimal("1"),
        settled_stake=Decimal("1"),
        net_profit=net_profit,
        roi=net_profit,
        won=1,
        lost=0,
        void=0,
    )


def test_threshold_decision_is_independent_of_ambient_decimal_precision() -> None:
    authority = "owner-authority-v1"
    scientific = _scientific()
    plan_sha = scientific.binding_sha256
    champion = CandidateRef("champion", "champion", authority, SHA_A, plan_sha)
    challenger = CandidateRef("challenger", "challenger", authority, SHA_A, plan_sha)
    protocol = ChampionChallengerProtocol(
        experiment_id="exp-decimal-context",
        research_question_id=scientific.research_question_id,
        hypothesis_id=scientific.hypothesis_id,
        scientific_protocol=scientific,
        champion=champion,
        challengers=(challenger,),
        cases=(_case(),),
        primary_metric="net_profit",
        minimum_total_improvement=Decimal("0.0000000000000000000000000001"),
    )
    cells = (
        ExperimentRunCell(
            "case-1",
            "champion",
            _evidence(
                run_id="run-champion",
                strategy_id="champion",
                source_sha=SHA_A,
                net_profit=Decimal("1"),
                research_plan_sha256=plan_sha,
            ),
        ),
        ExperimentRunCell(
            "case-1",
            "challenger",
            _evidence(
                run_id="run-challenger",
                strategy_id="challenger",
                source_sha=SHA_B,
                net_profit=Decimal("1.0000000000000000000000000002"),
                research_plan_sha256=plan_sha,
            ),
        ),
    )

    with localcontext() as context:
        context.prec = 2
        report = evaluate_champion_challenger(protocol, cells)

    assert report.decision is ExperimentDecision.CHALLENGER_ELIGIBLE
    assert report.selected_candidate_id == "challenger"
    assert report.aggregate_primary_improvements["challenger"] == Decimal(
        "0.0000000000000000000000000002"
    )
    assert report.case_metrics[0]["delta"] == "0.0000000000000000000000000002"
