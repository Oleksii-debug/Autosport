"""Durable, versioned persistence for the owner EconomicGoalContract.

This module is intentionally a narrow authority boundary. It serializes the
existing typed :class:`EconomicGoalContract` without turning that contract into
an executable risk policy. Automatic writers may only publish an immediate
non-expanding successor of the already persisted owner contract.
"""

from __future__ import annotations

import json
import os
import stat
import weakref
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from enum import Enum, IntEnum
from pathlib import Path
from types import MethodType
from typing import Final

from .economic_goal import (
    AutomationLevel,
    EconomicGoalContract,
    EconomicGoalContractError,
    EconomicObjective,
    validate_automatic_transition,
    _canonical_contract_snapshot,
)
from .integrity import atomic_write_json
from .json_integrity import strict_json_loads
from .workspace_lock import WorkspaceEconomicLock, _open_read_only_descriptor


ECONOMIC_GOAL_SCHEMA: Final = "autosport.economic_goal_contract"
ECONOMIC_GOAL_SCHEMA_VERSION: Final = 1
_MAX_ECONOMIC_GOAL_DECIMAL_TEXT_CHARS: Final = 512
_MAX_ECONOMIC_GOAL_RESTRICTION_MEMBERS: Final = 1024
_MAX_ECONOMIC_GOAL_RESTRICTION_TEXT_CHARS: Final = 512
_MAX_ECONOMIC_GOAL_JSON_TEXT_CHARS: Final = 2 * 1024 * 1024
_MAX_ECONOMIC_GOAL_JSON_BYTES: Final = 4 * _MAX_ECONOMIC_GOAL_JSON_TEXT_CHARS

_CONTRACT_KEYS: Final = frozenset(
    {
        "goal_id",
        "revision",
        "bankroll_id",
        "currency",
        "objective",
        "max_stake_fraction",
        "max_stake_amount",
        "max_session_loss_fraction",
        "max_day_loss_fraction",
        "max_drawdown_fraction",
        "max_capital_at_risk_fraction",
        "max_event_concentration_fraction",
        "max_market_concentration_fraction",
        "max_provider_concentration_fraction",
        "max_sport_concentration_fraction",
        "max_turnover_fraction",
        "max_risk_of_ruin",
        "max_execution_slippage_fraction",
        "max_quote_age_seconds",
        "minimum_data_quality",
        "max_concurrent_positions",
        "max_parlay_legs",
        "automation_level",
        "emergency_stop",
        "blocked_sports",
        "blocked_providers",
        "blocked_markets",
    }
)
_ROOT_KEYS: Final = frozenset({"schema", "schema_version", "contract"})
_CONTRACT_KEYS_ORDERED: Final = (
    "goal_id",
    "revision",
    "bankroll_id",
    "currency",
    "objective",
    "max_stake_fraction",
    "max_stake_amount",
    "max_session_loss_fraction",
    "max_day_loss_fraction",
    "max_drawdown_fraction",
    "max_capital_at_risk_fraction",
    "max_event_concentration_fraction",
    "max_market_concentration_fraction",
    "max_provider_concentration_fraction",
    "max_sport_concentration_fraction",
    "max_turnover_fraction",
    "max_risk_of_ruin",
    "max_execution_slippage_fraction",
    "max_quote_age_seconds",
    "minimum_data_quality",
    "max_concurrent_positions",
    "max_parlay_legs",
    "automation_level",
    "emergency_stop",
    "blocked_sports",
    "blocked_providers",
    "blocked_markets",
)
_DECIMAL_FIELDS: Final = (
    "max_stake_fraction",
    "max_session_loss_fraction",
    "max_day_loss_fraction",
    "max_drawdown_fraction",
    "max_capital_at_risk_fraction",
    "max_event_concentration_fraction",
    "max_market_concentration_fraction",
    "max_provider_concentration_fraction",
    "max_sport_concentration_fraction",
    "max_turnover_fraction",
    "max_risk_of_ruin",
    "max_execution_slippage_fraction",
    "max_quote_age_seconds",
    "minimum_data_quality",
)
_RESTRICTION_FIELDS: Final = (
    "blocked_sports",
    "blocked_providers",
    "blocked_markets",
)


def _require_exact_keys(
    name: str,
    value: dict[str, object],
    expected: frozenset[str],
    _error_type=EconomicGoalContractError,
) -> None:
    keys = frozenset(value)
    if keys != expected:
        missing = sorted(expected - keys)
        extra = sorted(keys - expected)
        raise _error_type(
            f"{name} keys must match schema exactly; missing={missing!r} extra={extra!r}"
        )


