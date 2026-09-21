from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.economic_goal import EconomicGoalContract
from autosport.economic_goal_provenance import provenance_for
from autosport.paper_risk_policy_store import (
    PAPER_RISK_POLICY_SCHEMA,
    PAPER_RISK_POLICY_SCHEMA_VERSION,
    PaperRiskPolicyStore,
    PaperRiskPolicyStoreError,
    paper_risk_policy_from_json,
    paper_risk_policy_from_payload,
    paper_risk_policy_to_payload,
)
from autosport.risk import PaperRiskPolicy


def _goal(**changes: object) -> EconomicGoalContract:
    values: dict[str, object] = {
        "goal_id": "owner-paper-campaign",
        "revision": 1,
        "bankroll_id": "paper-main",
        "currency": "EUR",
    }
    values.update(changes)
    return EconomicGoalContract(**values)  # type: ignore[arg-type]


def _policy(goal: EconomicGoalContract | None) -> PaperRiskPolicy:
    return PaperRiskPolicy(
        max_ticket_fraction=Decimal("0.017"),
        max_committed_fraction=Decimal("0.133"),
        minimum_cash_reserve_fraction=Decimal("0.271"),
        economic_goal=goal,
    )


def test_payload_roundtrip_binds_exact_goal_and_policy_provenance() -> None:
    goal = _goal()
    policy = _policy(goal)

    payload = paper_risk_policy_to_payload(policy)
    body = payload["policy"]

    assert payload["schema"] == PAPER_RISK_POLICY_SCHEMA
    assert payload["schema_version"] == PAPER_RISK_POLICY_SCHEMA_VERSION == 1
    assert isinstance(body, dict)
    assert body["max_ticket_fraction"] == "0.017"
    assert body["max_committed_fraction"] == "0.133"
    assert body["minimum_cash_reserve_fraction"] == "0.271"
    assert (
        body["economic_goal_contract_sha256"]
        == provenance_for(goal).contract_sha256
    )
    assert payload["policy_provenance_sha256"] == policy.provenance_sha256
    assert paper_risk_policy_from_payload(payload, economic_goal=goal) == policy


def test_store_roundtrip_survives_fresh_restart_instance(tmp_path) -> None:
    goal = _goal(goal_id="кампанія-власника")
    policy = _policy(goal)
    store = PaperRiskPolicyStore(tmp_path)
    store.initialize_owner(policy)
    first_bytes = store.path.read_bytes()

    restarted_store = PaperRiskPolicyStore(tmp_path)

    assert restarted_store.load(economic_goal=goal) == policy
    assert restarted_store.path.read_bytes() == first_bytes


def test_owner_initialization_is_creation_only_and_preserves_existing_bytes(
    tmp_path,
) -> None:
    goal = _goal()
    store = PaperRiskPolicyStore(tmp_path)
    store.initialize_owner(_policy(goal))
    before = store.path.read_bytes()

    with pytest.raises(PaperRiskPolicyStoreError, match="already exists"):
        store.initialize_owner(
            PaperRiskPolicy(
                max_ticket_fraction=Decimal("0.010"),
                max_committed_fraction=Decimal("0.100"),
                minimum_cash_reserve_fraction=Decimal("0.300"),
                economic_goal=goal,
            )
        )

    assert store.path.read_bytes() == before
    assert PaperRiskPolicyStore(tmp_path).load(economic_goal=goal) == _policy(goal)


def test_reconstruction_rejects_different_economic_goal_revision(tmp_path) -> None:
    goal = _goal()
    store = PaperRiskPolicyStore(tmp_path)
    store.initialize_owner(_policy(goal))

    different_goal = replace(goal, revision=2)

    with pytest.raises(
        PaperRiskPolicyStoreError,
        match="economic goal contract does not match",
    ):
        store.load(economic_goal=different_goal)


def test_fraction_tamper_with_stale_policy_provenance_fails_closed() -> None:
    goal = _goal()
    payload = paper_risk_policy_to_payload(_policy(goal))
    body = payload["policy"]
    assert isinstance(body, dict)
    body["max_ticket_fraction"] = "0.016"

    with pytest.raises(PaperRiskPolicyStoreError, match="provenance mismatch"):
        paper_risk_policy_from_payload(payload, economic_goal=goal)


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_schema",
        "wrong_version",
        "extra_root_key",
        "extra_policy_key",
        "numeric_decimal",
        "bad_goal_sha",
        "bad_policy_sha",
    ],
)
def test_payload_rejects_malformed_or_ambiguous_authority(mutation: str) -> None:
    goal = _goal()
    payload = paper_risk_policy_to_payload(_policy(goal))
    body = payload["policy"]
    assert isinstance(body, dict)

    if mutation == "wrong_schema":
        payload["schema"] = "autosport.other"
    elif mutation == "wrong_version":
        payload["schema_version"] = 2
    elif mutation == "extra_root_key":
        payload["unexpected"] = True
    elif mutation == "extra_policy_key":
        body["unexpected"] = "authority"
    elif mutation == "numeric_decimal":
        body["max_ticket_fraction"] = 0.017
    elif mutation == "bad_goal_sha":
        body["economic_goal_contract_sha256"] = "A" * 64
    elif mutation == "bad_policy_sha":
        payload["policy_provenance_sha256"] = "z" * 64
    else:  # pragma: no cover - exhaustive guard for future edits
        raise AssertionError(mutation)

    with pytest.raises(PaperRiskPolicyStoreError):
        paper_risk_policy_from_payload(payload, economic_goal=goal)


def test_strict_json_rejects_duplicate_schema_key() -> None:
    duplicate = (
        '{"schema":"autosport.paper_risk_policy",'
        '"schema":"autosport.other","schema_version":1,'
        '"policy":{},"policy_provenance_sha256":"' + ("0" * 64) + '"}'
    )

    with pytest.raises(PaperRiskPolicyStoreError, match="invalid paper risk policy JSON"):
        paper_risk_policy_from_json(duplicate, economic_goal=_goal())


def test_corrupt_durable_file_fails_closed_on_restart(tmp_path) -> None:
    goal = _goal()
    store = PaperRiskPolicyStore(tmp_path)
    store.initialize_owner(_policy(goal))
    store.path.write_text('{"schema":', encoding="utf-8")

    with pytest.raises(PaperRiskPolicyStoreError, match="invalid paper risk policy JSON"):
        store.load(economic_goal=goal)


def test_unbound_policy_roundtrip_requires_exact_unbound_state() -> None:
    policy = _policy(None)
    payload = paper_risk_policy_to_payload(policy)

    assert paper_risk_policy_from_payload(payload, economic_goal=None) == policy

    with pytest.raises(
        PaperRiskPolicyStoreError,
        match="economic goal contract does not match",
    ):
        paper_risk_policy_from_payload(payload, economic_goal=_goal())
