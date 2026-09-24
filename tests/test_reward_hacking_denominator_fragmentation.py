from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import hashlib

import pytest

from autosport.policy_utility_evidence import (
    AuthorityRef,
    DecisionKind,
    PolicyUtilityError,
    PolicyUtilityEvidence,
    PolicyUtilityStore,
    UtilityCompleteness,
    UtilityTruthClass,
)


NOW = datetime(2026, 9, 21, 16, 10, tzinfo=timezone.utc)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _ref(label: str) -> AuthorityRef:
    return AuthorityRef(
        family=f"test.{label}",
        evidence_id=f"{label}-evidence",
        sha256=_sha(label),
    )


def _evidence(**overrides: object) -> PolicyUtilityEvidence:
    values: dict[str, object] = {
        "environment_id": "reward-hacking-environment",
        "episode_id": "episode-1",
        "action_id": "action-1",
        "outcome_id": "outcome-1",
        "reward_id": "reward-1",
        "transition_id": "transition-1",
        "policy_id": _sha("policy-1"),
        "model_id": "model-1",
        "strategy_id": "strategy-1",
        "config_sha256": _sha("config"),
        "protocol_sha256": _sha("protocol"),
        "economic_goal_fingerprint": _sha("goal"),
        "risk_fingerprint": _sha("risk"),
        "bankroll_id": "bankroll-1",
        "portfolio_identity": "portfolio-1",
        "utility_definition_family": "owner-net-utility",
        "utility_definition_version": "v1",
        "utility_definition_sha256": _sha("utility-definition"),
        "completeness": UtilityCompleteness.INCOMPLETE,
        "truth_class": UtilityTruthClass.OBSERVED,
        "decision_kind": DecisionKind.POSITIONED,
        "available_at": NOW,
        "currency": "EUR",
        "utility_value": Decimal("1.00"),
        "authority_refs": (_ref("economic-authority"),),
        "denominator_ref": _ref("complete-denominator"),
        "support_count": 1,
    }
    values.update(overrides)
    return PolicyUtilityEvidence(**values)


@pytest.mark.parametrize(
    "replacement",
    (
        {"denominator_ref": None},
        {"denominator_ref": _ref("winners-only-denominator")},
    ),
)
def test_same_causal_update_cannot_relabel_or_drop_denominator(
    tmp_path, replacement: dict[str, object]
) -> None:
    store = PolicyUtilityStore(tmp_path / "policy-utility.jsonl")
    original = _evidence()

    assert store.append(original) is True
    relabelled = replace(original, **replacement)

    assert relabelled.semantic_key == original.semantic_key
    assert relabelled.evidence_id != original.evidence_id
    with pytest.raises(PolicyUtilityError, match="semantic drift"):
        store.append(relabelled)

    reloaded = PolicyUtilityStore(store.path)
    assert reloaded.list() == (original,)


def test_same_causal_update_cannot_inflate_support_count(tmp_path) -> None:
    store = PolicyUtilityStore(tmp_path / "policy-utility.jsonl")
    original = _evidence(support_count=1)

    assert store.append(original) is True
    inflated = replace(
        original,
        support_count=10_000,
        effective_sample_size=Decimal("10000"),
        uncertainty=Decimal("0"),
    )

    assert inflated.semantic_key == original.semantic_key
    assert inflated.evidence_id != original.evidence_id
    with pytest.raises(PolicyUtilityError, match="semantic drift"):
        store.append(inflated)

    assert PolicyUtilityStore(store.path).list() == (original,)


def test_fragmented_observed_records_remain_non_authoritative_after_restart(
    tmp_path,
) -> None:
    store = PolicyUtilityStore(tmp_path / "policy-utility.jsonl")
    first = _evidence()
    second = _evidence(
        action_id="action-fragment-2",
        outcome_id="outcome-fragment-2",
        reward_id="reward-fragment-2",
        transition_id="transition-fragment-2",
    )

    assert first.semantic_key != second.semantic_key
    assert store.append(first) is True
    assert store.append(second) is True

    reloaded = PolicyUtilityStore(store.path)
    persisted = reloaded.list()
    assert {item.evidence_id for item in persisted} == {
        first.evidence_id,
        second.evidence_id,
    }
    for item in persisted:
        assert item.source_resolved is False
        assert item.policy_update_eligible is False
        with pytest.raises(
            PolicyUtilityError,
            match="product-owned authority re-resolution is required",
        ):
            item.require_policy_update_eligible()


def test_observed_wait_cannot_launder_positive_utility_even_with_refs() -> None:
    with pytest.raises(
        PolicyUtilityError,
        match="OBSERVED WAIT/NO_BET utility must be zero or absent",
    ):
        _evidence(
            decision_kind=DecisionKind.WAIT_NO_BET,
            utility_value=Decimal("0.01"),
            denominator_ref=_ref("complete-denominator"),
            counterfactual_ref=_ref("counterfactual"),
        )


def test_observed_wait_zero_utility_still_has_no_update_authority() -> None:
    wait = _evidence(
        decision_kind=DecisionKind.WAIT_NO_BET,
        utility_value=Decimal("0"),
    )

    assert wait.source_resolved is False
    assert wait.policy_update_eligible is False
    with pytest.raises(
        PolicyUtilityError,
        match="product-owned authority re-resolution is required",
    ):
        wait.require_policy_update_eligible()
