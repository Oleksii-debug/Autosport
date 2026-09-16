from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.strategy_comparison import StrategyRunEvidence
from autosport.strategy_experiment import (
    CandidateRef,
    ChampionChallengerProtocol,
    EvaluationCase,
    ExperimentDecision,
    ExperimentRunCell,
    GuardrailRule,
    evaluate_champion_challenger,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


def _evidence(
    *,
    run_id: str,
    strategy_id: str,
    canonical_strategy_id: str,
    source_sha: str,
    net_profit: str,
    roi: str,
    final_balance: str,
    research_plan_sha256: str | None = None,
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
        event_count=3,
        strategy_id=strategy_id,
        canonical_strategy_id=canonical_strategy_id,
        agent_names=("market", "risk"),
        agent_composition_sha256=SHA_A,
        research_plan_sha256=research_plan_sha256,
        price_semantics="executable",
        executable_quote_verified=True,
        paper_fill_fidelity_verified=True,
        price_source_ids=("source-1",),
        initial_bankroll=Decimal("1000"),
        final_balance=Decimal(final_balance),
        committed_stake=Decimal("100"),
        settled_stake=Decimal("100"),
        net_profit=Decimal(net_profit),
        roi=Decimal(roi),
        won=2,
        lost=1,
        void=0,
    )


def _protocol(*, guardrails: tuple[GuardrailRule, ...] = ()) -> ChampionChallengerProtocol:
    authority = "owner-authority-v1"
    champion = CandidateRef(
        candidate_id="baseline-v1",
        canonical_strategy_id="baseline-v1",
        runtime_ref="autosport.baseline:v1",
        authority_fingerprint=authority,
        agent_composition_sha256=SHA_A,
    )
    challenger = CandidateRef(
        candidate_id="candidate-v2",
        canonical_strategy_id="candidate-v2",
        runtime_ref="autosport.candidate:v2",
        authority_fingerprint=authority,
        agent_composition_sha256=SHA_A,
    )
    case = EvaluationCase(
        case_id="case-1",
        dataset_name="paper-case",
        sport="football",
        dataset_schema_version=2,
        market_sha256=SHA_A,
        sealed_results_sha256=SHA_B,
        historical_import_identity=SHA_C,
        replay_dataset_hash=SHA_D,
        event_count=3,
        initial_bankroll=Decimal("1000"),
    )
    return ChampionChallengerProtocol(
        experiment_id="exp-20260916-01",
        champion=champion,
        challengers=(challenger,),
        cases=(case,),
        primary_metric="net_profit",
        minimum_total_improvement=Decimal("5"),
        guardrails=guardrails,
    )


def test_protocol_hash_is_deterministic_and_mutation_bound() -> None:
    first = _protocol()
    second = _protocol()
    assert first.protocol_sha256 == second.protocol_sha256

    changed = ChampionChallengerProtocol(
        experiment_id=first.experiment_id,
        champion=first.champion,
        challengers=first.challengers,
        cases=first.cases,
        primary_metric=first.primary_metric,
        minimum_total_improvement=Decimal("6"),
    )
    assert changed.protocol_sha256 != first.protocol_sha256


def test_permission_fingerprint_cannot_widen() -> None:
    champion = CandidateRef("champion", "baseline-v1", "runtime:champion", "same", SHA_A)
    challenger = CandidateRef("challenger", "candidate-v2", "runtime:challenger", "wider", SHA_A)
    case = EvaluationCase(
        "case",
        "dataset",
        "football",
        2,
        SHA_A,
        SHA_B,
        SHA_C,
        SHA_D,
        1,
        Decimal("100"),
    )
    with pytest.raises(PermissionError, match="may not widen or alter authority"):
        ChampionChallengerProtocol(
            "exp",
            champion,
            (challenger,),
            (case,),
            "net_profit",
        )


def test_complete_matrix_can_retain_champion_when_guardrail_fails() -> None:
    protocol = _protocol(
        guardrails=(GuardrailRule("final_balance", higher_is_better=True, max_regression=Decimal("0")),)
    )
    cells = (
        ExperimentRunCell(
            "case-1",
            "baseline-v1",
            _evidence(
                run_id="run-champion",
                strategy_id="baseline-v1",
                canonical_strategy_id="baseline-v1",
                source_sha=SHA_A,
                net_profit="10",
                roi="0.1",
                final_balance="1010",
            ),
        ),
        ExperimentRunCell(
            "case-1",
            "candidate-v2",
            _evidence(
                run_id="run-challenger",
                strategy_id="candidate-v2",
                canonical_strategy_id="candidate-v2",
                source_sha=SHA_B,
                net_profit="20",
                roi="0.2",
                final_balance="1009",
            ),
        ),
    )

    report = evaluate_champion_challenger(protocol, cells)
    assert report.decision is ExperimentDecision.RETAIN_CHAMPION
    assert report.selected_candidate_id == "baseline-v1"
    assert report.eligible_challenger_ids == ()
    assert report.to_dict()["truth"]["active_strategy_mutation"] is False
    assert report.to_dict()["truth"]["real_money_execution"] is False


def test_complete_matrix_selects_at_most_one_challenger() -> None:
    protocol = _protocol()
    cells = (
        ExperimentRunCell(
            "case-1",
            "baseline-v1",
            _evidence(
                run_id="run-champion",
                strategy_id="baseline-v1",
                canonical_strategy_id="baseline-v1",
                source_sha=SHA_A,
                net_profit="10",
                roi="0.1",
                final_balance="1010",
            ),
        ),
        ExperimentRunCell(
            "case-1",
            "candidate-v2",
            _evidence(
                run_id="run-challenger",
                strategy_id="candidate-v2",
                canonical_strategy_id="candidate-v2",
                source_sha=SHA_B,
                net_profit="20",
                roi="0.2",
                final_balance="1020",
            ),
        ),
    )

    report = evaluate_champion_challenger(protocol, cells)
    assert report.decision is ExperimentDecision.CHALLENGER_ELIGIBLE
    assert report.selected_candidate_id == "candidate-v2"
    assert report.eligible_challenger_ids == ("candidate-v2",)
    assert report.aggregate_primary_improvements["candidate-v2"] == Decimal("10")


def test_matrix_rejects_duplicate_or_missing_evidence() -> None:
    protocol = _protocol()
    champion = ExperimentRunCell(
        "case-1",
        "baseline-v1",
        _evidence(
            run_id="run-champion",
            strategy_id="baseline-v1",
            canonical_strategy_id="baseline-v1",
            source_sha=SHA_A,
            net_profit="10",
            roi="0.1",
            final_balance="1010",
        ),
    )
    with pytest.raises(ValueError, match="incomplete or unexpected"):
        evaluate_champion_challenger(protocol, (champion,))

    challenger = ExperimentRunCell(
        "case-1",
        "candidate-v2",
        _evidence(
            run_id="run-challenger",
            strategy_id="candidate-v2",
            canonical_strategy_id="candidate-v2",
            source_sha=SHA_B,
            net_profit="20",
            roi="0.2",
            final_balance="1020",
        ),
    )
    with pytest.raises(ValueError, match="duplicate candidate/case cells"):
        evaluate_champion_challenger(protocol, (champion, challenger, challenger))


def test_reused_summary_evidence_is_rejected() -> None:
    protocol = _protocol()
    champion = _evidence(
        run_id="run-champion",
        strategy_id="baseline-v1",
        canonical_strategy_id="baseline-v1",
        source_sha=SHA_A,
        net_profit="10",
        roi="0.1",
        final_balance="1010",
    )
    challenger = _evidence(
        run_id="run-challenger",
        strategy_id="candidate-v2",
        canonical_strategy_id="candidate-v2",
        source_sha=SHA_A,
        net_profit="20",
        roi="0.2",
        final_balance="1020",
    )
    with pytest.raises(ValueError, match="summary evidence reused"):
        evaluate_champion_challenger(
            protocol,
            (
                ExperimentRunCell("case-1", "baseline-v1", champion),
                ExperimentRunCell("case-1", "candidate-v2", challenger),
            ),
        )
