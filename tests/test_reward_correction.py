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
        with pytest.raises(RewardCorrectionError, match="already belongs|advance exactly once"):
            ledger.append_correction(conflict)


def test_timestamp_aliases_canonicalize_before_identity_and_replay(tmp_path):
    path = tmp_path / "corrections.sqlite3"
    reward0 = ref("learning.reward", "reward-0")
    reward1 = ref("learning.reward", "reward-1")
    utc_alias = correction(
        superseded=reward0,
        corrected=reward1,
        available_at="2026-09-23T00:00:00+00:00",
    )
    offset_alias = correction(
        superseded=reward0,
        corrected=reward1,
        available_at="2026-09-23T02:00:00+02:00",
    )

    assert utc_alias.corrected_available_at == "2026-09-23T00:00:00Z"
    assert offset_alias.corrected_available_at == "2026-09-23T00:00:00Z"
    assert utc_alias == offset_alias
    assert utc_alias.correction_id == offset_alias.correction_id

    with RewardCorrectionLedger.create(path) as ledger:
        first = ledger.append_correction(utc_alias)
        assert ledger.append_correction(offset_alias) == first

    with RewardCorrectionLedger.open(path) as reopened:
        assert reopened.latest_correction(
            action_id=sha("action"),
            transition_id=sha("transition"),
        ) == utc_alias
        assert reopened.append_correction(offset_alias) == first


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
    raw.execute("UPDATE corrections SET corrected_reward_value='999' WHERE generation=1")
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
                "UPDATE corrections SET corrected_reward_value='1' WHERE correction_id=?",
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


def test_late_artifact_registration_cannot_turn_external_dependency_into_cycle(tmp_path):
    path = tmp_path / "corrections.sqlite3"
    a = ref("learning.policy", "a")
    b = ref("learning.policy", "b")
    with RewardCorrectionLedger.create(path) as ledger:
        ledger.append_artifact(DependencyArtifact(a, (b,)))
        with pytest.raises(RewardCorrectionError, match="already consumed"):
            ledger.append_artifact(DependencyArtifact(b, (a,)))


def test_reward_identity_cannot_branch_across_correction_lineages(tmp_path):
    path = tmp_path / "corrections.sqlite3"
    reward0 = ref("learning.reward", "reward-0")
    reward1 = ref("learning.reward", "reward-1")
    with RewardCorrectionLedger.create(path) as ledger:
        ledger.append_correction(correction(superseded=reward0, corrected=reward1))
        conflicting = RewardCorrectionAssertion(
            action_id=sha("other-action"),
            transition_id=sha("other-transition"),
            superseded_reward=reward0,
            corrected_reward=ref("learning.reward", "reward-other"),
            corrected_reward_value=Decimal("2"),
            corrected_available_at="2026-09-23T01:00:00Z",
            correction_source=ref("settlement.authority", "settlement-other"),
            generation=1,
        )
        with pytest.raises(RewardCorrectionError, match="already belongs"):
            ledger.append_correction(conflicting)


def test_reopen_rejects_orphan_invalidation_row(tmp_path):
    path = tmp_path / "corrections.sqlite3"
    reward0 = ref("learning.reward", "reward-0")
    node = artifact("learning.policy", "policy-1", reward0)
    with RewardCorrectionLedger.create(path) as ledger:
        ledger.append_artifact(node)
    raw = sqlite3.connect(path)
    raw.execute(
        "INSERT INTO invalidations VALUES (?,?,?,?)",
        (sha("missing-correction"), node.artifact.authority_family,
         node.artifact.evidence_id, node.artifact.evidence_sha256),
    )
    raw.commit()
    raw.close()
    with pytest.raises(RewardCorrectionError, match="orphan"):
        RewardCorrectionLedger.open(path)


def test_reopen_rejects_missing_performance_index(tmp_path):
    path = tmp_path / "corrections.sqlite3"
    with RewardCorrectionLedger.create(path):
        pass
    raw = sqlite3.connect(path)
    raw.execute("DROP INDEX dependencies_by_dependency")
    raw.commit()
    raw.close()
    with pytest.raises(RewardCorrectionError, match="performance indexes"):
        RewardCorrectionLedger.open(path)


