from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from autosport.continual_learning_cycle import (
    ContinualLearningRecipe,
    FrozenPolicySuite,
    VerifiedLearningExperience,
)
from autosport.forward_campaign_population import (
    ForwardPopulationSample,
    PopulationRole,
    TerminalEvidenceState,
    build_forward_campaign_manifest,
    build_forward_campaign_session,
)
from autosport.forward_evidence_completeness import DecisionState
from autosport.forward_learning_population_binding import (
    ForwardLearningPopulationBinding,
    ForwardLearningPopulationBindingError,
    PopulationConsumer,
    bind_forward_learning_populations,
    verify_forward_learning_population_binding,
)
from autosport.learning_environment import (
    Action,
    EvidenceTruth,
    RewardEvidence,
    Transition,
)
from autosport.policy_evaluation import PolicyEvaluationCase, PolicyRewardMode


ENVIRONMENT = "e" * 64
PROTOCOL = "a" * 64
RIGHTS = "b" * 64
BOUND_AT = "2026-10-03T10:00:00Z"


def _sha(ordinal: int) -> str:
    return f"{ordinal:064x}"


def _ts(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _sample(
    ordinal: int,
    *,
    role: PopulationRole,
    session: int,
    action: bool,
    reward: str | None = None,
) -> ForwardPopulationSample:
    base = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc) + timedelta(days=session)
    observed = base + timedelta(minutes=ordinal)
    decided = observed + timedelta(minutes=3)
    return ForwardPopulationSample(
        sample_id=f"sample-{ordinal}",
        session_id=f"session-{session}",
        role=role,
        source_id="parlayapi:table_tennis",
        event_id=f"event-{ordinal}",
        regime_id="table-tennis:pre-match" if ordinal % 2 else "table-tennis:live",
        source_evidence_sha256=_sha(100 + ordinal),
        source_rights_manifest_sha256=RIGHTS,
        observed_at=_ts(observed),
        available_at=_ts(observed + timedelta(minutes=1)),
        decision_cutoff=_ts(observed + timedelta(minutes=2)),
        decided_at=_ts(decided),
        outcome_available_at=_ts(decided + timedelta(minutes=5)),
        settled_at=_ts(decided + timedelta(minutes=6)),
        decision_state=DecisionState.ACTION if action else DecisionState.NO_BET,
        terminal_state=(
            TerminalEvidenceState.SETTLED if action else TerminalEvidenceState.NEGATIVE
        ),
        terminal_evidence_sha256=_sha(200 + ordinal),
        observed_reward=Decimal(reward) if action else None,
    )


def _experience(
    sample: ForwardPopulationSample,
    *,
    decided_at: str | None = None,
    reward: str | None = None,
) -> VerifiedLearningExperience:
    decision = decided_at or sample.decided_at
    action = Action(
        environment_id=ENVIRONMENT,
        observation_id=_sha(300 + int(sample.sample_id.split("-")[1])),
        action_type="BET",
        decided_at=decision,
    )
    ordinal = int(sample.sample_id.split("-")[1])
    reward_evidence = RewardEvidence(
        environment_id=ENVIRONMENT,
        action_id=action.action_id,
        outcome_id=_sha(400 + ordinal),
        reward=Decimal(reward) if reward is not None else sample.observed_reward,
        available_at=sample.outcome_available_at,
        truth=EvidenceTruth.OBSERVED,
    )
    transition = Transition(
        environment_id=ENVIRONMENT,
        episode_id=_sha(500 + ordinal),
        step_index=ordinal,
        observation_id=action.observation_id,
        action_id=action.action_id,
        outcome_id=reward_evidence.outcome_id,
        reward_id=reward_evidence.reward_id,
        decision_at=decision,
        resolved_at=sample.settled_at,
    )
    return VerifiedLearningExperience(
        sample_id=sample.sample_id,
        source_evidence_sha256=sample.source_evidence_sha256,
        action=action,
        reward=reward_evidence,
        transition=transition,
    )


