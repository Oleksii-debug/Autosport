from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.learning_environment import (
    CausalLearningEnvironment,
    EnvironmentIdentity,
    EvidenceTruth,
    Observation,
    Outcome,
    RewardEvidence,
)
from autosport.research_curriculum import (
    CurriculumOutcome,
    CurriculumPurpose,
    NightResearchCurriculum,
    ReplayCandidate,
    ReplayEvidenceBinding,
    ReplayProvenance,
    ResearchCurriculumError,
)
from autosport.research_supervisor import ResearchPhase, ResearchSupervisor, SupervisorStatus
from autosport.research_trigger_adapter import ResearchTriggerAdapter
from autosport.scientific_registry import ResearchQuestion, ScientificRegistry

SOURCE_SHA = "a" * 64


def _workspace(tmp_path, *, max_budget_units=8):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific-registry.json")
    for question_id in ("question-1", "question-2"):
        registry.append(
            ResearchQuestion(
                question_id=question_id,
                statement=f"Investigate unresolved signal for {question_id}",
                source_sha256=SOURCE_SHA,
                created_at="2026-09-19T03:00:00Z",
            )
        )
    supervisor = ResearchSupervisor.initialize_pristine(
        tmp_path / "research-supervisor.json", registry
    )
    curriculum = NightResearchCurriculum.initialize_pristine(
        tmp_path / "research-curriculum.json",
        ResearchTriggerAdapter(supervisor),
        max_budget_units=max_budget_units,
    )
    return registry, supervisor, curriculum


def _binding(
    *,
    episode_key,
    provenance=ReplayProvenance.HISTORICAL_OBSERVED,
    truth=None,
    outcome_available_at=None,
    outcome_evidence=(("result", "sealed"),),
    reward_value=Decimal("-1"),
    reward_evidence=(("metric", "sealed"),),
    source_suffix="test-replay-source",
):
    if truth is None:
        truth = (
            EvidenceTruth.SIMULATED
            if provenance
            in {
                ReplayProvenance.HISTORICAL_COUNTERFACTUAL_LIMITED,
                ReplayProvenance.SYNTHETIC_WORLD_MODEL,
            }
            else EvidenceTruth.OBSERVED
        )
    outcome_available_at = (
        outcome_available_at or "2026-09-20T00:00:00Z"
    )
    identity = EnvironmentIdentity(
        source_id=(
            f"autosport.replay-provenance/{provenance.value}/{source_suffix}"
        ),
        config_id="test-config",
        data_id="test-data",
        protocol_id="test-protocol",
        cutoff_ts="2026-09-20T00:00:00Z",
        seed=1,
    )
    environment = CausalLearningEnvironment(
        identity,
        episode_key=episode_key,
        policy_id="test-policy",
        admissible_actions=frozenset({"WAIT"}),
    )
    observation = Observation(
        environment_id=environment.environment_id,
        observed_at="2026-09-19T03:00:00Z",
        available_at="2026-09-19T03:00:00Z",
        evidence=(("signal", "sealed"),),
    )
    action = environment.act(
        observation,
        action_type="WAIT",
        decision_at="2026-09-19T03:05:00Z",
    )
    simulation_model_id = "test-simulation-v1" if truth is EvidenceTruth.SIMULATED else None
    outcome = Outcome(
        environment_id=environment.environment_id,
        action_id=action.action_id,
        revealed_at=outcome_available_at,
        truth=truth,
        evidence=outcome_evidence,
        simulation_model_id=simulation_model_id,
    )
    reward = RewardEvidence(
        environment_id=environment.environment_id,
        action_id=action.action_id,
        outcome_id=outcome.outcome_id,
        reward=reward_value,
        available_at=outcome_available_at,
        truth=truth,
        evidence=reward_evidence,
        simulation_model_id=simulation_model_id,
    )
    transition = environment.resolve(
        action.action_id,
        outcome=outcome,
        reward=reward,
        resolved_at=outcome_available_at,
    )
    checkpoint = environment.checkpoint()
    return ReplayEvidenceBinding(
        identity=identity,
        episode=environment.episode,
        checkpoint=checkpoint,
        transition=transition,
        outcome=outcome,
        reward=reward,
        provenance=provenance,
    )


