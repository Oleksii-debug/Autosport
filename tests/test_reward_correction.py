from __future__ import annotations

import sqlite3
from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from autosport.reward_correction import (
    DependencyArtifact,
    EvidenceRef,
    RewardCorrectionAssertion,
    RewardCorrectionError,
    RewardCorrectionLedger,
)


def digest(seed: str) -> str:
    import hashlib
    return hashlib.sha256(seed.encode()).hexdigest()


def ref(family: str, seed: str) -> EvidenceRef:
    return EvidenceRef(family, seed, digest(f"sha:{family}:{seed}"))


def sha(seed: str) -> str:
    return digest(seed)


def artifact(family: str, seed: str, *dependencies: EvidenceRef) -> DependencyArtifact:
    return DependencyArtifact(ref(family, seed), tuple(sorted(dependencies)))


def correction(
    *,
    superseded: EvidenceRef,
    corrected: EvidenceRef,
    generation: int = 1,
    predecessor: str | None = None,
    available_at: str = "2026-09-23T00:00:00Z",
    value: str = "-1.25",
    source_seed: str = "settlement-1",
) -> RewardCorrectionAssertion:
    return RewardCorrectionAssertion(
        action_id=sha("action"),
        transition_id=sha("transition"),
        superseded_reward=superseded,
        corrected_reward=corrected,
        corrected_reward_value=Decimal(value),
        corrected_available_at=available_at,
        correction_source=ref("settlement.authority", source_seed),
        generation=generation,
        predecessor_correction_id=predecessor,
    )


def test_correction_invalidates_exact_transitive_descendants_only(tmp_path):
    path = tmp_path / "corrections.sqlite3"
    reward0 = ref("learning.reward", "reward-0")
    other_reward = ref("learning.reward", "reward-other")
    update = artifact("learning.policy-update", "update-1", reward0)
    policy = artifact("learning.policy", "policy-1", update.artifact)
    evaluation = artifact("science.evaluation", "evaluation-1", policy.artifact)
    promotion = artifact("science.promotion", "promotion-1", evaluation.artifact)
    unrelated = artifact("learning.policy", "unrelated-policy", other_reward)

    with RewardCorrectionLedger.create(path) as ledger:
        for node in (update, policy, evaluation, promotion, unrelated):
            ledger.append_artifact(node)

        reward1 = ref("learning.reward", "reward-1")
        receipt = ledger.append_correction(
            correction(superseded=reward0, corrected=reward1)
        )

        assert receipt.invalidated_artifacts == tuple(
            sorted((update.artifact, policy.artifact, evaluation.artifact, promotion.artifact))
        )
        assert all(
            ledger.is_invalidated(item)
            for item in (update.artifact, policy.artifact, evaluation.artifact, promotion.artifact)
        )
        assert not ledger.is_invalidated(unrelated.artifact)
        assert receipt.source_origin_proven is False
        assert receipt.policy_replay_authorized is False
        assert receipt.promotion_authorized is False


def test_no_new_descendant_can_depend_on_superseded_or_invalidated_truth(tmp_path):
    path = tmp_path / "corrections.sqlite3"
    reward0 = ref("learning.reward", "reward-0")
    update = artifact("learning.policy-update", "update-1", reward0)
    policy = artifact("learning.policy", "policy-1", update.artifact)
    with RewardCorrectionLedger.create(path) as ledger:
        ledger.append_artifact(update)
        ledger.append_artifact(policy)
        ledger.append_correction(
            correction(superseded=reward0, corrected=ref("learning.reward", "reward-1"))
        )
        with pytest.raises(RewardCorrectionError, match="superseded reward"):
            ledger.append_artifact(artifact("science.evaluation", "late-1", reward0))
        with pytest.raises(RewardCorrectionError, match="invalidated evidence"):
            ledger.append_artifact(
                artifact("science.evaluation", "late-2", policy.artifact)
            )


def test_same_correction_is_idempotent_but_conflicting_generation_is_rejected(tmp_path):
    path = tmp_path / "corrections.sqlite3"
    reward0 = ref("learning.reward", "reward-0")
    reward1 = ref("learning.reward", "reward-1")
    exact = correction(superseded=reward0, corrected=reward1)
    conflict = correction(
        superseded=reward0,
        corrected=ref("learning.reward", "reward-2"),
        value="3.0",
    )
    with RewardCorrectionLedger.create(path) as ledger:
        first = ledger.append_correction(exact)
        replay = ledger.append_correction(exact)
        assert replay == first
        with pytest.raises(RewardCorrectionError, match="advance exactly once"):
            ledger.append_correction(conflict)


def test_second_correction_requires_exact_predecessor_reward_and_causal_time(tmp_path):
    path = tmp_path / "corrections.sqlite3"
    reward0 = ref("learning.reward", "reward-0")
    reward1 = ref("learning.reward", "reward-1")
    reward2 = ref("learning.reward", "reward-2")
    with RewardCorrectionLedger.create(path) as ledger:
        first = correction(
            superseded=reward0,
            corrected=reward1,
            available_at="2026-09-23T00:10:00Z",
        )
        ledger.append_correction(first)

        with pytest.raises(RewardCorrectionError, match="prior corrected reward"):
            ledger.append_correction(
                correction(
                    superseded=reward0,
                    corrected=reward2,
                    generation=2,
                    predecessor=first.correction_id,
                    available_at="2026-09-23T00:20:00Z",
                )
            )
        with pytest.raises(RewardCorrectionError, match="cannot move backward"):
            ledger.append_correction(
                correction(
                    superseded=reward1,
                    corrected=reward2,
                    generation=2,
                    predecessor=first.correction_id,
                    available_at="2026-09-23T00:09:59Z",
                )
            )

        second = correction(
            superseded=reward1,
            corrected=reward2,
            generation=2,
            predecessor=first.correction_id,
            available_at="2026-09-23T00:20:00Z",
            value="0.5",
            source_seed="settlement-2",
        )
        receipt = ledger.append_correction(second)
        assert receipt.generation == 2
        assert ledger.latest_correction(
            action_id=sha("action"), transition_id=sha("transition")
        ) == second


