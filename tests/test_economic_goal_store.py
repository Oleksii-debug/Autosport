from __future__ import annotations

import gc
import os
from dataclasses import fields, replace
from decimal import Decimal

import pytest

import autosport.economic_goal_store as economic_goal_store_module

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


def test_payload_decoder_rejects_oversize_decimal_text_before_parsing() -> None:
    payload = economic_goal_to_payload(_goal())
    body = payload["contract"]
    assert type(body) is dict
    body["max_turnover_fraction"] = "1" * 513

    with pytest.raises(EconomicGoalContractError, match="Decimal text exceeds"):
        economic_goal_from_payload(payload)


def test_payload_decoder_rejects_oversize_restriction_cardinality_before_sorting() -> None:
    payload = economic_goal_to_payload(_goal())
    body = payload["contract"]
    assert type(body) is dict
    body["blocked_sports"] = [f"sport:{index:04d}" for index in range(1025)]

    with pytest.raises(EconomicGoalContractError, match="restriction-count limit"):
        economic_goal_from_payload(payload)


def test_payload_decoder_rejects_oversize_restriction_member_before_sorting() -> None:
    payload = economic_goal_to_payload(_goal())
    body = payload["contract"]
    assert type(body) is dict
    body["blocked_sports"] = ["s" * 513]

    with pytest.raises(EconomicGoalContractError, match="non-canonical restriction text"):
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


def test_payload_decoder_rejects_mapping_and_list_subclasses() -> None:
    class DictSubclass(dict):
        pass

    class ListSubclass(list):
        pass

    payload = economic_goal_to_payload(_goal())
    with pytest.raises(EconomicGoalContractError, match="JSON object"):
        economic_goal_from_payload(DictSubclass(payload))

    body = payload["contract"]
    assert type(body) is dict
    body["blocked_sports"] = ListSubclass(["boxing", "tennis"])
    with pytest.raises(EconomicGoalContractError, match="sorted JSON array"):
        economic_goal_from_payload(payload)


def test_payload_encoder_rejects_contract_subclasses() -> None:
    class ContractSubclass(EconomicGoalContract):
        pass

    goal = _goal()
    subclass = ContractSubclass(
        **{field.name: getattr(goal, field.name) for field in fields(EconomicGoalContract)}
    )
    with pytest.raises(EconomicGoalContractError, match="requires an EconomicGoalContract"):
        economic_goal_to_payload(subclass)


def test_json_decoder_rejects_string_subclasses_before_parsing() -> None:
    class TextSubclass(str):
        pass

    with pytest.raises(EconomicGoalContractError, match="must be text"):
        economic_goal_from_json(TextSubclass("{}"))


def test_json_decoder_rejects_oversize_document() -> None:
    oversized = "{}" + (" " * economic_goal_store_module._MAX_ECONOMIC_GOAL_JSON_TEXT_CHARS)

    with pytest.raises(
        EconomicGoalContractError,
        match="JSON text exceeds the canonical size limit",
    ):
        economic_goal_from_json(oversized)


def test_json_decoder_bound_is_not_rebound_by_module_constant(monkeypatch) -> None:
    monkeypatch.setattr(economic_goal_store_module, "_MAX_ECONOMIC_GOAL_JSON_TEXT_CHARS", 1)

    payload = economic_goal_to_payload(_goal())
    import json

    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    assert economic_goal_from_json(text) == _goal()


def test_payload_decoder_rejects_schema_and_objective_subclasses_before_semantic_lookup() -> None:
    class TextSubclass(str):
        comparisons = 0

        def __eq__(self, other):
            type(self).comparisons += 1
            raise AssertionError("hostile comparison executed")

    payload = economic_goal_to_payload(_goal())
    payload["schema"] = TextSubclass(ECONOMIC_GOAL_SCHEMA)
    with pytest.raises(EconomicGoalContractError, match="unsupported economic goal schema"):
        economic_goal_from_payload(payload)
    assert TextSubclass.comparisons == 0

    payload = economic_goal_to_payload(_goal())
    body = payload["contract"]
    assert type(body) is dict
    body["objective"] = TextSubclass("long_run_risk_adjusted_bankroll_growth")
    with pytest.raises(EconomicGoalContractError, match="objective must be a string"):
        economic_goal_from_payload(payload)
    assert TextSubclass.comparisons == 0


