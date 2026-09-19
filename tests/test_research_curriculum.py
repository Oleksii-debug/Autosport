from decimal import Decimal

import pytest

from autosport.research_curriculum import (
    CurriculumOutcome,
    CurriculumPurpose,
    NightResearchCurriculum,
    ReplayCandidate,
    ReplayProvenance,
    ResearchCurriculumError,
)
from autosport.research_supervisor import ResearchPhase, ResearchSupervisor, SupervisorStatus
from autosport.research_trigger_adapter import ResearchTriggerAdapter
from autosport.scientific_registry import ResearchQuestion, ScientificRegistry

SOURCE_SHA = "a" * 64
ENVIRONMENT_ID = "b" * 64


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


def _candidate(
    *,
    question_id="question-1",
    episode_id="1" * 64,
    available_at="2026-09-19T03:10:00Z",
    provenance=ReplayProvenance.HISTORICAL_OBSERVED,
    reasons=("high-uncertainty",),
    features=(("uncertainty_bucket", "high"),),
    priority=5,
    expected_learning_value=Decimal("0.8"),
    outcome_available_at=None,
):
    return ReplayCandidate(
        question_id=question_id,
        episode_id=episode_id,
        environment_id=ENVIRONMENT_ID,
        available_at=available_at,
        provenance=provenance,
        reasons=reasons,
        selector_features=features,
        priority=priority,
        expected_learning_value=expected_learning_value,
        outcome_available_at=outcome_available_at,
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
    assert state["selections"][0]["selected_episode_id"] == "1" * 64


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
    curriculum.select(
        (hard,),
        purpose=CurriculumPurpose.CURRICULUM,
        selector_policy_version="night-v1",
        as_of="2026-09-19T03:20:00Z",
        seed=1,
        budget_units=1,
    )
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
    assert record.selected_episode_id == "a" * 64
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
