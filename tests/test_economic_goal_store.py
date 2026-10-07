from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import os

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


class _GoalSubclass(EconomicGoalContract):
    def __getattribute__(self, name: str) -> object:
        if name == "goal_id":
            return "caller-substituted-goal"
        return super().__getattribute__(name)


class _StringSubclass(str):
    pass


class _HostileComparableString(str):
    comparisons = 0

    def __hash__(self) -> int:
        type(self).comparisons += 1
        return super().__hash__()

    def __eq__(self, other: object) -> bool:
        type(self).comparisons += 1
        return super().__eq__(other)


class _DictSubclass(dict[str, object]):
    pass


class _ListSubclass(list[str]):
    pass


class _HostileDecimalField:
    calls = 0

    def __str__(self) -> str:
        type(self).calls += 1
        return "0.99"


class _HostilePathLike:
    calls = 0

    def __fspath__(self) -> str:
        type(self).calls += 1
        return "/tmp/forged-economic-goal-store"


class _HostileStoreSubclass(EconomicGoalStore):
    hash_calls = 0
    equality_calls = 0

    def __hash__(self) -> int:
        type(self).hash_calls += 1
        return 7

    def __eq__(self, other: object) -> bool:
        type(self).equality_calls += 1
        return self is other


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



def test_persistence_revalidation_ignores_contract_post_init_rebinding(
    monkeypatch,
) -> None:
    goal = _goal()
    _HostileDecimalField.calls = 0
    object.__setattr__(
        goal,
        "max_stake_fraction",
        _HostileDecimalField(),
    )
    monkeypatch.setattr(
        economic_goal_store_module._CANONICAL_GOAL_TYPE,
        "__post_init__",
        lambda self: None,
    )

    with pytest.raises(EconomicGoalContractError):
        economic_goal_to_payload(goal)

    assert _HostileDecimalField.calls == 0


def test_persistence_revalidates_mutated_exact_contract_before_serialization() -> None:
    goal = _goal()
    _HostileDecimalField.calls = 0
    object.__setattr__(
        goal,
        "max_stake_fraction",
        _HostileDecimalField(),
    )

    with pytest.raises(EconomicGoalContractError):
        economic_goal_to_payload(goal)

    assert _HostileDecimalField.calls == 0


def test_owner_store_rejects_mutated_exact_contract_without_publication(
    tmp_path,
) -> None:
    goal = _goal()
    object.__setattr__(
        goal,
        "max_stake_fraction",
        _HostileDecimalField(),
    )
    store = EconomicGoalStore(tmp_path)

    with pytest.raises(EconomicGoalContractError):
        store.initialize_owner(goal)

    assert not store.path.exists()

def test_persistence_type_witness_ignores_module_contract_rebinding(
    monkeypatch,
) -> None:
    canonical = _goal()
    monkeypatch.setattr(
        economic_goal_store_module,
        "EconomicGoalContract",
        _GoalSubclass,
    )

    payload = economic_goal_to_payload(canonical)
    assert economic_goal_from_payload(payload) == canonical


def test_persistence_rejects_forged_subclass_after_module_rebinding(
    monkeypatch,
) -> None:
    canonical = _goal()
    forged = _GoalSubclass(
        goal_id=canonical.goal_id,
        revision=canonical.revision,
        bankroll_id=canonical.bankroll_id,
        currency=canonical.currency,
        objective=canonical.objective,
        max_stake_fraction=canonical.max_stake_fraction,
        max_stake_amount=canonical.max_stake_amount,
        max_session_loss_fraction=canonical.max_session_loss_fraction,
        max_day_loss_fraction=canonical.max_day_loss_fraction,
        max_drawdown_fraction=canonical.max_drawdown_fraction,
        max_capital_at_risk_fraction=canonical.max_capital_at_risk_fraction,
        max_event_concentration_fraction=canonical.max_event_concentration_fraction,
        max_market_concentration_fraction=canonical.max_market_concentration_fraction,
        max_provider_concentration_fraction=canonical.max_provider_concentration_fraction,
        max_sport_concentration_fraction=canonical.max_sport_concentration_fraction,
        max_turnover_fraction=canonical.max_turnover_fraction,
        max_risk_of_ruin=canonical.max_risk_of_ruin,
        max_execution_slippage_fraction=canonical.max_execution_slippage_fraction,
        max_quote_age_seconds=canonical.max_quote_age_seconds,
        minimum_data_quality=canonical.minimum_data_quality,
        max_concurrent_positions=canonical.max_concurrent_positions,
        max_parlay_legs=canonical.max_parlay_legs,
        automation_level=canonical.automation_level,
        emergency_stop=canonical.emergency_stop,
        blocked_sports=canonical.blocked_sports,
        blocked_providers=canonical.blocked_providers,
        blocked_markets=canonical.blocked_markets,
    )
    monkeypatch.setattr(
        economic_goal_store_module,
        "EconomicGoalContract",
        _GoalSubclass,
    )

    with pytest.raises(EconomicGoalContractError, match="canonical EconomicGoalContract"):
        economic_goal_to_payload(forged)