def test_payload_encoder_revalidates_post_construction_contract_mutation() -> None:
    goal = _goal()
    object.__setattr__(goal, "max_stake_fraction", "0.01")

    with pytest.raises(EconomicGoalContractError):
        economic_goal_to_payload(goal)


def test_payload_encoder_ignores_rebound_codec_authorities(monkeypatch) -> None:
    goal = _goal()
    expected = economic_goal_to_payload(goal)

    def forged(*args, **kwargs):
        raise AssertionError("rebound goal codec authority executed")

    monkeypatch.setattr(economic_goal_store_module, "EconomicGoalContract", object)
    monkeypatch.setattr(economic_goal_store_module, "EconomicGoalContractError", RuntimeError)

    assert economic_goal_to_payload(goal) == expected


def test_payload_decoder_ignores_rebound_semantic_helpers(monkeypatch) -> None:
    payload = economic_goal_to_payload(_goal())

    def forged(*args, **kwargs):
        raise AssertionError("rebound durable decoder dependency executed")

    monkeypatch.setattr(economic_goal_store_module, "_require_exact_keys", forged)
    monkeypatch.setattr(economic_goal_store_module, "_decimal_text", forged)
    monkeypatch.setattr(economic_goal_store_module, "_restriction_set", forged)
    monkeypatch.setattr(economic_goal_store_module, "EconomicObjective", object)
    monkeypatch.setattr(economic_goal_store_module, "AutomationLevel", object)
    monkeypatch.setattr(economic_goal_store_module, "EconomicGoalContract", object)

    restored = economic_goal_from_payload(payload)
    assert restored == _goal()


def test_json_decoder_ignores_rebound_parser_and_payload_decoder(monkeypatch) -> None:
    payload = economic_goal_to_payload(_goal())
    import json

    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def forged(*args, **kwargs):
        raise AssertionError("rebound JSON decoder dependency executed")

    monkeypatch.setattr(economic_goal_store_module, "strict_json_loads", forged)
    monkeypatch.setattr(economic_goal_store_module, "economic_goal_from_payload", forged)

    assert economic_goal_from_json(text) == _goal()



