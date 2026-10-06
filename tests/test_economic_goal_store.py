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


def test_store_operation_descriptors_do_not_expose_mutable_operation_slot() -> None:
    for name in ("load", "initialize_owner", "persist_automatic_successor"):
        descriptor = EconomicGoalStore.__dict__[name]
        assert not hasattr(descriptor, "_operation")
        try:
            object.__setattr__(descriptor, "_operation", lambda _self: None)
        except AttributeError:
            continue
        raise AssertionError(f"{name} descriptor exposed mutable operation storage")


def test_store_constructor_rejects_direct_base_metaclass_rebinding(tmp_path) -> None:
    original = EconomicGoalStore.__dict__["__init__"]

    def attacker(_self, _workspace) -> None:
        raise AssertionError("forged constructor authority executed")

    with pytest.raises(
        TypeError,
        match="economic goal store authority operation binding is immutable",
    ):
        type.__setattr__(EconomicGoalStore, "__init__", attacker)

    with pytest.raises(
        TypeError,
        match="economic goal store authority operation binding is immutable",
    ):
        type.__delattr__(EconomicGoalStore, "__init__")

    assert EconomicGoalStore.__dict__["__init__"] is original
    store = EconomicGoalStore(tmp_path)
    assert store.workspace == tmp_path.resolve()


def test_store_public_authority_operations_ignore_direct_dict_shadowing(tmp_path) -> None:
    store = EconomicGoalStore(tmp_path)

    store.__dict__["load"] = lambda: None
    store.__dict__["initialize_owner"] = lambda _contract: None
    store.__dict__["persist_automatic_successor"] = lambda _contract: None

    assert callable(store.load)
    assert callable(store.initialize_owner)
    assert callable(store.persist_automatic_successor)


def test_store_authority_seal_ignores_rebound_authority_name_set(monkeypatch) -> None:
    monkeypatch.setattr(
        EconomicGoalStore,
        "_AUTHORITY_NAMES",
        frozenset(),
        raising=False,
    )

    for name, replacement in (
        ("load", lambda self: None),
        ("initialize_owner", lambda self, _contract: None),
        ("persist_automatic_successor", lambda self, _contract: None),
    ):
        try:
            setattr(EconomicGoalStore, name, replacement)
        except TypeError as exc:
            assert "authority operation binding is immutable" in str(exc)
        else:
            raise AssertionError(f"{name} class binding escaped the sealed name set")


def test_store_public_authority_operations_reject_class_rebinding() -> None:
    for name, replacement in (
        ("__init__", lambda self, _workspace: None),
        ("load", lambda self: None),
        ("initialize_owner", lambda self, _contract: None),
        ("persist_automatic_successor", lambda self, _contract: None),
    ):
        try:
            setattr(EconomicGoalStore, name, replacement)
        except TypeError as exc:
            assert "authority operation binding is immutable" in str(exc)
        else:
            raise AssertionError(f"{name} class binding was mutable")


def test_store_public_authority_operations_reject_direct_type_mutation() -> None:
    replacements = (
        ("__init__", lambda self, _workspace: None),
        ("load", lambda self: None),
        ("initialize_owner", lambda self, _contract: None),
        ("persist_automatic_successor", lambda self, _contract: None),
        ("_authority_operations_sealed", False),
    )
    for name, replacement in replacements:
        with pytest.raises(
            TypeError,
            match="economic goal store authority operation binding is immutable",
        ):
            type.__setattr__(EconomicGoalStore, name, replacement)
        with pytest.raises(
            TypeError,
            match="economic goal store authority operation binding is immutable",
        ):
            type.__delattr__(EconomicGoalStore, name)


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


def test_owner_initialization_race_does_not_replace_late_incumbent(tmp_path) -> None:
    goal = _goal()
    store = EconomicGoalStore(tmp_path)
    incumbent_bytes = b'{"authority":"late-incumbent"}\n'
    canonical_create = economic_goal_store_module._CANONICAL_ATOMIC_CREATE_OWNER_JSON

    def racing_writer(path, payload):
        # Simulate a non-cooperating actor winning the pathname after
        # initialize_owner() has already observed it as absent.
        path.write_bytes(incumbent_bytes)
        canonical_create(path, payload)

    with pytest.raises(EconomicGoalContractError, match="already exists"):
        economic_goal_store_module._BOUND_STORE_INITIALIZE_OWNER(
            store,
            goal,
            _writer=racing_writer,
        )

    assert store.path.read_bytes() == incumbent_bytes
    assert list(tmp_path.glob(f".{store.path.name}.*.owner-init.tmp")) == []


def test_owner_initialization_atomic_create_leaves_single_link_and_no_staging(
    tmp_path,
) -> None:
    goal = _goal()
    store = EconomicGoalStore(tmp_path)

    store.initialize_owner(goal)

    assert os.stat(store.path, follow_symlinks=False).st_nlink == 1
    assert list(tmp_path.glob(f".{store.path.name}.*.owner-init.tmp")) == []
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
        "signed_zero_decimal",
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
    elif mutation == "signed_zero_decimal":
        body["max_stake_fraction"] = "-0"
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


