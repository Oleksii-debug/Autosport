from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

import autosport.paper_risk_policy_store as risk_store_module
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


def _weaker_policy(goal: EconomicGoalContract) -> PaperRiskPolicy:
    return PaperRiskPolicy(
        max_ticket_fraction=Decimal("0.050"),
        max_committed_fraction=Decimal("0.400"),
        minimum_cash_reserve_fraction=Decimal("0.050"),
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
    assert paper_risk_policy_from_payload(
        payload,
        economic_goal=goal,
        expected_policy_provenance_sha256=policy.provenance_sha256,
    ) == policy


def test_store_roundtrip_survives_fresh_restart_instance(tmp_path) -> None:
    goal = _goal(goal_id="кампанія-власника")
    policy = _policy(goal)
    store = PaperRiskPolicyStore(tmp_path)
    store.initialize_owner(policy)
    first_bytes = store.path.read_bytes()

    restarted_store = PaperRiskPolicyStore(tmp_path)

    assert restarted_store.load(
        economic_goal=goal,
        expected_policy_provenance_sha256=policy.provenance_sha256,
    ) == policy
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
    expected = _policy(goal)
    assert PaperRiskPolicyStore(tmp_path).load(
        economic_goal=goal,
        expected_policy_provenance_sha256=expected.provenance_sha256,
    ) == expected


def test_reconstruction_rejects_different_economic_goal_revision(tmp_path) -> None:
    goal = _goal()
    store = PaperRiskPolicyStore(tmp_path)
    policy = _policy(goal)
    store.initialize_owner(policy)

    different_goal = replace(goal, revision=2)

    with pytest.raises(
        PaperRiskPolicyStoreError,
        match="economic goal contract does not match",
    ):
        store.load(
            economic_goal=different_goal,
            expected_policy_provenance_sha256=policy.provenance_sha256,
        )


def test_fraction_tamper_with_stale_policy_provenance_fails_closed() -> None:
    goal = _goal()
    policy = _policy(goal)
    payload = paper_risk_policy_to_payload(policy)
    body = payload["policy"]
    assert isinstance(body, dict)
    body["max_ticket_fraction"] = "0.016"

    with pytest.raises(PaperRiskPolicyStoreError, match="provenance mismatch"):
        paper_risk_policy_from_payload(
            payload,
            economic_goal=goal,
            expected_policy_provenance_sha256=policy.provenance_sha256,
        )


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
    policy = _policy(goal)
    payload = paper_risk_policy_to_payload(policy)
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
        paper_risk_policy_from_payload(
            payload,
            economic_goal=goal,
            expected_policy_provenance_sha256=policy.provenance_sha256,
        )


def test_coherent_fraction_and_self_digest_rewrite_cannot_rebind_external_authority() -> None:
    goal = _goal()
    owner_policy = _policy(goal)
    payload = paper_risk_policy_to_payload(owner_policy)
    body = payload["policy"]
    assert isinstance(body, dict)

    rebound_policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("0.016"),
        max_committed_fraction=owner_policy.max_committed_fraction,
        minimum_cash_reserve_fraction=owner_policy.minimum_cash_reserve_fraction,
        economic_goal=goal,
    )
    body["max_ticket_fraction"] = "0.016"
    payload["policy_provenance_sha256"] = rebound_policy.provenance_sha256

    with pytest.raises(
        PaperRiskPolicyStoreError,
        match="does not match external authority",
    ):
        paper_risk_policy_from_payload(
            payload,
            economic_goal=goal,
            expected_policy_provenance_sha256=owner_policy.provenance_sha256,
        )


def test_external_policy_authority_must_be_canonical_sha256() -> None:
    goal = _goal()
    policy = _policy(goal)
    payload = paper_risk_policy_to_payload(policy)

    with pytest.raises(
        PaperRiskPolicyStoreError,
        match="expected_policy_provenance_sha256",
    ):
        paper_risk_policy_from_payload(
            payload,
            economic_goal=goal,
            expected_policy_provenance_sha256="A" * 64,
        )


def test_strict_json_rejects_duplicate_schema_key() -> None:
    duplicate = (
        '{"schema":"autosport.paper_risk_policy",'
        '"schema":"autosport.other","schema_version":1,'
        '"policy":{},"policy_provenance_sha256":"' + ("0" * 64) + '"}'
    )

    goal = _goal()
    policy = _policy(goal)
    with pytest.raises(PaperRiskPolicyStoreError, match="invalid paper risk policy JSON"):
        paper_risk_policy_from_json(
            duplicate,
            economic_goal=goal,
            expected_policy_provenance_sha256=policy.provenance_sha256,
        )