def _candidate(
    *,
    question_id="question-1",
    episode_id="episode-1",
    available_at="2026-09-19T03:10:00Z",
    provenance=ReplayProvenance.HISTORICAL_OBSERVED,
    truth=None,
    reasons=("high-uncertainty",),
    features=(("uncertainty_bucket", "high"),),
    priority=5,
    expected_learning_value=Decimal("0.8"),
    outcome_available_at=None,
):
    binding = _binding(
        episode_key=episode_id,
        provenance=provenance,
        truth=truth,
        outcome_available_at=outcome_available_at,
    )
    return ReplayCandidate(
        question_id=question_id,
        evidence_binding=binding,
        available_at=available_at,
        reasons=reasons,
        selector_features=features,
        priority=priority,
        expected_learning_value=expected_learning_value,
    )


def _finish(supervisor, run_id):
    current = supervisor.status(run_id)
    minute = 21
    while current.phase is not ResearchPhase.COMPLETE:
        current = supervisor.advance(
            run_id,
            expected_phase=current.phase,
            at=f"2026-09-19T03:{minute:02d}:00Z",
            budget_cost=1,
        )
        minute += 1
    assert current.status is SupervisorStatus.COMPLETED


def test_selection_dispatch_restart_is_idempotent(tmp_path):
    registry, supervisor, curriculum = _workspace(tmp_path)
    candidates = (
        _candidate(episode_id="2" * 64, priority=4, expected_learning_value=Decimal("0.4")),
        _candidate(episode_id="1" * 64, priority=7, expected_learning_value=Decimal("0.6")),
    )
    first = curriculum.select_and_dispatch(
        candidates,
        purpose=CurriculumPurpose.CURRICULUM,
        selector_policy_version="night-v1",
        as_of="2026-09-19T03:20:00Z",
        seed=17,
        budget_units=2,
    )

    reopened_supervisor = ResearchSupervisor(supervisor.path, registry)
    reopened = NightResearchCurriculum(
        curriculum.path,
        ResearchTriggerAdapter(reopened_supervisor),
        max_budget_units=8,
    )
    replay = reopened.select_and_dispatch(
        candidates,
        purpose=CurriculumPurpose.CURRICULUM,
        selector_policy_version="night-v1",
        as_of="2026-09-19T03:20:00Z",
        seed=17,
        budget_units=2,
    )

    assert replay == first
    assert len(reopened_supervisor.list_runs()) == 1
    state = reopened.snapshot()
    assert state["consumed_budget_units"] == 2
    assert len(state["selections"]) == 1
    assert state["selections"][0]["selected_episode_id"] == candidates[1].episode_id


def test_exact_replay_survives_fully_consumed_curriculum_budget(tmp_path):
    _, supervisor, curriculum = _workspace(tmp_path, max_budget_units=2)
    candidate = _candidate(episode_id="c" * 64)
    first = curriculum.select_and_dispatch(
        (candidate,),
        purpose=CurriculumPurpose.CURRICULUM,
        selector_policy_version="night-v1",
        as_of="2026-09-19T03:20:00Z",
        seed=3,
        budget_units=2,
    )
    replay = curriculum.select_and_dispatch(
        (candidate,),
        purpose=CurriculumPurpose.CURRICULUM,
        selector_policy_version="night-v1",
        as_of="2026-09-19T03:20:00Z",
        seed=3,
        budget_units=2,
    )

    assert replay == first
    assert len(supervisor.list_runs()) == 1
    assert curriculum.snapshot()["consumed_budget_units"] == 2