def test_persistence_rejects_contract_subclass() -> None:
    canonical = _goal()
    derived = _GoalSubclass(
        goal_id=canonical.goal_id,
        revision=canonical.revision,
        bankroll_id=canonical.bankroll_id,
        currency=canonical.currency,
        objective=canonical.objective,
        max_stake_fraction=canonical.max_stake_fraction,
        max_stake_amount=canonical.max_stake_amount,
        max_session_loss_fraction=canonical.max_session_loss_fraction,
        max_day_loss_fraction=canonical.max_day_loss_fraction,
        max_drawdown_fraction=canonical.max_drawdown_fraction,
        max_capital_at_risk_fraction=canonical.max_capital_at_risk_fraction,
        max_event_concentration_fraction=canonical.max_event_concentration_fraction,
        max_market_concentration_fraction=canonical.max_market_concentration_fraction,
        max_provider_concentration_fraction=canonical.max_provider_concentration_fraction,
        max_sport_concentration_fraction=canonical.max_sport_concentration_fraction,
        max_turnover_fraction=canonical.max_turnover_fraction,
        max_risk_of_ruin=canonical.max_risk_of_ruin,
        max_execution_slippage_fraction=canonical.max_execution_slippage_fraction,
        max_quote_age_seconds=canonical.max_quote_age_seconds,
        minimum_data_quality=canonical.minimum_data_quality,
        max_concurrent_positions=canonical.max_concurrent_positions,
        max_parlay_legs=canonical.max_parlay_legs,
        automation_level=canonical.automation_level,
        emergency_stop=canonical.emergency_stop,
        blocked_sports=canonical.blocked_sports,
        blocked_providers=canonical.blocked_providers,
        blocked_markets=canonical.blocked_markets,
    )

    with pytest.raises(
        EconomicGoalContractError,
        match="canonical EconomicGoalContract",
    ):
        economic_goal_to_payload(derived)


def test_json_decoder_rejects_string_subclass() -> None:
    with pytest.raises(EconomicGoalContractError, match="JSON must be text"):
        economic_goal_from_json(_StringSubclass("{}"))


def test_payload_decoder_rejects_hostile_schema_before_comparison() -> None:
    payload = economic_goal_to_payload(_goal())
    _HostileComparableString.comparisons = 0
    payload["schema"] = _HostileComparableString("autosport.economic_goal_contract")

    with pytest.raises(EconomicGoalContractError, match="unsupported economic goal schema"):
        economic_goal_from_payload(payload)

    assert _HostileComparableString.comparisons == 0


def test_payload_decoder_rejects_hostile_objective_before_enum_lookup() -> None:
    payload = economic_goal_to_payload(_goal())
    body = payload["contract"]
    assert type(body) is dict
    _HostileComparableString.comparisons = 0
    body["objective"] = _HostileComparableString(
        "long_run_risk_adjusted_bankroll_growth"
    )

    with pytest.raises(EconomicGoalContractError, match="objective must be a string"):
        economic_goal_from_payload(payload)

    assert _HostileComparableString.comparisons == 0


def test_payload_decoder_rejects_mapping_subclass() -> None:
    payload = _DictSubclass(economic_goal_to_payload(_goal()))

    with pytest.raises(EconomicGoalContractError, match="JSON object"):
        economic_goal_from_payload(payload)


def test_payload_decoder_rejects_oversize_restriction_list_before_sort() -> None:
    payload = economic_goal_to_payload(_goal())
    body = payload["contract"]
    assert type(body) is dict
    body["blocked_sports"] = [
        f"sport:{index:04d}"
        for index in range(1025)
    ]

    with pytest.raises(EconomicGoalContractError, match="restriction-count limit"):
        economic_goal_from_payload(payload)


def test_payload_decoder_rejects_oversize_restriction_text_before_sort() -> None:
    payload = economic_goal_to_payload(_goal())
    body = payload["contract"]
    assert type(body) is dict
    body["blocked_sports"] = ["x" * 513]

    with pytest.raises(EconomicGoalContractError, match="non-canonical restriction text"):
        economic_goal_from_payload(payload)


