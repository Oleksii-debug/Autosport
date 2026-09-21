from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib

import pytest

from autosport.learning_environment import (
    CausalLearningEnvironment,
    EnvironmentIdentity,
    EvidenceTruth,
    Observation,
    Outcome,
    RewardEvidence,
)
from autosport.policy_update_authority import UTILITY_AUTHORITY_UNRESOLVED
from autosport.policy_utility_evidence import (
    AuthorityRef,
    DecisionKind,
    PolicyUtilityEvidence,
    PolicyUtilityStore,
    UtilityCompleteness,
    UtilityTruthClass,
)
from autosport.policy_utility_terminalizer import (
    PolicyUtilityTerminalizationError,
    PolicyUtilityTerminalizer,
    TerminalizationDisposition,
)
from autosport.transparent_bandit_policy import BanditPolicyState


UTC_NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64


def _evidence(**overrides: object) -> PolicyUtilityEvidence:
    values: dict[str, object] = {
        "environment_id": "env-1",
        "episode_id": "episode-1",
        "action_id": "action-1",
        "outcome_id": "outcome-1",
        "reward_id": "reward-1",
        "transition_id": "transition-1",
        "policy_id": "policy-1",
        "model_id": "model-1",
        "strategy_id": "strategy-1",
        "config_sha256": SHA_A,
        "protocol_sha256": SHA_B,
        "economic_goal_fingerprint": SHA_C,
        "risk_fingerprint": SHA_D,
        "bankroll_id": "bankroll-1",
        "portfolio_identity": "paper-portfolio",
        "utility_definition_family": "owner-net-utility",
        "utility_definition_version": "v1",
        "utility_definition_sha256": SHA_E,
        "completeness": UtilityCompleteness.INCOMPLETE,
        "truth_class": UtilityTruthClass.OBSERVED,
        "decision_kind": DecisionKind.POSITIONED,
        "available_at": UTC_NOW,
        "currency": None,
        "utility_value": None,
        "authority_refs": (
            AuthorityRef("campaign-economics", "econ-1", SHA_A),
        ),
        "denominator_ref": None,
        "counterfactual_ref": None,
        "support_count": None,
        "effective_sample_size": None,
        "uncertainty": None,
    }
    values.update(overrides)
    return PolicyUtilityEvidence(**values)  # type: ignore[arg-type]


def _blocked_learning_case():
    identity = EnvironmentIdentity(
        source_id="paper-replay-source-v1",
        config_id="terminalizer-config-v1",
        data_id="terminalizer-dataset-v1",
        protocol_id="rq-terminalizer-001",
        cutoff_ts="2026-09-20T12:00:02Z",
        seed=41,
    )
    environment = CausalLearningEnvironment(
        identity,
        episode_key="terminalizer-episode",
        policy_id="transparent-bandit-bootstrap-v1",
        admissible_actions=frozenset({"PAPER_PROPOSAL", "WAIT"}),
    )
    observation = Observation(
        environment_id=environment.environment_id,
        observed_at="2026-09-20T12:00:01Z",
        available_at="2026-09-20T12:00:02Z",
        evidence=(("quote", "2.10"),),
    )
    action = environment.act(
        observation,
        action_type="PAPER_PROPOSAL",
        decision_at="2026-09-20T12:00:03Z",
    )
    outcome = Outcome(
        environment_id=environment.environment_id,
        action_id=action.action_id,
        revealed_at="2026-09-20T12:10:00Z",
        truth=EvidenceTruth.OBSERVED,
        evidence=(("result", "home-win"),),
    )
    reward = RewardEvidence(
        environment_id=environment.environment_id,
        action_id=action.action_id,
        outcome_id=outcome.outcome_id,
        reward=Decimal("0.25"),
        available_at="2026-09-20T12:10:01Z",
        truth=EvidenceTruth.OBSERVED,
        evidence=(("paper_settlement", "canonical"),),
    )
    transition = environment.resolve(
        action.action_id,
        outcome=outcome,
        reward=reward,
        resolved_at="2026-09-20T12:10:02Z",
    )
    config_sha256 = hashlib.sha256(b"terminalizer-config-v1").hexdigest()
    policy = BanditPolicyState.initial(
        environment_id=environment.environment_id,
        protocol_id="rq-terminalizer-001",
        config_sha256=config_sha256,
        seed=41,
        action_types=frozenset({"PAPER_PROPOSAL", "WAIT"}),
    )
    utility = PolicyUtilityEvidence(
        environment_id=policy.environment_id,
        episode_id=transition.episode_id,
        action_id=action.action_id,
        outcome_id=reward.outcome_id,
        reward_id=reward.reward_id,
        transition_id=transition.transition_id,
        policy_id=policy.policy_id,
        model_id="transparent-bandit",
        strategy_id="paper-proposal",
        config_sha256=policy.config_sha256,
        protocol_sha256=hashlib.sha256(b"rq-terminalizer-001").hexdigest(),
        economic_goal_fingerprint=hashlib.sha256(b"goal").hexdigest(),
        risk_fingerprint=hashlib.sha256(b"risk").hexdigest(),
        bankroll_id="paper-bankroll",
        portfolio_identity="paper-portfolio",
        utility_definition_family="owner-net-utility",
        utility_definition_version="v1",
        utility_definition_sha256=hashlib.sha256(b"owner-net-utility-v1").hexdigest(),
        completeness=UtilityCompleteness.INCOMPLETE,
        truth_class=UtilityTruthClass.OBSERVED,
        decision_kind=DecisionKind.POSITIONED,
        available_at=UTC_NOW,
        currency="EUR",
        utility_value=Decimal("0.20"),
        authority_refs=(
            AuthorityRef(
                "campaign-economics",
                "campaign-evidence-v1",
                hashlib.sha256(b"campaign-economics").hexdigest(),
            ),
        ),
    )
    return policy, action, reward, transition, utility