def test_hard_example_curriculum_cannot_be_confirmation_population(tmp_path):
    _, _, curriculum = _workspace(tmp_path)
    hard = _candidate(
        episode_id="3" * 64,
        reasons=("observed-loss", "repeated-failure"),
        outcome_available_at="2026-09-19T03:15:00Z",
    )
    record = curriculum.select(
        (hard,),
        purpose=CurriculumPurpose.CURRICULUM,
        selector_policy_version="night-v1",
        as_of="2026-09-19T03:20:00Z",
        seed=1,
        budget_units=1,
    )
    assert record.outcome_information_available is True
    assert record.selected_outcome_available_at == "2026-09-19T03:15:00Z"
    with pytest.raises(ResearchCurriculumError, match="no causally eligible"):
        curriculum.select(
            (hard,),
            purpose=CurriculumPurpose.CONFIRMATORY,
            selector_policy_version="confirm-v1",
            as_of="2026-09-19T03:20:00Z",
            seed=1,
            budget_units=1,
        )


@pytest.mark.parametrize(
    "candidate",
    [
        _candidate(
            episode_id="4" * 64,
            outcome_available_at="2026-09-19T03:19:00Z",
        ),
        _candidate(
            episode_id="5" * 64,
            provenance=ReplayProvenance.SYNTHETIC_WORLD_MODEL,
        ),
        _candidate(
            episode_id="6" * 64,
            provenance=ReplayProvenance.HISTORICAL_COUNTERFACTUAL_LIMITED,
        ),
    ],
)
def test_confirmation_fails_closed_on_outcome_or_nonobserved_evidence(tmp_path, candidate):
    _, _, curriculum = _workspace(tmp_path)
    with pytest.raises(ResearchCurriculumError, match="no causally eligible"):
        curriculum.select(
            (candidate,),
            purpose=CurriculumPurpose.CONFIRMATORY,
            selector_policy_version="confirm-v1",
            as_of="2026-09-19T03:20:00Z",
            seed=1,
            budget_units=1,
        )


def test_canonical_simulated_evidence_cannot_be_relabelled_observed():
    with pytest.raises(
        ResearchCurriculumError,
        match="simulated canonical evidence cannot be relabelled observed",
    ):
        _binding(
            episode_key="synthetic-relabel",
            truth=EvidenceTruth.SIMULATED,
            provenance=ReplayProvenance.HISTORICAL_OBSERVED,
        )


def test_replay_provenance_is_derived_from_environment_source_identity():
    binding = _binding(
        episode_key="paper-source",
        provenance=ReplayProvenance.PAPER_LIVE,
    )

    assert binding.provenance is ReplayProvenance.PAPER_LIVE
    assert binding.provenance_source_id.startswith(
        "autosport.replay-provenance/PAPER_LIVE/"
    )


def test_real_execution_provenance_is_rejected_by_paper_shadow_environment():
    with pytest.raises(
        ResearchCurriculumError,
        match="cannot substantiate REAL_EXECUTION provenance",
    ):
        _binding(
            episode_key="unsupported-real",
            provenance=ReplayProvenance.REAL_EXECUTION,
        )


def test_confirmatory_observed_binding_is_eligible_before_outcome_reveal(tmp_path):
    _, _, curriculum = _workspace(tmp_path)
    candidate = _candidate(
        episode_id="confirmatory-clean",
        provenance=ReplayProvenance.HISTORICAL_OBSERVED,
        outcome_available_at="2026-09-20T00:00:00Z",
    )
    record = curriculum.select(
        (candidate,),
        purpose=CurriculumPurpose.CONFIRMATORY,
        selector_policy_version="confirm-v1",
        as_of="2026-09-19T03:20:00Z",
        seed=11,
        budget_units=1,
    )

    assert record.selected_episode_id == candidate.episode_id
    assert record.selected_evidence_binding_id == candidate.evidence_binding.binding_id
    assert record.selected_evidence_truth is EvidenceTruth.OBSERVED
    assert record.outcome_information_available is False