def _decimal_text(
    name: str,
    value: object,
    _decimal_type=Decimal,
    _invalid_operation=InvalidOperation,
    _error_type=EconomicGoalContractError,
    _max_chars=_MAX_ECONOMIC_GOAL_DECIMAL_TEXT_CHARS,
) -> Decimal:
    if type(value) is not str:
        raise _error_type(f"{name} must be a Decimal string")
    if not value or value != value.strip():
        raise _error_type(f"{name} must be a canonical Decimal string")
    if len(value) > _max_chars:
        raise _error_type(
            f"{name} Decimal text exceeds the canonical size limit"
        )
    try:
        parsed = _decimal_type(value)
    except _invalid_operation as exc:
        raise _error_type(f"{name} is not a valid Decimal string") from exc
    if not parsed.is_finite():
        raise _error_type(f"{name} must be finite")
    if str(parsed) != value:
        raise _error_type(f"{name} must use canonical Decimal text")
    return parsed


def _restriction_set(
    name: str,
    value: object,
    _error_type=EconomicGoalContractError,
    _max_members=_MAX_ECONOMIC_GOAL_RESTRICTION_MEMBERS,
    _max_text_chars=_MAX_ECONOMIC_GOAL_RESTRICTION_TEXT_CHARS,
) -> frozenset[str]:
    if type(value) is not list:
        raise _error_type(f"{name} must be a sorted JSON array")
    if len(value) > _max_members:
        raise _error_type(
            f"{name} exceeds the canonical restriction-count limit"
        )
    for item in value:
        if type(item) is not str:
            raise _error_type(f"{name} must contain only strings")
        if (
            not item
            or item != item.strip()
            or len(item) > _max_text_chars
            or "\x00" in item
        ):
            raise _error_type(
                f"{name} contains non-canonical restriction text"
            )
    if value != sorted(value) or len(value) != len(set(value)):
        raise _error_type(
            f"{name} must be sorted and contain unique strings"
        )
    return frozenset(value)


_CANONICAL_GOAL_TYPE: Final = EconomicGoalContract
_CANONICAL_GOAL_VALIDATOR: Final = EconomicGoalContract.__post_init__
_CANONICAL_OBJECTIVE_TYPE: Final = EconomicObjective
_CANONICAL_AUTOMATION_TYPE: Final = AutomationLevel
_CANONICAL_ENUM_VALUE_GETTER: Final = Enum.__dict__["value"].fget
_CANONICAL_INT_ENUM_VALUE_GETTER: Final = Enum.__dict__["value"].fget
_CANONICAL_TRANSITION_VALIDATOR: Final = validate_automatic_transition
_CANONICAL_STRICT_JSON_LOADS: Final = strict_json_loads
_CANONICAL_ATOMIC_WRITE_JSON: Final = atomic_write_json
_CANONICAL_WORKSPACE_LOCK_TYPE: Final = WorkspaceEconomicLock
_CANONICAL_WORKSPACE_LOCK_ENTER: Final = WorkspaceEconomicLock.__enter__
_CANONICAL_WORKSPACE_LOCK_EXIT: Final = WorkspaceEconomicLock.__exit__
_CANONICAL_WORKSPACE_LOCK_ACQUIRE: Final = WorkspaceEconomicLock.acquire
_CANONICAL_WORKSPACE_LOCK_RELEASE: Final = WorkspaceEconomicLock.release
_CANONICAL_WORKSPACE_LOCK_NEW: Final = WorkspaceEconomicLock.__new__
_CANONICAL_WORKSPACE_LOCK_INIT: Final = WorkspaceEconomicLock.__init__
_CANONICAL_PATH_CONSTRUCTOR: Final = Path
_CANONICAL_PATH_TYPE: Final = type(Path("."))
_CANONICAL_PATH_RESOLVE: Final = Path.resolve
_CANONICAL_PATH_JOIN: Final = Path.__truediv__
_CANONICAL_STORE_FILE_NAME: Final = "economic_goal_contract.json"
_CANONICAL_OPEN_READ_ONLY_DESCRIPTOR: Final = _open_read_only_descriptor
_CANONICAL_OS_FSTAT: Final = os.fstat
_CANONICAL_OS_STAT: Final = os.stat
_CANONICAL_OS_FDOPEN: Final = os.fdopen
_CANONICAL_OS_SAMEOPENFILE: Final = os.path.sameopenfile
_CANONICAL_OS_CLOSE: Final = os.close
_CANONICAL_STAT_ISREG: Final = stat.S_ISREG
_CANONICAL_JSON_DUMPS: Final = json.dumps

_STORE_BINDINGS_BY_ID: Final = {}
_CANONICAL_OBJECT_GETATTRIBUTE: Final = object.__getattribute__
_CANONICAL_OBJECT_SETATTR: Final = object.__setattr__
_CANONICAL_METHOD_TYPE: Final = MethodType

_CANONICAL_WORKSPACE_CONTEXTMANAGER: Final = contextmanager


