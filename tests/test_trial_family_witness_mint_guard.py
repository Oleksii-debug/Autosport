from __future__ import annotations

import pytest

import autosport._trial_family_cross_ledger_witness as cross_ledger
import autosport._trial_family_witness_mint_guard as mint_guard
from autosport.research_multiplicity import SequentialDecision
from autosport.scientific_registry import ResearchOutcome

from test_trial_family_accounting import (
    _experiment,
    _foundation,
    _look,
    _promotion_evidence,
    T1,
)


def test_generic_trial_event_append_cannot_mint_reserved_sequential_witness(tmp_path):
    _, _, _, _, _, _, store = _foundation(tmp_path)
    before = store.path.read_bytes()

    with pytest.raises(ValueError, match="reserved for product-owned cross-ledger witness"):
        store._append_event(
            cross_ledger._WITNESS_KIND,
            T1,
            {},
        )

    assert store.path.read_bytes() == before


def test_guard_module_does_not_export_pre_guard_append_capability():
    assert not hasattr(mint_guard, "_ORIGINAL_APPEND_EVENT")
    assert not hasattr(mint_guard, "_install_reserved_witness_mint_guard")


def test_promotion_lock_capability_is_closure_local(tmp_path, monkeypatch):
    registry, _, bundle, candidate, member, plan, store = _foundation(tmp_path)
    attempt = store.start_attempt(
        semantic_attempt_id="closure-local-promotion-lock",
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
    assert store.register_sequential_look(
        attempt_id=attempt.attempt_id,
        evidence=look,
        registry=registry,
    ) is SequentialDecision.REJECT_NULL
    promotion = _promotion_evidence(store=store, experiment=experiment, bundle=bundle)
    registry.append(promotion)

    class ReboundModuleLock:
        def __init__(self, *args, **kwargs):
            raise AssertionError("mutable module-global lock capability was dispatched")

    monkeypatch.setattr(mint_guard, "WorkspaceEconomicLock", ReboundModuleLock)
    snapshot = store.assert_promotion_evidence_eligible(
        evidence=promotion,
        registry=registry,
        accounted_attempt_count=1,
    )
    assert snapshot.total_attempts == snapshot.completed_attempts == snapshot.positive == 1
