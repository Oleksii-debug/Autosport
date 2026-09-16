from __future__ import annotations

from decimal import Decimal, localcontext

from autosport.strategy_comparison import StrategyRunEvidence
from autosport.strategy_experiment import (
    CandidateRef,
    ChampionChallengerProtocol,
    EvaluationCase,
    ExperimentDecision,
    ExperimentRunCell,
    evaluate_champion_challenger,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


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
        research_plan_sha256=None,
        price_semantics="executable",
        executable_quote_verified=True,
        paper_fill_fidelity_verified=True,
        price_source_ids=("source-1",),
        initial_bankroll=Decimal("1000"),
        final_balance=Decimal("1000") + net_profit,
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
    champion = CandidateRef("champion", "champion", authority, SHA_A)
    challenger = CandidateRef("challenger", "challenger", authority, SHA_A)
    protocol = ChampionChallengerProtocol(
        experiment_id="exp-decimal-context",
        research_question_id="rq-decimal-context",
        hypothesis_id="hyp-decimal-context",
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