@_CANONICAL_WORKSPACE_CONTEXTMANAGER
def _workspace_lock_scope(
    workspace: str | Path,
    _lock_type=_CANONICAL_WORKSPACE_LOCK_TYPE,
    _canonical_lock_type=_CANONICAL_WORKSPACE_LOCK_TYPE,
    _lock_new=_CANONICAL_WORKSPACE_LOCK_NEW,
    _lock_init=_CANONICAL_WORKSPACE_LOCK_INIT,
    _lock_acquire=_CANONICAL_WORKSPACE_LOCK_ACQUIRE,
    _lock_release=_CANONICAL_WORKSPACE_LOCK_RELEASE,
    _object_setattr=_CANONICAL_OBJECT_SETATTR,
    _method_type=_CANONICAL_METHOD_TYPE,
    _enter=_CANONICAL_WORKSPACE_LOCK_ENTER,
    _exit=_CANONICAL_WORKSPACE_LOCK_EXIT,
):
    if _lock_type is _canonical_lock_type:
        lock = _lock_new(_lock_type)
        _lock_init(lock, workspace)
    else:
        lock = _lock_type(workspace)
    _object_setattr(
        lock,
        "acquire",
        _method_type(_lock_acquire, lock),
    )
    _object_setattr(
        lock,
        "release",
        _method_type(_lock_release, lock),
    )
    _enter(lock)
    try:
        yield lock
    except BaseException as exc:
        if _exit(lock, type(exc), exc, exc.__traceback__):
            return
        raise
    else:
        _exit(lock, None, None, None)



def _resolve_store_binding(
    store: object,
    _bindings=_STORE_BINDINGS_BY_ID,
    _error_type=EconomicGoalContractError,
    _object_getattribute=_CANONICAL_OBJECT_GETATTRIBUTE,
):
    entry = _bindings.get(id(store))
    if entry is None or entry[0]() is not store:
        raise _error_type("economic goal store binding is unavailable")
    workspace, path, path_exists, path_open = entry[1:]
    instance_state = _object_getattribute(store, "__dict__")
    if instance_state.get("workspace") is not workspace:
        raise _error_type("economic goal store workspace binding was rebound")
    if instance_state.get("path") is not path:
        raise _error_type("economic goal store path binding was rebound")
    return workspace, path, path_exists, path_open


def _snapshot_economic_goal_contract(
    contract: EconomicGoalContract,
    _goal_type=EconomicGoalContract,
    _goal_validator=EconomicGoalContract.__post_init__,
    _object_new=object.__new__,
    _object_setattr=object.__setattr__,
    _error_type=EconomicGoalContractError,
    _snapshot=_canonical_contract_snapshot,
    _ordered_field_names=_CONTRACT_KEYS_ORDERED,
) -> EconomicGoalContract:
    """Capture one validated, non-shared contract image for authority decisions."""

    if type(contract) is not _goal_type:
        raise _error_type(
            "economic goal persistence requires an EconomicGoalContract"
        )
    _goal_validator(contract)
    values = _snapshot(contract)
    _goal_validator(contract)
    final_values = _snapshot(contract)
    if values != final_values:
        raise _error_type("economic goal changed during persistence snapshot")
    snapshot = _object_new(_goal_type)
    for name, value in zip(_ordered_field_names, final_values):
        _object_setattr(snapshot, name, value)
    _goal_validator(snapshot)
    return snapshot


