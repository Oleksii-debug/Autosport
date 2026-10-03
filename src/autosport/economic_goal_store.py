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
from pathlib import Path
import stat
from typing import Final

from .economic_goal import (
    AutomationLevel,
    EconomicGoalContract,
    EconomicGoalContractError,
    EconomicObjective,
    validate_automatic_transition,
)
from .integrity import atomic_write_json
from .json_integrity import strict_json_loads
from .workspace_lock import WorkspaceEconomicLock


ECONOMIC_GOAL_SCHEMA: Final = "autosport.economic_goal_contract"
ECONOMIC_GOAL_SCHEMA_VERSION: Final = 1
_MAX_ECONOMIC_GOAL_BYTES: Final = 128 * 1024

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


def _open_exclusive_write_descriptor(path: Path) -> int:
    """Create the final owner-authority pathname without following aliases."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    if os.name != "nt":
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        if not no_follow:
            raise OSError("platform lacks no-follow economic-goal creation support")
        return os.open(path, flags | no_follow, 0o600)

    import ctypes
    import msvcrt
    from ctypes import wintypes

    create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE

    generic_write = 0x40000000
    file_share_read = 0x00000001
    create_new = 1
    file_attribute_normal = 0x00000080
    file_flag_open_reparse_point = 0x00200000
    invalid_handle_value = ctypes.c_void_p(-1).value

    kernel_handle = create_file(
        str(path),
        generic_write,
        file_share_read,
        None,
        create_new,
        file_attribute_normal | file_flag_open_reparse_point,
        None,
    )
    if kernel_handle == invalid_handle_value:
        error_code = ctypes.get_last_error()
        if error_code in (80, 183):  # ERROR_FILE_EXISTS / ERROR_ALREADY_EXISTS
            raise FileExistsError(
                error_code,
                "economic goal authority path already exists",
                str(path),
            )
        raise ctypes.WinError(error_code)

    close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    try:
        return msvcrt.open_osfhandle(kernel_handle, flags)
    except BaseException:
        close_handle(kernel_handle)
        raise


def _exclusive_create_owner_contract(path: Path, payload: dict[str, object]) -> None:
    """Publish the initial owner authority once, never by pathname replacement."""

    try:
        encoded = (
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise EconomicGoalContractError(
            "owner economic goal is not canonical JSON"
        ) from exc
    if len(encoded) > _MAX_ECONOMIC_GOAL_BYTES:
        raise EconomicGoalContractError(
            "owner economic goal exceeds the bounded authority size"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor: int | None = None
    try:
        descriptor = _open_exclusive_write_descriptor(path)
        written = 0
        while written < len(encoded):
            count = os.write(descriptor, encoded[written:])
            if count <= 0:
                raise OSError("economic goal authority write made no progress")
            written += count
        os.fsync(descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _open_read_only_descriptor(path: Path) -> int:
    """Open the contract pathname without following its final alias."""

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if os.name != "nt":
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        if not no_follow:
            raise OSError("platform lacks no-follow economic-goal reads")
        return os.open(path, flags | no_follow)

    import ctypes
    import msvcrt
    from ctypes import wintypes

    create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE

    generic_read = 0x80000000
    file_share_read = 0x00000001
    file_share_write = 0x00000002
    file_share_delete = 0x00000004
    open_existing = 3
    file_attribute_normal = 0x00000080
    file_flag_open_reparse_point = 0x00200000
    invalid_handle_value = ctypes.c_void_p(-1).value

    kernel_handle = create_file(
        str(path),
        generic_read,
        file_share_read | file_share_write | file_share_delete,
        None,
        open_existing,
        file_attribute_normal | file_flag_open_reparse_point,
        None,
    )
    if kernel_handle == invalid_handle_value:
        raise ctypes.WinError(ctypes.get_last_error())

    close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    try:
        return msvcrt.open_osfhandle(kernel_handle, flags)
    except BaseException:
        close_handle(kernel_handle)
        raise


def _require_regular_single_link(metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise EconomicGoalContractError(
            "persisted economic goal must be a single-link regular non-symlink file"
        )


def _read_canonical_contract_text(path: Path) -> str:
    """Read one bounded contract image from a stable non-alias pathname."""

    descriptor: int | None = None
    verification_descriptor: int | None = None
    final_verification_descriptor: int | None = None
    try:
        path_before = path.lstat()
        _require_regular_single_link(path_before)
        descriptor = _open_read_only_descriptor(path)
        opened_before = os.fstat(descriptor)
        _require_regular_single_link(opened_before)

        chunks: list[bytes] = []
        total = 0
        while total <= _MAX_ECONOMIC_GOAL_BYTES:
            chunk = os.read(
                descriptor,
                min(64 * 1024, _MAX_ECONOMIC_GOAL_BYTES + 1 - total),
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > _MAX_ECONOMIC_GOAL_BYTES:
            raise EconomicGoalContractError(
                "persisted economic goal exceeds the bounded authority size"
            )

        opened_after = os.fstat(descriptor)
        _require_regular_single_link(opened_after)
        if (
            opened_before.st_size != opened_after.st_size
            or opened_before.st_mtime_ns != opened_after.st_mtime_ns
            or opened_before.st_ctime_ns != opened_after.st_ctime_ns
        ):
            raise EconomicGoalContractError(
                "persisted economic goal changed while being read"
            )

        verification_descriptor = _open_read_only_descriptor(path)
        verification_stat = os.fstat(verification_descriptor)
        _require_regular_single_link(verification_stat)
        if not os.path.sameopenfile(descriptor, verification_descriptor):
            raise EconomicGoalContractError(
                "persisted economic goal pathname changed while being read"
            )

        # Re-check the pathname after the first descriptor identity proof, then
        # open it one final time. This closes the replace-after-verification-open
        # window even when an attacker supplies a same-shape regular file.
        path_after = path.lstat()
        _require_regular_single_link(path_after)
        final_verification_descriptor = _open_read_only_descriptor(path)
        final_verification_stat = os.fstat(final_verification_descriptor)
        _require_regular_single_link(final_verification_stat)
        if not os.path.sameopenfile(descriptor, final_verification_descriptor):
            raise EconomicGoalContractError(
                "persisted economic goal pathname changed while being read"
            )

        try:
            return b"".join(chunks).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EconomicGoalContractError(
                "persisted economic goal must be valid UTF-8"
            ) from exc
    except EconomicGoalContractError:
        raise
    except OSError as exc:
        raise EconomicGoalContractError(
            "cannot safely read persisted economic goal"
        ) from exc
    finally:
        for candidate in (
            final_verification_descriptor,
            verification_descriptor,
            descriptor,
        ):
            if candidate is not None:
                try:
                    os.close(candidate)
                except OSError:
                    pass


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
    if not isinstance(value, str):
        raise EconomicGoalContractError(f"{name} must be a Decimal string")
    if not value or value != value.strip():
        raise EconomicGoalContractError(f"{name} must be a canonical Decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise EconomicGoalContractError(f"{name} is not a valid Decimal string") from exc
    if not parsed.is_finite():
        raise EconomicGoalContractError(f"{name} must be finite")
    if str(parsed) != value:
        raise EconomicGoalContractError(f"{name} must use canonical Decimal text")
    return parsed


def _restriction_set(name: str, value: object) -> frozenset[str]:
    if not isinstance(value, list):
        raise EconomicGoalContractError(f"{name} must be a sorted JSON array")
    if any(not isinstance(item, str) for item in value):
        raise EconomicGoalContractError(f"{name} must contain only strings")
    if value != sorted(value) or len(value) != len(set(value)):
        raise EconomicGoalContractError(
            f"{name} must be sorted and contain unique strings"
        )
    return frozenset(value)


def economic_goal_to_payload(contract: EconomicGoalContract) -> dict[str, object]:
    """Return the canonical schema-v1 JSON payload for ``contract``."""

    if not isinstance(contract, EconomicGoalContract):
        raise EconomicGoalContractError(
            "economic goal persistence requires an EconomicGoalContract"
        )

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
    return {
        "schema": ECONOMIC_GOAL_SCHEMA,
        "schema_version": ECONOMIC_GOAL_SCHEMA_VERSION,
        "contract": body,
    }


def economic_goal_from_payload(payload: object) -> EconomicGoalContract:
    """Decode schema-v1 persistence input and fail closed on any ambiguity."""

    if not isinstance(payload, dict) or not all(
        isinstance(key, str) for key in payload
    ):
        raise EconomicGoalContractError("economic goal payload must be a JSON object")
    root: dict[str, object] = payload
    _require_exact_keys("economic goal payload", root, _ROOT_KEYS)

    if root["schema"] != ECONOMIC_GOAL_SCHEMA:
        raise EconomicGoalContractError("unsupported economic goal schema")
    version = root["schema_version"]
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != ECONOMIC_GOAL_SCHEMA_VERSION
    ):
        raise EconomicGoalContractError("unsupported economic goal schema_version")

    raw_contract = root["contract"]
    if not isinstance(raw_contract, dict) or not all(
        isinstance(key, str) for key in raw_contract
    ):
        raise EconomicGoalContractError("contract must be a JSON object")
    body: dict[str, object] = raw_contract
    _require_exact_keys("contract", body, _CONTRACT_KEYS)

    decoded = dict(body)
    for field in _DECIMAL_FIELDS:
        decoded[field] = _decimal_text(field, body[field])
    if body["max_stake_amount"] is None:
        decoded["max_stake_amount"] = None
    else:
        decoded["max_stake_amount"] = _decimal_text(
            "max_stake_amount", body["max_stake_amount"]
        )

    try:
        decoded["objective"] = EconomicObjective(body["objective"])
    except (TypeError, ValueError) as exc:
        raise EconomicGoalContractError("unsupported economic objective") from exc

    automation = body["automation_level"]
    if isinstance(automation, bool) or not isinstance(automation, int):
        raise EconomicGoalContractError("automation_level must be an integer")
    try:
        decoded["automation_level"] = AutomationLevel(automation)
    except ValueError as exc:
        raise EconomicGoalContractError("unsupported automation_level") from exc

    for field in _RESTRICTION_FIELDS:
        decoded[field] = _restriction_set(field, body[field])

    try:
        return EconomicGoalContract(**decoded)  # type: ignore[arg-type]
    except EconomicGoalContractError:
        raise
    except (TypeError, ValueError) as exc:
        raise EconomicGoalContractError("malformed economic goal contract") from exc


def economic_goal_from_json(text: str) -> EconomicGoalContract:
    """Decode one strict JSON document into a validated contract."""

    if not isinstance(text, str):
        raise EconomicGoalContractError("economic goal JSON must be text")
    try:
        payload = strict_json_loads(text)
    except (TypeError, ValueError) as exc:
        raise EconomicGoalContractError("invalid economic goal JSON") from exc
    return economic_goal_from_payload(payload)


class EconomicGoalStore:
    """Workspace-local durable owner-contract store.

    ``initialize_owner`` is creation-only. Automatic actors have only
    ``persist_automatic_successor`` which, under the canonical workspace economic
    writer lock, reloads the durable predecessor and applies the monotonic authority
    validator before atomic publication. The lock makes validation and publication
    one cooperating-writer critical section so stale concurrent revisions cannot
    overwrite a newly tightened authority state.
    """

    FILE_NAME: Final = "economic_goal_contract.json"

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace)
        self.path = self.workspace / self.FILE_NAME

    def load(self) -> EconomicGoalContract:
        return economic_goal_from_json(_read_canonical_contract_text(self.path))

    def load_optional(self) -> EconomicGoalContract | None:
        """Return None only for a genuinely absent pathname.

        Any existing pathname object, including a broken symlink/reparse alias,
        must cross the canonical verified reader and therefore fail closed rather
        than being reclassified as missing authority.
        """

        try:
            self.path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise EconomicGoalContractError(
                "cannot inspect persisted economic goal path"
            ) from exc
        return self.load()

    def initialize_owner(self, contract: EconomicGoalContract) -> None:
        """Create the first owner contract without any final-path replacement."""

        if not isinstance(contract, EconomicGoalContract):
            raise TypeError("owner contract must be an EconomicGoalContract")
        with WorkspaceEconomicLock(self.workspace):
            try:
                _exclusive_create_owner_contract(
                    self.path,
                    economic_goal_to_payload(contract),
                )
            except FileExistsError as exc:
                raise EconomicGoalContractError(
                    "persisted economic goal already exists; owner replacement requires "
                    "a separate authority boundary"
                ) from exc
            except EconomicGoalContractError:
                raise
            except OSError as exc:
                raise EconomicGoalContractError(
                    "cannot create persisted economic goal authority"
                ) from exc

    def persist_automatic_successor(self, candidate: EconomicGoalContract) -> None:
        """Publish one machine revision only when durable authority cannot expand."""

        with WorkspaceEconomicLock(self.workspace):
            previous = self.load()
            validate_automatic_transition(previous, candidate)
            atomic_write_json(self.path, economic_goal_to_payload(candidate))