def test_payload_decoder_rejects_noncanonical_restriction_text_before_set_build() -> None:
    payload = economic_goal_to_payload(_goal())
    body = payload["contract"]
    assert type(body) is dict
    body["blocked_sports"] = [" padded "]

    with pytest.raises(EconomicGoalContractError, match="non-canonical restriction text"):
        economic_goal_from_payload(payload)


def test_payload_decoder_rejects_restriction_list_subclass() -> None:
    payload = economic_goal_to_payload(_goal())
    body = payload["contract"]
    assert type(body) is dict
    body["blocked_sports"] = _ListSubclass(["boxing", "tennis"])

    with pytest.raises(EconomicGoalContractError, match="sorted JSON array"):
        economic_goal_from_payload(payload)


def test_payload_decoder_ignores_rebound_semantic_helpers(
    monkeypatch,
) -> None:
    goal = _goal()
    payload = economic_goal_to_payload(goal)

    monkeypatch.setattr(
        economic_goal_store_module,
        "_require_exact_keys",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        economic_goal_store_module,
        "_decimal_text",
        lambda *args, **kwargs: Decimal("0.99"),
    )
    monkeypatch.setattr(
        economic_goal_store_module,
        "_restriction_set",
        lambda *args, **kwargs: frozenset(),
    )

    assert economic_goal_from_payload(payload) == goal


def test_persistence_schema_semantics_ignore_rebound_public_constants(
    monkeypatch,
) -> None:
    goal = _goal()

    monkeypatch.setattr(
        economic_goal_store_module,
        "ECONOMIC_GOAL_SCHEMA",
        "autosport.attacker",
    )
    monkeypatch.setattr(
        economic_goal_store_module,
        "ECONOMIC_GOAL_SCHEMA_VERSION",
        999,
    )
    monkeypatch.setattr(
        economic_goal_store_module,
        "_ROOT_KEYS",
        frozenset({"contract"}),
    )
    monkeypatch.setattr(
        economic_goal_store_module,
        "_CONTRACT_KEYS",
        frozenset(),
    )
    monkeypatch.setattr(
        economic_goal_store_module,
        "_DECIMAL_FIELDS",
        (),
    )
    monkeypatch.setattr(
        economic_goal_store_module,
        "_RESTRICTION_FIELDS",
        (),
    )

    payload = economic_goal_to_payload(goal)
    assert payload["schema"] == "autosport.economic_goal_contract"
    assert payload["schema_version"] == 1
    assert economic_goal_from_payload(payload) == goal

    body = payload["contract"]
    assert type(body) is dict
    del body["max_risk_of_ruin"]
    with pytest.raises(EconomicGoalContractError, match="keys must match schema exactly"):
        economic_goal_from_payload(payload)


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


def test_store_authority_ignores_rebound_public_dependencies(
    tmp_path,
    monkeypatch,
) -> None:
    def forged_dependency(*args, **kwargs):
        raise AssertionError("rebound public dependency executed")

    for name in (
        "Path",
        "WorkspaceEconomicLock",
        "atomic_write_json",
        "_open_read_only_descriptor",
        "strict_json_loads",
        "economic_goal_to_payload",
        "economic_goal_from_payload",
        "economic_goal_from_json",
        "validate_automatic_transition",
        "_read_economic_goal_text",
        "_register_store_binding",
        "_require_store_binding",
    ):
        monkeypatch.setattr(
            economic_goal_store_module,
            name,
            forged_dependency,
        )

    store = EconomicGoalStore(tmp_path)
    initial = _goal()
    store.initialize_owner(initial)
    assert store.load() == initial

    tightened = replace(
        initial,
        revision=2,
        max_stake_fraction=Decimal("0.01"),
    )
    store.persist_automatic_successor(tightened)
    assert store.load() == tightened


def test_store_rejects_workspace_subclasses_and_pathlike_before_conversion(tmp_path) -> None:
    with pytest.raises(TypeError, match="exact str or exact Path"):
        EconomicGoalStore(_StringSubclass(str(tmp_path)))

    _HostilePathLike.calls = 0
    with pytest.raises(TypeError, match="exact str or exact Path"):
        EconomicGoalStore(_HostilePathLike())  # type: ignore[arg-type]
    assert _HostilePathLike.calls == 0