def test_open_refuses_missing_ledger(tmp_path):
    with pytest.raises(RewardCorrectionError, match="does not exist"):
        RewardCorrectionLedger.open(tmp_path / "missing.sqlite3")


def test_reopen_rejects_hash_valid_second_lineage_reusing_reward_identity(tmp_path):
    path = tmp_path / "corrections.sqlite3"
    reward0 = ref("learning.reward", "reward-0")
    reward1 = ref("learning.reward", "reward-1")
    with RewardCorrectionLedger.create(path) as ledger:
        ledger.append_correction(correction(superseded=reward0, corrected=reward1))

    forged = RewardCorrectionAssertion(
        action_id=sha("forged-action"),
        transition_id=sha("forged-transition"),
        superseded_reward=reward1,
        corrected_reward=ref("learning.reward", "forged-new"),
        corrected_reward_value=Decimal("4"),
        corrected_available_at="2026-09-23T02:00:00Z",
        correction_source=ref("settlement.authority", "forged-source"),
        generation=1,
    )
    raw = sqlite3.connect(path)
    raw.execute(
        "INSERT INTO corrections VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            forged.correction_id, forged.action_id, forged.transition_id, forged.generation,
            forged.predecessor_correction_id,
            forged.superseded_reward.authority_family, forged.superseded_reward.evidence_id,
            forged.superseded_reward.evidence_sha256,
            forged.corrected_reward.authority_family, forged.corrected_reward.evidence_id,
            forged.corrected_reward.evidence_sha256, str(forged.corrected_reward_value),
            forged.corrected_available_at, forged.correction_source.authority_family,
            forged.correction_source.evidence_id, forged.correction_source.evidence_sha256,
            forged.correction_id,
        ),
    )
    raw.commit()
    raw.close()
    with pytest.raises(RewardCorrectionError, match="reuses durable reward identity"):
        RewardCorrectionLedger.open(path)


def test_decimal_reward_aliases_share_one_correction_identity_and_replay(tmp_path):
    path = tmp_path / "corrections.sqlite3"
    reward0 = ref("learning.reward", "reward-0")
    reward1 = ref("learning.reward", "reward-1")
    canonical = correction(superseded=reward0, corrected=reward1, value="0.25")
    aliased = correction(superseded=reward0, corrected=reward1, value="0.2500")
    assert canonical.correction_id == aliased.correction_id
    with RewardCorrectionLedger.create(path) as ledger:
        first = ledger.append_correction(canonical)
        replay = ledger.append_correction(aliased)
        assert replay == first
    raw = sqlite3.connect(path)
    assert raw.execute("SELECT corrected_reward_value FROM corrections").fetchone()[0] == "0.25"
    raw.close()
    with RewardCorrectionLedger.open(path) as reopened:
        assert reopened.latest_correction(action_id=sha("action"), transition_id=sha("transition")).corrected_reward_value == Decimal("0.25")


def test_decimal_negative_zero_alias_is_canonical_zero(tmp_path):
    reward0 = ref("learning.reward", "reward-0")
    reward1 = ref("learning.reward", "reward-1")
    positive = correction(superseded=reward0, corrected=reward1, value="0")
    negative = correction(superseded=reward0, corrected=reward1, value="-0.000")
    assert positive.correction_id == negative.correction_id
    path = tmp_path / "corrections.sqlite3"
    with RewardCorrectionLedger.create(path) as ledger:
        ledger.append_correction(negative)
    raw = sqlite3.connect(path)
    assert raw.execute("SELECT corrected_reward_value FROM corrections").fetchone()[0] == "0"
    raw.close()