def economic_goal_to_payload(
    contract: EconomicGoalContract,
    _goal_type=EconomicGoalContract,
    _goal_validator=EconomicGoalContract.__post_init__,
    _schema=ECONOMIC_GOAL_SCHEMA,
    _schema_version=ECONOMIC_GOAL_SCHEMA_VERSION,
    _error_type=EconomicGoalContractError,
    _json_dumps=_CANONICAL_JSON_DUMPS,
    _max_json_chars=_MAX_ECONOMIC_GOAL_JSON_TEXT_CHARS,
    _max_json_bytes=_MAX_ECONOMIC_GOAL_JSON_BYTES,
    _objective_value_getter=_CANONICAL_ENUM_VALUE_GETTER,
    _automation_value_getter=_CANONICAL_INT_ENUM_VALUE_GETTER,
    _snapshot=_canonical_contract_snapshot,
    _ordered_field_names=_CONTRACT_KEYS_ORDERED,
) -> dict[str, object]:
    """Return the canonical schema-v1 JSON payload for ``contract``."""

    if type(contract) is not _goal_type:
        raise _error_type(
            "economic goal persistence requires an EconomicGoalContract"
        )
    _goal_validator(contract)
    first_snapshot = _snapshot(contract)
    _goal_validator(contract)
    second_snapshot = _snapshot(contract)
    if first_snapshot != second_snapshot:
        raise _error_type("economic goal changed during payload encoding")

    values = dict(zip(_ordered_field_names, second_snapshot))
    goal_id = values["goal_id"]
    revision = values["revision"]
    bankroll_id = values["bankroll_id"]
    currency = values["currency"]
    objective = values["objective"]
    max_stake_fraction = values["max_stake_fraction"]
    max_stake_amount = values["max_stake_amount"]
    max_session_loss_fraction = values["max_session_loss_fraction"]
    max_day_loss_fraction = values["max_day_loss_fraction"]
    max_drawdown_fraction = values["max_drawdown_fraction"]
    max_capital_at_risk_fraction = values["max_capital_at_risk_fraction"]
    max_event_concentration_fraction = values["max_event_concentration_fraction"]
    max_market_concentration_fraction = values["max_market_concentration_fraction"]
    max_provider_concentration_fraction = values["max_provider_concentration_fraction"]
    max_sport_concentration_fraction = values["max_sport_concentration_fraction"]
    max_turnover_fraction = values["max_turnover_fraction"]
    max_risk_of_ruin = values["max_risk_of_ruin"]
    max_execution_slippage_fraction = values["max_execution_slippage_fraction"]
    max_quote_age_seconds = values["max_quote_age_seconds"]
    minimum_data_quality = values["minimum_data_quality"]
    max_concurrent_positions = values["max_concurrent_positions"]
    max_parlay_legs = values["max_parlay_legs"]
    automation_level = values["automation_level"]
    emergency_stop = values["emergency_stop"]
    blocked_sports = values["blocked_sports"]
    blocked_providers = values["blocked_providers"]
    blocked_markets = values["blocked_markets"]

    body: dict[str, object] = {
        "goal_id": goal_id,
        "revision": revision,
        "bankroll_id": bankroll_id,
        "currency": currency,
        "objective": _objective_value_getter(objective),
        "max_stake_fraction": str(max_stake_fraction),
        "max_stake_amount": (
            None if max_stake_amount is None else str(max_stake_amount)
        ),
        "max_session_loss_fraction": str(max_session_loss_fraction),
        "max_day_loss_fraction": str(max_day_loss_fraction),
        "max_drawdown_fraction": str(max_drawdown_fraction),
        "max_capital_at_risk_fraction": str(max_capital_at_risk_fraction),
        "max_event_concentration_fraction": str(
            max_event_concentration_fraction
        ),
        "max_market_concentration_fraction": str(
            max_market_concentration_fraction
        ),
        "max_provider_concentration_fraction": str(
            max_provider_concentration_fraction
        ),
        "max_sport_concentration_fraction": str(
            max_sport_concentration_fraction
        ),
        "max_turnover_fraction": str(max_turnover_fraction),
        "max_risk_of_ruin": str(max_risk_of_ruin),
        "max_execution_slippage_fraction": str(
            max_execution_slippage_fraction
        ),
        "max_quote_age_seconds": str(max_quote_age_seconds),
        "minimum_data_quality": str(minimum_data_quality),
        "max_concurrent_positions": max_concurrent_positions,
        "max_parlay_legs": max_parlay_legs,
        "automation_level": _automation_value_getter(automation_level),
        "emergency_stop": emergency_stop,
        "blocked_sports": sorted(blocked_sports),
        "blocked_providers": sorted(blocked_providers),
        "blocked_markets": sorted(blocked_markets),
    }
    payload: dict[str, object] = {
        "schema": _schema,
        "schema_version": _schema_version,
        "contract": body,
    }
    try:
        persisted_text = _json_dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ) + "\n"
        persisted_bytes = persisted_text.encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise _error_type(
            "economic goal payload is not canonically serializable"
        ) from exc
    if len(persisted_text) > _max_json_chars:
        raise _error_type(
            "economic goal payload exceeds the canonical persistence text-size limit"
        )
    if len(persisted_bytes) > _max_json_bytes:
        raise _error_type(
            "economic goal payload exceeds the canonical persistence byte-size limit"
        )
    return payload


def _build_economic_goal_contract(
    values: dict[str, object],
    _goal_type=EconomicGoalContract,
    _goal_validator=EconomicGoalContract.__post_init__,
    _object_new=object.__new__,
    _object_setattr=object.__setattr__,
    _field_names=_CONTRACT_KEYS,
) -> EconomicGoalContract:
    contract = _object_new(_goal_type)
    for name in _field_names:
        _object_setattr(contract, name, values[name])
    _goal_validator(contract)
    return contract