def test_store_rejects_subclass_before_hash_or_equality_authority_hooks(tmp_path) -> None:
    _HostileStoreSubclass.hash_calls = 0
    _HostileStoreSubclass.equality_calls = 0

    with pytest.raises(TypeError, match="exact store type"):
        _HostileStoreSubclass(tmp_path)

    assert _HostileStoreSubclass.hash_calls == 0
    assert _HostileStoreSubclass.equality_calls == 0


def test_store_exact_type_guard_ignores_module_class_rebinding(
    tmp_path,
    monkeypatch,
) -> None:
    canonical_store_type = EconomicGoalStore
    monkeypatch.setattr(
        economic_goal_store_module,
        "EconomicGoalStore",
        _HostileStoreSubclass,
    )

    # A saved reference to the defining class remains canonical even after the
    # mutable module export is rebound.
    store = canonical_store_type(tmp_path)
    assert store.path == tmp_path / canonical_store_type.FILE_NAME

    with pytest.raises(TypeError, match="exact store type"):
        _HostileStoreSubclass(tmp_path)


def test_store_methods_fail_closed_after_runtime_class_swap(tmp_path) -> None:
    store = EconomicGoalStore(tmp_path)
    store.__class__ = _HostileStoreSubclass

    for operation in (
        lambda: EconomicGoalStore.load(store),
        lambda: EconomicGoalStore.initialize_owner(store, _goal()),
        lambda: EconomicGoalStore.persist_automatic_successor(
            store,
            replace(_goal(), revision=2),
        ),
    ):
        with pytest.raises(TypeError, match="exact store type"):
            operation()

    assert _HostileStoreSubclass.hash_calls == 0
    assert _HostileStoreSubclass.equality_calls == 0
    assert not (tmp_path / EconomicGoalStore.FILE_NAME).exists()


def test_store_relative_workspace_binding_survives_cwd_change(
    tmp_path,
    monkeypatch,
) -> None:
    first_cwd = tmp_path / "first"
    second_cwd = tmp_path / "second"
    first_cwd.mkdir()
    second_cwd.mkdir()
    monkeypatch.chdir(first_cwd)

    store = EconomicGoalStore("workspace")
    expected_workspace = first_cwd / "workspace"
    assert store.workspace == expected_workspace

    monkeypatch.chdir(second_cwd)
    store.initialize_owner(_goal())

    assert store.path == expected_workspace / EconomicGoalStore.FILE_NAME
    assert store.path.exists()
    assert not (second_cwd / "workspace" / EconomicGoalStore.FILE_NAME).exists()