def _case(
    sample: ForwardPopulationSample,
    *,
    regime_id: str | None = None,
) -> PolicyEvaluationCase:
    return PolicyEvaluationCase(
        sample_id=sample.sample_id,
        observed_at=sample.observed_at,
        reward_available_at=sample.outcome_available_at,
        admissible_actions=("BET", "WAIT"),
        action_rewards=(("BET", Decimal("1")), ("WAIT", Decimal("0"))),
        action_costs=(("BET", Decimal("0")), ("WAIT", Decimal("0"))),
        behavior_propensities=(("BET", Decimal("0.5")), ("WAIT", Decimal("0.5"))),
        reward_truth=EvidenceTruth.OBSERVED,
        reward_mode=PolicyRewardMode.MECHANICAL_PAPER,
        source_evidence_sha256=sample.source_evidence_sha256,
        regime_id=regime_id or sample.regime_id,
        counterfactual_source_id="mechanical-paper:v1",
    )


def _manifest(samples: tuple[ForwardPopulationSample, ...]):
    first_values = tuple(value for value in samples if value.session_id == "session-1")
    second_values = tuple(value for value in samples if value.session_id == "session-2")
    first = build_forward_campaign_session(
        session_id="session-1",
        session_index=1,
        opened_at="2026-10-02T08:00:00Z",
        closed_at="2026-10-02T09:00:00Z",
        samples=first_values,
        drift_evidence_sha256=_sha(601),
    )
    second = build_forward_campaign_session(
        session_id="session-2",
        session_index=2,
        opened_at="2026-10-03T08:00:00Z",
        closed_at="2026-10-03T09:00:00Z",
        samples=second_values,
        drift_evidence_sha256=_sha(602),
        predecessor_session_sha256=first.session_sha256,
    )
    return build_forward_campaign_manifest(
        campaign_id="account-c-binding-campaign",
        scientific_protocol_sha256=PROTOCOL,
        created_at="2026-10-03T09:01:00Z",
        samples=samples,
        sessions=(first, second),
    )


def _inputs():
    samples = (
        _sample(1, role=PopulationRole.NEW, session=1, action=True, reward="1.25"),
        _sample(2, role=PopulationRole.NEW, session=1, action=False),
        _sample(3, role=PopulationRole.REPLAY, session=2, action=True, reward="-0.5"),
        _sample(4, role=PopulationRole.RETENTION, session=2, action=True, reward="0"),
        _sample(5, role=PopulationRole.HOLDOUT, session=2, action=False),
    )
    manifest = _manifest(samples)
    new = (_experience(samples[0]),)
    replay = (_experience(samples[2]),)
    retention = FrozenPolicySuite(
        suite_id="retention-suite-v1",
        role="retention",
        cases=(_case(samples[3]),),
    )
    holdout = FrozenPolicySuite(
        suite_id="holdout-suite-v1",
        role="holdout",
        cases=(_case(samples[4]),),
    )
    recipe = ContinualLearningRecipe(
        champion_policy_id=_sha(700),
        retention_suite_sha256=retention.suite_sha256,
        holdout_suite_sha256=holdout.suite_sha256,
        max_updates=2,
    )
    return manifest, samples, new, replay, retention, holdout, recipe


def _bind():
    manifest, samples, new, replay, retention, holdout, recipe = _inputs()
    binding = bind_forward_learning_populations(
        manifest,
        new_experiences=new,
        replay_experiences=replay,
        retention_suite=retention,
        holdout_suite=holdout,
        recipe=recipe,
        bound_at=BOUND_AT,
    )
    return binding, manifest, samples, new, replay, retention, holdout, recipe


def test_exact_four_way_binding_is_restart_and_authority_replay_verifiable() -> None:
    binding, manifest, _, new, replay, retention, holdout, recipe = _bind()

    assert verify_forward_learning_population_binding(
        binding,
        manifest,
        new_experiences=new,
        replay_experiences=replay,
        retention_suite=retention,
        holdout_suite=holdout,
        recipe=recipe,
    )
    restored = ForwardLearningPopulationBinding.from_payload(
        json.loads(json.dumps(binding.canonical_payload()))
    )
    assert restored == binding
    assert restored.binding_sha256 == binding.binding_sha256
    assert restored.profitability_claim is False
    assert restored.promotion_authorized is False
    assert restored.real_money_execution is False
    assert {member.role for member in restored.members} == set(PopulationRole)


def test_negative_new_sample_remains_denominator_evidence_not_reward() -> None:
    binding, _, samples, _, _, _, _, _ = _bind()
    negative = next(member for member in binding.members if member.sample_id == "sample-2")

    assert negative.consumer is PopulationConsumer.NEGATIVE_EVIDENCE
    assert negative.consumer_evidence_sha256 == samples[1].terminal_evidence_sha256