def economic_goal_from_payload(
    payload: object,
    _goal_type=EconomicGoalContract,
    _goal_builder=_build_economic_goal_contract,
    _goal_error=EconomicGoalContractError,
    _objective_type=EconomicObjective,
    _automation_type=AutomationLevel,
    _exact_keys=_require_exact_keys,
    _decimal_decoder=_decimal_text,
    _restriction_decoder=_restriction_set,
    _root_keys=_ROOT_KEYS,
    _contract_keys=_CONTRACT_KEYS,
    _decimal_fields=_DECIMAL_FIELDS,
    _restriction_fields=_RESTRICTION_FIELDS,
    _schema=ECONOMIC_GOAL_SCHEMA,
    _schema_version=ECONOMIC_GOAL_SCHEMA_VERSION,
) -> EconomicGoalContract:
    """Decode schema-v1 persistence input and fail closed on any ambiguity."""

    if type(payload) is not dict or not all(
        type(key) is str for key in payload
    ):
        raise _goal_error("economic goal payload must be a JSON object")
    root: dict[str, object] = payload
    _exact_keys("economic goal payload", root, _root_keys)

    if type(root["schema"]) is not str or root["schema"] != _schema:
        raise _goal_error("unsupported economic goal schema")
    version = root["schema_version"]
    if type(version) is not int or version != _schema_version:
        raise _goal_error("unsupported economic goal schema_version")

    raw_contract = root["contract"]
    if type(raw_contract) is not dict or not all(
        type(key) is str for key in raw_contract
    ):
        raise _goal_error("contract must be a JSON object")
    body: dict[str, object] = raw_contract
    _exact_keys("contract", body, _contract_keys)

    decoded = dict(body)
    for field in _decimal_fields:
        decoded[field] = _decimal_decoder(field, body[field])
    if body["max_stake_amount"] is None:
        decoded["max_stake_amount"] = None
    else:
        decoded["max_stake_amount"] = _decimal_decoder(
            "max_stake_amount", body["max_stake_amount"]
        )

    if type(body["objective"]) is not str:
        raise _goal_error("objective must be a string")
    try:
        decoded["objective"] = _objective_type(body["objective"])
    except (TypeError, ValueError) as exc:
        raise _goal_error("unsupported economic objective") from exc

    automation = body["automation_level"]
    if type(automation) is not int:
        raise _goal_error("automation_level must be an integer")
    try:
        decoded["automation_level"] = _automation_type(automation)
    except ValueError as exc:
        raise _goal_error("unsupported automation_level") from exc

    for field in _restriction_fields:
        decoded[field] = _restriction_decoder(field, body[field])

    try:
        contract = _goal_builder(decoded)
        if type(contract) is not _goal_type:
            raise _goal_error("malformed economic goal contract")
        return contract
    except _goal_error:
        raise
    except (TypeError, ValueError, KeyError) as exc:
        raise _goal_error("malformed economic goal contract") from exc


def economic_goal_from_json(
    text: str,
    _loads=strict_json_loads,
    _payload_decoder=economic_goal_from_payload,
    _error_type=EconomicGoalContractError,
    _max_chars=_MAX_ECONOMIC_GOAL_JSON_TEXT_CHARS,
) -> EconomicGoalContract:
    """Decode one strict JSON document into a validated contract."""

    if type(text) is not str:
        raise _error_type("economic goal JSON must be text")
    if len(text) > _max_chars:
        raise _error_type("economic goal JSON text exceeds the canonical size limit")
    try:
        payload = _loads(text)
    except (TypeError, ValueError, RecursionError, OverflowError) as exc:
        raise _error_type("invalid economic goal JSON") from exc
    return _payload_decoder(payload)



def _require_canonical_goal_file(
    stat_result: os.stat_result,
    _is_regular=_CANONICAL_STAT_ISREG,
    _error_type=EconomicGoalContractError,
) -> None:
    if not _is_regular(stat_result.st_mode):
        raise _error_type(
            "persisted economic goal must be a regular non-symlink file"
        )
    if stat_result.st_nlink != 1:
        raise _error_type(
            "persisted economic goal must not have hard-link aliases"
        )


def _read_economic_goal_text(
    path: Path,
    _open_descriptor=_CANONICAL_OPEN_READ_ONLY_DESCRIPTOR,
    _fstat=_CANONICAL_OS_FSTAT,
    _stat=_CANONICAL_OS_STAT,
    _fdopen=_CANONICAL_OS_FDOPEN,
    _sameopenfile=_CANONICAL_OS_SAMEOPENFILE,
    _close=_CANONICAL_OS_CLOSE,
    _validate_file=_require_canonical_goal_file,
    _max_bytes=_MAX_ECONOMIC_GOAL_JSON_BYTES,
    _max_chars=_MAX_ECONOMIC_GOAL_JSON_TEXT_CHARS,
    _error_type=EconomicGoalContractError,
) -> str:
    descriptor = None
    final_descriptor = None
    verification_descriptor = None
    post_read_descriptor = None
    raw = b""
    primary_error: BaseException | None = None
    try:
        descriptor = _open_descriptor(path)
        opened_before = _fstat(descriptor)
        path_before = _stat(path, follow_symlinks=False)
        _validate_file(opened_before)
        _validate_file(path_before)

        with _fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(_max_bytes + 1)

        final_descriptor = _open_descriptor(path)
        opened_after = _fstat(descriptor)
        final_stat = _fstat(final_descriptor)
        path_after = _stat(path, follow_symlinks=False)
        verification_descriptor = _open_descriptor(path)
        verification_stat = _fstat(verification_descriptor)

        for stat_result in (
            opened_after,
            final_stat,
            path_after,
            verification_stat,
        ):
            _validate_file(stat_result)

        if (
            not _sameopenfile(descriptor, final_descriptor)
            or not _sameopenfile(descriptor, verification_descriptor)
        ):
            raise _error_type(
                "persisted economic goal changed during verified read"
            )

        with _fdopen(final_descriptor, "rb", closefd=False) as final_handle:
            final_raw = final_handle.read(_max_bytes + 1)
        if raw != final_raw:
            raise _error_type(
                "persisted economic goal bytes changed during verified read"
            )

        post_read_descriptor = _open_descriptor(path)
        post_read_stat = _fstat(post_read_descriptor)
        _validate_file(post_read_stat)
        if not _sameopenfile(descriptor, post_read_descriptor):
            raise _error_type(
                "persisted economic goal changed after verified read"
            )
    except _error_type as exc:
        primary_error = exc
        raise
    except OSError as exc:
        primary_error = _error_type(
            f"cannot safely read persisted economic goal: {exc}"
        )
        raise primary_error from exc
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        cleanup_error: OSError | None = None
        for candidate in (
            post_read_descriptor,
            verification_descriptor,
            final_descriptor,
            descriptor,
        ):
            if candidate is None:
                continue
            try:
                _close(candidate)
            except OSError as exc:
                if cleanup_error is None:
                    cleanup_error = exc
        if cleanup_error is not None and primary_error is None:
            raise _error_type(
                "cannot close persisted economic goal read descriptor"
            ) from cleanup_error

    if len(raw) > _max_bytes:
        raise _error_type(
            "economic goal JSON exceeds the canonical byte-size limit"
        )
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _error_type(
            "persisted economic goal must be valid UTF-8"
        ) from exc
    if len(text) > _max_chars:
        raise _error_type(
            "economic goal JSON text exceeds the canonical size limit"
        )
    return text


