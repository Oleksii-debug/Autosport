from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.strategy_comparison import StrategyRunEvidence

from autosport.strategy_experiment import (
    CandidateRef,
    ChampionChallengerProtocol,
    EvaluationCase,
    ExperimentRunCell,
    GuardrailRule,
    ScientificProtocolBinding,
)


SHA = "a" * 64


class _TrapStr(str):
    def strip(self, *args: object, **kwargs: object) -> str:
        raise AssertionError("identity str subclass virtual method must not execute")


class _TupleSubclass(tuple):
    pass


class _ProtocolSubclass(ScientificProtocolBinding):
    pass


class _CandidateSubclass(CandidateRef):
    pass


class _CaseSubclass(EvaluationCase):
    pass


class _GuardrailSubclass(GuardrailRule):
    pass


def _scientific(cls=ScientificProtocolBinding) -> ScientificProtocolBinding:
    return cls(
        research_protocol_id="protocol-1",
        research_question_id="question-1",
        research_question_sha256=SHA,
        hypothesis_id="hypothesis-1",
        hypothesis_sha256=SHA,
        inclusion_criteria="all frozen cases",
        exclusion_criteria="none",
        lawful_source_requirements="lawful source only",
        causal_cutoff="2026-10-01T00:00:00Z",
        evaluation_design="frozen paired evaluation",
        feature_set_version="features-v1",
        uncertainty_method="predeclared",
        multiple_comparison_control="single family",
        robustness_checks=("source sensitivity",),
        random_seed_policy="deterministic",
        stopping_rule="evaluate all cells",
        promotion_rule="minimum improvement",
        expected_artifacts=("decision report",),
        code_config_sha256=SHA,
        frozen_at_utc="2026-09-30T00:00:00Z",
    )


def _candidate(candidate_id: str = "candidate-1", cls=CandidateRef) -> CandidateRef:
    return cls(
        candidate_id=candidate_id,
        canonical_strategy_id=candidate_id,
        authority_fingerprint="owner-authority-v1",
        agent_composition_sha256=SHA,
        research_plan_sha256=SHA,
    )


def _case(cls=EvaluationCase) -> EvaluationCase:
    return cls(
        case_id="case-1",
        dataset_name="dataset-1",
        sport="football",
        dataset_schema_version=3,
        market_sha256=SHA,
        sealed_results_sha256=SHA,
        historical_import_identity=None,
        replay_dataset_hash=SHA,
        event_count=1,
        price_semantics="executable",
        executable_quote_verified=True,
        paper_fill_fidelity_verified=True,
        price_source_ids=("source-1",),
        initial_bankroll=Decimal("1000"),
    )


def _protocol(**overrides: object) -> ChampionChallengerProtocol:
    values: dict[str, object] = {
        "experiment_id": "experiment-1",
        "research_question_id": "question-1",
        "hypothesis_id": "hypothesis-1",
        "scientific_protocol": _scientific(),
        "champion": _candidate("champion"),
        "challengers": (_candidate("challenger"),),
        "cases": (_case(),),
        "primary_metric": "net_profit",
        "guardrails": (),
    }
    values.update(overrides)
    return ChampionChallengerProtocol(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "field",
    ("candidate_id", "canonical_strategy_id", "authority_fingerprint"),
)
def test_candidate_identity_rejects_str_subclass_before_virtual_method(field: str) -> None:
    values: dict[str, object] = {
        "candidate_id": "candidate-1",
        "canonical_strategy_id": "candidate-1",
        "authority_fingerprint": "owner-authority-v1",
        "agent_composition_sha256": SHA,
        "research_plan_sha256": SHA,
    }
    values[field] = _TrapStr(str(values[field]))

    with pytest.raises(ValueError, match="non-empty trimmed string"):
        CandidateRef(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("candidate_id", " candidate-1"),
        ("candidate_id", "candidate-1 "),
        ("canonical_strategy_id", " strategy-1"),
        ("authority_fingerprint", "owner-authority-v1 "),
    ),
)
def test_candidate_identity_rejects_noncanonical_whitespace(field: str, value: str) -> None:
    values: dict[str, object] = {
        "candidate_id": "candidate-1",
        "canonical_strategy_id": "candidate-1",
        "authority_fingerprint": "owner-authority-v1",
        "agent_composition_sha256": SHA,
        "research_plan_sha256": SHA,
    }
    values[field] = value

    with pytest.raises(ValueError, match="non-empty trimmed string"):
        CandidateRef(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "field",
    ("agent_composition_sha256", "research_plan_sha256"),
)
def test_candidate_hash_identity_rejects_uppercase_alias(field: str) -> None:
    values: dict[str, object] = {
        "candidate_id": "candidate-1",
        "canonical_strategy_id": "candidate-1",
        "authority_fingerprint": "owner-authority-v1",
        "agent_composition_sha256": SHA,
        "research_plan_sha256": SHA,
    }
    values[field] = "A" * 64

    with pytest.raises(ValueError, match="canonical lowercase SHA-256"):
        CandidateRef(**values)  # type: ignore[arg-type]