def test_later_generation_invalidates_only_artifacts_built_from_that_generation(tmp_path):
    path = tmp_path / "corrections.sqlite3"
    reward0 = ref("learning.reward", "reward-0")
    reward1 = ref("learning.reward", "reward-1")
    reward2 = ref("learning.reward", "reward-2")
    with RewardCorrectionLedger.create(path) as ledger:
        first = correction(superseded=reward0, corrected=reward1)
        ledger.append_correction(first)
        corrected_update = artifact("learning.policy-update", "corrected-update", reward1)
        corrected_policy = artifact("learning.policy", "corrected-policy", corrected_update.artifact)
        ledger.append_artifact(corrected_update)
        ledger.append_artifact(corrected_policy)

        second = correction(
            superseded=reward1,
            corrected=reward2,
            generation=2,
            predecessor=first.correction_id,
            available_at="2026-09-23T00:20:00Z",
            source_seed="settlement-2",
        )
        receipt = ledger.append_correction(second)
        assert receipt.invalidated_artifacts == tuple(
            sorted((corrected_update.artifact, corrected_policy.artifact))
        )


def test_restart_rederives_same_chain_and_invalidation(tmp_path):
    path = tmp_path / "corrections.sqlite3"
    reward0 = ref("learning.reward", "reward-0")
    reward1 = ref("learning.reward", "reward-1")
    update = artifact("learning.policy-update", "update-1", reward0)
    with RewardCorrectionLedger.create(path) as ledger:
        ledger.append_artifact(update)
        expected = correction(superseded=reward0, corrected=reward1)
        receipt = ledger.append_correction(expected)
        assert ledger.is_invalidated(update.artifact)

    with RewardCorrectionLedger.open(path) as reopened:
        assert reopened.latest_correction(
            action_id=sha("action"), transition_id=sha("transition")
        ) == expected
        assert reopened.is_invalidated(update.artifact)
        assert reopened.append_correction(expected) == receipt
        reopened.verify_integrity()


def test_tampered_correction_bytes_fail_on_reopen(tmp_path):
    path = tmp_path / "corrections.sqlite3"
    reward0 = ref("learning.reward", "reward-0")
    reward1 = ref("learning.reward", "reward-1")
    with RewardCorrectionLedger.create(path) as ledger:
        ledger.append_correction(correction(superseded=reward0, corrected=reward1))

    raw = sqlite3.connect(path)
    raw.execute("DROP TRIGGER corrections_no_update")
    raw.execute("UPDATE corrections SET payload_json='{}' WHERE generation=1")
    raw.commit()
    raw.close()

    with pytest.raises(RewardCorrectionError):
        RewardCorrectionLedger.open(path)


def test_sql_mutation_is_blocked_by_immutability_triggers(tmp_path):
    path = tmp_path / "corrections.sqlite3"
    reward0 = ref("learning.reward", "reward-0")
    reward1 = ref("learning.reward", "reward-1")
    with RewardCorrectionLedger.create(path) as ledger:
        current = correction(superseded=reward0, corrected=reward1)
        ledger.append_correction(current)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            ledger._connection.execute(
                "UPDATE corrections SET payload_json='{}' WHERE correction_id=?",
                (current.correction_id,),
            )


def test_receipt_truth_flags_cannot_be_constructed_true(tmp_path):
    path = tmp_path / "corrections.sqlite3"
    reward0 = ref("learning.reward", "reward-0")
    reward1 = ref("learning.reward", "reward-1")
    with RewardCorrectionLedger.create(path) as ledger:
        receipt = ledger.append_correction(correction(superseded=reward0, corrected=reward1))
        with pytest.raises(FrozenInstanceError):
            receipt.policy_replay_authorized = True


def test_artifact_replay_is_idempotent_and_conflicting_digest_fails(tmp_path):
    path = tmp_path / "corrections.sqlite3"
    reward0 = ref("learning.reward", "reward-0")
    node = artifact("learning.policy-update", "update-1", reward0)
    with RewardCorrectionLedger.create(path) as ledger:
        assert ledger.append_artifact(node) == node.artifact
        assert ledger.append_artifact(node) == node.artifact
        conflicting_ref = EvidenceRef(
            node.artifact.authority_family,
            node.artifact.evidence_id,
            digest("different"),
        )
        conflict = DependencyArtifact(conflicting_ref, node.dependencies)
        with pytest.raises(RewardCorrectionError, match="bound differently"):
            ledger.append_artifact(conflict)


def test_validation_rejects_noncanonical_inputs():
    good = ref("learning.reward", "r0")
    with pytest.raises(RewardCorrectionError, match="lowercase"):
        EvidenceRef("learning.reward", "bad", digest("x").upper())
    with pytest.raises(RewardCorrectionError, match="distinct reward"):
        correction(superseded=good, corrected=good)
    dep_a = ref("z.family", "a")
    dep_b = ref("a.family", "b")
    with pytest.raises(RewardCorrectionError, match="sorted"):
        DependencyArtifact(ref("artifact", "x"), (dep_a, dep_b))


def test_open_refuses_missing_ledger(tmp_path):
    with pytest.raises(RewardCorrectionError, match="does not exist"):
        RewardCorrectionLedger.open(tmp_path / "missing.sqlite3")