_CANONICAL_GOAL_TEXT_READER: Final = _read_economic_goal_text


_ECONOMIC_GOAL_STORE_AUTHORITY_NAMES: Final = frozenset(
    {
        "__init__",
        "load",
        "initialize_owner",
        "persist_automatic_successor",
        "_authority_operations_sealed",
    }
)


class _EconomicGoalStoreMeta(type):
    """Seal public store authority entrypoints against class-level rebinding."""

    _AUTHORITY_NAMES: Final = _ECONOMIC_GOAL_STORE_AUTHORITY_NAMES

    def __setattr__(
        cls,
        name: str,
        value: object,
        _authority_names=_ECONOMIC_GOAL_STORE_AUTHORITY_NAMES,
    ) -> None:
        if (
            cls.__dict__.get("_authority_operations_sealed", False)
            and name in _authority_names
        ):
            raise TypeError(
                "economic goal store authority operation binding is immutable"
            )
        super().__setattr__(name, value)

    def __delattr__(
        cls,
        name: str,
        _authority_names=_ECONOMIC_GOAL_STORE_AUTHORITY_NAMES,
    ) -> None:
        if (
            cls.__dict__.get("_authority_operations_sealed", False)
            and name in _authority_names
        ):
            raise TypeError(
                "economic goal store authority operation binding is immutable"
            )
        super().__delattr__(name)


def _build_store_class_guard(name: str):
    """Block direct base-metaclass replacement of sealed store authority names."""

    class _StoreClassGuard:
        __slots__ = ()

        def __get__(self, instance, owner=None):
            if instance is None:
                return self
            binding = instance.__dict__[name]
            descriptor_get = getattr(binding, "__get__", None)
            if descriptor_get is None:
                return binding
            return descriptor_get(None, instance)

        def __set__(self, _instance, _value) -> None:
            raise TypeError(
                "economic goal store authority operation binding is immutable"
            )

        def __delete__(self, _instance) -> None:
            raise TypeError(
                "economic goal store authority operation binding is immutable"
            )

    return _StoreClassGuard()


def _make_store_operation_descriptor(operation):
    """Make a closure-owned public store operation non-shadowable on an instance."""

    class _ImmutableStoreOperation:
        __slots__ = ()

        def __get__(self, instance, owner=None):
            if instance is None:
                return operation
            if type(instance) is not owner:
                raise TypeError(
                    "economic goal store authority requires the exact store type"
                )
            return MethodType(operation, instance)

        def __set__(self, _instance, _value) -> None:
            raise TypeError(
                "economic goal store authority operation binding is immutable"
            )

        def __delete__(self, _instance) -> None:
            raise TypeError(
                "economic goal store authority operation binding is immutable"
            )

    return _ImmutableStoreOperation()