def test_pre_reveal_identity_and_tie_break_ignore_future_outcome_reward_content(
    tmp_path,
):
    first = _candidate(
        episode_id="future-stable",
        outcome_evidence=(("result", "future-a"),),
        reward_value=Decimal("-1"),
        reward_evidence=(("metric", "future-a"),),
    )
    second = _candidate(
        episode_id="future-stable",
        outcome_evidence=(("result", "future-b"),),
        reward_value=Decimal("7"),
        reward_evidence=(("metric", "future-b"),),
    )

    assert first.evidence_binding.binding_id != second.evidence_binding.binding_id
    assert first.candidate_id == second.candidate_id

    selected_episode_ids = []
    selection_ids = []
    for folder_name, candidate in (("first", first), ("second", second)):
        workspace = tmp_path / folder_name
        workspace.mkdir()
        _, _, curriculum = _workspace(workspace)
        rival = _candidate(
            episode_id="future-rival",
            priority=candidate.priority,
            expected_learning_value=candidate.expected_learning_value,
        )
        record = curriculum.select(
            (candidate, rival),
            purpose=CurriculumPurpose.CONFIRMATORY,
            selector_policy_version="confirm-v1",
            as_of="2026-09-19T03:20:00Z",
            seed=13,
            budget_units=1,
        )
        selected_episode_ids.append(record.selected_episode_id)
        selection_ids.append(record.selection_id)

    assert selected_episode_ids[0] == selected_episode_ids[1]
    assert selection_ids[0] == selection_ids[1]


def test_dispatch_rejects_fabricated_unpersisted_selection_without_side_effect(tmp_path):
    _, supervisor, curriculum = _workspace(tmp_path)
    record = curriculum.select(
        (_candidate(episode_id="persisted-selection"),),
        purpose=CurriculumPurpose.CURRICULUM,
        selector_policy_version="night-v1",
        as_of="2026-09-19T03:20:00Z",
        seed=12,
        budget_units=1,
    )
    fabricated = (
        replace(
            record,
            purpose=CurriculumPurpose.CONFIRMATORY,
            outcome_information_available=True,
        ),
        replace(
            record,
            selected_provenance=ReplayProvenance.SYNTHETIC_WORLD_MODEL,
            selected_evidence_truth=EvidenceTruth.SIMULATED,
        ),
        replace(record, seed=999),
    )

    for forged in fabricated:
        with pytest.raises(
            ResearchCurriculumError,
            match="exact durably persisted selection evidence",
        ):
            curriculum.dispatch(forged)

    state = curriculum.snapshot()
    assert state["dispatches"] == {}
    assert state["consumed_budget_units"] == 0
    assert len(supervisor.list_runs()) == 0


def test_future_candidate_is_not_visible(tmp_path):
    _, _, curriculum = _workspace(tmp_path)
    with pytest.raises(ResearchCurriculumError, match="no causally eligible"):
        curriculum.select(
            (_candidate(available_at="2026-09-19T03:20:01Z"),),
            purpose=CurriculumPurpose.CURRICULUM,
            selector_policy_version="night-v1",
            as_of="2026-09-19T03:20:00Z",
            seed=1,
            budget_units=1,
        )


def test_budget_exhaustion_blocks_second_supervisor_dispatch(tmp_path):
    _, supervisor, curriculum = _workspace(tmp_path, max_budget_units=2)
    curriculum.select_and_dispatch(
        (_candidate(episode_id="7" * 64),),
        purpose=CurriculumPurpose.CURRICULUM,
        selector_policy_version="night-v1",
        as_of="2026-09-19T03:20:00Z",
        seed=1,
        budget_units=2,
    )
    with pytest.raises(ResearchCurriculumError, match="budget exhausted"):
        curriculum.select_and_dispatch(
            (_candidate(question_id="question-2", episode_id="8" * 64),),
            purpose=CurriculumPurpose.CURRICULUM,
            selector_policy_version="night-v1",
            as_of="2026-09-19T03:21:00Z",
            seed=2,
            budget_units=1,
        )
    assert len(supervisor.list_runs()) == 1


def test_pause_stop_survive_restart_and_block_selection(tmp_path):
    registry, supervisor, curriculum = _workspace(tmp_path)
    curriculum.pause()
    with pytest.raises(ResearchCurriculumError, match="not active"):
        curriculum.select(
            (_candidate(),),
            purpose=CurriculumPurpose.CURRICULUM,
            selector_policy_version="night-v1",
            as_of="2026-09-19T03:20:00Z",
            seed=1,
            budget_units=1,
        )
    curriculum.resume()
    curriculum.stop("operator stop")
    reopened = NightResearchCurriculum(
        curriculum.path,
        ResearchTriggerAdapter(ResearchSupervisor(supervisor.path, registry)),
        max_budget_units=8,
    )
    with pytest.raises(ResearchCurriculumError, match="not active"):
        reopened.select(
            (_candidate(),),
            purpose=CurriculumPurpose.CURRICULUM,
            selector_policy_version="night-v1",
            as_of="2026-09-19T03:20:00Z",
            seed=1,
            budget_units=1,
        )