def test_negative_training_sample_cannot_be_smuggled_into_learner() -> None:
    manifest, samples, new, replay, retention, holdout, recipe = _inputs()
    forged_negative_reward = replace(
        new[0],
        sample_id=samples[1].sample_id,
        source_evidence_sha256=samples[1].source_evidence_sha256,
    )
    smuggled = (*new, forged_negative_reward)

    with pytest.raises(ForwardLearningPopulationBindingError, match="extra=.*sample-2"):
        bind_forward_learning_populations(
            manifest,
            new_experiences=smuggled,
            replay_experiences=replay,
            retention_suite=retention,
            holdout_suite=holdout,
            recipe=recipe,
            bound_at=BOUND_AT,
        )


def test_missing_or_role_swapped_learning_member_fails_closed() -> None:
    manifest, _, new, replay, retention, holdout, recipe = _inputs()

    with pytest.raises(ForwardLearningPopulationBindingError, match="new learning.*missing"):
        bind_forward_learning_populations(
            manifest,
            new_experiences=(),
            replay_experiences=replay,
            retention_suite=retention,
            holdout_suite=holdout,
            recipe=recipe,
            bound_at=BOUND_AT,
        )
    with pytest.raises(ForwardLearningPopulationBindingError, match="new learning"):
        bind_forward_learning_populations(
            manifest,
            new_experiences=replay,
            replay_experiences=new,
            retention_suite=retention,
            holdout_suite=holdout,
            recipe=recipe,
            bound_at=BOUND_AT,
        )


def test_training_source_evidence_must_match_manifest() -> None:
    manifest, _, new, replay, retention, holdout, recipe = _inputs()
    altered = (replace(new[0], source_evidence_sha256="f" * 64),)

    with pytest.raises(ForwardLearningPopulationBindingError, match="source evidence mismatch"):
        bind_forward_learning_populations(
            manifest,
            new_experiences=altered,
            replay_experiences=replay,
            retention_suite=retention,
            holdout_suite=holdout,
            recipe=recipe,
            bound_at=BOUND_AT,
        )


def test_training_decision_and_reward_must_match_campaign_evidence() -> None:
    manifest, samples, _, replay, retention, holdout, recipe = _inputs()
    later = _ts(
        datetime.fromisoformat(samples[0].decided_at.replace("Z", "+00:00"))
        + timedelta(seconds=1)
    )

    with pytest.raises(ForwardLearningPopulationBindingError, match="decision timestamp mismatch"):
        bind_forward_learning_populations(
            manifest,
            new_experiences=(_experience(samples[0], decided_at=later),),
            replay_experiences=replay,
            retention_suite=retention,
            holdout_suite=holdout,
            recipe=recipe,
            bound_at=BOUND_AT,
        )
    with pytest.raises(ForwardLearningPopulationBindingError, match="observed reward mismatch"):
        bind_forward_learning_populations(
            manifest,
            new_experiences=(_experience(samples[0], reward="999"),),
            replay_experiences=replay,
            retention_suite=retention,
            holdout_suite=holdout,
            recipe=recipe,
            bound_at=BOUND_AT,
        )


def test_evaluation_source_regime_and_outcome_time_are_exactly_bound() -> None:
    manifest, samples, new, replay, retention, holdout, recipe = _inputs()
    wrong_source = FrozenPolicySuite(
        retention.suite_id,
        retention.role,
        (replace(retention.cases[0], source_evidence_sha256="f" * 64),),
    )
    source_recipe = replace(recipe, retention_suite_sha256=wrong_source.suite_sha256)
    with pytest.raises(ForwardLearningPopulationBindingError, match="source evidence mismatch"):
        bind_forward_learning_populations(
            manifest,
            new_experiences=new,
            replay_experiences=replay,
            retention_suite=wrong_source,
            holdout_suite=holdout,
            recipe=source_recipe,
            bound_at=BOUND_AT,
        )

    wrong_regime = FrozenPolicySuite(
        retention.suite_id,
        retention.role,
        (_case(samples[3], regime_id="table-tennis:other"),),
    )
    regime_recipe = replace(recipe, retention_suite_sha256=wrong_regime.suite_sha256)
    with pytest.raises(ForwardLearningPopulationBindingError, match="regime mismatch"):
        bind_forward_learning_populations(
            manifest,
            new_experiences=new,
            replay_experiences=replay,
            retention_suite=wrong_regime,
            holdout_suite=holdout,
            recipe=regime_recipe,
            bound_at=BOUND_AT,
        )

    late_case = replace(
        retention.cases[0],
        reward_available_at="2026-10-03T08:59:59Z",
    )
    late_suite = FrozenPolicySuite(retention.suite_id, retention.role, (late_case,))
    late_recipe = replace(recipe, retention_suite_sha256=late_suite.suite_sha256)
    with pytest.raises(
        ForwardLearningPopulationBindingError,
        match="outcome availability mismatch",
    ):
        bind_forward_learning_populations(
            manifest,
            new_experiences=new,
            replay_experiences=replay,
            retention_suite=late_suite,
            holdout_suite=holdout,
            recipe=late_recipe,
            bound_at=BOUND_AT,
        )


