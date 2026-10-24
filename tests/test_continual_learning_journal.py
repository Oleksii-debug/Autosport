from dataclasses import replace
from decimal import Decimal
import sqlite3

import pytest

from autosport.continual_learning_cycle import (
    ContinualLearningRecipe,
    FrozenPolicySuite,
    VerifiedLearningExperience,
    run_continual_learning_cycle,
)
from autosport.continual_learning_journal import (
    ContinualLearningJournal,
    ContinualLearningJournalConflict,
    ContinualLearningJournalError,
)
from autosport.learning_environment import (
    Action,
    EvidenceTruth,
    RewardEvidence,
    Transition,
)
from autosport.policy_evaluation import (
    PolicyEvaluationCase,
    PolicyRewardMode,
    QualifiedCounterfactualAuthority,
)
from autosport.transparent_bandit_policy import BanditPolicyState


ENVIRONMENT = "a" * 64
CONFIG = "b" * 64
COMPLETED_AT = "2026-10-23T12:00:00+00:00"


def _experience(
    sample_id: str,
    *,
    action_type: str,
    reward_value: str,
    ordinal: int,
    source_character: str,
) -> VerifiedLearningExperience:
    observation_id = f"{ordinal:064x}"
    action = Action(
        environment_id=ENVIRONMENT,
        observation_id=observation_id,
        action_type=action_type,
        decided_at=f"2026-10-22T00:{ordinal:02d}:00+00:00",
    )
    outcome_id = f"{ordinal + 1000:064x}"
    reward = RewardEvidence(
        environment_id=ENVIRONMENT,
        action_id=action.action_id,
        outcome_id=outcome_id,
        reward=Decimal(reward_value),
        available_at=f"2026-10-22T00:{ordinal:02d}:10+00:00",
        truth=EvidenceTruth.OBSERVED,
    )
    transition = Transition(
        environment_id=ENVIRONMENT,
        episode_id=f"{ordinal + 2000:064x}",
        step_index=ordinal,
        observation_id=observation_id,
        action_id=action.action_id,
        outcome_id=outcome_id,
        reward_id=reward.reward_id,
        decision_at=action.decided_at,
        resolved_at=f"2026-10-22T00:{ordinal:02d}:20+00:00",
    )
    return VerifiedLearningExperience(
        sample_id=sample_id,
        source_evidence_sha256=source_character * 64,
        action=action,
        reward=reward,
        transition=transition,
    )


def _case(
    sample_id: str,
    *,
    bet_reward: str,
    wait_reward: str,
    source_character: str,
) -> PolicyEvaluationCase:
    return PolicyEvaluationCase(
        sample_id=sample_id,
        observed_at="2026-10-22T08:00:00+00:00",
        reward_available_at="2026-10-22T10:00:00+00:00",
        admissible_actions=("BET", "WAIT"),
        action_rewards=(
            ("BET", Decimal(bet_reward)),
            ("WAIT", Decimal(wait_reward)),
        ),
        action_costs=(("BET", Decimal("0")), ("WAIT", Decimal("0"))),
        behavior_propensities=(
            ("BET", Decimal("0.5")),
            ("WAIT", Decimal("0.5")),
        ),
        reward_truth=EvidenceTruth.OBSERVED,
        reward_mode=PolicyRewardMode.MECHANICAL_PAPER,
        source_evidence_sha256=source_character * 64,
        regime_id="table-tennis:pre-match",
        counterfactual_source_id="mechanical-paper-cycle:v1",
    )


def _inputs(*, retention_bet: str = "1"):
    champion = BanditPolicyState.initial(
        environment_id=ENVIRONMENT,
        protocol_id="provider-free-cycle-v1",
        config_sha256=CONFIG,
        seed=23,
        action_types=frozenset({"BET", "WAIT"}),
    )
    bootstrap = _experience(
        "bootstrap-wait",
        action_type="WAIT",
        reward_value="1",
        ordinal=1,
        source_character="c",
    )
    champion, _ = champion.update(
        action=bootstrap.action,
        reward=bootstrap.reward,
        transition=bootstrap.transition,
    )
    new = (
        _experience(
            "new-bet",
            action_type="BET",
            reward_value="3",
            ordinal=2,
            source_character="1",
        ),
    )
    replay = (
        _experience(
            "replay-bet",
            action_type="BET",
            reward_value="3",
            ordinal=3,
            source_character="2",
        ),
    )
    retention = FrozenPolicySuite(
        "retention-v1",
        "retention",
        (
            _case(
                "retention-1",
                bet_reward=retention_bet,
                wait_reward="1",
                source_character="3",
            ),
        ),
    )
    holdout = FrozenPolicySuite(
        "holdout-v1",
        "holdout",
        (
            _case(
                "holdout-1",
                bet_reward="2",
                wait_reward="0",
                source_character="4",
            ),
        ),
    )
    recipe = ContinualLearningRecipe(
        champion_policy_id=champion.policy_id,
        retention_suite_sha256=retention.suite_sha256,
        holdout_suite_sha256=holdout.suite_sha256,
        max_updates=2,
        min_holdout_improvement=Decimal("1"),
        max_retention_drop=Decimal("0"),
        min_effective_sample_size=Decimal("1"),
    )
    authority = QualifiedCounterfactualAuthority(
        authority_id="mechanical-paper-cycle:v1",
        authority_version="1",
        evaluator_source_sha256="d" * 64,
        qualification_evidence_sha256="e" * 64,
        reward_definition_sha256="f" * 64,
        reward_mode=PolicyRewardMode.MECHANICAL_PAPER,
        scope="table-tennis:pre-match",
    )
    return champion, new, replay, retention, holdout, recipe, authority