def test_store_path_ignores_mutated_public_filename_attribute(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(EconomicGoalStore, "FILE_NAME", "attacker.json")

    store = EconomicGoalStore(tmp_path)
    store.initialize_owner(_goal())

    assert store.path == tmp_path.resolve(strict=False) / "economic_goal_contract.json"
    assert store.path.exists()
    assert not (tmp_path / "attacker.json").exists()


def test_store_rejects_explicit_reinitialization_rebinding(tmp_path) -> None:
    canonical = tmp_path / "canonical"
    redirected = tmp_path / "redirected"
    store = EconomicGoalStore(canonical)

    with pytest.raises(RuntimeError, match="already registered"):
        EconomicGoalStore.__init__(store, redirected)

    assert store.workspace == canonical.resolve(strict=False)
    assert store.path == canonical.resolve(strict=False) / EconomicGoalStore.FILE_NAME

    store.initialize_owner(_goal())

    assert store.path.exists()
    assert not (redirected / EconomicGoalStore.FILE_NAME).exists()


def test_store_binding_cannot_be_rebound_after_construction(tmp_path) -> None:
    canonical = tmp_path / "canonical"
    redirected = tmp_path / "redirected"
    store = EconomicGoalStore(canonical)

    with pytest.raises(AttributeError):
        store.workspace = redirected  # type: ignore[misc]
    with pytest.raises(AttributeError):
        store.path = redirected / store.FILE_NAME  # type: ignore[misc]

    # Even hostile shadow attributes are non-authoritative because all store
    # operations read the out-of-band canonical binding.
    store.__dict__["workspace"] = redirected
    store.__dict__["path"] = redirected / store.FILE_NAME
    object.__setattr__(store, "_workspace", redirected)
    object.__setattr__(store, "_path", redirected / store.FILE_NAME)

    assert store.workspace == canonical
    assert store.path == canonical / store.FILE_NAME

    store.initialize_owner(_goal())

    assert (canonical / store.FILE_NAME).exists()
    assert not (redirected / store.FILE_NAME).exists()


def test_store_rejects_payload_too_large_to_reload_before_publication(tmp_path) -> None:
    oversized = frozenset(
        f"sport:{index:04d}:" + ("x" * 500)
        for index in range(150)
    )
    goal = _goal(blocked_sports=oversized)
    store = EconomicGoalStore(tmp_path)

    with pytest.raises(
        EconomicGoalContractError,
        match="persistence size limit",
    ):
        store.initialize_owner(goal)

    assert not store.path.exists()


def test_store_path_authority_ignores_public_method_rebinding(
    tmp_path,
    monkeypatch,
) -> None:
    expected_workspace = tmp_path.resolve()
    expected_path = expected_workspace / "economic_goal_contract.json"

    def forbidden(*args, **kwargs):
        raise AssertionError("rebound Path authority method executed")

    monkeypatch.setattr(economic_goal_store_module.Path, "resolve", forbidden)
    monkeypatch.setattr(economic_goal_store_module.Path, "__truediv__", forbidden)

    store = EconomicGoalStore(tmp_path)
    assert store.workspace == expected_workspace
    assert store.path == expected_path


def test_owner_creation_only_check_ignores_path_exists_rebinding(
    tmp_path,
    monkeypatch,
) -> None:
    store = EconomicGoalStore(tmp_path)
    original = _goal()
    store.initialize_owner(original)

    monkeypatch.setattr(
        economic_goal_store_module.Path,
        "exists",
        lambda *args, **kwargs: False,
    )

    with pytest.raises(EconomicGoalContractError, match="already exists"):
        store.initialize_owner(_goal(goal_id="replacement"))

    assert store.load() == original


def test_store_persistence_ignores_public_primitive_rebinding(
    tmp_path,
    monkeypatch,
) -> None:
    store = EconomicGoalStore(tmp_path)
    goal = _goal()
    store.initialize_owner(goal)

    def forbidden(*args, **kwargs):
        raise AssertionError("rebound public persistence primitive executed")

    monkeypatch.setattr(economic_goal_store_module.os, "fstat", forbidden)
    monkeypatch.setattr(economic_goal_store_module.os, "stat", forbidden)
    monkeypatch.setattr(economic_goal_store_module.os, "fdopen", forbidden)
    monkeypatch.setattr(economic_goal_store_module.os.path, "sameopenfile", forbidden)
    monkeypatch.setattr(economic_goal_store_module.os, "close", forbidden)
    monkeypatch.setattr(economic_goal_store_module.stat, "S_ISREG", forbidden)
    monkeypatch.setattr(economic_goal_store_module.json, "dumps", forbidden)
    monkeypatch.setattr(economic_goal_store_module, "Decimal", forbidden)

    assert store.load() == goal
    assert economic_goal_to_payload(goal)["contract"]["goal_id"] == goal.goal_id


def test_store_load_rejects_in_place_mutation_during_verified_read(
    tmp_path,
    monkeypatch,
) -> None:
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

    monkeypatch.setattr(
        economic_goal_store_module,
        "_CANONICAL_OPEN_READ_ONLY_DESCRIPTOR",
        racing_open,
    )

    with pytest.raises(EconomicGoalContractError, match="bytes changed"):
        store.load()


def test_store_load_rejects_path_replacement_after_second_descriptor_open(
    tmp_path,
    monkeypatch,
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

    monkeypatch.setattr(
        economic_goal_store_module,
        "_CANONICAL_OS_STAT",
        racing_stat,
    )

    with pytest.raises(EconomicGoalContractError, match="changed during verified read"):
        store.load()


def test_store_load_rejects_path_replacement_after_final_byte_read(
    tmp_path,
    monkeypatch,
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

    monkeypatch.setattr(
        economic_goal_store_module,
        "_CANONICAL_OPEN_READ_ONLY_DESCRIPTOR",
        racing_open,
    )

    with pytest.raises(EconomicGoalContractError, match="changed after verified read"):
        store.load()


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
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_bytes(b" " * 262_145)

    with pytest.raises(EconomicGoalContractError, match="byte-size limit"):
        store.load()


def test_store_load_normalizes_invalid_utf8_to_contract_error(tmp_path) -> None:
    store = EconomicGoalStore(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_bytes(b"\xff")

    with pytest.raises(EconomicGoalContractError, match="valid UTF-8"):
        store.load()


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


def test_strict_json_rejects_pathological_nesting_as_contract_error() -> None:
    deeply_nested = "[" * 2_000 + "0" + "]" * 2_000

    with pytest.raises(EconomicGoalContractError, match="invalid economic goal JSON"):
        economic_goal_from_json(deeply_nested)


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
