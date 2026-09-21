from __future__ import annotations

import pytest

import autosport._trial_family_cross_ledger_witness as cross_ledger
from autosport.research_multiplicity import SequentialDecision
from autosport.scientific_registry import ResearchOutcome

from test_trial_family_accounting import (
    _experiment,
    _foundation,
    _look,
    _promotion_evidence,
    T1,
)


def _completed_positive(tmp_path):
    registry, _, bundle, candidate, member, plan, store = _foundation(tmp_path)
    attempt = store.start_attempt(
        semantic_attempt_id="cross-ledger-positive",
        member_authority_id=member.member_authority_id,
        candidate=candidate,
        created_at=T1,
    )
    registry.append(bundle)
    experiment = _experiment(outcome=ResearchOutcome.POSITIVE)
    registry.append(experiment)
    store.complete_attempt(
        attempt_id=attempt.attempt_id,
        experiment_id=experiment.experiment_id,
        registry=registry,
    )
    look = _look(plan, member, bundle, experiment)
    promotion = _promotion_evidence(store=store, experiment=experiment, bundle=bundle)
    return registry, bundle, member, plan, store, attempt, experiment, look, promotion


def test_promotion_first_then_backdated_terminal_look_cannot_authorize_old_evidence(tmp_path):
    registry, _, _, _, store, attempt, _, look, promotion = _completed_positive(tmp_path)

    # Freeze the exact promotion record first. The later durable look carries an
    # earlier product timestamp, reproducing the cross-ledger causal exploit.
    registry.append(promotion)
    assert store.register_sequential_look(
        attempt_id=attempt.attempt_id,
        evidence=look,
        registry=registry,
    ) is SequentialDecision.REJECT_NULL

    with pytest.raises(
        ValueError,
        match="already durable before sequential-look registration witness",
    ):
        store.assert_promotion_evidence_eligible(
            evidence=promotion,
            registry=registry,
            accounted_attempt_count=1,
        )


def test_terminal_look_witness_first_then_promotion_remains_eligible(tmp_path):
    registry, _, _, _, store, attempt, _, look, promotion = _completed_positive(tmp_path)

    assert store.register_sequential_look(
        attempt_id=attempt.attempt_id,
        evidence=look,
        registry=registry,
    ) is SequentialDecision.REJECT_NULL
    registry.append(promotion)

    snapshot = store.assert_promotion_evidence_eligible(
        evidence=promotion,
        registry=registry,
        accounted_attempt_count=1,
    )
    assert snapshot.total_attempts == snapshot.completed_attempts == snapshot.positive == 1


def test_orphan_sequential_append_never_authorizes_promotion(tmp_path, monkeypatch):
    registry, _, _, _, store, attempt, _, look, promotion = _completed_positive(tmp_path)

    def crash_before_witness(*args, **kwargs):
        raise RuntimeError("fault after sequential append before cross-ledger witness")

    monkeypatch.setattr(cross_ledger, "_publish_sequential_witness", crash_before_witness)
    with pytest.raises(RuntimeError, match="fault after sequential append"):
        store.register_sequential_look(
            attempt_id=attempt.attempt_id,
            evidence=look,
            registry=registry,
        )
    assert len(store._sequential().assessments()) == 1

    registry.append(promotion)
    with pytest.raises(ValueError, match="exact sequential-look registry witness"):
        store.assert_promotion_evidence_eligible(
            evidence=promotion,
            registry=registry,
            accounted_attempt_count=1,
        )


def test_retry_recovers_orphan_witness_before_promotion_without_minting_second_look(
    tmp_path,
    monkeypatch,
):
    registry, _, _, _, store, attempt, _, look, promotion = _completed_positive(tmp_path)
    original_publish = cross_ledger._publish_sequential_witness
    calls = 0

    def crash_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("fault after sequential append before cross-ledger witness")
        return original_publish(*args, **kwargs)

    monkeypatch.setattr(cross_ledger, "_publish_sequential_witness", crash_once)
    with pytest.raises(RuntimeError, match="fault after sequential append"):
        store.register_sequential_look(
            attempt_id=attempt.attempt_id,
            evidence=look,
            registry=registry,
        )

    assert store.register_sequential_look(
        attempt_id=attempt.attempt_id,
        evidence=look,
        registry=registry,
    ) is SequentialDecision.REJECT_NULL
    assert len(store._sequential().assessments()) == 1

    registry.append(promotion)
    snapshot = store.assert_promotion_evidence_eligible(
        evidence=promotion,
        registry=registry,
        accounted_attempt_count=1,
    )
    assert snapshot.positive == 1
