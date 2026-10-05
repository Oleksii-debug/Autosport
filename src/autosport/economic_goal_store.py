"""Durable, versioned persistence for the owner EconomicGoalContract.

This module is intentionally a narrow authority boundary. It serializes the
existing typed :class:`EconomicGoalContract` without turning that contract into
an executable risk policy. Automatic writers may only publish an immediate
non-expanding successor of the already persisted owner contract.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import os
import stat
import threading
from pathlib import Path
from typing import Final
import weakref

from .economic_goal import (
    EconomicGoalContract,
    EconomicGoalContractError,
    validate_automatic_transition,
)
from .integrity import atomic_write_json
from .json_integrity import strict_json_loads
from .workspace_lock import WorkspaceEconomicLock, _open_read_only_descriptor


ECONOMIC_GOAL_SCHEMA: Final = "autosport.economic_goal_contract"
ECONOMIC_GOAL_SCHEMA_VERSION: Final = 1
_CANONICAL_ECONOMIC_GOAL_SCHEMA: Final = "autosport.economic_goal_contract"
_CANONICAL_ECONOMIC_GOAL_SCHEMA_VERSION: Final = 1
_ECONOMIC_GOAL_FILE_NAME: Final = "economic_goal_contract.json"
_MAX_ECONOMIC_GOAL_JSON_CHARS: Final = 65_536
_MAX_ECONOMIC_GOAL_JSON_BYTES: Final = 262_144
_MAX_ECONOMIC_GOAL_DECIMAL_TEXT_CHARS: Final = 512
_MAX_ECONOMIC_GOAL_RESTRICTION_MEMBERS: Final = 1024
_MAX_ECONOMIC_GOAL_RESTRICTION_TEXT_CHARS: Final = 512
_CANONICAL_GOAL_TYPE: Final = EconomicGoalContract
_CANONICAL_GOAL_VALIDATOR: Final = EconomicGoalContract.__post_init__
_CANONICAL_OBJECTIVE_TYPE: Final = type(
    EconomicGoalContract.__dataclass_fields__["objective"].default
)
_CANONICAL_AUTOMATION_TYPE: Final = type(
    EconomicGoalContract.__dataclass_fields__["automation_level"].default
)
_CANONICAL_PATH_CONSTRUCTOR: Final = Path
_CANONICAL_PATH_TYPE: Final = type(Path("."))
_CANONICAL_PATH_RESOLVE: Final = Path.resolve
_CANONICAL_PATH_EXISTS: Final = Path.exists
_CANONICAL_PATH_JOIN: Final = Path.__truediv__
_CANONICAL_TRANSITION_VALIDATOR: Final = validate_automatic_transition
_CANONICAL_ATOMIC_WRITE_JSON: Final = atomic_write_json
_CANONICAL_WORKSPACE_LOCK_TYPE: Final = WorkspaceEconomicLock
_CANONICAL_OPEN_READ_ONLY_DESCRIPTOR: Final = _open_read_only_descriptor
_CANONICAL_STRICT_JSON_LOADS: Final = strict_json_loads
_CANONICAL_DECIMAL_TYPE: Final = Decimal
_CANONICAL_INVALID_OPERATION: Final = InvalidOperation
_CANONICAL_JSON_DUMPS: Final = json.dumps
_CANONICAL_OS_FSTAT: Final = os.fstat
_CANONICAL_OS_STAT: Final = os.stat
_CANONICAL_OS_FDOPEN: Final = os.fdopen
_CANONICAL_OS_SAMEOPENFILE: Final = os.path.sameopenfile
_CANONICAL_OS_CLOSE: Final = os.close
_CANONICAL_STAT_ISREG: Final = stat.S_ISREG

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
_CANONICAL_CONTRACT_KEYS: Final = _CONTRACT_KEYS
_CANONICAL_ROOT_KEYS: Final = _ROOT_KEYS
_CANONICAL_DECIMAL_FIELDS: Final = _DECIMAL_FIELDS
_CANONICAL_RESTRICTION_FIELDS: Final = _RESTRICTION_FIELDS


def _require_exact_keys(
    name: str, value: dict[str, object], expected: frozenset[str]
) -> None:
    keys = frozenset(value)
    if keys != expected:
        missing = sorted(expected - keys)
        extra = sorted(keys - expected)
        raise EconomicGoalContractError(
            f"{name} keys must match schema exactly; missing={missing!r} extra={extra!r}"
        )


def _decimal_text(name: str, value: object) -> Decimal:
    if type(value) is not str:
        raise EconomicGoalContractError(f"{name} must be a Decimal string")
    if not value or value != value.strip():
        raise EconomicGoalContractError(f"{name} must be a canonical Decimal string")
    if len(value) > _MAX_ECONOMIC_GOAL_DECIMAL_TEXT_CHARS:
        raise EconomicGoalContractError(
            f"{name} Decimal text exceeds the canonical size limit"
        )
    try:
        parsed = _CANONICAL_DECIMAL_TYPE(value)
    except _CANONICAL_INVALID_OPERATION as exc:
        raise EconomicGoalContractError(f"{name} is not a valid Decimal string") from exc
    if not parsed.is_finite():
        raise EconomicGoalContractError(f"{name} must be finite")
    if str(parsed) != value:
        raise EconomicGoalContractError(f"{name} must use canonical Decimal text")
    return parsed


def _restriction_set(name: str, value: object) -> frozenset[str]:
    if type(value) is not list:
        raise EconomicGoalContractError(f"{name} must be a sorted JSON array")
    if len(value) > _MAX_ECONOMIC_GOAL_RESTRICTION_MEMBERS:
        raise EconomicGoalContractError(
            f"{name} exceeds the canonical restriction-count limit"
        )
    for item in value:
        if type(item) is not str:
            raise EconomicGoalContractError(f"{name} must contain only strings")
        if (
            not item
            or item != item.strip()
            or len(item) > _MAX_ECONOMIC_GOAL_RESTRICTION_TEXT_CHARS
            or "\x00" in item
        ):
            raise EconomicGoalContractError(
                f"{name} contains non-canonical restriction text"
            )
    if value != sorted(value) or len(value) != len(set(value)):
        raise EconomicGoalContractError(
            f"{name} must be sorted and contain unique strings"
        )
    return frozenset(value)


_CANONICAL_REQUIRE_EXACT_KEYS: Final = _require_exact_keys
_CANONICAL_DECIMAL_TEXT: Final = _decimal_text
_CANONICAL_RESTRICTION_SET: Final = _restriction_set


def economic_goal_to_payload(contract: EconomicGoalContract) -> dict[str, object]:
    """Return the canonical schema-v1 JSON payload for ``contract``."""

    if type(contract) is not _CANONICAL_GOAL_TYPE:
        raise EconomicGoalContractError(
            "economic goal persistence requires a canonical EconomicGoalContract"
        )
    _CANONICAL_GOAL_VALIDATOR(contract)

    body: dict[str, object] = {
        "goal_id": contract.goal_id,
        "revision": contract.revision,
        "bankroll_id": contract.bankroll_id,
        "currency": contract.currency,
        "objective": contract.objective.value,
        "max_stake_fraction": str(contract.max_stake_fraction),
        "max_stake_amount": (
            None if contract.max_stake_amount is None else str(contract.max_stake_amount)
        ),
        "max_session_loss_fraction": str(contract.max_session_loss_fraction),
        "max_day_loss_fraction": str(contract.max_day_loss_fraction),
        "max_drawdown_fraction": str(contract.max_drawdown_fraction),
        "max_capital_at_risk_fraction": str(contract.max_capital_at_risk_fraction),
        "max_event_concentration_fraction": str(
            contract.max_event_concentration_fraction
        ),
        "max_market_concentration_fraction": str(
            contract.max_market_concentration_fraction
        ),
        "max_provider_concentration_fraction": str(
            contract.max_provider_concentration_fraction
        ),
        "max_sport_concentration_fraction": str(
            contract.max_sport_concentration_fraction
        ),
        "max_turnover_fraction": str(contract.max_turnover_fraction),
        "max_risk_of_ruin": str(contract.max_risk_of_ruin),
        "max_execution_slippage_fraction": str(
            contract.max_execution_slippage_fraction
        ),
        "max_quote_age_seconds": str(contract.max_quote_age_seconds),
        "minimum_data_quality": str(contract.minimum_data_quality),
        "max_concurrent_positions": contract.max_concurrent_positions,
        "max_parlay_legs": contract.max_parlay_legs,
        "automation_level": int(contract.automation_level),
        "emergency_stop": contract.emergency_stop,
        "blocked_sports": sorted(contract.blocked_sports),
        "blocked_providers": sorted(contract.blocked_providers),
        "blocked_markets": sorted(contract.blocked_markets),
    }
    payload: dict[str, object] = {
        "schema": _CANONICAL_ECONOMIC_GOAL_SCHEMA,
        "schema_version": _CANONICAL_ECONOMIC_GOAL_SCHEMA_VERSION,
        "contract": body,
    }
    try:
        persisted_text = _CANONICAL_JSON_DUMPS(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ) + "\n"
        persisted_bytes = persisted_text.encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise EconomicGoalContractError(
            "economic goal payload is not canonically serializable"
        ) from exc
    if (
        len(persisted_text) > _MAX_ECONOMIC_GOAL_JSON_CHARS
        or len(persisted_bytes) > _MAX_ECONOMIC_GOAL_JSON_BYTES
    ):
        raise EconomicGoalContractError(
            "economic goal payload exceeds the canonical persistence size limit"
        )
    return payload


_CANONICAL_GOAL_SERIALIZER: Final = economic_goal_to_payload


def economic_goal_from_payload(payload: object) -> EconomicGoalContract:
    """Decode schema-v1 persistence input and fail closed on any ambiguity."""

    if type(payload) is not dict or not all(
        type(key) is str for key in payload
    ):
        raise EconomicGoalContractError("economic goal payload must be a JSON object")
    root: dict[str, object] = payload
    _CANONICAL_REQUIRE_EXACT_KEYS("economic goal payload", root, _CANONICAL_ROOT_KEYS)

    schema = root["schema"]
    if type(schema) is not str or schema != _CANONICAL_ECONOMIC_GOAL_SCHEMA:
        raise EconomicGoalContractError("unsupported economic goal schema")
    version = root["schema_version"]
    if (
        type(version) is not int
        or version != _CANONICAL_ECONOMIC_GOAL_SCHEMA_VERSION
    ):
        raise EconomicGoalContractError("unsupported economic goal schema_version")

    raw_contract = root["contract"]
    if type(raw_contract) is not dict or not all(
        type(key) is str for key in raw_contract
    ):
        raise EconomicGoalContractError("contract must be a JSON object")
    body: dict[str, object] = raw_contract
    _CANONICAL_REQUIRE_EXACT_KEYS("contract", body, _CANONICAL_CONTRACT_KEYS)

    decoded = dict(body)
    for field in _CANONICAL_DECIMAL_FIELDS:
        decoded[field] = _CANONICAL_DECIMAL_TEXT(field, body[field])
    if body["max_stake_amount"] is None:
        decoded["max_stake_amount"] = None
    else:
        decoded["max_stake_amount"] = _CANONICAL_DECIMAL_TEXT(
            "max_stake_amount", body["max_stake_amount"]
        )

    objective = body["objective"]
    if type(objective) is not str:
        raise EconomicGoalContractError("economic objective must be a string")
    try:
        decoded["objective"] = _CANONICAL_OBJECTIVE_TYPE(objective)
    except ValueError as exc:
        raise EconomicGoalContractError("unsupported economic objective") from exc

    automation = body["automation_level"]
    if type(automation) is not int:
        raise EconomicGoalContractError("automation_level must be an integer")
    try:
        decoded["automation_level"] = _CANONICAL_AUTOMATION_TYPE(automation)
    except ValueError as exc:
        raise EconomicGoalContractError("unsupported automation_level") from exc

    for field in _CANONICAL_RESTRICTION_FIELDS:
        decoded[field] = _CANONICAL_RESTRICTION_SET(field, body[field])

    try:
        return _CANONICAL_GOAL_TYPE(**decoded)  # type: ignore[arg-type]
    except EconomicGoalContractError:
        raise
    except (TypeError, ValueError) as exc:
        raise EconomicGoalContractError("malformed economic goal contract") from exc


_CANONICAL_PAYLOAD_DECODER: Final = economic_goal_from_payload


def _require_canonical_goal_file(stat_result: os.stat_result) -> None:
    if not _CANONICAL_STAT_ISREG(stat_result.st_mode):
        raise EconomicGoalContractError(
            "persisted economic goal must be a regular non-symlink file"
        )
    if stat_result.st_nlink != 1:
        raise EconomicGoalContractError(
            "persisted economic goal must not have hard-link aliases"
        )


def _read_economic_goal_text(path: Path) -> str:
    descriptor: int | None = None
    final_descriptor: int | None = None
    path_verification_descriptor: int | None = None
    post_read_descriptor: int | None = None
    try:
        descriptor = _CANONICAL_OPEN_READ_ONLY_DESCRIPTOR(path)
        opened_before = _CANONICAL_OS_FSTAT(descriptor)
        path_before = _CANONICAL_OS_STAT(path, follow_symlinks=False)
        _require_canonical_goal_file(opened_before)
        _require_canonical_goal_file(path_before)

        with _CANONICAL_OS_FDOPEN(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(_MAX_ECONOMIC_GOAL_JSON_BYTES + 1)

        final_descriptor = _CANONICAL_OPEN_READ_ONLY_DESCRIPTOR(path)
        opened_after = _CANONICAL_OS_FSTAT(descriptor)
        final_stat = _CANONICAL_OS_FSTAT(final_descriptor)
        path_after = _CANONICAL_OS_STAT(path, follow_symlinks=False)
        path_verification_descriptor = _CANONICAL_OPEN_READ_ONLY_DESCRIPTOR(path)
        path_verification_stat = _CANONICAL_OS_FSTAT(path_verification_descriptor)
        _require_canonical_goal_file(opened_after)
        _require_canonical_goal_file(final_stat)
        _require_canonical_goal_file(path_after)
        _require_canonical_goal_file(path_verification_stat)
        if (
            not _CANONICAL_OS_SAMEOPENFILE(descriptor, final_descriptor)
            or not _CANONICAL_OS_SAMEOPENFILE(descriptor, path_verification_descriptor)
        ):
            raise EconomicGoalContractError(
                "persisted economic goal changed during verified read"
            )
        with _CANONICAL_OS_FDOPEN(final_descriptor, "rb", closefd=False) as final_handle:
            final_raw = final_handle.read(_MAX_ECONOMIC_GOAL_JSON_BYTES + 1)
        if raw != final_raw:
            raise EconomicGoalContractError(
                "persisted economic goal bytes changed during verified read"
            )

        # The pathname must still name the verified inode after the second byte
        # image has been read. A checkpoint opened before that read cannot detect
        # replacement during the read window.
        post_read_descriptor = _CANONICAL_OPEN_READ_ONLY_DESCRIPTOR(path)
        post_read_stat = _CANONICAL_OS_FSTAT(post_read_descriptor)
        _require_canonical_goal_file(post_read_stat)
        if not _CANONICAL_OS_SAMEOPENFILE(descriptor, post_read_descriptor):
            raise EconomicGoalContractError(
                "persisted economic goal changed after verified read"
            )
    except EconomicGoalContractError:
        raise
    except OSError as exc:
        raise EconomicGoalContractError(
            f"cannot safely read persisted economic goal: {exc}"
        ) from exc
    finally:
        for candidate in (
            post_read_descriptor,
            path_verification_descriptor,
            final_descriptor,
            descriptor,
        ):
            if candidate is None:
                continue
            try:
                _CANONICAL_OS_CLOSE(candidate)
            except OSError:
                pass

    if len(raw) > _MAX_ECONOMIC_GOAL_JSON_BYTES:
        raise EconomicGoalContractError(
            "economic goal JSON exceeds the canonical byte-size limit"
        )
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise EconomicGoalContractError(
            "persisted economic goal must be valid UTF-8"
        ) from exc


_CANONICAL_GOAL_TEXT_READER: Final = _read_economic_goal_text


def economic_goal_from_json(text: str) -> EconomicGoalContract:
    """Decode one strict JSON document into a validated contract."""

    if type(text) is not str:
        raise EconomicGoalContractError("economic goal JSON must be text")
    if len(text) > _MAX_ECONOMIC_GOAL_JSON_CHARS:
        raise EconomicGoalContractError(
            "economic goal JSON exceeds the canonical size limit"
        )
    try:
        payload = _CANONICAL_STRICT_JSON_LOADS(text)
    except (TypeError, ValueError, RecursionError, OverflowError) as exc:
        raise EconomicGoalContractError("invalid economic goal JSON") from exc
    return _CANONICAL_PAYLOAD_DECODER(payload)


_CANONICAL_GOAL_JSON_DECODER: Final = economic_goal_from_json


def _make_store_binding_registry():
    # Workspace/path identity is authority-bearing. Keep the canonical binding
    # outside caller-visible mutable attributes. The registry is keyed by the
    # builtin object identity integer rather than by the store object itself so
    # hostile __hash__/__eq__ implementations can never participate in authority
    # lookup. A weak reference prevents stale id reuse from inheriting a binding.
    bindings: dict[int, tuple[weakref.ReferenceType[object], Path, Path]] = {}
    guard = threading.RLock()

    def register(store: object, workspace: Path, path: Path) -> None:
        identity = id(store)

        def cleanup(reference: weakref.ReferenceType[object]) -> None:
            with guard:
                current = bindings.get(identity)
                if current is not None and current[0] is reference:
                    bindings.pop(identity, None)

        reference = weakref.ref(store, cleanup)
        with guard:
            current = bindings.get(identity)
            if current is not None and current[0]() is store:
                raise RuntimeError(
                    "EconomicGoalStore canonical binding is already registered"
                )
            bindings[identity] = (reference, workspace, path)

    def require(store: object) -> tuple[Path, Path]:
        identity = id(store)
        with guard:
            binding = bindings.get(identity)
        if binding is None or binding[0]() is not store:
            raise RuntimeError("EconomicGoalStore canonical binding is unavailable")
        return binding[1], binding[2]

    return register, require


_register_store_binding, _require_store_binding = _make_store_binding_registry()
_CANONICAL_REGISTER_STORE_BINDING: Final = _register_store_binding
_CANONICAL_REQUIRE_STORE_BINDING: Final = _require_store_binding


class EconomicGoalStore:
    """Workspace-local durable owner-contract store.

    ``initialize_owner`` is creation-only. Automatic actors have only
    ``persist_automatic_successor`` which, under the canonical workspace economic
    writer lock, reloads the durable predecessor and applies the monotonic authority
    validator before atomic publication. The lock makes validation and publication
    one cooperating-writer critical section so stale concurrent revisions cannot
    overwrite a newly tightened authority state.
    """

    FILE_NAME: Final = _ECONOMIC_GOAL_FILE_NAME

    def __init__(self, workspace: str | Path) -> None:
        if type(self) is not __class__:
            raise TypeError("EconomicGoalStore authority requires the exact store type")
        if type(workspace) not in {str, _CANONICAL_PATH_TYPE}:
            raise TypeError(
                "EconomicGoalStore workspace must be exact str or exact Path"
            )
        try:
            canonical_workspace = _CANONICAL_PATH_RESOLVE(_CANONICAL_PATH_CONSTRUCTOR(workspace), strict=False)
        except (OSError, RuntimeError) as exc:
            raise EconomicGoalContractError(
                "EconomicGoalStore workspace cannot be canonically resolved"
            ) from exc
        _CANONICAL_REGISTER_STORE_BINDING(
            self,
            canonical_workspace,
            _CANONICAL_PATH_JOIN(canonical_workspace, _ECONOMIC_GOAL_FILE_NAME),
        )

    @property
    def workspace(self) -> Path:
        if type(self) is not __class__:
            raise TypeError("EconomicGoalStore authority requires the exact store type")
        return _CANONICAL_REQUIRE_STORE_BINDING(self)[0]

    @property
    def path(self) -> Path:
        if type(self) is not __class__:
            raise TypeError("EconomicGoalStore authority requires the exact store type")
        return _CANONICAL_REQUIRE_STORE_BINDING(self)[1]

    def load(self) -> EconomicGoalContract:
        if type(self) is not __class__:
            raise TypeError("EconomicGoalStore authority requires the exact store type")
        _, path = _CANONICAL_REQUIRE_STORE_BINDING(self)
        return _CANONICAL_GOAL_JSON_DECODER(_CANONICAL_GOAL_TEXT_READER(path))

    def initialize_owner(self, contract: EconomicGoalContract) -> None:
        """Create the first owner contract while holding the economic writer lock."""

        if type(self) is not __class__:
            raise TypeError("EconomicGoalStore authority requires the exact store type")
        workspace, path = _CANONICAL_REQUIRE_STORE_BINDING(self)
        with _CANONICAL_WORKSPACE_LOCK_TYPE(workspace):
            if _CANONICAL_PATH_EXISTS(path):
                raise EconomicGoalContractError(
                    "persisted economic goal already exists; owner replacement requires "
                    "a separate authority boundary"
                )
            _CANONICAL_ATOMIC_WRITE_JSON(path, _CANONICAL_GOAL_SERIALIZER(contract))

    def persist_automatic_successor(self, candidate: EconomicGoalContract) -> None:
        """Publish one machine revision only when durable authority cannot expand."""

        if type(self) is not __class__:
            raise TypeError("EconomicGoalStore authority requires the exact store type")
        workspace, path = _CANONICAL_REQUIRE_STORE_BINDING(self)
        with _CANONICAL_WORKSPACE_LOCK_TYPE(workspace):
            previous = _CANONICAL_GOAL_JSON_DECODER(_CANONICAL_GOAL_TEXT_READER(path))
            _CANONICAL_TRANSITION_VALIDATOR(previous, candidate)
            _CANONICAL_ATOMIC_WRITE_JSON(path, _CANONICAL_GOAL_SERIALIZER(candidate))