def test_payload_encoder_rejects_bound_default_rebinding() -> None:
    contract = _goal()
    operation = economic_goal_store_module._BOUND_ECONOMIC_GOAL_TO_PAYLOAD
    original_defaults = operation.__defaults__
    assert original_defaults is not None

    operation.__defaults__ = (
        original_defaults[0],
        lambda value: value,
        *original_defaults[2:],
    )
    try:
        with pytest.raises(
            EconomicGoalContractError,
            match="payload encoder defaults authority changed",
        ):
            economic_goal_to_payload(contract)
    finally:
        operation.__defaults__ = original_defaults


def test_payload_encoder_rejects_nested_snapshot_default_rebinding() -> None:
    contract = _goal()
    operation = economic_goal_store_module._BOUND_ECONOMIC_GOAL_TO_PAYLOAD
    original_defaults = operation.__defaults__
    assert original_defaults is not None
    snapshotter = original_defaults[-2]
    snapshot_defaults = snapshotter.__defaults__
    assert snapshot_defaults is not None

    snapshotter.__defaults__ = (
        (),
        snapshot_defaults[1],
    )
    try:
        with pytest.raises(
            EconomicGoalContractError,
            match="payload encoder nested defaults authority changed",
        ):
            economic_goal_to_payload(contract)
    finally:
        snapshotter.__defaults__ = snapshot_defaults
        operation.__defaults__ = original_defaults


def test_store_successor_rejects_nested_lock_scope_default_rebinding(tmp_path) -> None:
    previous = _goal()
    candidate = replace(previous, revision=2, max_stake_fraction=Decimal("0.01"))
    store = EconomicGoalStore(tmp_path)
    store.initialize_owner(previous)

    operation = economic_goal_store_module._BOUND_STORE_PERSIST_AUTOMATIC_SUCCESSOR
    original_defaults = operation.__defaults__
    assert original_defaults is not None
    lock_scope = original_defaults[-1]
    lock_defaults = lock_scope.__defaults__
    assert lock_defaults is not None

    lock_scope.__defaults__ = (
        object,
        *lock_defaults[1:],
    )
    try:
        with pytest.raises(
            EconomicGoalContractError,
            match="economic-goal store write nested defaults authority changed",
        ):
            store.persist_automatic_successor(candidate)
    finally:
        lock_scope.__defaults__ = lock_defaults
        operation.__defaults__ = original_defaults


def test_payload_decoder_rejects_bound_nested_default_rebinding() -> None:
    payload = economic_goal_to_payload(_goal())
    operation = economic_goal_store_module._BOUND_ECONOMIC_GOAL_FROM_PAYLOAD
    original_defaults = operation.__defaults__
    assert original_defaults is not None
    snapshotter = original_defaults[2]
    snapshot_defaults = snapshotter.__defaults__
    assert snapshot_defaults is not None

    snapshotter.__defaults__ = (
        object,
        *snapshot_defaults[1:],
    )
    try:
        with pytest.raises(
            EconomicGoalContractError,
            match="payload decoder nested defaults authority changed",
        ):
            economic_goal_from_payload(payload)
    finally:
        snapshotter.__defaults__ = snapshot_defaults
        operation.__defaults__ = original_defaults


def test_json_decoder_rejects_bound_payload_decoder_default_rebinding() -> None:
    payload = economic_goal_to_payload(_goal())
    import json

    text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    operation = economic_goal_store_module._BOUND_ECONOMIC_GOAL_FROM_JSON
    original_defaults = operation.__defaults__
    assert original_defaults is not None

    operation.__defaults__ = (
        original_defaults[0],
        original_defaults[1],
        lambda value: _goal(),
        *original_defaults[3:],
    )
    try:
        with pytest.raises(
            EconomicGoalContractError,
            match="JSON decoder defaults authority changed",
        ):
            economic_goal_from_json(text)
    finally:
        operation.__defaults__ = original_defaults


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



def test_store_load_rejects_bound_default_rebinding(tmp_path) -> None:
    store = EconomicGoalStore(tmp_path)
    store.initialize_owner(_goal())
    operation = economic_goal_store_module._BOUND_STORE_LOAD
    original_defaults = operation.__defaults__
    assert original_defaults is not None

    operation.__defaults__ = (
        original_defaults[0],
        original_defaults[1],
        lambda _text: _goal(),
        *original_defaults[3:],
    )
    try:
        with pytest.raises(
            EconomicGoalContractError,
            match="economic-goal store load defaults authority changed",
        ):
            store.load()
    finally:
        operation.__defaults__ = original_defaults


def test_store_successor_rejects_nested_transition_default_rebinding(tmp_path) -> None:
    previous = _goal()
    candidate = replace(
        previous,
        revision=2,
        max_stake_fraction=Decimal("0.01"),
    )
    store = EconomicGoalStore(tmp_path)
    store.initialize_owner(previous)
    operation = economic_goal_store_module._BOUND_STORE_PERSIST_AUTOMATIC_SUCCESSOR
    original_defaults = operation.__defaults__
    assert original_defaults is not None
    transition = original_defaults[3]
    transition_defaults = transition.__defaults__
    assert transition_defaults is not None

    transition.__defaults__ = (
        transition_defaults[0],
        lambda *args: None,
        *transition_defaults[2:],
    )
    try:
        with pytest.raises(
            EconomicGoalContractError,
            match="economic-goal store write nested defaults authority changed",
        ):
            store.persist_automatic_successor(candidate)
    finally:
        transition.__defaults__ = transition_defaults
        operation.__defaults__ = original_defaults


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