def test_store_ignores_rebound_canonical_filename(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(EconomicGoalStore, "FILE_NAME", "attacker.json")

    store = EconomicGoalStore(tmp_path)
    store.initialize_owner(_goal())

    assert (tmp_path / "economic_goal_contract.json").exists()
    assert not (tmp_path / "attacker.json").exists()


def test_store_captures_bound_path_method_witnesses(monkeypatch, tmp_path) -> None:
    store = EconomicGoalStore(tmp_path)

    def forged_exists(*args, **kwargs):
        raise AssertionError("rebound Path.exists executed")

    def forged_read_text(*args, **kwargs):
        raise AssertionError("rebound Path.read_text executed")

    monkeypatch.setattr(type(store.path), "exists", forged_exists)
    store.initialize_owner(_goal())

    monkeypatch.setattr(type(store.path), "read_text", forged_read_text)
    assert store.load() == _goal()


def test_store_rejects_instance_binding_rebind(tmp_path) -> None:
    store = EconomicGoalStore(tmp_path)
    store.initialize_owner(_goal())

    object.__setattr__(store, "path", tmp_path / "attacker.json")
    with pytest.raises(EconomicGoalContractError, match="binding was rebound"):
        store.load()

    object.__setattr__(store, "path", tmp_path / "economic_goal_contract.json")
    object.__setattr__(store, "workspace", tmp_path / "other-workspace")
    with pytest.raises(EconomicGoalContractError, match="binding was rebound"):
        store.persist_automatic_successor(
            replace(_goal(), revision=2, max_stake_fraction=Decimal("0.01"))
        )


def test_store_ignores_rebound_lock_construction(monkeypatch, tmp_path) -> None:
    store = EconomicGoalStore(tmp_path)

    def forged_new(*args, **kwargs):
        raise AssertionError("rebound WorkspaceEconomicLock.__new__ executed")

    def forged_init(*args, **kwargs):
        raise AssertionError("rebound WorkspaceEconomicLock.__init__ executed")

    monkeypatch.setattr(WorkspaceEconomicLock, "__new__", staticmethod(forged_new))
    monkeypatch.setattr(WorkspaceEconomicLock, "__init__", forged_init)

    store.initialize_owner(_goal())
    assert store.load() == _goal()


def test_store_ignores_rebound_lock_acquire_release(monkeypatch, tmp_path) -> None:
    store = EconomicGoalStore(tmp_path)

    def forged_acquire(*args, **kwargs):
        raise AssertionError("rebound WorkspaceEconomicLock.acquire executed")

    def forged_release(*args, **kwargs):
        raise AssertionError("rebound WorkspaceEconomicLock.release executed")

    monkeypatch.setattr(WorkspaceEconomicLock, "acquire", forged_acquire)
    monkeypatch.setattr(WorkspaceEconomicLock, "release", forged_release)

    store.initialize_owner(_goal())
    assert store.load() == _goal()


def test_store_ignores_rebound_lock_lifecycle(monkeypatch, tmp_path) -> None:
    store = EconomicGoalStore(tmp_path)

    def forged_enter(*args, **kwargs):
        raise AssertionError("rebound WorkspaceEconomicLock.__enter__ executed")

    def forged_exit(*args, **kwargs):
        raise AssertionError("rebound WorkspaceEconomicLock.__exit__ executed")

    monkeypatch.setattr(WorkspaceEconomicLock, "__enter__", forged_enter)
    monkeypatch.setattr(WorkspaceEconomicLock, "__exit__", forged_exit)

    store.initialize_owner(_goal())
    assert store.load() == _goal()


def test_store_ignores_rebound_lock_scope_helper(monkeypatch, tmp_path) -> None:
    store = EconomicGoalStore(tmp_path)
    store.initialize_owner(_goal())

    def forged(*args, **kwargs):
        raise AssertionError("rebound lock scope executed")

    monkeypatch.setattr(economic_goal_store_module, "_workspace_lock_scope", forged)

    candidate = replace(_goal(), revision=2, max_stake_fraction=Decimal("0.01"))
    store.persist_automatic_successor(candidate)
    assert EconomicGoalStore(tmp_path).load() == candidate


def test_store_ignores_rebound_module_authorities(monkeypatch, tmp_path) -> None:
    previous = _goal()
    candidate = replace(
        previous,
        revision=2,
        max_stake_fraction=Decimal("0.01"),
        automation_level=AutomationLevel.RECOMMENDATION,
    )

    def forged(*args, **kwargs):
        raise AssertionError("rebound economic-goal store authority executed")

    for name in (
        "Path",
        "WorkspaceEconomicLock",
        "atomic_write_json",
        "economic_goal_to_payload",
        "economic_goal_from_json",
        "validate_automatic_transition",
        "strict_json_loads",
        "EconomicGoalContract",
        "EconomicObjective",
        "AutomationLevel",
        "_STORE_BINDINGS_BY_ID",
        "_CANONICAL_STORE_FILE_NAME",
    ):
        monkeypatch.setattr(economic_goal_store_module, name, forged)

    store = EconomicGoalStore(tmp_path)
    store.initialize_owner(previous)
    assert store.load() == previous

    store.persist_automatic_successor(candidate)
    assert store.load() == candidate


def test_successor_does_not_dispatch_rebound_instance_load(monkeypatch, tmp_path) -> None:
    store = EconomicGoalStore(tmp_path)
    store.initialize_owner(_goal())

    candidate = replace(
        _goal(),
        revision=2,
        max_stake_fraction=Decimal("0.01"),
        automation_level=AutomationLevel.RECOMMENDATION,
    )

    def forged(*args, **kwargs):
        raise AssertionError("rebound instance load executed")

    monkeypatch.setattr(store, "load", forged)
    store.persist_automatic_successor(candidate)

    assert store.load is forged
    assert EconomicGoalStore(tmp_path).load() == candidate


def test_store_methods_ignore_rebound_lock_scope_module_name(monkeypatch, tmp_path) -> None:
    previous = _goal()
    candidate = replace(
        previous,
        revision=2,
        max_stake_fraction=Decimal("0.01"),
        automation_level=AutomationLevel.RECOMMENDATION,
    )

    def forged(*args, **kwargs):
        raise AssertionError("rebound lock scope executed")

    monkeypatch.setattr(economic_goal_store_module, "_workspace_lock_scope", forged)

    store = EconomicGoalStore(tmp_path)
    store.initialize_owner(previous)
    store.persist_automatic_successor(candidate)

    assert EconomicGoalStore(tmp_path).load() == candidate


def test_payload_decoder_ignores_rebound_schema_helpers(monkeypatch) -> None:
    payload = economic_goal_to_payload(_goal())

    def forged(*args, **kwargs):
        raise AssertionError("rebound payload helper executed")

    for name in (
        "_require_exact_keys",
        "_decimal_text",
        "_restriction_set",
        "EconomicGoalContract",
        "EconomicObjective",
        "AutomationLevel",
    ):
        monkeypatch.setattr(economic_goal_store_module, name, forged)

    assert economic_goal_from_payload(payload) == _goal()



def test_store_path_ignores_rebound_class_file_name(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(EconomicGoalStore, "FILE_NAME", "attacker.json")

    store = EconomicGoalStore(tmp_path)

    assert store.path == tmp_path / "economic_goal_contract.json"
    store.initialize_owner(_goal())
    assert not (tmp_path / "attacker.json").exists()



def test_codec_ignores_rebound_size_bounds(monkeypatch) -> None:
    payload = economic_goal_to_payload(_goal())
    body = payload["contract"]
    assert type(body) is dict

    monkeypatch.setattr(economic_goal_store_module, "_MAX_ECONOMIC_GOAL_DECIMAL_TEXT_CHARS", 10000)
    monkeypatch.setattr(economic_goal_store_module, "_MAX_ECONOMIC_GOAL_RESTRICTION_MEMBERS", 10000)
    monkeypatch.setattr(economic_goal_store_module, "_MAX_ECONOMIC_GOAL_RESTRICTION_TEXT_CHARS", 10000)

    oversized_decimal = economic_goal_to_payload(_goal())
    decimal_body = oversized_decimal["contract"]
    assert type(decimal_body) is dict
    decimal_body["max_turnover_fraction"] = "1" * 513
    with pytest.raises(EconomicGoalContractError, match="Decimal text exceeds"):
        economic_goal_from_payload(oversized_decimal)

    oversized_count = economic_goal_to_payload(_goal())
    count_body = oversized_count["contract"]
    assert type(count_body) is dict
    count_body["blocked_sports"] = [f"sport:{index:04d}" for index in range(1025)]
    with pytest.raises(EconomicGoalContractError, match="restriction-count limit"):
        economic_goal_from_payload(oversized_count)

    oversized_member = economic_goal_to_payload(_goal())
    member_body = oversized_member["contract"]
    assert type(member_body) is dict
    member_body["blocked_sports"] = ["s" * 513]
    with pytest.raises(EconomicGoalContractError, match="non-canonical restriction text"):
        economic_goal_from_payload(oversized_member)


def test_store_path_exists_witness_ignores_rebound_error(monkeypatch, tmp_path) -> None:
    def forged_stat(self):
        raise PermissionError("stat blocked")

    monkeypatch.setattr(type(tmp_path), "stat", forged_stat)
    store = EconomicGoalStore(tmp_path)
    canonical_error = EconomicGoalContractError
    monkeypatch.setattr(economic_goal_store_module, "EconomicGoalContractError", RuntimeError)

    with pytest.raises(canonical_error) as excinfo:
        store.initialize_owner(_goal())
    assert type(excinfo.value) is canonical_error


def test_store_captures_transitive_path_witnesses(monkeypatch, tmp_path) -> None:
    store = EconomicGoalStore(tmp_path)

    def forged_open(*args, **kwargs):
        raise AssertionError("rebound Path.open executed")

    def forged_stat(*args, **kwargs):
        raise AssertionError("rebound Path.stat executed")

    monkeypatch.setattr(type(store.path), "open", forged_open)
    monkeypatch.setattr(type(store.path), "stat", forged_stat)

    store.initialize_owner(_goal())
    assert store.load() == _goal()


def test_store_binding_ignores_class_descriptor_rebinding(monkeypatch, tmp_path) -> None:
    store = EconomicGoalStore(tmp_path)
    store.initialize_owner(_goal())

    class HostileDescriptor:
        def __get__(self, instance, owner):
            raise AssertionError("rebound store descriptor executed")

        def __set__(self, instance, value):
            raise AssertionError("rebound store descriptor setter executed")

    monkeypatch.setattr(EconomicGoalStore, "workspace", HostileDescriptor(), raising=False)
    monkeypatch.setattr(EconomicGoalStore, "path", HostileDescriptor(), raising=False)

    assert store.load() == _goal()


def test_store_binding_validation_does_not_dispatch_path_equality(monkeypatch, tmp_path) -> None:
    store = EconomicGoalStore(tmp_path)
    store.initialize_owner(_goal())

    def forged_eq(*args, **kwargs):
        raise AssertionError("rebound Path.__eq__ executed")

    monkeypatch.setattr(type(store.path), "__eq__", forged_eq)

    assert store.load() == _goal()


def test_store_rejects_equal_but_distinct_path_rebind(tmp_path) -> None:
    store = EconomicGoalStore(tmp_path)
    store.initialize_owner(_goal())

    original_path = store.path
    object.__setattr__(store, "path", type(original_path)(str(original_path)))

    with pytest.raises(EconomicGoalContractError, match="path binding was rebound"):
        store.load()



def test_store_binding_registry_releases_dead_store_entries(tmp_path) -> None:
    store = EconomicGoalStore(tmp_path)
    store_id = id(store)

    assert store_id in economic_goal_store_module._STORE_BINDINGS_BY_ID

    del store
    gc.collect()

    assert store_id not in economic_goal_store_module._STORE_BINDINGS_BY_ID


def test_store_path_exists_ignores_rebound_error_authority(monkeypatch, tmp_path) -> None:
    def forged_stat(*args, **kwargs):
        raise OSError("stat failure")

    monkeypatch.setattr(type(tmp_path), "stat", forged_stat)

    store = EconomicGoalStore(tmp_path)
    binding = economic_goal_store_module._STORE_BINDINGS_BY_ID[id(store)]
    path_exists = binding[3]

    monkeypatch.setattr(economic_goal_store_module, "EconomicGoalContractError", RuntimeError)

    with pytest.raises(
        EconomicGoalContractError,
        match="cannot inspect persisted economic goal path",
    ):
        path_exists()


def test_store_failure_paths_ignore_rebound_error_authority(monkeypatch, tmp_path) -> None:
    store = EconomicGoalStore(tmp_path)
    store.initialize_owner(_goal())

    monkeypatch.setattr(economic_goal_store_module, "EconomicGoalContractError", RuntimeError)

    with pytest.raises(EconomicGoalContractError, match="already exists"):
        store.initialize_owner(_goal())

    store.path.unlink()
    with pytest.raises(EconomicGoalContractError, match="cannot read persisted economic goal"):
        store.load()

    with pytest.raises(EconomicGoalContractError, match="cannot read persisted economic goal"):
        store.persist_automatic_successor(
            replace(_goal(), revision=2, max_stake_fraction=Decimal("0.01"))
        )


def test_store_rejects_oversized_durable_text_before_json_decoder(tmp_path) -> None:
    store = EconomicGoalStore(tmp_path)
    store.path.write_text(
        "x" * (economic_goal_store_module._MAX_ECONOMIC_GOAL_JSON_TEXT_CHARS + 1),
        encoding="utf-8",
    )

    calls = 0

    def forged_decoder(text):
        nonlocal calls
        calls += 1
        raise AssertionError("JSON decoder executed for oversized durable text")

    with pytest.raises(EconomicGoalContractError, match="JSON text exceeds"):
        store.load(_json_decoder=forged_decoder)

    assert calls == 0


def test_successor_rejects_oversized_predecessor_before_transition_or_write(tmp_path) -> None:
    store = EconomicGoalStore(tmp_path)
    store.path.write_text(
        "x" * (economic_goal_store_module._MAX_ECONOMIC_GOAL_JSON_TEXT_CHARS + 1),
        encoding="utf-8",
    )
    before = store.path.read_bytes()

    calls = []

    def forged_decoder(text):
        calls.append("decode")
        raise AssertionError("decoder executed")

    def forged_transition(previous, candidate):
        calls.append("transition")
        raise AssertionError("transition executed")

    def forged_writer(path, payload):
        calls.append("write")
        raise AssertionError("writer executed")

    with pytest.raises(EconomicGoalContractError, match="JSON text exceeds"):
        store.persist_automatic_successor(
            replace(_goal(), revision=2, max_stake_fraction=Decimal("0.01")),
            _json_decoder=forged_decoder,
            _transition_validator=forged_transition,
            _writer=forged_writer,
        )

    assert calls == []
    assert store.path.read_bytes() == before


def test_store_normalizes_invalid_utf8_read_failure(tmp_path) -> None:
    store = EconomicGoalStore(tmp_path)
    store.path.write_bytes(b"\xff")

    with pytest.raises(EconomicGoalContractError, match="cannot read persisted economic goal"):
        store.load()


def test_store_binding_resolver_ignores_rebound_object_getattribute(monkeypatch, tmp_path) -> None:
    store = EconomicGoalStore(tmp_path)
    store.initialize_owner(_goal())

    def forged(*args, **kwargs):
        raise AssertionError("rebound object getattribute authority executed")

    monkeypatch.setattr(
        economic_goal_store_module,
        "_CANONICAL_OBJECT_GETATTRIBUTE",
        forged,
    )

    assert store.load() == _goal()


def test_store_load_rejects_symlinked_authority_file(tmp_path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text('{"schema":"attacker"}', encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = EconomicGoalStore(workspace)
    try:
        store.path.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(EconomicGoalContractError):
        store.load()


def test_store_load_rejects_hardlinked_authority_file(tmp_path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text('{"schema":"attacker"}', encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = EconomicGoalStore(workspace)
    try:
        os.link(outside, store.path)
    except OSError as exc:
        pytest.skip(f"hard-link creation unavailable: {exc}")

    with pytest.raises(EconomicGoalContractError, match="hard-link aliases"):
        store.load()


def test_store_load_rejects_oversize_bytes_without_unbounded_read(tmp_path) -> None:
    store = EconomicGoalStore(tmp_path)
    store.path.write_bytes(
        b" " * (economic_goal_store_module._MAX_ECONOMIC_GOAL_JSON_BYTES + 1)
    )

    with pytest.raises(EconomicGoalContractError, match="byte-size limit"):
        store.load()


def test_store_load_normalizes_invalid_utf8_to_contract_error(tmp_path) -> None:
    store = EconomicGoalStore(tmp_path)
    store.path.write_bytes(b"\xff")

    with pytest.raises(EconomicGoalContractError, match="valid UTF-8"):
        store.load()



def test_verified_reader_surfaces_descriptor_close_failure(monkeypatch, tmp_path) -> None:
    store = EconomicGoalStore(tmp_path)
    store.initialize_owner(_goal())

    def forged_close(fd):
        raise OSError("close failure")

    with pytest.raises(
        EconomicGoalContractError,
        match="cannot close persisted economic goal read descriptor",
    ):
        economic_goal_store_module._read_economic_goal_text(
            store.path,
            _close=forged_close,
        )


def test_verified_reader_preserves_primary_failure_when_cleanup_also_fails(
    monkeypatch, tmp_path
) -> None:
    store = EconomicGoalStore(tmp_path)
    store.initialize_owner(_goal())

    original_open = economic_goal_store_module._open_read_only_descriptor

    def forged_open(path):
        raise OSError("primary open failure")

    def forged_close(fd):
        raise OSError("secondary close failure")

    with pytest.raises(
        EconomicGoalContractError,
        match="cannot safely read persisted economic goal",
    ):
        economic_goal_store_module._read_economic_goal_text(
            store.path,
            _open_descriptor=forged_open,
            _close=forged_close,
        )



def test_verified_read_rejects_in_place_mutation_between_byte_images(tmp_path) -> None:
    store = EconomicGoalStore(tmp_path)
    store.initialize_owner(_goal())
    original_open = economic_goal_store_module._CANONICAL_OPEN_READ_ONLY_DESCRIPTOR
    calls = 0

    def racing_open(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_text('{"schema":"attacker"}', encoding="utf-8")
        return original_open(path)

    with pytest.raises(EconomicGoalContractError, match="bytes changed"):
        economic_goal_store_module._read_economic_goal_text(
            store.path,
            _open_descriptor=racing_open,
        )


def test_verified_read_rejects_path_replacement_after_second_descriptor_open(
    tmp_path,
) -> None:
    store = EconomicGoalStore(tmp_path)
    store.initialize_owner(_goal())
    replacement = tmp_path / "replacement.json"
    replacement.write_text('{"schema":"attacker"}', encoding="utf-8")
    original_stat = economic_goal_store_module._CANONICAL_OS_STAT
    path_stat_calls = 0

    def racing_stat(path, *args, **kwargs):
        nonlocal path_stat_calls
        if path == store.path:
            path_stat_calls += 1
            if path_stat_calls == 2:
                os.replace(replacement, store.path)
        return original_stat(path, *args, **kwargs)

    with pytest.raises(EconomicGoalContractError, match="changed during verified read"):
        economic_goal_store_module._read_economic_goal_text(
            store.path,
            _stat=racing_stat,
        )


def test_verified_read_rejects_path_replacement_after_final_byte_read(
    tmp_path,
) -> None:
    store = EconomicGoalStore(tmp_path)
    store.initialize_owner(_goal())
    replacement = tmp_path / "replacement.json"
    replacement.write_text('{"schema":"attacker"}', encoding="utf-8")
    original_open = economic_goal_store_module._CANONICAL_OPEN_READ_ONLY_DESCRIPTOR
    calls = 0

    def racing_open(path):
        nonlocal calls
        calls += 1
        if calls == 4:
            os.replace(replacement, path)
        return original_open(path)

    with pytest.raises(EconomicGoalContractError, match="changed after verified read"):
        economic_goal_store_module._read_economic_goal_text(
            store.path,
            _open_descriptor=racing_open,
        )