def test_incomplete_evidence_journals_as_non_authoritative_blocked_and_is_idempotent(
    tmp_path,
) -> None:
    utility_store_path = tmp_path / "utility.jsonl"
    terminalizer = PolicyUtilityTerminalizer.from_path(utility_store_path)
    evidence = _evidence()

    first = terminalizer.terminalize(evidence)
    retry = terminalizer.terminalize(evidence)

    assert first.disposition is TerminalizationDisposition.BLOCKED
    assert first.persisted is True
    assert retry.evidence_id == first.evidence_id
    assert retry.semantic_key == first.semantic_key
    assert retry.disposition is first.disposition
    assert retry.record_id == first.record_id
    assert retry.persisted is False
    record = terminalizer.resolve(first.evidence_id)
    assert record.candidate_evidence_id == evidence.evidence_id
    assert record.candidate_semantic_key == evidence.semantic_key
    assert record.disposition is TerminalizationDisposition.BLOCKED
    assert not utility_store_path.exists()
    assert terminalizer.journal_path.exists()


def test_unsupported_evidence_is_inconclusive_and_survives_restart(tmp_path) -> None:
    path = tmp_path / "utility.jsonl"
    evidence = _evidence(completeness=UtilityCompleteness.UNSUPPORTED)

    first = PolicyUtilityTerminalizer.from_path(path).terminalize(evidence)
    reopened = PolicyUtilityTerminalizer.from_path(path)

    assert first.disposition is TerminalizationDisposition.INCONCLUSIVE
    assert reopened.resolve(first.evidence_id).disposition is TerminalizationDisposition.INCONCLUSIVE
    assert reopened.terminalize(evidence).persisted is False
    assert not path.exists()


def test_terminalizer_requires_exact_policy_utility_evidence(tmp_path) -> None:
    terminalizer = PolicyUtilityTerminalizer.from_path(tmp_path / "utility.jsonl")
    with pytest.raises(TypeError, match="PolicyUtilityEvidence"):
        terminalizer.terminalize(object())  # type: ignore[arg-type]


def test_terminalizer_rejects_policy_utility_subclass_before_journal_mutation(tmp_path) -> None:
    class ForgedPolicyUtilityEvidence(PolicyUtilityEvidence):
        pass

    base = _evidence()
    forged = ForgedPolicyUtilityEvidence(
        **{field: getattr(base, field) for field in base.__dataclass_fields__}
    )
    terminalizer = PolicyUtilityTerminalizer.from_path(tmp_path / "utility.jsonl")

    with pytest.raises(TypeError, match="PolicyUtilityEvidence"):
        terminalizer.terminalize(forged)
    assert not terminalizer.journal_path.exists()