def test_lock_scope_ignores_rebound_canonical_dispatch_aliases(
    monkeypatch, tmp_path
) -> None:
    store = EconomicGoalStore(tmp_path)

    def forged(*args, **kwargs):
        raise AssertionError("rebound canonical lock dispatch authority executed")

    for name in (
        "_CANONICAL_WORKSPACE_LOCK_TYPE",
        "_CANONICAL_WORKSPACE_LOCK_NEW",
        "_CANONICAL_WORKSPACE_LOCK_INIT",
        "_CANONICAL_WORKSPACE_LOCK_ACQUIRE",
        "_CANONICAL_WORKSPACE_LOCK_RELEASE",
        "_CANONICAL_WORKSPACE_LOCK_ENTER",
        "_CANONICAL_WORKSPACE_LOCK_EXIT",
        "_CANONICAL_OBJECT_SETATTR",
        "_CANONICAL_METHOD_TYPE",
    ):
        monkeypatch.setattr(economic_goal_store_module, name, forged)

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


def test_successor_rejects_rebound_instance_load_and_preserves_canonical_write(tmp_path) -> None:
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

    with pytest.raises(
        TypeError,
        match="economic goal store authority operation binding is immutable",
    ):
        store.load = forged  # type: ignore[method-assign]

    store.persist_automatic_successor(candidate)

    assert store.load() == candidate
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


def test_store_path_exists_ignores_preconstruction_lstat_rebinding(monkeypatch, tmp_path) -> None:
    original = _goal()
    initial = EconomicGoalStore(tmp_path)
    initial.initialize_owner(original)
    authority_path = tmp_path.resolve() / "economic_goal_contract.json"
    before = authority_path.read_bytes()
    calls = 0

    def forged_lstat(self):
        nonlocal calls
        calls += 1
        raise FileNotFoundError("forged not-found authority")

    monkeypatch.setattr(type(tmp_path), "lstat", forged_lstat)
    restarted = EconomicGoalStore(tmp_path)
    candidate = replace(
        original,
        revision=2,
        max_stake_fraction=Decimal("0.01"),
    )

    with pytest.raises(EconomicGoalContractError, match="already exists"):
        restarted.initialize_owner(candidate)

    assert calls == 0
    assert authority_path.read_bytes() == before
    assert restarted.load() == original


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


def test_store_constructor_ignores_prebound_state_descriptors(monkeypatch, tmp_path) -> None:
    class HostileDescriptor:
        def __get__(self, instance, owner):
            raise AssertionError("prebound store descriptor executed")

        def __set__(self, instance, value):
            raise AssertionError("prebound store descriptor setter executed")

    monkeypatch.setattr(EconomicGoalStore, "workspace", HostileDescriptor(), raising=False)
    monkeypatch.setattr(EconomicGoalStore, "path", HostileDescriptor(), raising=False)

    store = EconomicGoalStore(tmp_path)
    instance_state = object.__getattribute__(store, "__dict__")
    canonical_workspace = tmp_path.resolve()
    assert instance_state["workspace"] == canonical_workspace
    assert instance_state["path"] == canonical_workspace / "economic_goal_contract.json"

    store.initialize_owner(_goal())
    assert store.load() == _goal()
    assert (canonical_workspace / "economic_goal_contract.json").exists()


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