def test_reopen_rejects_noncanonical_decimal_storage_even_with_parseable_value(tmp_path):
    path = tmp_path / "corrections.sqlite3"
    reward0 = ref("learning.reward", "reward-0")
    reward1 = ref("learning.reward", "reward-1")
    with RewardCorrectionLedger.create(path) as ledger:
        ledger.append_correction(correction(superseded=reward0, corrected=reward1, value="0.25"))
    raw = sqlite3.connect(path)
    raw.execute("DROP TRIGGER corrections_no_update")
    raw.execute("UPDATE corrections SET corrected_reward_value='0.2500'")
    raw.execute(
        "CREATE TRIGGER corrections_no_update BEFORE UPDATE ON corrections "
        "BEGIN SELECT RAISE(ABORT,'reward corrections are immutable'); END"
    )
    raw.commit(); raw.close()
    with pytest.raises(RewardCorrectionError, match="canonical Decimal"):
        RewardCorrectionLedger.open(path)




def test_reopen_rejects_same_name_noop_immutability_trigger(tmp_path):
    path = tmp_path / "corrections.sqlite3"
    with RewardCorrectionLedger.create(path):
        pass

    raw = sqlite3.connect(path)
    raw.execute("DROP TRIGGER corrections_no_update")
    raw.execute(
        "CREATE TRIGGER corrections_no_update BEFORE UPDATE ON corrections "
        "BEGIN SELECT 1; END"
    )
    raw.commit()
    raw.close()

    with pytest.raises(RewardCorrectionError, match="immutability triggers mismatch"):
        RewardCorrectionLedger.open(path)

def test_create_race_cannot_unlink_existing_winner(tmp_path, monkeypatch):
    path = tmp_path / "corrections.sqlite3"
    path_type = type(path)
    real_exists = path_type.exists
    stale_reads = 0

    def stale_exists(candidate):
        nonlocal stale_reads
        if candidate == path and stale_reads < 2:
            stale_reads += 1
            return False
        return real_exists(candidate)

    monkeypatch.setattr(path_type, "exists", stale_exists)

    with RewardCorrectionLedger.create(path) as ledger:
        ledger.verify_integrity()

    with pytest.raises(RewardCorrectionError, match="already exists"):
        RewardCorrectionLedger.create(path)

    assert real_exists(path)
    with RewardCorrectionLedger.open(path) as reopened:
        reopened.verify_integrity()

@pytest.mark.parametrize(
    "value",
    ("1E+512", "1E-512", "0E-512", "-0E-512"),
)
def test_extreme_decimal_scale_is_rejected_before_fixed_point_materialization(
    tmp_path, value
):
    path = tmp_path / "corrections.sqlite3"
    reward0 = ref("learning.reward", "reward-0")
    reward1 = ref("learning.reward", "reward-1")
    candidate = correction(superseded=reward0, corrected=reward1, value=value)

    with RewardCorrectionLedger.create(path) as ledger:
        with pytest.raises(RewardCorrectionError, match="fixed-point text exceeds"):
            ledger.append_correction(candidate)
        assert ledger.latest_correction(
            action_id=sha("action"), transition_id=sha("transition")
        ) is None


def test_positive_zero_exponent_does_not_false_trigger_resource_bound(tmp_path):
    path = tmp_path / "corrections.sqlite3"
    reward0 = ref("learning.reward", "reward-0")
    reward1 = ref("learning.reward", "reward-1")
    candidate = correction(
        superseded=reward0,
        corrected=reward1,
        value="0E+100000000",
    )

    with RewardCorrectionLedger.create(path) as ledger:
        ledger.append_correction(candidate)

    raw = sqlite3.connect(path)
    assert raw.execute(
        "SELECT corrected_reward_value FROM corrections"
    ).fetchone()[0] == "0"
    raw.close()


def test_decimal_resource_bound_preserves_normal_canonicalization(tmp_path):
    path = tmp_path / "corrections.sqlite3"
    reward0 = ref("learning.reward", "reward-0")
    reward1 = ref("learning.reward", "reward-1")
    candidate = correction(
        superseded=reward0,
        corrected=reward1,
        value="123.450000",
    )

    with RewardCorrectionLedger.create(path) as ledger:
        ledger.append_correction(candidate)

    raw = sqlite3.connect(path)
    assert raw.execute(
        "SELECT corrected_reward_value FROM corrections"
    ).fetchone()[0] == "123.45"
    raw.close()