def test_corrupt_durable_file_fails_closed_on_restart(tmp_path) -> None:
    goal = _goal()
    store = PaperRiskPolicyStore(tmp_path)
    policy = _policy(goal)
    store.initialize_owner(policy)
    store.path.write_text('{"schema":', encoding="utf-8")

    with pytest.raises(PaperRiskPolicyStoreError, match="invalid paper risk policy JSON"):
        store.load(
            economic_goal=goal,
            expected_policy_provenance_sha256=policy.provenance_sha256,
        )


def test_unbound_policy_roundtrip_requires_exact_unbound_state() -> None:
    policy = _policy(None)
    payload = paper_risk_policy_to_payload(policy)

    assert paper_risk_policy_from_payload(
        payload,
        economic_goal=None,
        expected_policy_provenance_sha256=policy.provenance_sha256,
    ) == policy

    with pytest.raises(
        PaperRiskPolicyStoreError,
        match="economic goal contract does not match",
    ):
        paper_risk_policy_from_payload(
            payload,
            economic_goal=_goal(),
            expected_policy_provenance_sha256=policy.provenance_sha256,
        )


def test_delete_after_commit_cannot_rebootstrap_weaker_policy(tmp_path) -> None:
    goal = _goal()
    owner = _policy(goal)
    store = PaperRiskPolicyStore(tmp_path)
    store.initialize_owner(owner)
    store.path.unlink()

    with pytest.raises(PaperRiskPolicyStoreError, match="anti-rollback"):
        PaperRiskPolicyStore(tmp_path).initialize_owner(_weaker_policy(goal))

    assert not store.path.exists()


def test_coherent_valid_policy_replacement_is_rejected_by_independent_authority(
    tmp_path,
) -> None:
    goal = _goal()
    owner = _policy(goal)
    replacement = _weaker_policy(goal)
    store = PaperRiskPolicyStore(tmp_path)
    store.initialize_owner(owner)

    risk_store_module.atomic_write_json(
        store.path,
        paper_risk_policy_to_payload(replacement),
    )

    with pytest.raises(PaperRiskPolicyStoreError, match="anti-rollback"):
        PaperRiskPolicyStore(tmp_path).load(
            economic_goal=goal,
            expected_policy_provenance_sha256=replacement.provenance_sha256,
        )


def test_publish_then_crash_recovers_exact_prepared_policy(
    tmp_path,
    monkeypatch,
) -> None:
    goal = _goal()
    policy = _policy(goal)
    store = PaperRiskPolicyStore(tmp_path)
    real_write = risk_store_module.atomic_write_json

    def write_then_crash(path, payload) -> None:
        real_write(path, payload)
        raise RuntimeError("simulated crash after policy publication")

    monkeypatch.setattr(risk_store_module, "atomic_write_json", write_then_crash)
    with pytest.raises(RuntimeError, match="simulated crash"):
        store.initialize_owner(policy)
    monkeypatch.setattr(risk_store_module, "atomic_write_json", real_write)

    assert PaperRiskPolicyStore(tmp_path).load(
        economic_goal=goal,
        expected_policy_provenance_sha256=policy.provenance_sha256,
    ) == policy


def test_prepublication_crash_aborts_and_allows_fresh_owner_retry(
    tmp_path,
    monkeypatch,
) -> None:
    goal = _goal()
    policy = _policy(goal)
    store = PaperRiskPolicyStore(tmp_path)
    real_write = risk_store_module.atomic_write_json

    def crash_before_write(path, payload) -> None:
        raise RuntimeError("simulated crash before policy publication")

    monkeypatch.setattr(risk_store_module, "atomic_write_json", crash_before_write)
    with pytest.raises(RuntimeError, match="simulated crash"):
        store.initialize_owner(policy)
    monkeypatch.setattr(risk_store_module, "atomic_write_json", real_write)

    restarted = PaperRiskPolicyStore(tmp_path)
    restarted.initialize_owner(policy)
    assert restarted.load(
        economic_goal=goal,
        expected_policy_provenance_sha256=policy.provenance_sha256,
    ) == policy