def test_pause_after_selection_blocks_new_dispatch_without_supervisor_run(tmp_path):
    _, supervisor, curriculum = _workspace(tmp_path)
    record = curriculum.select(
        (_candidate(episode_id="d" * 64),),
        purpose=CurriculumPurpose.CURRICULUM,
        selector_policy_version="night-v1",
        as_of="2026-09-19T03:20:00Z",
        seed=4,
        budget_units=1,
    )
    curriculum.pause()

    with pytest.raises(ResearchCurriculumError, match="not active"):
        curriculum.dispatch(record)

    assert len(supervisor.list_runs()) == 0


def test_stop_after_dispatch_reservation_does_not_orphan_supervisor_run(
    tmp_path, monkeypatch
):
    _, supervisor, curriculum = _workspace(tmp_path)
    record = curriculum.select(
        (_candidate(episode_id="e" * 64),),
        purpose=CurriculumPurpose.CURRICULUM,
        selector_policy_version="night-v1",
        as_of="2026-09-19T03:20:00Z",
        seed=5,
        budget_units=1,
    )
    original_accept = ResearchTriggerAdapter.accept

    def stop_then_accept(adapter, event):
        curriculum.stop("operator stop after dispatch admission")
        return original_accept(adapter, event)

    monkeypatch.setattr(ResearchTriggerAdapter, "accept", stop_then_accept)
    receipt = curriculum.dispatch(record)

    assert curriculum.status.value == "STOPPED"
    assert supervisor.status(receipt.run_id).run_id == receipt.run_id
    dispatch = curriculum.snapshot()["dispatches"][record.selection_id]
    assert dispatch["status"] == "ACCEPTED"
    assert dispatch["run_id"] == receipt.run_id


def test_pending_dispatch_cannot_publish_outcome_and_exact_retry_recovers(
    tmp_path, monkeypatch
):
    _, supervisor, curriculum = _workspace(tmp_path)
    record = curriculum.select(
        (_candidate(episode_id="f" * 64),),
        purpose=CurriculumPurpose.CURRICULUM,
        selector_policy_version="night-v1",
        as_of="2026-09-19T03:20:00Z",
        seed=6,
        budget_units=1,
    )
    original_accept = ResearchTriggerAdapter.accept

    def fail_once(adapter, event):
        monkeypatch.setattr(ResearchTriggerAdapter, "accept", original_accept)
        raise RuntimeError("simulated crash after durable dispatch reservation")

    monkeypatch.setattr(ResearchTriggerAdapter, "accept", fail_once)
    with pytest.raises(RuntimeError, match="simulated crash"):
        curriculum.dispatch(record)

    pending = curriculum.snapshot()["dispatches"][record.selection_id]
    assert pending["status"] == "PENDING"
    assert len(supervisor.list_runs()) == 0
    with pytest.raises(ResearchCurriculumError, match="no accepted supervisor dispatch"):
        curriculum.record_outcome(
            record.selection_id,
            outcome=CurriculumOutcome.NEGATIVE,
            at="2026-09-19T03:21:00Z",
        )

    recovered = curriculum.dispatch(record)
    accepted = curriculum.snapshot()["dispatches"][record.selection_id]
    assert accepted["status"] == "ACCEPTED"
    assert accepted["run_id"] == recovered.run_id
    assert len(supervisor.list_runs()) == 1