def test_blocked_update_is_durable_replay_safe_and_preserves_champion(tmp_path) -> None:
    policy, action, reward, transition, utility = _blocked_learning_case()
    path = tmp_path / "utility.jsonl"

    first = PolicyUtilityTerminalizer.from_path(path).terminalize_blocked_update(
        policy=policy,
        action=action,
        reward=reward,
        transition=transition,
        utility=utility,
    )
    retry = PolicyUtilityTerminalizer.from_path(path).terminalize_blocked_update(
        policy=policy,
        action=action,
        reward=reward,
        transition=transition,
        utility=utility,
    )

    assert first.terminal.persisted is True
    assert retry.terminal.persisted is False
    assert first.update_evidence == retry.update_evidence
    assert first.champion_policy_id == retry.champion_policy_id == policy.policy_id
    assert first.update_evidence.reason_codes == (UTILITY_AUTHORITY_UNRESOLVED,)
    assert first.update_evidence.predecessor_policy_id == policy.policy_id
    assert first.update_evidence.successor_policy_id == policy.policy_id
    assert policy.generation == 0
    terminal = PolicyUtilityTerminalizer.from_path(path).resolve(utility.evidence_id)
    assert terminal.candidate_evidence_id == utility.evidence_id
    assert not path.exists()


def test_blocked_update_captures_utility_binding_mismatch_without_raw_reward_learning(
    tmp_path,
) -> None:
    policy, action, reward, transition, utility = _blocked_learning_case()
    mismatched = PolicyUtilityEvidence(
        **{
            field: (
                "different-reward" if field == "reward_id" else getattr(utility, field)
            )
            for field in utility.__dataclass_fields__
        }
    )

    receipt = PolicyUtilityTerminalizer.from_path(
        tmp_path / "utility.jsonl"
    ).terminalize_blocked_update(
        policy=policy,
        action=action,
        reward=reward,
        transition=transition,
        utility=mismatched,
    )

    assert receipt.champion_policy_id == policy.policy_id
    assert receipt.update_evidence.reason_codes == (
        UTILITY_AUTHORITY_UNRESOLVED,
        "utility_reward_mismatch",
    )
    assert receipt.update_evidence.successor_policy_id == policy.policy_id
    assert policy.generation == 0


def test_same_owner_scoped_causal_key_wrong_candidate_cannot_poison_canonical_utility_store(
    tmp_path,
) -> None:
    path = tmp_path / "utility.jsonl"
    canonical = _evidence()
    forged = _evidence(
        utility_definition_version="forged-v2",
        utility_definition_sha256=SHA_A,
        authority_refs=(AuthorityRef("campaign-economics", "forged", SHA_B),),
    )
    assert forged.semantic_key == canonical.semantic_key
    assert forged.evidence_id != canonical.evidence_id

    terminalizer = PolicyUtilityTerminalizer.from_path(path)
    forged_receipt = terminalizer.terminalize(forged)
    canonical_receipt = terminalizer.terminalize(canonical)

    assert forged_receipt.persisted is True
    assert canonical_receipt.persisted is True
    assert forged_receipt.record_id != canonical_receipt.record_id
    assert len(terminalizer.records()) == 2
    assert not path.exists()

    # Neither unresolved caller assertion may reserve the canonical store key.
    canonical_store = PolicyUtilityStore(path)
    assert canonical_store.list() == ()
    assert canonical_store.append(canonical) is True
    assert canonical_store.list() == (canonical,)
    assert terminalizer.resolve(forged.evidence_id).candidate_evidence_id == forged.evidence_id
    assert terminalizer.resolve(canonical.evidence_id).candidate_evidence_id == canonical.evidence_id


def test_terminal_receipt_rejects_invalid_champion_identity(tmp_path) -> None:
    policy, action, reward, transition, utility = _blocked_learning_case()
    receipt = PolicyUtilityTerminalizer.from_path(
        tmp_path / "utility.jsonl"
    ).terminalize_blocked_update(
        policy=policy,
        action=action,
        reward=reward,
        transition=transition,
        utility=utility,
    )
    assert receipt.champion_policy_id == policy.policy_id
    assert len(receipt.terminal.record_id) == 64