def _enqueue(journal: ContinualLearningJournal, cycle_id="cycle-1", **changes):
    champion, new, replay, retention, holdout, recipe, authority = _inputs(
        retention_bet=changes.pop("retention_bet", "1")
    )
    values = {
        "new_experiences": new,
        "replay_experiences": replay,
        "retention_suite": retention,
        "holdout_suite": holdout,
        "recipe": recipe,
        "completed_at": COMPLETED_AT,
        "counterfactual_authority": authority,
    }
    values.update(changes)
    status, inserted = journal.enqueue(cycle_id, champion, **values)
    return status, inserted, (champion, new, replay, retention, holdout, recipe, authority)


def test_crash_resume_reuses_exact_committed_checkpoint(tmp_path) -> None:
    path = tmp_path / "learning.sqlite3"
    journal = ContinualLearningJournal(path)
    status, inserted, inputs = _enqueue(journal)
    assert inserted is True
    assert status.state == "QUEUED"

    with pytest.raises(RuntimeError, match="injected crash"):
        journal.run("cycle-1", fail_after_updates_for_testing=1)

    checkpoint = journal.status("cycle-1")
    assert checkpoint.state == "TRAINING"
    assert checkpoint.completed_updates == 1
    assert checkpoint.total_updates == 2

    restarted = ContinualLearningJournal(path)
    resumed = restarted.run("cycle-1")
    direct = run_continual_learning_cycle(
        inputs[0],
        new_experiences=inputs[1],
        replay_experiences=inputs[2],
        retention_suite=inputs[3],
        holdout_suite=inputs[4],
        recipe=inputs[5],
        completed_at=COMPLETED_AT,
        counterfactual_authority=inputs[6],
    )
    assert resumed == direct
    assert restarted.status("cycle-1").state == "CANDIDATE_READY"
    assert restarted.verify("cycle-1")


def test_exact_redelivery_and_final_rerun_are_idempotent(tmp_path) -> None:
    journal = ContinualLearningJournal(tmp_path / "learning.sqlite3")
    first, inserted, _ = _enqueue(journal)
    second, inserted_again, _ = _enqueue(journal)
    assert inserted is True
    assert inserted_again is False
    assert first == second

    result = journal.run("cycle-1")
    with sqlite3.connect(journal.path) as connection:
        event_count = connection.execute(
            "SELECT COUNT(*) FROM continual_cycle_events"
        ).fetchone()[0]
    assert journal.run("cycle-1") == result
    with sqlite3.connect(journal.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM continual_cycle_events"
        ).fetchone()[0] == event_count


def test_cycle_identity_cannot_be_rebound_or_aliased(tmp_path) -> None:
    journal = ContinualLearningJournal(tmp_path / "learning.sqlite3")
    _, _, inputs = _enqueue(journal)
    different_new = (replace(inputs[1][0], sample_id="new-bet-other"),)
    with pytest.raises(ContinualLearningJournalConflict, match="different"):
        _enqueue(journal, new_experiences=different_new)
    with pytest.raises(ContinualLearningJournalConflict, match="already has cycle_id"):
        _enqueue(journal, cycle_id="cycle-alias")


def test_candidate_handoff_is_candidate_only_and_factory_bound(tmp_path) -> None:
    journal = ContinualLearningJournal(tmp_path / "learning.sqlite3")
    _enqueue(journal)
    result = journal.run("cycle-1")
    handoff = journal.candidate_handoff("cycle-1")
    assert handoff["candidate_policy_id"] == result.candidate_policy_id
    assert handoff["cycle_evidence_sha256"] == result.evidence_sha256
    assert handoff["holdout_evaluation_sha256"] == (
        result.holdout_evaluation.evaluation_sha256
    )
    assert handoff["promotion_authorized"] is False
    assert handoff["execution_authorized"] is False
    assert BanditPolicyState.from_payload(handoff["candidate_policy"]).policy_id == (
        result.candidate_policy_id
    )


def test_rollback_has_no_candidate_handoff(tmp_path) -> None:
    journal = ContinualLearningJournal(tmp_path / "learning.sqlite3")
    _enqueue(journal, retention_bet="-1")
    result = journal.run("cycle-1")
    assert result.state == "ROLLED_BACK"
    assert journal.status("cycle-1").candidate_policy_id is None
    with pytest.raises(ContinualLearningJournalError, match="no qualified"):
        journal.candidate_handoff("cycle-1")


def test_checkpoint_and_event_tamper_fail_closed(tmp_path) -> None:
    journal = ContinualLearningJournal(tmp_path / "learning.sqlite3")
    _enqueue(journal)
    with pytest.raises(RuntimeError):
        journal.run("cycle-1", fail_after_updates_for_testing=1)

    with sqlite3.connect(journal.path) as connection:
        checkpoint = connection.execute(
            "SELECT checkpoint_json FROM continual_cycles WHERE cycle_id='cycle-1'"
        ).fetchone()[0]
        altered = checkpoint.replace('"next_update_index":1', '"next_update_index":2')
        connection.execute(
            "UPDATE continual_cycles SET checkpoint_json=? WHERE cycle_id='cycle-1'",
            (altered,),
        )
        connection.commit()
    with pytest.raises(ContinualLearningJournalError):
        ContinualLearningJournal(journal.path).run("cycle-1")

    clean = ContinualLearningJournal(tmp_path / "events.sqlite3")
    _enqueue(clean)
    clean.run("cycle-1")
    with sqlite3.connect(clean.path) as connection:
        connection.execute(
            "UPDATE continual_cycle_events SET event_sha256=? WHERE sequence=1",
            ("f" * 64,),
        )
        connection.commit()
    with pytest.raises(ContinualLearningJournalError, match="event digest"):
        clean.verify("cycle-1")
