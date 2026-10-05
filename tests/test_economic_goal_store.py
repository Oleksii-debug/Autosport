from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.economic_goal import (
    AutomationLevel,
    EconomicGoalContract,
    EconomicGoalContractError,
)
from autosport.economic_goal_store import (
    ECONOMIC_GOAL_SCHEMA,
    ECONOMIC_GOAL_SCHEMA_VERSION,
    EconomicGoalStore,
    economic_goal_from_json,
    economic_goal_from_payload,
    economic_goal_to_payload,
)


def _goal(**changes: object) -> EconomicGoalContract:
    values: dict[str, object] = {
        "goal_id": "owner-goal-v1",
        "revision": 1,
        "bankroll_id": "paper-main",
        "currency": "EUR",
        "max_stake_fraction": Decimal("0.02"),
        "max_stake_amount": Decimal("25.00"),
        "max_session_loss_fraction": Decimal("0.05"),
        "max_day_loss_fraction": Decimal("0.05"),
        "max_drawdown_fraction": Decimal("0.20"),
        "max_capital_at_risk_fraction": Decimal("0.20"),
        "max_event_concentration_fraction": Decimal("0.50"),
        "max_market_concentration_fraction": Decimal("0.50"),
        "max_provider_concentration_fraction": Decimal("0.50"),
        "max_sport_concentration_fraction": Decimal("0.50"),
        "max_turnover_fraction": Decimal("1.5"),
        "max_risk_of_ruin": Decimal("0.01"),
        "max_execution_slippage_fraction": Decimal("0.01"),
        "max_quote_age_seconds": Decimal("5"),
        "minimum_data_quality": Decimal("0.70"),
        "max_concurrent_positions": 5,
        "max_parlay_legs": 4,
        "automation_level": AutomationLevel.SUPERVISED_EXECUTION,
        "emergency_stop": False,
        "blocked_sports": frozenset({"tennis", "boxing"}),
        "blocked_providers": frozenset({"provider:z", "provider:a"}),
        "blocked_markets": frozenset({"market:unsupported"}),
    }
    values.update(changes)
    return EconomicGoalContract(**values)  # type: ignore[arg-type]


def test_payload_roundtrip_is_versioned_exact_and_canonical() -> None:
    goal = _goal()

    payload = economic_goal_to_payload(goal)
    body = payload["contract"]

    assert payload["schema"] == ECONOMIC_GOAL_SCHEMA
    assert payload["schema_version"] == ECONOMIC_GOAL_SCHEMA_VERSION == 1
    assert isinstance(body, dict)
    assert body["max_stake_fraction"] == "0.02"
    assert body["max_stake_amount"] == "25.00"
    assert body["blocked_sports"] == ["boxing", "tennis"]
    assert body["blocked_providers"] == ["provider:a", "provider:z"]
    assert economic_goal_from_payload(payload) == goal


def test_store_roundtrip_survives_fresh_restart_instance(tmp_path) -> None:
    goal = _goal(goal_id="мета-власника")
    store = EconomicGoalStore(tmp_path)
    store.initialize_owner(goal)
    first_bytes = store.path.read_bytes()

    restarted_store = EconomicGoalStore(tmp_path)

    assert restarted_store.load() == goal
    assert restarted_store.path.read_bytes() == first_bytes


def test_owner_initialization_is_creation_only_and_preserves_existing_bytes(tmp_path) -> None:
    goal = _goal()
    store = EconomicGoalStore(tmp_path)
    store.initialize_owner(goal)
    before = store.path.read_bytes()

    with pytest.raises(EconomicGoalContractError, match="already exists"):
        store.initialize_owner(replace(goal, revision=2, max_stake_fraction=Decimal("0.01")))

    assert store.path.read_bytes() == before
    assert EconomicGoalStore(tmp_path).load() == goal


def test_automatic_tightening_persists_and_survives_restart(tmp_path) -> None:
    previous = _goal()
    candidate = replace(
        previous,
        revision=2,
        max_stake_fraction=Decimal("0.01"),
        max_stake_amount=Decimal("20.00"),
        max_day_loss_fraction=Decimal("0.04"),
        minimum_data_quality=Decimal("0.85"),
        max_concurrent_positions=3,
        automation_level=AutomationLevel.RECOMMENDATION,
        emergency_stop=True,
        blocked_sports=previous.blocked_sports | {"greyhound"},
    )
    store = EconomicGoalStore(tmp_path)
    store.initialize_owner(previous)

    store.persist_automatic_successor(candidate)

    assert EconomicGoalStore(tmp_path).load() == candidate


def test_automatic_authority_expansion_fails_closed_without_mutating_durable_state(
    tmp_path,
) -> None:
    previous = _goal()
    candidate = replace(
        previous,
        revision=2,
        max_stake_fraction=Decimal("0.03"),
    )
    store = EconomicGoalStore(tmp_path)
    store.initialize_owner(previous)
    before = store.path.read_bytes()

    with pytest.raises(EconomicGoalContractError, match="must not increase"):
        store.persist_automatic_successor(candidate)

    assert store.path.read_bytes() == before
    assert EconomicGoalStore(tmp_path).load() == previous


def test_automatic_identity_rebinding_fails_closed_without_mutation(tmp_path) -> None:
    previous = _goal()
    candidate = replace(previous, revision=2, bankroll_id="other-bankroll")
    store = EconomicGoalStore(tmp_path)
    store.initialize_owner(previous)
    before = store.path.read_bytes()

    with pytest.raises(EconomicGoalContractError, match="preserve bankroll_id"):
        store.persist_automatic_successor(candidate)

    assert store.path.read_bytes() == before


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_schema",
        "wrong_version",
        "extra_root_key",
        "extra_contract_key",
        "numeric_decimal",
        "unsorted_restrictions",
        "unknown_automation",
    ],
)
def test_persisted_payload_rejects_malformed_or_ambiguous_authority(
    mutation: str,
) -> None:
    payload = economic_goal_to_payload(_goal())
    body = payload["contract"]
    assert isinstance(body, dict)

    if mutation == "wrong_schema":
        payload["schema"] = "autosport.other"
    elif mutation == "wrong_version":
        payload["schema_version"] = 2
    elif mutation == "extra_root_key":
        payload["unexpected"] = True
    elif mutation == "extra_contract_key":
        body["unexpected"] = "authority"
    elif mutation == "numeric_decimal":
        body["max_stake_fraction"] = 0.02
    elif mutation == "unsorted_restrictions":
        body["blocked_sports"] = ["tennis", "boxing"]
    elif mutation == "unknown_automation":
        body["automation_level"] = 99
    else:  # pragma: no cover - exhaustive guard for future edits
        raise AssertionError(mutation)

    with pytest.raises(EconomicGoalContractError):
        economic_goal_from_payload(payload)


def test_strict_json_rejects_duplicate_schema_key() -> None:
    duplicate = (
        '{"schema":"autosport.economic_goal_contract",'
        '"schema":"autosport.other","schema_version":1,"contract":{}}'
    )

    with pytest.raises(EconomicGoalContractError, match="invalid economic goal JSON"):
        economic_goal_from_json(duplicate)


def test_corrupt_durable_file_fails_closed_on_restart(tmp_path) -> None:
    store = EconomicGoalStore(tmp_path)
    store.initialize_owner(_goal())
    store.path.write_text('{"schema":', encoding="utf-8")

    with pytest.raises(EconomicGoalContractError, match="invalid economic goal JSON"):
        EconomicGoalStore(tmp_path).load()