class EconomicGoalStore(metaclass=_EconomicGoalStoreMeta):
    """Workspace-local durable owner-contract store.

    ``initialize_owner`` is creation-only. Automatic actors have only
    ``persist_automatic_successor`` which, under the canonical workspace economic
    writer lock, reloads the durable predecessor and applies the monotonic authority
    validator before atomic publication. The lock makes validation and publication
    one cooperating-writer critical section so stale concurrent revisions cannot
    overwrite a newly tightened authority state.
    """

    FILE_NAME: Final = "economic_goal_contract.json"

    def __init__(
        self,
        workspace: str | Path,
        _path_constructor=_CANONICAL_PATH_CONSTRUCTOR,
        _path_type=_CANONICAL_PATH_TYPE,
        _path_resolve=_CANONICAL_PATH_RESOLVE,
        _path_join=_CANONICAL_PATH_JOIN,
        _bindings=_STORE_BINDINGS_BY_ID,
        _weakref_ref=weakref.ref,
        _file_name=_CANONICAL_STORE_FILE_NAME,
        _error_type=EconomicGoalContractError,
    ) -> None:
        if type(self) is not __class__:
            raise TypeError("EconomicGoalStore authority requires the exact store type")
        if type(workspace) not in {str, _path_type}:
            raise TypeError(
                "EconomicGoalStore workspace must be exact str or exact Path"
            )
        try:
            workspace_path = _path_resolve(
                _path_constructor(workspace),
                strict=False,
            )
        except (OSError, RuntimeError) as exc:
            raise _error_type(
                "EconomicGoalStore workspace cannot be canonically resolved"
            ) from exc
        path = _path_join(workspace_path, _file_name)
        self.workspace = workspace_path
        self.path = path
        store_id = id(self)

        def release_binding(store_ref) -> None:
            entry = _bindings.get(store_id)
            if entry is not None and entry[0] is store_ref:
                _bindings.pop(store_id, None)

        store_ref = _weakref_ref(self, release_binding)
        path_lstat = path.lstat
        path_open = path.open

        def path_exists(
            _error_type=EconomicGoalContractError,
        ) -> bool:
            try:
                path_lstat()
            except FileNotFoundError:
                return False
            except OSError as exc:
                raise _error_type(
                    f"cannot inspect persisted economic goal path: {exc}"
                ) from exc
            return True

        _bindings[store_id] = (
            store_ref,
            workspace_path,
            path,
            path_exists,
            path_open,
        )

    def load(
        self,
        _json_decoder=economic_goal_from_json,
        _binding_resolver=_resolve_store_binding,
        _text_reader=_CANONICAL_GOAL_TEXT_READER,
    ) -> EconomicGoalContract:
        if type(self) is not __class__:
            raise TypeError("EconomicGoalStore authority requires the exact store type")
        _, path, _, _ = _binding_resolver(self)
        return _json_decoder(_text_reader(path))

    def initialize_owner(
        self,
        contract: EconomicGoalContract,
        _lock_type=_CANONICAL_WORKSPACE_LOCK_TYPE,
        _payload_encoder=economic_goal_to_payload,
        _writer=_CANONICAL_ATOMIC_WRITE_JSON,
        _binding_resolver=_resolve_store_binding,
        _error_type=EconomicGoalContractError,
        _lock_scope=_workspace_lock_scope,
    ) -> None:
        """Create the first owner contract while holding the economic writer lock."""

        if type(self) is not __class__:
            raise TypeError("EconomicGoalStore authority requires the exact store type")
        workspace, path, path_exists, _ = _binding_resolver(self)
        with _lock_scope(workspace, _lock_type):
            if path_exists():
                raise _error_type(
                    "persisted economic goal already exists; owner replacement requires "
                    "a separate authority boundary"
                )
            _writer(path, _payload_encoder(contract))

    def persist_automatic_successor(
        self,
        candidate: EconomicGoalContract,
        _lock_type=_CANONICAL_WORKSPACE_LOCK_TYPE,
        _transition_validator=_CANONICAL_TRANSITION_VALIDATOR,
        _payload_encoder=economic_goal_to_payload,
        _payload_decoder=economic_goal_from_payload,
        _writer=_CANONICAL_ATOMIC_WRITE_JSON,
        _json_decoder=economic_goal_from_json,
        _binding_resolver=_resolve_store_binding,
        _text_reader=_CANONICAL_GOAL_TEXT_READER,
        _lock_scope=_workspace_lock_scope,
    ) -> None:
        """Publish one machine revision only when durable authority cannot expand.

        The candidate is serialized before the monotonic transition proof and the
        proof is applied to a canonical decode of that exact payload. Publication
        then writes the same payload object. This binds validation to the durable
        image and prevents an object.__setattr__ race from widening the candidate
        between validation and serialization.
        """

        if type(self) is not __class__:
            raise TypeError("EconomicGoalStore authority requires the exact store type")
        workspace, path, _, _ = _binding_resolver(self)
        with _lock_scope(workspace, _lock_type):
            previous = _json_decoder(_text_reader(path))
            candidate_payload = _payload_encoder(candidate)
            candidate_snapshot = _payload_decoder(candidate_payload)
            _transition_validator(previous, candidate_snapshot)
            _writer(path, candidate_payload)


# Freeze public codec/store call shapes.  Authority-bearing dependencies remain
# captured by the implementations above, but external callers cannot inject
# replacement validators, writers, parsers, locks, paths, or transition proofs.
_BOUND_ECONOMIC_GOAL_TO_PAYLOAD = economic_goal_to_payload
_BOUND_ECONOMIC_GOAL_FROM_PAYLOAD = economic_goal_from_payload
_BOUND_ECONOMIC_GOAL_FROM_JSON = economic_goal_from_json
_BOUND_STORE_INIT = EconomicGoalStore.__init__
_BOUND_STORE_LOAD = EconomicGoalStore.load
_BOUND_STORE_INITIALIZE_OWNER = EconomicGoalStore.initialize_owner
_BOUND_STORE_PERSIST_AUTOMATIC_SUCCESSOR = EconomicGoalStore.persist_automatic_successor


