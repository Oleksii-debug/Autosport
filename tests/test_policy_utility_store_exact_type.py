from __future__ import annotations

from datetime import datetime, timezone

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


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64


def _values(**overrides: object) -> dict[str, object]:
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
        "portfolio_identity": "portfolio-1",
        "utility_definition_family": "owner-net-utility",
        "utility_definition_version": "v1",
        "utility_definition_sha256": SHA_E,
        "completeness": UtilityCompleteness.INCOMPLETE,
        "truth_class": UtilityTruthClass.OBSERVED,
        "decision_kind": DecisionKind.POSITIONED,
        "available_at": datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc),
        "authority_refs": (
            AuthorityRef(
                family="campaign-economics",
                evidence_id="econ-1",
                sha256=SHA_A,
            ),
        ),
    }
    values.update(overrides)
    return values


class ForgedPolicyUtilityEvidence(PolicyUtilityEvidence):
    pass


def test_store_rejects_subclass_before_any_durable_publication(tmp_path) -> None:
    path = tmp_path / "utility.jsonl"
    store = PolicyUtilityStore(path)

    first = PolicyUtilityEvidence(**_values())  # type: ignore[arg-type]
    assert store.append(first) is True
    before = path.read_bytes()

    forged = ForgedPolicyUtilityEvidence(  # type: ignore[arg-type]
        **_values(episode_id="episode-2")
    )
    with pytest.raises(PolicyUtilityError, match="exact PolicyUtilityEvidence"):
        store.append(forged)

    assert path.read_bytes() == before
    assert PolicyUtilityStore(path).list() == (first,)

    canonical = PolicyUtilityEvidence(  # type: ignore[arg-type]
        **_values(episode_id="episode-2")
    )
    assert store.append(canonical) is True
    after = path.read_bytes()
    assert store.append(canonical) is False
    assert path.read_bytes() == after
    assert PolicyUtilityStore(path).get(canonical.evidence_id) == canonical
