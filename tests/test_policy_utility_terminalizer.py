from __future__ import annotations

from datetime import datetime, timezone

import pytest

from autosport.policy_utility_evidence import (
    AuthorityRef,
    DecisionKind,
    PolicyUtilityError,
    PolicyUtilityEvidence,
    UtilityCompleteness,
    UtilityTruthClass,
)
from autosport.policy_utility_terminalizer import (
    PolicyUtilityTerminalizationError,
    PolicyUtilityTerminalizer,
    TerminalizationDisposition,
)


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
        "portfolio_identity": "portfolio-1",
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


def test_incomplete_evidence_closes_as_blocked_and_is_idempotent(tmp_path) -> None:
    terminalizer = PolicyUtilityTerminalizer.from_path(tmp_path / "utility.jsonl")
    evidence = _evidence()

    first = terminalizer.terminalize(evidence)
    retry = terminalizer.terminalize(evidence)

    assert first.disposition is TerminalizationDisposition.BLOCKED
    assert first.persisted is True
    assert retry == first.__class__(
        evidence_id=first.evidence_id,
        semantic_key=first.semantic_key,
        disposition=first.disposition,
        persisted=False,
    )
    assert terminalizer.resolve(first.evidence_id) == evidence


def test_unsupported_evidence_is_inconclusive_and_survives_restart(tmp_path) -> None:
    path = tmp_path / "utility.jsonl"
    evidence = _evidence(completeness=UtilityCompleteness.UNSUPPORTED)

    first = PolicyUtilityTerminalizer.from_path(path).terminalize(evidence)
    reopened = PolicyUtilityTerminalizer.from_path(path)

    assert first.disposition is TerminalizationDisposition.INCONCLUSIVE
    assert reopened.resolve(first.evidence_id) == evidence
    assert reopened.terminalize(evidence).persisted is False


def test_terminalizer_requires_exact_policy_utility_evidence() -> None:
    terminalizer = PolicyUtilityTerminalizer.from_path("utility.jsonl")
    with pytest.raises(TypeError, match="PolicyUtilityEvidence"):
        terminalizer.terminalize(object())  # type: ignore[arg-type]


def test_changed_evidence_for_same_causal_key_fails_closed(tmp_path) -> None:
    path = tmp_path / "utility.jsonl"
    first = _evidence()
    changed = _evidence(utility_definition_version="v2", utility_definition_sha256=SHA_A)
    terminalizer = PolicyUtilityTerminalizer.from_path(path)

    terminalizer.terminalize(first)
    with pytest.raises(PolicyUtilityError, match="semantic drift"):
        terminalizer.terminalize(changed)