def test_scientific_binding_rejects_tuple_subclass() -> None:
    with pytest.raises(ValueError, match="exact tuple"):
        ScientificProtocolBinding(
            research_protocol_id="protocol-1",
            research_question_id="question-1",
            research_question_sha256=SHA,
            hypothesis_id="hypothesis-1",
            hypothesis_sha256=SHA,
            inclusion_criteria="all frozen cases",
            exclusion_criteria="none",
            lawful_source_requirements="lawful source only",
            causal_cutoff="2026-10-01T00:00:00Z",
            evaluation_design="frozen paired evaluation",
            feature_set_version="features-v1",
            uncertainty_method="predeclared",
            multiple_comparison_control="single family",
            robustness_checks=_TupleSubclass(("source sensitivity",)),
            random_seed_policy="deterministic",
            stopping_rule="evaluate all cells",
            promotion_rule="minimum improvement",
            expected_artifacts=("decision report",),
            code_config_sha256=SHA,
            frozen_at_utc="2026-09-30T00:00:00Z",
        )


def test_champion_protocol_rejects_scientific_protocol_subclass() -> None:
    with pytest.raises(ValueError, match="exact ScientificProtocolBinding"):
        _protocol(scientific_protocol=_scientific(_ProtocolSubclass))


def test_champion_protocol_rejects_candidate_subclasses() -> None:
    with pytest.raises(ValueError, match="champion must be an exact CandidateRef"):
        _protocol(champion=_candidate("champion", _CandidateSubclass))
    with pytest.raises(ValueError, match="challengers must be a tuple"):
        _protocol(challengers=(_candidate("challenger", _CandidateSubclass),))


def test_champion_protocol_rejects_case_and_guardrail_subclasses() -> None:
    with pytest.raises(ValueError, match="cases must be a tuple"):
        _protocol(cases=(_case(_CaseSubclass),))
    with pytest.raises(ValueError, match="guardrails must be a tuple"):
        _protocol(guardrails=(_GuardrailSubclass("roi"),))


class _HashTrapStr(str):
    def __hash__(self) -> int:
        raise AssertionError("primary metric subclass hash must not execute")


def test_champion_protocol_rejects_primary_metric_subclass_before_hash_dispatch() -> None:
    with pytest.raises(ValueError, match="non-empty trimmed string"):
        _protocol(primary_metric=_HashTrapStr("net_profit"))


def _run_evidence(cls=StrategyRunEvidence) -> StrategyRunEvidence:
    return cls(
        source_path="summary.json",
        source_sha256=SHA,
        run_id="run-1",
        dataset_name="dataset-1",
        sport="football",
        dataset_schema_version=3,
        market_sha256=SHA,
        sealed_results_sha256=SHA,
        historical_import_identity=SHA,
        replay_dataset_hash=SHA,
        event_count=1,
        strategy_id="champion",
        canonical_strategy_id="champion",
        agent_names=("agent-1",),
        agent_composition_sha256=SHA,
        research_plan_sha256=SHA,
        price_semantics="executable",
        executable_quote_verified=True,
        paper_fill_fidelity_verified=True,
        price_source_ids=("source-1",),
        initial_bankroll=Decimal("1000"),
        final_balance=Decimal("1000"),
        committed_stake=Decimal("0"),
        settled_stake=Decimal("0"),
        net_profit=Decimal("0"),
        roi=Decimal("0"),
        won=0,
        lost=0,
        void=0,
    )


class _EvidenceSubclass(StrategyRunEvidence):
    pass


def test_experiment_cell_rejects_strategy_evidence_subclass() -> None:
    with pytest.raises(ValueError, match="exact StrategyRunEvidence"):
        ExperimentRunCell(
            case_id="case-1",
            candidate_id="champion",
            evidence=_run_evidence(_EvidenceSubclass),
        )


def test_experiment_cell_revalidates_exact_evidence_identity() -> None:
    evidence = _run_evidence()
    object.__setattr__(evidence, "run_id", _HashTrapStr("run-1"))

    with pytest.raises(ValueError, match="non-empty trimmed string"):
        ExperimentRunCell(
            case_id="case-1",
            candidate_id="champion",
            evidence=evidence,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("candidate_id", "candidate-1\nforged"),
        ("canonical_strategy_id", "strategy-1\tforged"),
        ("authority_fingerprint", "owner-authority-v1\rforged"),
        ("candidate_id", "candidate-1\x7fforged"),
    ),
)
def test_candidate_identity_rejects_non_nul_control_aliases(
    field: str,
    value: str,
) -> None:
    values: dict[str, object] = {
        "candidate_id": "candidate-1",
        "canonical_strategy_id": "candidate-1",
        "authority_fingerprint": "owner-authority-v1",
        "agent_composition_sha256": SHA,
        "research_plan_sha256": SHA,
    }
    values[field] = value

    with pytest.raises(ValueError, match="control characters"):
        CandidateRef(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value",
    ("case-1\nforged", "case-1\tforged", "case-1\x7fforged"),
)
def test_experiment_cell_case_identity_rejects_control_aliases(value: str) -> None:
    with pytest.raises(ValueError, match="control characters"):
        ExperimentRunCell(
            case_id=value,
            candidate_id="champion",
            evidence=_run_evidence(),
        )