def test_recipe_must_bind_exact_frozen_suites() -> None:
    manifest, _, new, replay, retention, holdout, recipe = _inputs()
    stale = replace(recipe, retention_suite_sha256="f" * 64)

    with pytest.raises(ForwardLearningPopulationBindingError, match="recipe retention"):
        bind_forward_learning_populations(
            manifest,
            new_experiences=new,
            replay_experiences=replay,
            retention_suite=retention,
            holdout_suite=holdout,
            recipe=stale,
            bound_at=BOUND_AT,
        )


def test_each_training_role_requires_real_action_and_respects_update_budget() -> None:
    _, samples, new, replay, retention, holdout, recipe = _inputs()
    negative_only_new = (
        _sample(1, role=PopulationRole.NEW, session=1, action=False),
        *samples[1:],
    )
    with pytest.raises(
        ForwardLearningPopulationBindingError,
        match="at least one observed ACTION",
    ):
        bind_forward_learning_populations(
            _manifest(negative_only_new),
            new_experiences=(),
            replay_experiences=replay,
            retention_suite=retention,
            holdout_suite=holdout,
            recipe=recipe,
            bound_at=BOUND_AT,
        )

    extra = _sample(6, role=PopulationRole.NEW, session=1, action=True, reward="0.25")
    expanded_samples = (*samples[:2], extra, *samples[2:])
    with pytest.raises(ForwardLearningPopulationBindingError, match="update budget"):
        bind_forward_learning_populations(
            _manifest(expanded_samples),
            new_experiences=(*new, _experience(extra)),
            replay_experiences=replay,
            retention_suite=retention,
            holdout_suite=holdout,
            recipe=recipe,
            bound_at=BOUND_AT,
        )


def test_binding_cannot_predate_complete_campaign_manifest() -> None:
    manifest, _, new, replay, retention, holdout, recipe = _inputs()

    with pytest.raises(ForwardLearningPopulationBindingError, match="cannot predate"):
        bind_forward_learning_populations(
            manifest,
            new_experiences=new,
            replay_experiences=replay,
            retention_suite=retention,
            holdout_suite=holdout,
            recipe=recipe,
            bound_at="2026-10-03T09:00:00Z",
        )


def test_payload_and_replayed_authority_tampering_are_rejected() -> None:
    binding, manifest, _, new, replay, retention, holdout, recipe = _bind()
    payload = binding.canonical_payload()
    payload["members"][0]["source_evidence_sha256"] = "f" * 64
    with pytest.raises(ForwardLearningPopulationBindingError):
        ForwardLearningPopulationBinding.from_payload(payload)

    altered_manifest = replace(manifest, scientific_protocol_sha256="f" * 64)
    with pytest.raises(ForwardLearningPopulationBindingError, match="replay mismatch"):
        verify_forward_learning_population_binding(
            binding,
            altered_manifest,
            new_experiences=new,
            replay_experiences=replay,
            retention_suite=retention,
            holdout_suite=holdout,
            recipe=recipe,
        )


@pytest.mark.parametrize(
    "claim",
    ("profitability_claim", "promotion_authorized", "real_money_execution"),
)
def test_binding_cannot_inflate_scientific_or_execution_authority(claim: str) -> None:
    binding, *_ = _bind()

    with pytest.raises(ForwardLearningPopulationBindingError, match=claim):
        replace(binding, **{claim: True})
