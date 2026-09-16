from __future__ import annotations

import json
from dataclasses import replace
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
    load_champion_challenger_protocol_json,
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
    price_source_ids: tuple[str, ...] = ("source-1",),
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
        price_source_ids=price_source_ids,
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


def _protocol(
    *, guardrails: tuple[GuardrailRule, ...] = ()
) -> ChampionChallengerProtocol:
    authority = "owner-authority-v1"
    champion = CandidateRef(
        candidate_id="baseline-v1",
        canonical_strategy_id="baseline-v1",
        authority_fingerprint=authority,
        agent_composition_sha256=SHA_A,
    )
    challenger = CandidateRef(
        candidate_id="candidate-v2",
        canonical_strategy_id="candidate-v2",
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
        price_semantics="executable",
        executable_quote_verified=True,
        paper_fill_fidelity_verified=True,
        price_source_ids=("source-1",),
        initial_bankroll=Decimal("1000"),
    )
    return ChampionChallengerProtocol(
        experiment_id="exp-20260916-01",
        research_question_id="rq-strategy-superiority-v1",
        hypothesis_id="hyp-candidate-v2-improves-profit-v1",
        champion=champion,
        challengers=(challenger,),
        cases=(case,),
        primary_metric="net_profit",
        minimum_total_improvement=Decimal("5"),
        guardrails=guardrails,
    )


def _valid_cells() -> tuple[ExperimentRunCell, ExperimentRunCell]:
    return (
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


def test_protocol_hash_is_deterministic_and_binds_frozen_research_identity() -> None:
    first = _protocol()
    second = _protocol()
    assert first.protocol_sha256 == second.protocol_sha256

    changed_threshold = ChampionChallengerProtocol(
        experiment_id=first.experiment_id,
        research_question_id=first.research_question_id,
        hypothesis_id=first.hypothesis_id,
        champion=first.champion,
        challengers=first.challengers,
        cases=first.cases,
        primary_metric=first.primary_metric,
        minimum_total_improvement=Decimal("6"),
    )
    assert changed_threshold.protocol_sha256 != first.protocol_sha256

    changed_hypothesis = ChampionChallengerProtocol(
        experiment_id=first.experiment_id,
        research_question_id=first.research_question_id,
        hypothesis_id="hyp-different-v1",
        champion=first.champion,
        challengers=first.challengers,
        cases=first.cases,
        primary_metric=first.primary_metric,
        minimum_total_improvement=first.minimum_total_improvement,
    )
    assert changed_hypothesis.protocol_sha256 != first.protocol_sha256


def test_candidate_runtime_identity_is_evidence_bound_not_free_form() -> None:
    candidate = _protocol().champion
    payload = candidate.to_dict()
    assert "runtime_ref" not in payload
    assert payload["runtime_identity_sha256"] == candidate.runtime_identity_sha256

    changed = replace(candidate, research_plan_sha256=SHA_B)
    assert changed.runtime_identity_sha256 != candidate.runtime_identity_sha256


def test_strict_protocol_json_round_trip_and_fail_closed_parsing() -> None:
    protocol = _protocol()
    raw = json.dumps(
        protocol.canonical_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    loaded = load_champion_challenger_protocol_json(raw)
    assert loaded.protocol_sha256 == protocol.protocol_sha256
    assert loaded.canonical_dict() == protocol.canonical_dict()

    with pytest.raises(ValueError, match="duplicate JSON object key"):
        load_champion_challenger_protocol_json(
            '{"protocol_schema_version":1,"protocol_schema_version":1}'
        )

    with pytest.raises(ValueError, match="non-standard JSON constant"):
        load_champion_challenger_protocol_json(
            raw.replace('"minimum_total_improvement":"5"', '"minimum_total_improvement":NaN')
        )

    payload = protocol.canonical_dict()
    payload["champion"]["runtime_ref"] = "unbound:runtime"
    with pytest.raises(ValueError, match="unexpected=.*runtime_ref"):
        load_champion_challenger_protocol_json(json.dumps(payload))

    deeply_nested = '{"x":' + ("[" * 40) + "0" + ("]" * 40) + "}"
    with pytest.raises(ValueError, match="maximum depth"):
        load_champion_challenger_protocol_json(deeply_nested)


def test_permission_fingerprint_cannot_widen() -> None:
    champion = CandidateRef("champion", "baseline-v1", "same", SHA_A)
    challenger = CandidateRef("challenger", "candidate-v2", "wider", SHA_A)
    case = _protocol().cases[0]
    with pytest.raises(PermissionError, match="may not widen or alter authority"):
        ChampionChallengerProtocol(
            experiment_id="exp",
            research_question_id="rq",
            hypothesis_id="hyp",
            champion=champion,
            challengers=(challenger,),
            cases=(case,),
            primary_metric="net_profit",
        )


def test_complete_matrix_can_retain_champion_when_guardrail_fails() -> None:
    protocol = _protocol(
        guardrails=(
            GuardrailRule(
                "final_balance",
                higher_is_better=True,
                max_regression=Decimal("0"),
            ),
        )
    )
    champion, challenger = _valid_cells()
    challenger = ExperimentRunCell(
        challenger.case_id,
        challenger.candidate_id,
        replace(challenger.evidence, final_balance=Decimal("1009")),
    )

    report = evaluate_champion_challenger(protocol, (champion, challenger))
    assert report.decision is ExperimentDecision.RETAIN_CHAMPION
    assert report.selected_candidate_id == "baseline-v1"
    assert report.eligible_challenger_ids == ()
    assert report.research_question_id == protocol.research_question_id
    assert report.hypothesis_id == protocol.hypothesis_id
    assert report.to_dict()["truth"]["active_strategy_mutation"] is False
    assert report.to_dict()["truth"]["real_money_execution"] is False


def test_complete_matrix_selects_at_most_one_challenger() -> None:
    protocol = _protocol()
    report = evaluate_champion_challenger(protocol, _valid_cells())

    assert report.decision is ExperimentDecision.CHALLENGER_ELIGIBLE
    assert report.selected_candidate_id == "candidate-v2"
    assert report.eligible_challenger_ids == ("candidate-v2",)
    assert report.aggregate_primary_improvements["candidate-v2"] == Decimal("10")
    assert report.authority_fingerprint == "owner-authority-v1"


def test_declared_price_identity_including_sources_must_match_evidence() -> None:
    protocol = _protocol()
    champion, challenger = _valid_cells()
    mismatched = ExperimentRunCell(
        challenger.case_id,
        challenger.candidate_id,
        replace(challenger.evidence, price_source_ids=("source-2",)),
    )

    with pytest.raises(ValueError, match="dataset/price identity mismatch"):
        evaluate_champion_challenger(protocol, (champion, mismatched))


def test_matrix_rejects_duplicate_or_missing_evidence() -> None:
    protocol = _protocol()
    champion, challenger = _valid_cells()
    with pytest.raises(ValueError, match="incomplete or unexpected"):
        evaluate_champion_challenger(protocol, (champion,))

    with pytest.raises(ValueError, match="duplicate candidate/case cells"):
        evaluate_champion_challenger(
            protocol, (champion, challenger, challenger)
        )


def test_reused_summary_evidence_is_rejected() -> None:
    protocol = _protocol()
    champion, challenger = _valid_cells()
    challenger = ExperimentRunCell(
        challenger.case_id,
        challenger.candidate_id,
        replace(challenger.evidence, source_sha256=SHA_A),
    )
    with pytest.raises(ValueError, match="summary evidence reused"):
        evaluate_champion_challenger(protocol, (champion, challenger))