def _make_store_callable_authority(operation, label: str):
    operation_code = operation.__code__
    operation_defaults = operation.__defaults__
    operation_kwdefaults = operation.__kwdefaults__
    operation_kwdefault_items = tuple((operation_kwdefaults or {}).items())
    nested_callables = tuple(
        value
        for value in (operation_defaults or ())
        if callable(value)
    )
    nested_authority = tuple(
        (
            callable_object,
            getattr(callable_object, "__code__", None),
            getattr(callable_object, "__defaults__", None),
            getattr(callable_object, "__kwdefaults__", None),
            tuple((getattr(callable_object, "__kwdefaults__", None) or {}).items()),
        )
        for callable_object in nested_callables
    )

    def require_authority() -> None:
        if operation.__code__ is not operation_code:
            raise EconomicGoalContractError(f"{label} authority changed")
        if operation.__defaults__ is not operation_defaults:
            raise EconomicGoalContractError(f"{label} defaults authority changed")
        current_kwdefaults = operation.__kwdefaults__
        if (
            current_kwdefaults is not operation_kwdefaults
            or tuple((current_kwdefaults or {}).items()) != operation_kwdefault_items
        ):
            raise EconomicGoalContractError(
                f"{label} keyword defaults authority changed"
            )
        for (
            callable_object,
            expected_code,
            expected_defaults,
            expected_kwdefaults,
            expected_kwdefault_items,
        ) in nested_authority:
            if getattr(callable_object, "__code__", None) is not expected_code:
                raise EconomicGoalContractError(f"{label} nested authority changed")
            if getattr(callable_object, "__defaults__", None) is not expected_defaults:
                raise EconomicGoalContractError(f"{label} nested defaults authority changed")
            current_nested_kwdefaults = getattr(callable_object, "__kwdefaults__", None)
            if (
                current_nested_kwdefaults is not expected_kwdefaults
                or tuple((current_nested_kwdefaults or {}).items())
                != expected_kwdefault_items
            ):
                raise EconomicGoalContractError(
                    f"{label} nested keyword defaults authority changed"
                )

    def bound(*args, **kwargs):
        require_authority()
        result = operation(*args, **kwargs)
        require_authority()
        return result

    return bound


def _bind_goal_encoder(operation):
    bound_operation = _make_store_callable_authority(
        operation, "economic-goal payload encoder"
    )

    def bound(contract: EconomicGoalContract) -> dict[str, object]:
        return bound_operation(contract)

    return bound


def _bind_goal_payload_decoder(operation):
    bound_operation = _make_store_callable_authority(
        operation, "economic-goal payload decoder"
    )

    def bound(payload: object) -> EconomicGoalContract:
        return bound_operation(payload)

    return bound


def _bind_goal_json_decoder(operation):
    bound_operation = _make_store_callable_authority(
        operation, "economic-goal JSON decoder"
    )

    def bound(text: str) -> EconomicGoalContract:
        return bound_operation(text)

    return bound


def _bind_store_init(operation):
    bound_operation = _make_store_callable_authority(
        operation, "economic-goal store constructor"
    )

    def bound(self: EconomicGoalStore, workspace: str | Path) -> None:
        bound_operation(self, workspace)

    return bound


def _bind_store_load(operation):
    bound_operation = _make_store_callable_authority(
        operation, "economic-goal store load"
    )

    def bound(self: EconomicGoalStore) -> EconomicGoalContract:
        return bound_operation(self)

    return bound


def _bind_store_contract_write(operation):
    bound_operation = _make_store_callable_authority(
        operation, "economic-goal store write"
    )

    def bound(self: EconomicGoalStore, contract: EconomicGoalContract) -> None:
        bound_operation(self, contract)

    return bound


economic_goal_to_payload = _bind_goal_encoder(_BOUND_ECONOMIC_GOAL_TO_PAYLOAD)
economic_goal_from_payload = _bind_goal_payload_decoder(_BOUND_ECONOMIC_GOAL_FROM_PAYLOAD)
economic_goal_from_json = _bind_goal_json_decoder(_BOUND_ECONOMIC_GOAL_FROM_JSON)
EconomicGoalStore.__init__ = _bind_store_init(_BOUND_STORE_INIT)
EconomicGoalStore.load = _make_store_operation_descriptor(
    _bind_store_load(_BOUND_STORE_LOAD)
)
EconomicGoalStore.initialize_owner = _make_store_operation_descriptor(
    _bind_store_contract_write(_BOUND_STORE_INITIALIZE_OWNER)
)
EconomicGoalStore.persist_automatic_successor = _make_store_operation_descriptor(
    _bind_store_contract_write(_BOUND_STORE_PERSIST_AUTOMATIC_SUCCESSOR)
)
EconomicGoalStore._authority_operations_sealed = True

# A custom metaclass __setattr__/__delattr__ is bypassable by explicitly calling
# type.__setattr__/type.__delattr__ on the class. Install data descriptors on the
# metaclass after the canonical class bindings are final so even those base-type
# operations must cross the immutable authority guard.
for _sealed_store_name in _ECONOMIC_GOAL_STORE_AUTHORITY_NAMES:
    setattr(
        _EconomicGoalStoreMeta,
        _sealed_store_name,
        _build_store_class_guard(_sealed_store_name),
    )
del _sealed_store_name