def test_no_bet_has_no_fixed_positive_selection_bonus(tmp_path):
    _, _, curriculum = _workspace(tmp_path)
    wait = _candidate(
        episode_id="9" * 64,
        reasons=("no-bet",),
        features=(("decision", "WAIT"),),
        priority=1,
        expected_learning_value=Decimal("0.1"),
    )
    unresolved = _candidate(
        episode_id="a" * 64,
        reasons=("model-disagreement",),
        features=(("decision", "PAPER_PROPOSAL"),),
        priority=2,
        expected_learning_value=Decimal("0.2"),
    )
    record = curriculum.select(
        (wait, unresolved),
        purpose=CurriculumPurpose.CURRICULUM,
        selector_policy_version="night-v1",
        as_of="2026-09-19T03:20:00Z",
        seed=1,
        budget_units=1,
    )
    assert record.selected_episode_id == unresolved.episode_id
    assert record.selected_reasons == ("model-disagreement",)


def test_negative_outcome_is_bound_to_completed_supervisor_run(tmp_path):
    _, supervisor, curriculum = _workspace(tmp_path, max_budget_units=32)
    receipt = curriculum.select_and_dispatch(
        (_candidate(),),
        purpose=CurriculumPurpose.CURRICULUM,
        selector_policy_version="night-v1",
        as_of="2026-09-19T03:20:00Z",
        seed=1,
        budget_units=20,
    )
    with pytest.raises(ResearchCurriculumError, match="completed"):
        curriculum.record_outcome(
            receipt.selection_id,
            outcome=CurriculumOutcome.NEGATIVE,
            at="2026-09-19T03:21:00Z",
            scientific_evidence_id="postmortem-1",
        )
    _finish(supervisor, receipt.run_id)
    curriculum.record_outcome(
        receipt.selection_id,
        outcome=CurriculumOutcome.NEGATIVE,
        at="2026-09-19T03:40:00Z",
        scientific_evidence_id="postmortem-1",
    )
    assert curriculum.snapshot()["outcomes"][receipt.selection_id]["outcome"] == "NEGATIVE"


def test_negative_cycle_checkpoints_then_selects_next_question(tmp_path):
    _, supervisor, curriculum = _workspace(tmp_path, max_budget_units=40)
    first = curriculum.select_and_dispatch(
        (
            _candidate(
                question_id="question-1",
                episode_id="1" * 64,
                reasons=("observed-loss",),
                outcome_available_at="2026-09-19T03:15:00Z",
            ),
        ),
        purpose=CurriculumPurpose.CURRICULUM,
        selector_policy_version="night-v1",
        as_of="2026-09-19T03:20:00Z",
        seed=7,
        budget_units=20,
    )
    _finish(supervisor, first.run_id)
    curriculum.record_outcome(
        first.selection_id,
        outcome=CurriculumOutcome.NEGATIVE,
        at="2026-09-19T03:40:00Z",
        scientific_evidence_id="postmortem-negative-1",
    )

    second = curriculum.select_and_dispatch(
        (
            _candidate(
                question_id="question-2",
                episode_id="2" * 64,
                available_at="2026-09-19T03:41:00Z",
                reasons=("unresolved-follow-up",),
                expected_learning_value=Decimal("0.9"),
            ),
        ),
        purpose=CurriculumPurpose.CURRICULUM,
        selector_policy_version="night-v1",
        as_of="2026-09-19T03:42:00Z",
        seed=8,
        budget_units=20,
    )

    state = curriculum.snapshot()
    assert second.run_id != first.run_id
    assert len(supervisor.list_runs()) == 2
    assert state["outcomes"][first.selection_id]["outcome"] == "NEGATIVE"
    assert {item["selected_question_id"] for item in state["selections"]} == {
        "question-1",
        "question-2",
    }
    assert state["consumed_budget_units"] == 40


def test_tampered_state_fails_restart(tmp_path):
    _, supervisor, curriculum = _workspace(tmp_path)
    state = curriculum.snapshot()
    state["consumed_budget_units"] = 7
    curriculum.path.write_text(__import__("json").dumps(state), encoding="utf-8")
    with pytest.raises(ResearchCurriculumError, match="digest mismatch"):
        NightResearchCurriculum(
            curriculum.path,
            ResearchTriggerAdapter(supervisor),
            max_budget_units=8,
        )