def test_store_binding_registry_cannot_be_mutated_to_redirect_authority(tmp_path) -> None:
    store = EconomicGoalStore(tmp_path)
    original = _goal()
    store.initialize_owner(original)

    registry = economic_goal_store_module._STORE_BINDINGS_BY_ID
    binding = registry[id(store)]
    attacker_workspace = (tmp_path / "attacker").resolve()
    attacker_workspace.mkdir()
    attacker_path = attacker_workspace / "economic_goal_contract.json"
    forged_binding = (
        binding[0],
        attacker_workspace,
        attacker_path,
        binding[3],
    )

    with pytest.raises(TypeError):
        registry[id(store)] = forged_binding  # type: ignore[index]

    instance_state = object.__getattribute__(store, "__dict__")
    instance_state["workspace"] = attacker_workspace
    instance_state["path"] = attacker_path

    with pytest.raises(EconomicGoalContractError, match="binding was rebound"):
        store.load()

    assert not attacker_path.exists()
    assert (tmp_path.resolve() / "economic_goal_contract.json").exists()


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
        economic_goal_store_module._BOUND_STORE_LOAD(
            store,
            _json_decoder=forged_decoder,
        )

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
        economic_goal_store_module._BOUND_STORE_PERSIST_AUTOMATIC_SUCCESSOR(
            store,
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


def test_verified_reader_ignores_rebound_os_authorities(monkeypatch, tmp_path) -> None:
    store = EconomicGoalStore(tmp_path)
    store.initialize_owner(_goal())

    def forged(*args, **kwargs):
        raise AssertionError("rebound OS read authority executed")

    for name in (
        "_open_read_only_descriptor",
        "_CANONICAL_OPEN_READ_ONLY_DESCRIPTOR",
        "_CANONICAL_OS_FSTAT",
        "_CANONICAL_OS_STAT",
        "_CANONICAL_OS_FDOPEN",
        "_CANONICAL_OS_SAMEOPENFILE",
        "_CANONICAL_OS_CLOSE",
        "_CANONICAL_STAT_ISREG",
    ):
        if hasattr(economic_goal_store_module, name):
            monkeypatch.setattr(economic_goal_store_module, name, forged)

    assert economic_goal_store_module._read_economic_goal_text(store.path)


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

    def forged_validate(*args, **kwargs):
        raise EconomicGoalContractError("primary validation failure")

    original_close = economic_goal_store_module._CANONICAL_OS_CLOSE

    def forged_close(fd):
        original_close(fd)
        raise OSError("secondary close failure")

    with pytest.raises(
        EconomicGoalContractError, match="primary validation failure"
    ):
        economic_goal_store_module._read_economic_goal_text(
            store.path,
            _validate_file=forged_validate,
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


def test_verified_read_rejects_in_place_mutation_after_final_byte_read(
    tmp_path,
) -> None:
    store = EconomicGoalStore(tmp_path)
    store.initialize_owner(_goal())
    original_open = economic_goal_store_module._CANONICAL_OPEN_READ_ONLY_DESCRIPTOR
    calls = 0

    def racing_open(path):
        nonlocal calls
        calls += 1
        if calls == 4:
            path.write_text('{"schema":"attacker"}', encoding="utf-8")
        return original_open(path)

    with pytest.raises(
        EconomicGoalContractError,
        match="bytes changed after verified read",
    ):
        economic_goal_store_module._read_economic_goal_text(
            store.path,
            _open_descriptor=racing_open,
        )


def _maximally_escaped_restrictions() -> frozenset[str]:
    return frozenset(
        ('"' * 500) + f"{index:012d}"
        for index in range(1024)
    )


def test_payload_encoder_rejects_json_escape_expansion_beyond_restart_envelope() -> None:
    restrictions = _maximally_escaped_restrictions()
    goal = _goal(
        blocked_sports=restrictions,
        blocked_providers=restrictions,
    )

    with pytest.raises(
        EconomicGoalContractError,
        match="persistence text-size limit",
    ):
        economic_goal_to_payload(goal)


def test_owner_initialization_does_not_publish_oversize_persisted_image(tmp_path) -> None:
    restrictions = _maximally_escaped_restrictions()
    goal = _goal(
        blocked_sports=restrictions,
        blocked_providers=restrictions,
    )
    store = EconomicGoalStore(tmp_path)
    calls = []

    def forged_writer(path, payload):
        calls.append((path, payload))
        raise AssertionError("writer executed for payload outside restart envelope")

    with pytest.raises(
        EconomicGoalContractError,
        match="persistence text-size limit",
    ):
        economic_goal_store_module._BOUND_STORE_INITIALIZE_OWNER(
            store,
            goal,
            _writer=forged_writer,
        )

    assert calls == []
    assert not store.path.exists()


def test_payload_encoder_enforces_utf8_byte_envelope() -> None:
    goal = _goal()

    with pytest.raises(
        EconomicGoalContractError,
        match="persistence byte-size limit",
    ):
        economic_goal_store_module._BOUND_ECONOMIC_GOAL_TO_PAYLOAD(
            goal,
            _max_json_bytes=1,
        )


def test_json_decoder_normalizes_pathological_nesting() -> None:
    nested = ("[" * 3000) + "0" + ("]" * 3000)

    with pytest.raises(EconomicGoalContractError, match="invalid economic goal JSON"):
        economic_goal_from_json(nested)


def test_store_rejects_subclass_and_noncanonical_workspace_types(tmp_path) -> None:
    class StoreSubclass(EconomicGoalStore):
        pass

    assert callable(StoreSubclass.load)
    assert callable(StoreSubclass.initialize_owner)
    assert callable(StoreSubclass.persist_automatic_successor)

    class TextSubclass(str):
        pass

    class HostilePathLike:
        calls = 0

        def __fspath__(self):
            type(self).calls += 1
            raise AssertionError("hostile __fspath__ executed")

    with pytest.raises(TypeError, match="exact store type"):
        StoreSubclass(tmp_path)

    with pytest.raises(TypeError, match="exact str or exact Path"):
        EconomicGoalStore(TextSubclass(str(tmp_path)))

    hostile = HostilePathLike()
    with pytest.raises(TypeError, match="exact str or exact Path"):
        EconomicGoalStore(hostile)  # type: ignore[arg-type]
    assert HostilePathLike.calls == 0


def test_store_canonical_workspace_survives_cwd_change(tmp_path, monkeypatch) -> None:
    first_cwd = tmp_path / "first"
    second_cwd = tmp_path / "second"
    first_cwd.mkdir()
    second_cwd.mkdir()

    monkeypatch.chdir(first_cwd)
    store = EconomicGoalStore("workspace")
    expected_workspace = (first_cwd / "workspace").resolve()
    expected_path = expected_workspace / "economic_goal_contract.json"

    assert store.workspace is not None
    assert store.workspace == expected_workspace
    assert store.path == expected_path

    monkeypatch.chdir(second_cwd)
    store.initialize_owner(_goal())

    assert expected_path.exists()
    assert store.load() == _goal()
    assert not (second_cwd / "workspace" / "economic_goal_contract.json").exists()


def test_store_constructor_ignores_rebound_path_resolve_and_join(monkeypatch, tmp_path) -> None:
    expected_workspace = tmp_path.resolve()
    expected_path = expected_workspace / "economic_goal_contract.json"

    def forged(*args, **kwargs):
        raise AssertionError("rebound Path authority executed")

    path_type = economic_goal_store_module._CANONICAL_PATH_TYPE
    monkeypatch.setattr(path_type, "resolve", forged)
    monkeypatch.setattr(path_type, "__truediv__", forged)

    store = EconomicGoalStore(tmp_path)

    assert store.workspace == expected_workspace
    assert store.path == expected_path


def test_payload_decoder_ignores_mutated_enum_value_maps(monkeypatch) -> None:
    payload = economic_goal_to_payload(_goal())

    forged_objective_map = dict(EconomicObjective._value2member_map_)
    forged_objective_map["attacker-objective"] = (
        EconomicObjective.LONG_RUN_RISK_ADJUSTED_BANKROLL_GROWTH
    )
    monkeypatch.setattr(
        EconomicObjective,
        "_value2member_map_",
        forged_objective_map,
    )

    forged_automation_map = dict(AutomationLevel._value2member_map_)
    forged_automation_map[
        int.__index__(AutomationLevel.SUPERVISED_EXECUTION)
    ] = AutomationLevel.HIGHER_AUTONOMY
    forged_automation_map[99] = AutomationLevel.ANALYSIS_ONLY
    monkeypatch.setattr(
        AutomationLevel,
        "_value2member_map_",
        forged_automation_map,
    )

    restored = economic_goal_from_payload(payload)
    assert restored.automation_level is AutomationLevel.SUPERVISED_EXECUTION

    forged_payload = {
        **payload,
        "contract": {
            **payload["contract"],
            "automation_level": 99,
        },
    }
    with pytest.raises(EconomicGoalContractError, match="unsupported automation_level"):
        economic_goal_from_payload(forged_payload)

    forged_objective_payload = {
        **payload,
        "contract": {
            **payload["contract"],
            "objective": "attacker-objective",
        },
    }
    with pytest.raises(EconomicGoalContractError, match="unsupported economic objective"):
        economic_goal_from_payload(forged_objective_payload)


def test_payload_encoder_uses_immutable_enum_base_values(monkeypatch) -> None:
    goal = _goal(automation_level=AutomationLevel.BOUNDED_AUTONOMY)

    monkeypatch.setattr(
        goal.objective,
        "_value_",
        "attacker-objective",
    )
    monkeypatch.setattr(
        goal.automation_level,
        "_value_",
        0,
    )

    payload = economic_goal_to_payload(goal)
    body = payload["contract"]
    assert body["objective"] == "long_run_risk_adjusted_bankroll_growth"
    assert body["automation_level"] == 3


def test_payload_encoder_ignores_rebound_enum_serialization_protocols(monkeypatch) -> None:
    goal = _goal()
    expected = economic_goal_to_payload(goal)

    def forged_value(self):
        raise AssertionError("rebound enum value descriptor executed")

    def forged_int(self):
        raise AssertionError("rebound IntEnum __int__ executed")

    monkeypatch.setattr(
        economic_goal_store_module.EconomicObjective,
        "value",
        property(forged_value),
        raising=False,
    )
    monkeypatch.setattr(
        AutomationLevel,
        "value",
        property(forged_value),
        raising=False,
    )
    monkeypatch.setattr(
        AutomationLevel,
        "__int__",
        forged_int,
    )

    assert economic_goal_to_payload(goal) == expected


def test_public_goal_codec_authority_rejects_helper_injection() -> None:
    goal = _goal()
    payload = economic_goal_to_payload(goal)

    with pytest.raises(TypeError):
        economic_goal_to_payload(
            goal,
            _goal_validator=lambda contract: None,
        )  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        economic_goal_from_payload(
            payload,
            _decimal_decoder=lambda name, value: Decimal("0"),
        )  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        economic_goal_from_json(
            "{}",
            _payload_decoder=lambda payload: goal,
        )  # type: ignore[call-arg]


def test_store_authority_rejects_in_place_nested_kwdefault_mutation() -> None:
    def nested_codec(*_args, policy="strict"):
        return None

    def operation(_value, helper=nested_codec):
        helper()

    bound = economic_goal_store_module._make_store_callable_authority(
        operation,
        "synthetic store authority",
    )
    original_kwdefaults = nested_codec.__kwdefaults__
    assert original_kwdefaults is not None
    original_policy = original_kwdefaults["policy"]

    nested_codec.__kwdefaults__["policy"] = "forged"
    try:
        with pytest.raises(
            EconomicGoalContractError,
            match="synthetic store authority nested keyword defaults authority changed",
        ):
            bound(None)
    finally:
        nested_codec.__kwdefaults__["policy"] = original_policy


def test_store_constructor_rejects_path_authority_injection(tmp_path) -> None:
    with pytest.raises(TypeError):
        EconomicGoalStore(
            tmp_path,
            _path_constructor=lambda value: tmp_path,
        )  # type: ignore[call-arg]


def test_store_write_authority_rejects_transition_injection_without_mutation(tmp_path) -> None:
    previous = _goal()
    store = EconomicGoalStore(tmp_path)
    store.initialize_owner(previous)
    expanding = replace(previous, revision=2, max_stake_fraction=Decimal("0.03"))

    with pytest.raises(TypeError):
        store.persist_automatic_successor(
            expanding,
            _transition_validator=lambda before, after: None,
        )  # type: ignore[call-arg]

    assert EconomicGoalStore(tmp_path).load() == previous


def test_store_public_methods_reject_writer_and_decoder_injection(tmp_path) -> None:
    goal = _goal()
    store = EconomicGoalStore(tmp_path)

    with pytest.raises(TypeError):
        store.initialize_owner(
            goal,
            _writer=lambda path, payload: None,
        )  # type: ignore[call-arg]

    store.initialize_owner(goal)
    with pytest.raises(TypeError):
        store.load(_json_decoder=lambda text: goal)  # type: ignore[call-arg]


def test_store_transition_proof_is_bound_to_exact_persisted_payload(tmp_path) -> None:
    previous = _goal()
    candidate = replace(previous, revision=2, max_stake_fraction=Decimal("0.01"))
    store = EconomicGoalStore(tmp_path)
    store.initialize_owner(previous)

    canonical_transition = economic_goal_store_module._CANONICAL_TRANSITION_VALIDATOR

    def mutate_original_after_proof_input_is_built(before, snapshot):
        canonical_transition(before, snapshot)
        object.__setattr__(candidate, "max_stake_fraction", Decimal("0.99"))

    economic_goal_store_module._BOUND_STORE_PERSIST_AUTOMATIC_SUCCESSOR(
        store,
        candidate,
        _transition_validator=mutate_original_after_proof_input_is_built,
    )

    restored = EconomicGoalStore(tmp_path).load()
    assert restored.revision == 2
    assert restored.max_stake_fraction == Decimal("0.01")
    assert candidate.max_stake_fraction == Decimal("0.99")


def test_payload_decoder_revalidates_with_captured_contract_validator(monkeypatch) -> None:
    payload = economic_goal_to_payload(_goal())
    body = payload["contract"]
    assert type(body) is dict
    body["max_stake_fraction"] = "2"

    with pytest.raises(
        TypeError,
        match="public authority operation binding is immutable",
    ):
        monkeypatch.setattr(EconomicGoalContract, "__post_init__", lambda self: None)

    monkeypatch.setattr(
        economic_goal_store_module,
        "_CANONICAL_GOAL_VALIDATOR",
        lambda self: None,
    )

    with pytest.raises(EconomicGoalContractError, match="between 0 and 1"):
        economic_goal_from_payload(payload)


def test_owner_initialization_rejects_dangling_symlink_entry(tmp_path) -> None:
    store = EconomicGoalStore(tmp_path)
    missing_target = tmp_path / "missing-target.json"
    try:
        store.path.symlink_to(missing_target)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    assert store.path.is_symlink()
    assert not store.path.exists()

    with pytest.raises(EconomicGoalContractError, match="already exists"):
        store.initialize_owner(_goal())

    assert store.path.is_symlink()


def test_persistence_snapshot_isolated_from_later_source_mutation() -> None:
    goal = _goal()
    snapshot = economic_goal_store_module._snapshot_economic_goal_contract(goal)

    object.__setattr__(goal, "max_stake_fraction", Decimal("0.99"))
    object.__setattr__(goal, "blocked_sports", frozenset())

    assert snapshot.max_stake_fraction == Decimal("0.02")
    assert snapshot.blocked_sports == frozenset({"football"})
    assert goal.max_stake_fraction == Decimal("0.99")
    assert goal.blocked_sports == frozenset()


def test_payload_encoder_rejects_source_mutation_between_canonical_snapshots() -> None:
    goal = _goal()
    canonical_snapshot = economic_goal_store_module._canonical_contract_snapshot
    mutated = False

    def snapshot_then_mutate(contract):
        nonlocal mutated
        snapshot = canonical_snapshot(contract)
        if not mutated:
            object.__setattr__(goal, "max_stake_fraction", Decimal("0.99"))
            object.__setattr__(goal, "blocked_sports", frozenset())
            mutated = True
        return snapshot

    with pytest.raises(EconomicGoalContractError, match="changed during payload encoding"):
        economic_goal_store_module._BOUND_ECONOMIC_GOAL_TO_PAYLOAD(
            goal,
            _snapshot=snapshot_then_mutate,
        )

    assert mutated is True



def test_store_snapshot_covers_every_captured_contract_slot(
    monkeypatch, tmp_path
) -> None:
    goal = _goal()
    field_names = economic_goal_store_module._CONTRACT_KEYS_ORDERED
    expected = economic_goal_to_payload(goal)["contract"]

    for name in field_names:
        class ForgedDescriptor:
            def __get__(self, instance, owner=None):
                raise AssertionError("rebound contract descriptor getter executed")

            def __set__(self, instance, value):
                raise AssertionError("rebound contract descriptor setter executed")

        monkeypatch.setattr(EconomicGoalContract, name, ForgedDescriptor())
        snapshot = economic_goal_store_module._snapshot_economic_goal_contract(goal)
        rebuilt = economic_goal_store_module._build_economic_goal_contract(
            dict(
                zip(
                    field_names,
                    economic_goal_store_module._canonical_contract_snapshot(goal),
                )
            ),
        )
        assert (
            economic_goal_store_module._canonical_contract_snapshot(snapshot)
            == economic_goal_store_module._canonical_contract_snapshot(rebuilt)
        )
        monkeypatch.undo()

    assert economic_goal_to_payload(goal)["contract"] == expected




def test_store_snapshot_helper_ignores_rebound_snapshot_alias(monkeypatch, tmp_path) -> None:
    goal = _goal(max_stake_fraction=Decimal("0.03"))

    def forged(*args, **kwargs):
        raise AssertionError("rebound canonical contract snapshot executed")

    monkeypatch.setattr(
        economic_goal_store_module,
        "_canonical_contract_snapshot",
        forged,
    )

    snapshot = economic_goal_store_module._snapshot_economic_goal_contract(goal)
    assert snapshot.max_stake_fraction == Decimal("0.03")




def test_store_successor_rejects_descriptor_laundered_expansion(
    monkeypatch, tmp_path
) -> None:
    previous = _goal()
    previous_snapshot = economic_goal_store_module._canonical_contract_snapshot(previous)
    candidate = replace(
        previous,
        revision=2,
        max_stake_fraction=Decimal("0.03"),
    )
    store = EconomicGoalStore(tmp_path)
    store.initialize_owner(previous)

    class ForgedDescriptor:
        def __get__(self, instance, owner=None):
            return Decimal("0.02")

    monkeypatch.setattr(
        EconomicGoalContract,
        "max_stake_fraction",
        ForgedDescriptor(),
    )

    with pytest.raises(EconomicGoalContractError, match="must not increase"):
        store.persist_automatic_successor(candidate)

    restored = EconomicGoalStore(tmp_path).load()
    assert (
        economic_goal_store_module._canonical_contract_snapshot(restored)
        == previous_snapshot
    )


def test_payload_snapshot_uses_sealed_contract_constructor_authority(monkeypatch) -> None:
    goal = _goal()
    expected = economic_goal_to_payload(goal)

    def forged(*args, **kwargs):
        raise AssertionError("rebound EconomicGoalContract constructor executed")

    for name in ("__init__", "__post_init__"):
        with pytest.raises(
            TypeError,
            match="public authority operation binding is immutable",
        ):
            monkeypatch.setattr(EconomicGoalContract, name, forged)

    assert economic_goal_to_payload(goal) == expected


def test_payload_decoder_uses_sealed_contract_constructor_authority(monkeypatch) -> None:
    expected = _goal()
    payload = economic_goal_to_payload(expected)

    def forged(*args, **kwargs):
        raise AssertionError("rebound EconomicGoalContract constructor executed")

    for name in ("__init__", "__post_init__"):
        with pytest.raises(
            TypeError,
            match="public authority operation binding is immutable",
        ):
            monkeypatch.setattr(EconomicGoalContract, name, forged)

    restored = economic_goal_from_payload(payload)
    assert restored == expected

def test_payload_encoder_rejects_bound_keyword_default_rebinding() -> None:
    contract = _goal()
    operation = economic_goal_store_module._BOUND_ECONOMIC_GOAL_TO_PAYLOAD
    original_kwdefaults = operation.__kwdefaults__

    operation.__kwdefaults__ = {"forged_authority": object()}
    try:
        with pytest.raises(
            EconomicGoalContractError,
            match="payload encoder keyword defaults authority changed",
        ):
            economic_goal_to_payload(contract)
    finally:
        operation.__kwdefaults__ = original_kwdefaults


def test_goal_store_field_order_authority_ignores_runtime_rebinding(monkeypatch) -> None:
    contract = _goal()

    canonical_snapshot = economic_goal_store._snapshot_economic_goal_contract(contract)
    canonical_payload = economic_goal_store.economic_goal_to_payload(contract)

    monkeypatch.setattr(
        economic_goal_store,
        "_CONTRACT_KEYS_ORDERED",
        tuple(reversed(economic_goal_store._CONTRACT_KEYS_ORDERED)),
    )

    rebound_snapshot = economic_goal_store._snapshot_economic_goal_contract(contract)
    rebound_payload = economic_goal_store.economic_goal_to_payload(contract)

    assert rebound_snapshot == canonical_snapshot
    assert rebound_payload == canonical_payload


def test_goal_store_field_order_authority_does_not_accept_forged_shape(monkeypatch) -> None:
    contract = _goal()
    forged = ("goal_id",)

    monkeypatch.setattr(economic_goal_store, "_CONTRACT_KEYS_ORDERED", forged)

    snapshot = economic_goal_store._snapshot_economic_goal_contract(contract)
    payload = economic_goal_store.economic_goal_to_payload(contract)

    assert snapshot == contract
    assert payload["contract"]["goal_id"] == contract.goal_id
    assert payload["contract"]["currency"] == contract.currency


def test_store_callable_authority_rejects_transitive_nested_code_mutation() -> None:
    def leaf_validator(_value):
        return True

    def nested_validator(_value, _leaf=leaf_validator):
        return _leaf(_value)

    def operation(_value, _validator=nested_validator):
        return _validator(_value)

    bound = economic_goal_store_module._make_store_callable_authority(
        operation,
        "synthetic transitive authority",
    )
    original_code = leaf_validator.__code__

    def forged_leaf(_value):
        raise AssertionError("forged transitive validator executed")

    leaf_validator.__code__ = forged_leaf.__code__
    try:
        with pytest.raises(
            EconomicGoalContractError,
            match="synthetic transitive authority transitive nested authority changed",
        ):
            bound(object())
    finally:
        leaf_validator.__code__ = original_code


def test_public_payload_encoder_rejects_transitive_contract_validator_mutation() -> None:
    goal = _goal()
    validator = economic_goal_store_module._CANONICAL_GOAL_VALIDATOR
    nested_validator = validator.__defaults__[2]
    original_code = nested_validator.__code__

    def forged_decimal_validator(_name, value, *args, **kwargs):
        return value

    nested_validator.__code__ = forged_decimal_validator.__code__
    try:
        with pytest.raises(
            EconomicGoalContractError,
            match="economic-goal payload encoder transitive nested authority changed",
        ):
            economic_goal_to_payload(goal)
    finally:
        nested_validator.__code__ = original_code

def test_store_callable_authority_ignores_rebound_introspection_globals(
    monkeypatch,
) -> None:
    def operation(_value):
        return "canonical"

    bound = economic_goal_store_module._make_store_callable_authority(
        operation,
        "synthetic",
    )
    original_code = operation.__code__

    def forged_operation(_value):
        return "forged"

    monkeypatch.setattr(
        economic_goal_store_module,
        "enumerate",
        lambda *_args, **_kwargs: (),
        raising=False,
    )
    monkeypatch.setattr(
        economic_goal_store_module,
        "getattr",
        lambda *_args, **_kwargs: None,
        raising=False,
    )
    monkeypatch.setattr(
        economic_goal_store_module,
        "tuple",
        lambda *_args, **_kwargs: (),
        raising=False,
    )
    monkeypatch.setattr(
        economic_goal_store_module,
        "EconomicGoalContractError",
        RuntimeError,
    )

    operation.__code__ = forged_operation.__code__
    try:
        with pytest.raises(
            EconomicGoalContractError,
            match="synthetic authority changed",
        ):
            bound(object())
    finally:
        operation.__code__ = original_code

def test_store_instance_descriptor_ignores_rebound_method_type(
    monkeypatch,
    tmp_path,
) -> None:
    goal = _goal()
    store = EconomicGoalStore(tmp_path)
    store.initialize_owner(goal)

    def forged_method_type(*_args, **_kwargs):
        return lambda *_call_args, **_call_kwargs: "forged"

    monkeypatch.setattr(
        economic_goal_store_module,
        "MethodType",
        forged_method_type,
    )

    assert store.load() == goal


def test_store_class_guard_ignores_rebound_getattr(
    monkeypatch,
    tmp_path,
) -> None:
    goal = _goal()
    store = EconomicGoalStore(tmp_path)
    store.initialize_owner(goal)

    def forged_getattr(*_args, **_kwargs):
        return lambda *_descriptor_args: (lambda _store: "forged")

    monkeypatch.setattr(
        economic_goal_store_module,
        "getattr",
        forged_getattr,
        raising=False,
    )

    assert EconomicGoalStore.load(store) == goal

def test_payload_decoder_ignores_rebound_frozenset_that_would_erase_restrictions(
    monkeypatch,
) -> None:
    expected = _goal(
        blocked_sports=frozenset({"football", "tennis"}),
        blocked_providers=frozenset({"provider:a"}),
        blocked_markets=frozenset({"market:x"}),
    )
    payload = economic_goal_to_payload(expected)
    canonical_frozenset = frozenset

    def forged_frozenset(value=()):
        if type(value) is dict:
            return canonical_frozenset(value)
        return canonical_frozenset()

    monkeypatch.setattr(
        economic_goal_store_module,
        "frozenset",
        forged_frozenset,
        raising=False,
    )

    restored = economic_goal_from_payload(payload)

    assert restored == expected
    assert restored.blocked_sports == canonical_frozenset({"football", "tennis"})
    assert restored.blocked_providers == canonical_frozenset({"provider:a"})
    assert restored.blocked_markets == canonical_frozenset({"market:x"})

def test_payload_decoder_rejects_mapping_subclass_when_module_type_is_rebound(
    monkeypatch,
) -> None:
    class MappingSubclass(dict):
        pass

    payload = MappingSubclass(economic_goal_to_payload(_goal()))
    canonical_type = type

    def forged_type(value):
        if canonical_type(value) is MappingSubclass:
            return dict
        return canonical_type(value)

    monkeypatch.setattr(
        economic_goal_store_module,
        "type",
        forged_type,
        raising=False,
    )

    with pytest.raises(EconomicGoalContractError, match="JSON object"):
        economic_goal_from_payload(payload)


def test_store_constructor_rejects_subclass_when_module_type_is_rebound(
    monkeypatch,
    tmp_path,
) -> None:
    class StoreSubclass(EconomicGoalStore):
        pass

    canonical_type = type

    def forged_type(value):
        if canonical_type(value) is StoreSubclass:
            return EconomicGoalStore
        return canonical_type(value)

    monkeypatch.setattr(
        economic_goal_store_module,
        "type",
        forged_type,
        raising=False,
    )

    with pytest.raises(TypeError, match="exact store type"):
        StoreSubclass(tmp_path)

