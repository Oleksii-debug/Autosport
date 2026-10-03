"""Durable immutable runtime authority for PAPER deployment semantics.

Scientific evidence belongs to ``ScientificRegistry`` and market evidence belongs to
``SQLiteMarketStore``.  This module persists the remaining runtime identity that must be
stable across restart: environment, episode/admissible action universe, and the exact
versioned meanings of those actions.  Records are append-only and hash chained; every
read revalidates the full file before exposing an authority by ID.

Runtime availability is a store-owned first-seen boundary. Callers cannot backdate it:
new records are timestamped only by the production-owned UTC clock, while exact retries
reuse the immutable timestamp already durably recorded.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from types import MappingProxyType
from typing import Any, Final, Mapping

from . import learning_environment as _learning_environment
from .learning_environment import EnvironmentIdentity, Episode
from . import monotonic_workspace_authority as _monotonic_workspace_authority
from .monotonic_authority_root_binding import AuthorityRootSelectionBinding
from .monotonic_workspace_authority import (
    AUTHORITY_ID as MONOTONIC_AUTHORITY_ID,
    AuthorityPhase,
    MonotonicWorkspaceAuthority,
    RecoveryDisposition,
)
from .monotonic_workspace_binding import WorkspaceIdentityBinding
from .workspace_lock import WorkspaceEconomicLock


STORE_SCHEMA: Final = "autosport.deployment_runtime_authority_store"
STORE_SCHEMA_VERSION: Final = 2
RECORD_SCHEMA: Final = "autosport.deployment_runtime_authority"
RECORD_SCHEMA_VERSION: Final = 2
_EMPTY_CHAIN_SHA256: Final = hashlib.sha256(b"").hexdigest()
_HEX: Final = frozenset("0123456789abcdef")
_AUTHORITY_DOMAIN: Final = "deployment-runtime-authority"
_AUTHORITY_BINDING_SCHEMA: Final = "autosport.deployment_runtime_authority.monotonic_binding"
_AUTHORITY_BINDING_SCHEMA_VERSION: Final = 1
_CANONICAL_STORE_SCHEMA_VALUE: Final = STORE_SCHEMA
_CANONICAL_STORE_SCHEMA_VERSION_VALUE: Final = STORE_SCHEMA_VERSION
_CANONICAL_RECORD_SCHEMA_VALUE: Final = RECORD_SCHEMA
_CANONICAL_RECORD_SCHEMA_VERSION_VALUE: Final = RECORD_SCHEMA_VERSION
_CANONICAL_EMPTY_CHAIN_SHA256_VALUE: Final = _EMPTY_CHAIN_SHA256
_CANONICAL_HEX_VALUE: Final = _HEX
_CANONICAL_AUTHORITY_DOMAIN_VALUE: Final = _AUTHORITY_DOMAIN
_CANONICAL_AUTHORITY_BINDING_SCHEMA_VALUE: Final = _AUTHORITY_BINDING_SCHEMA
_CANONICAL_AUTHORITY_BINDING_SCHEMA_VERSION_VALUE: Final = _AUTHORITY_BINDING_SCHEMA_VERSION
_CANONICAL_MONOTONIC_AUTHORITY_TYPE: Final = MonotonicWorkspaceAuthority
_CANONICAL_MONOTONIC_AUTHORITY_ID: Final = MONOTONIC_AUTHORITY_ID
_CANONICAL_MONOTONIC_MODULE: Final = _monotonic_workspace_authority
_CANONICAL_MONOTONIC_MODULE_AUTHORITY_PHASE: Final = (
    _monotonic_workspace_authority.AuthorityPhase
)
_CANONICAL_MONOTONIC_MODULE_RECOVERY_DISPOSITION: Final = (
    _monotonic_workspace_authority.RecoveryDisposition
)
_CANONICAL_MONOTONIC_MODULE_AUTHORITY_RECORD: Final = (
    _monotonic_workspace_authority.AuthorityRecord
)
_CANONICAL_MONOTONIC_MODULE_HISTORY_TYPE: Final = (
    _monotonic_workspace_authority._History
)
_CANONICAL_MONOTONIC_MODULE_WORKSPACE_LOCK: Final = (
    _monotonic_workspace_authority.WorkspaceEconomicLock
)
_CANONICAL_MONOTONIC_MODULE_STRICT_JSON_LOADS: Final = (
    _monotonic_workspace_authority.strict_json_loads
)
_CANONICAL_MONOTONIC_MODULE_STAT: Final = _monotonic_workspace_authority.stat
_CANONICAL_MONOTONIC_MODULE_RECORD_FILE_RE: Final = (
    _monotonic_workspace_authority._RECORD_FILE_RE
)
_CANONICAL_MONOTONIC_MODULE_SHA256_RE: Final = (
    _monotonic_workspace_authority._SHA256_RE
)
_CANONICAL_MONOTONIC_MODULE_CONSTANTS: Final = (
    ("AUTHORITY_SCHEMA", _monotonic_workspace_authority.AUTHORITY_SCHEMA),
    (
        "AUTHORITY_SCHEMA_VERSION",
        _monotonic_workspace_authority.AUTHORITY_SCHEMA_VERSION,
    ),
    ("AUTHORITY_ID", _monotonic_workspace_authority.AUTHORITY_ID),
    ("_NAMESPACE_SCHEMA", _monotonic_workspace_authority._NAMESPACE_SCHEMA),
    (
        "_NAMESPACE_MARKER_KEYS",
        _monotonic_workspace_authority._NAMESPACE_MARKER_KEYS,
    ),
    ("_RECORD_KEYS", _monotonic_workspace_authority._RECORD_KEYS),
)
_CANONICAL_MONOTONIC_MODULE_HELPERS: Final = tuple(
    (
        name,
        helper,
        getattr(helper, "__code__", None),
    )
    for name in (
        "_text",
        "_digest",
        "_record_hash",
        "_sync_directory_lineage",
        "_durable_exclusive_json_create",
        "strict_json_loads",
    )
    for helper in (getattr(_monotonic_workspace_authority, name),)
)
_CANONICAL_AUTHORITY_PHASE_TYPE: Final = AuthorityPhase
_CANONICAL_RECOVERY_DISPOSITION_TYPE: Final = RecoveryDisposition
_CANONICAL_PATH_TYPE: Final = Path
_CANONICAL_CONCRETE_PATH_TYPE: Final = type(Path("."))
_CANONICAL_PATH_READ_TEXT: Final = _CANONICAL_CONCRETE_PATH_TYPE.read_text
_CANONICAL_PATH_READ_TEXT_CODE: Final = getattr(
    _CANONICAL_PATH_READ_TEXT,
    "__code__",
    None,
)
_CANONICAL_PATH_EXPANDUSER: Final = _CANONICAL_CONCRETE_PATH_TYPE.expanduser
_CANONICAL_PATH_EXPANDUSER_CODE: Final = getattr(
    _CANONICAL_PATH_EXPANDUSER,
    "__code__",
    None,
)
_CANONICAL_PATH_RESOLVE: Final = _CANONICAL_CONCRETE_PATH_TYPE.resolve
_CANONICAL_PATH_RESOLVE_CODE: Final = getattr(
    _CANONICAL_PATH_RESOLVE,
    "__code__",
    None,
)
_CANONICAL_DATETIME_TYPE: Final = datetime
_CANONICAL_TIMEZONE_MODULE: Final = timezone
_CANONICAL_WORKSPACE_IDENTITY_BINDING_TYPE: Final = WorkspaceIdentityBinding
_CANONICAL_AUTHORITY_ROOT_SELECTION_BINDING_TYPE: Final = AuthorityRootSelectionBinding
_CANONICAL_WORKSPACE_BINDING_SURFACE: Final = tuple(
    (
        name,
        descriptor,
        getattr(descriptor, "__code__", None),
    )
    for name in ("validate_existing", "ensure_bound")
    for descriptor in (vars(WorkspaceIdentityBinding)[name],)
)
_CANONICAL_ROOT_SELECTION_SURFACE: Final = tuple(
    (
        name,
        descriptor,
        getattr(descriptor, "__code__", None),
    )
    for name in (
        "namespace_activation_path",
        "validate_existing",
        "ensure_bound",
        "validate_namespace_activation",
        "ensure_namespace_activated",
    )
    for descriptor in (vars(AuthorityRootSelectionBinding)[name],)
)
_CANONICAL_RLOCK_FACTORY: Final = RLock
_CANONICAL_RLOCK_TYPE: Final = type(RLock())
_CANONICAL_WORKSPACE_ECONOMIC_LOCK_TYPE: Final = WorkspaceEconomicLock
_CANONICAL_MONOTONIC_AUTHORITY_INIT: Final = MonotonicWorkspaceAuthority.__init__
_CANONICAL_MONOTONIC_RECOVER: Final = MonotonicWorkspaceAuthority.recover
_CANONICAL_MONOTONIC_RECOVER_CODE: Final = MonotonicWorkspaceAuthority.recover.__code__
_CANONICAL_MONOTONIC_READ_HISTORY: Final = MonotonicWorkspaceAuthority.read_history
_CANONICAL_MONOTONIC_READ_HISTORY_CODE: Final = (
    MonotonicWorkspaceAuthority.read_history.__code__
)
_MONOTONIC_READ_RECOVERY_INTERNAL_NAMES: Final = (
    "_validate_authority_root_selection",
    "_validate_authority_root_activation",
    "_validate_workspace_binding",
    "_ensure_authority_root_bound",
    "_ensure_authority_root_activated",
    "_load_bound_history",
    "_new_terminal_record",
    "_append_record",
    "_load_history",
    "_validate_namespace_marker",
    "_ensure_namespace_marker",
    "_decode_record",
    "_payload",
    "_namespace_payload",
)
_CANONICAL_MONOTONIC_READ_RECOVERY_INTERNAL_SURFACE: Final = tuple(
    (
        name,
        descriptor,
        getattr(
            getattr(descriptor, "__func__", descriptor),
            "__code__",
            None,
        ),
    )
    for name in _MONOTONIC_READ_RECOVERY_INTERNAL_NAMES
    for descriptor in (vars(MonotonicWorkspaceAuthority)[name],)
)
_CANONICAL_OBJECT_NEW: Final = object.__new__
_CANONICAL_HASHLIB_MODULE: Final = hashlib
_CANONICAL_HASHLIB_SHA256: Final = hashlib.sha256
_CANONICAL_OS_MODULE: Final = os
_CANONICAL_OS_PATH_NORMCASE: Final = os.path.normcase
_CANONICAL_OS_PATH_NORMPATH: Final = os.path.normpath
_CANONICAL_OS_OPEN: Final = os.open
_CANONICAL_OS_FSTAT: Final = os.fstat
_CANONICAL_OS_LSTAT: Final = os.lstat
_CANONICAL_OS_FSYNC: Final = os.fsync
_CANONICAL_OS_CLOSE: Final = os.close
_CANONICAL_OS_REPLACE: Final = os.replace
_CANONICAL_STAT_MODULE: Final = stat
_CANONICAL_STAT_ISREG: Final = stat.S_ISREG
_CANONICAL_HASHLIB_SHA256_CODE: Final = getattr(hashlib.sha256, "__code__", None)
_CANONICAL_JSON_MODULE: Final = json
_CANONICAL_JSON_DUMPS: Final = json.dumps
_CANONICAL_JSON_DUMPS_CODE: Final = getattr(json.dumps, "__code__", None)
_CANONICAL_JSON_LOADS: Final = json.loads
_CANONICAL_JSON_LOADS_CODE: Final = getattr(json.loads, "__code__", None)
_CANONICAL_JSON_DECODE_ERROR: Final = json.JSONDecodeError
_CANONICAL_UUID_MODULE: Final = uuid
_CANONICAL_UUID4: Final = uuid.uuid4
_CANONICAL_UUID4_CODE: Final = getattr(uuid.uuid4, "__code__", None)
_MISSING_AUTHORITY_CLASS_SLOT: Final = object()
_CANONICAL_MONOTONIC_AUTHORITY_CLASS_SURFACE: Final = tuple(
    (
        name,
        member,
        getattr(member, "__code__", None),
    )
    for name in ("__new__", "__init__")
    for member in (
        vars(MonotonicWorkspaceAuthority).get(
            name,
            _MISSING_AUTHORITY_CLASS_SLOT,
        ),
    )
)

# The instance dictionary is retained for explicit crash/fault injection seams, but
# positive/read authority must never dispatch through caller-owned instance shadows.
# These methods either expose validated durable truth or are part of its verification
# graph; _write_atomic_path and _observed_now deliberately remain injectable.
_SEALED_STORE_DISPATCH_NAMES: Final = frozenset(
    {
        "append",
        "get",
        "records",
        "_configure",
        "_assert_static_authority_contract",
        "_read_payload",
        "_records_from_payload",
        "_finalize_published_path",
        "_state_sha256",
        "_recover_state",
        "_read_validated_records_locked",
        "_assert_binding_integrity",
        "_new_transaction_id",
        "_observed_now",
    }
)


class DeploymentRuntimeAuthorityError(ValueError):
    """Durable runtime authority is missing, malformed, or conflicting."""


def _new_local_lock() -> object:
    if RLock is not _CANONICAL_RLOCK_FACTORY:
        raise DeploymentRuntimeAuthorityError(
            "runtime authority local lock constructor dispatch was replaced"
        )
    return _CANONICAL_RLOCK_FACTORY()


def _workspace_economic_lock(workspace: Path) -> WorkspaceEconomicLock:
    if WorkspaceEconomicLock is not _CANONICAL_WORKSPACE_ECONOMIC_LOCK_TYPE:
        raise DeploymentRuntimeAuthorityError(
            "runtime authority workspace lock constructor dispatch was replaced"
        )
    return _CANONICAL_WORKSPACE_ECONOMIC_LOCK_TYPE(workspace)


def _assert_canonical_monotonic_authority_constructor() -> None:
    if MonotonicWorkspaceAuthority is not _CANONICAL_MONOTONIC_AUTHORITY_TYPE:
        raise DeploymentRuntimeAuthorityError(
            "monotonic workspace authority constructor dispatch was replaced"
        )
    class_dict = vars(_CANONICAL_MONOTONIC_AUTHORITY_TYPE)
    for name, expected, expected_code in (
        _CANONICAL_MONOTONIC_AUTHORITY_CLASS_SURFACE
    ):
        current = class_dict.get(name, _MISSING_AUTHORITY_CLASS_SLOT)
        if (
            current is not expected
            or (
                expected_code is not None
                and getattr(current, "__code__", None) is not expected_code
            )
        ):
            raise DeploymentRuntimeAuthorityError(
                "monotonic workspace authority constructor dispatch was replaced"
            )


_CANONICAL_MONOTONIC_CONSTRUCTOR_ASSERT: Final = _assert_canonical_monotonic_authority_constructor
_CANONICAL_MONOTONIC_CONSTRUCTOR_ASSERT_CODE: Final = (
    _assert_canonical_monotonic_authority_constructor.__code__
)


def _construct_monotonic_authority(
    *,
    workspace: Path,
    domain: str,
    key: str,
    authority_root: str | Path | None,
) -> MonotonicWorkspaceAuthority:
    if (
        _assert_canonical_monotonic_authority_constructor
        is not _CANONICAL_MONOTONIC_CONSTRUCTOR_ASSERT
        or _CANONICAL_MONOTONIC_CONSTRUCTOR_ASSERT.__code__
        is not _CANONICAL_MONOTONIC_CONSTRUCTOR_ASSERT_CODE
    ):
        raise DeploymentRuntimeAuthorityError(
            "monotonic workspace authority constructor guard dispatch was replaced"
        )
    _CANONICAL_MONOTONIC_CONSTRUCTOR_ASSERT()
    authority = _CANONICAL_OBJECT_NEW(_CANONICAL_MONOTONIC_AUTHORITY_TYPE)
    _CANONICAL_MONOTONIC_AUTHORITY_INIT(
        authority,
        workspace=workspace,
        domain=domain,
        key=key,
        authority_root=authority_root,
    )
    _CANONICAL_MONOTONIC_CONSTRUCTOR_ASSERT()
    if type(authority) is not _CANONICAL_MONOTONIC_AUTHORITY_TYPE:
        raise DeploymentRuntimeAuthorityError(
            "monotonic workspace authority construction returned noncanonical type"
        )
    return authority


_CANONICAL_NEW_LOCAL_LOCK_HELPER: Final = _new_local_lock
_CANONICAL_NEW_LOCAL_LOCK_HELPER_CODE: Final = _new_local_lock.__code__
_CANONICAL_WORKSPACE_LOCK_HELPER: Final = _workspace_economic_lock
_CANONICAL_WORKSPACE_LOCK_HELPER_CODE: Final = _workspace_economic_lock.__code__
_CANONICAL_MONOTONIC_CONSTRUCTOR_HELPER: Final = _construct_monotonic_authority
_CANONICAL_MONOTONIC_CONSTRUCTOR_HELPER_CODE: Final = _construct_monotonic_authority.__code__


def _assert_monotonic_read_recovery_dispatch(
    authority: MonotonicWorkspaceAuthority,
) -> None:
    if _monotonic_workspace_authority is not _CANONICAL_MONOTONIC_MODULE:
        raise DeploymentRuntimeAuthorityError(
            "runtime authority monotonic module was replaced"
        )
    if (
        _CANONICAL_MONOTONIC_MODULE.AuthorityPhase
        is not _CANONICAL_MONOTONIC_MODULE_AUTHORITY_PHASE
        or _CANONICAL_MONOTONIC_MODULE.RecoveryDisposition
        is not _CANONICAL_MONOTONIC_MODULE_RECOVERY_DISPOSITION
        or _CANONICAL_MONOTONIC_MODULE.AuthorityRecord
        is not _CANONICAL_MONOTONIC_MODULE_AUTHORITY_RECORD
        or _CANONICAL_MONOTONIC_MODULE._History
        is not _CANONICAL_MONOTONIC_MODULE_HISTORY_TYPE
        or _CANONICAL_MONOTONIC_MODULE.WorkspaceEconomicLock
        is not _CANONICAL_MONOTONIC_MODULE_WORKSPACE_LOCK
        or _CANONICAL_MONOTONIC_MODULE.strict_json_loads
        is not _CANONICAL_MONOTONIC_MODULE_STRICT_JSON_LOADS
        or _CANONICAL_MONOTONIC_MODULE.stat
        is not _CANONICAL_MONOTONIC_MODULE_STAT
        or _CANONICAL_MONOTONIC_MODULE._RECORD_FILE_RE
        is not _CANONICAL_MONOTONIC_MODULE_RECORD_FILE_RE
        or _CANONICAL_MONOTONIC_MODULE._SHA256_RE
        is not _CANONICAL_MONOTONIC_MODULE_SHA256_RE
    ):
        raise DeploymentRuntimeAuthorityError(
            "runtime authority monotonic dependency was replaced"
        )
    for name, expected_value in _CANONICAL_MONOTONIC_MODULE_CONSTANTS:
        if getattr(_CANONICAL_MONOTONIC_MODULE, name, None) != expected_value:
            raise DeploymentRuntimeAuthorityError(
                "runtime authority monotonic constant was replaced"
            )
    for name, expected_helper, expected_code in _CANONICAL_MONOTONIC_MODULE_HELPERS:
        current_helper = getattr(_CANONICAL_MONOTONIC_MODULE, name, None)
        if (
            current_helper is not expected_helper
            or (
                expected_code is not None
                and getattr(current_helper, "__code__", None) is not expected_code
            )
        ):
            raise DeploymentRuntimeAuthorityError(
                "runtime authority monotonic helper was replaced"
            )
    if type(authority) is not _CANONICAL_MONOTONIC_AUTHORITY_TYPE:
        raise DeploymentRuntimeAuthorityError(
            "runtime authority monotonic read type was replaced"
        )
    class_dict = vars(_CANONICAL_MONOTONIC_AUTHORITY_TYPE)
    for name, expected, expected_code in (
        (
            "recover",
            _CANONICAL_MONOTONIC_RECOVER,
            _CANONICAL_MONOTONIC_RECOVER_CODE,
        ),
        (
            "read_history",
            _CANONICAL_MONOTONIC_READ_HISTORY,
            _CANONICAL_MONOTONIC_READ_HISTORY_CODE,
        ),
    ):
        current = class_dict.get(name)
        if (
            current is not expected
            or getattr(current, "__code__", None) is not expected_code
        ):
            raise DeploymentRuntimeAuthorityError(
                "runtime authority monotonic read dispatch was replaced"
            )
    for name, expected, expected_code in (
        _CANONICAL_MONOTONIC_READ_RECOVERY_INTERNAL_SURFACE
    ):
        current = class_dict.get(name)
        current_callable = getattr(current, "__func__", current)
        if (
            current is not expected
            or (
                expected_code is not None
                and getattr(current_callable, "__code__", None) is not expected_code
            )
        ):
            raise DeploymentRuntimeAuthorityError(
                "runtime authority monotonic read internal dispatch was replaced"
            )
    instance_dict = object.__getattribute__(authority, "__dict__")
    protected_instance_names = {
        "recover",
        "read_history",
        *_MONOTONIC_READ_RECOVERY_INTERNAL_NAMES,
    }
    if protected_instance_names.intersection(instance_dict):
        raise DeploymentRuntimeAuthorityError(
            "runtime authority monotonic read instance dispatch was replaced"
        )


def _monotonic_recover(
    authority: MonotonicWorkspaceAuthority,
    *,
    observed_state_sha256: str | None,
    tx_id: str | None = None,
    semantic_binding_sha256: str | None = None,
) -> object:
    _assert_monotonic_read_recovery_dispatch(authority)
    return _CANONICAL_MONOTONIC_RECOVER(
        authority,
        observed_state_sha256=observed_state_sha256,
        tx_id=tx_id,
        semantic_binding_sha256=semantic_binding_sha256,
    )


def _monotonic_read_history(
    authority: MonotonicWorkspaceAuthority,
) -> tuple[object, ...]:
    _assert_monotonic_read_recovery_dispatch(authority)
    return _CANONICAL_MONOTONIC_READ_HISTORY(authority)


_CANONICAL_MONOTONIC_READ_DISPATCH_GUARD: Final = _assert_monotonic_read_recovery_dispatch
_CANONICAL_MONOTONIC_READ_DISPATCH_GUARD_CODE: Final = (
    _assert_monotonic_read_recovery_dispatch.__code__
)
_CANONICAL_MONOTONIC_RECOVER_HELPER: Final = _monotonic_recover
_CANONICAL_MONOTONIC_RECOVER_HELPER_CODE: Final = _monotonic_recover.__code__
_CANONICAL_MONOTONIC_READ_HISTORY_HELPER: Final = _monotonic_read_history
_CANONICAL_MONOTONIC_READ_HISTORY_HELPER_CODE: Final = _monotonic_read_history.__code__


def _assert_monotonic_read_helpers() -> None:
    if (
        _assert_monotonic_read_recovery_dispatch
        is not _CANONICAL_MONOTONIC_READ_DISPATCH_GUARD
        or _CANONICAL_MONOTONIC_READ_DISPATCH_GUARD.__code__
        is not _CANONICAL_MONOTONIC_READ_DISPATCH_GUARD_CODE
        or _monotonic_recover is not _CANONICAL_MONOTONIC_RECOVER_HELPER
        or _CANONICAL_MONOTONIC_RECOVER_HELPER.__code__
        is not _CANONICAL_MONOTONIC_RECOVER_HELPER_CODE
        or _monotonic_read_history
        is not _CANONICAL_MONOTONIC_READ_HISTORY_HELPER
        or _CANONICAL_MONOTONIC_READ_HISTORY_HELPER.__code__
        is not _CANONICAL_MONOTONIC_READ_HISTORY_HELPER_CODE
    ):
        raise DeploymentRuntimeAuthorityError(
            "runtime authority monotonic read helper dispatch was replaced"
        )


_CANONICAL_MONOTONIC_READ_HELPER_ASSERT: Final = _assert_monotonic_read_helpers
_CANONICAL_MONOTONIC_READ_HELPER_ASSERT_CODE: Final = (
    _assert_monotonic_read_helpers.__code__
)


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise DeploymentRuntimeAuthorityError(f"{name} must be canonical non-empty text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise DeploymentRuntimeAuthorityError(f"{name} must be valid UTF-8") from exc
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(character not in _HEX for character in text):
        raise DeploymentRuntimeAuthorityError(f"{name} must be lowercase SHA-256 hex")
    return text


_CANONICAL_SHA_VALIDATOR: Final = _sha
_CANONICAL_SHA_VALIDATOR_CODE: Final = _sha.__code__


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DeploymentRuntimeAuthorityError(f"{name} must be timezone-aware ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DeploymentRuntimeAuthorityError(f"{name} must be timezone-aware ISO-8601")
    return parsed.astimezone(timezone.utc)


def _timestamp(value: object, name: str) -> str:
    return _instant(value, name).isoformat().replace("+00:00", "Z")


_CANONICAL_TIMESTAMP_VALIDATOR: Final = _timestamp
_CANONICAL_TIMESTAMP_VALIDATOR_CODE: Final = _timestamp.__code__


def _utc_now_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


_CANONICAL_UTC_NOW_TIMESTAMP: Final = _utc_now_timestamp
_CANONICAL_UTC_NOW_TIMESTAMP_CODE: Final = _utc_now_timestamp.__code__


def _canonical_json(value: object) -> str:
    if (
        json is not _CANONICAL_JSON_MODULE
        or _CANONICAL_JSON_MODULE.dumps is not _CANONICAL_JSON_DUMPS
        or (
            _CANONICAL_JSON_DUMPS_CODE is not None
            and getattr(_CANONICAL_JSON_DUMPS, "__code__", None)
            is not _CANONICAL_JSON_DUMPS_CODE
        )
    ):
        raise DeploymentRuntimeAuthorityError(
            "runtime authority JSON serialization dispatch was replaced"
        )
    try:
        return _CANONICAL_JSON_DUMPS(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise DeploymentRuntimeAuthorityError("runtime authority is not canonical JSON") from exc


_CANONICAL_JSON_ENCODER: Final = _canonical_json
_CANONICAL_JSON_ENCODER_CODE: Final = _canonical_json.__code__


def _digest(value: object) -> str:
    if (
        hashlib is not _CANONICAL_HASHLIB_MODULE
        or _CANONICAL_HASHLIB_MODULE.sha256 is not _CANONICAL_HASHLIB_SHA256
        or (
            _CANONICAL_HASHLIB_SHA256_CODE is not None
            and getattr(_CANONICAL_HASHLIB_SHA256, "__code__", None)
            is not _CANONICAL_HASHLIB_SHA256_CODE
        )
        or _canonical_json is not _CANONICAL_JSON_ENCODER
        or _CANONICAL_JSON_ENCODER.__code__ is not _CANONICAL_JSON_ENCODER_CODE
    ):
        raise DeploymentRuntimeAuthorityError(
            "runtime authority digest dispatch was replaced"
        )
    return _CANONICAL_HASHLIB_SHA256(
        _CANONICAL_JSON_ENCODER(value).encode("utf-8")
    ).hexdigest()


_CANONICAL_DIGEST: Final = _digest
_CANONICAL_DIGEST_CODE: Final = _digest.__code__


def _fsync_directory(path: Path) -> None:
    if (
        os is not _CANONICAL_OS_MODULE
        or os.open is not _CANONICAL_OS_OPEN
        or os.fsync is not _CANONICAL_OS_FSYNC
        or os.close is not _CANONICAL_OS_CLOSE
    ):
        raise DeploymentRuntimeAuthorityError(
            "runtime authority durability dispatch was replaced"
        )
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = _CANONICAL_OS_OPEN(path, flags)
    try:
        _CANONICAL_OS_FSYNC(descriptor)
    finally:
        _CANONICAL_OS_CLOSE(descriptor)


_CANONICAL_FSYNC_DIRECTORY: Final = _fsync_directory
_CANONICAL_FSYNC_DIRECTORY_CODE: Final = _fsync_directory.__code__


def _durably_finalize_published_path(path: Path) -> None:
    if (
        os is not _CANONICAL_OS_MODULE
        or stat is not _CANONICAL_STAT_MODULE
        or os.open is not _CANONICAL_OS_OPEN
        or os.fstat is not _CANONICAL_OS_FSTAT
        or os.lstat is not _CANONICAL_OS_LSTAT
        or os.fsync is not _CANONICAL_OS_FSYNC
        or os.close is not _CANONICAL_OS_CLOSE
        or stat.S_ISREG is not _CANONICAL_STAT_ISREG
        or _fsync_directory is not _CANONICAL_FSYNC_DIRECTORY
        or _CANONICAL_FSYNC_DIRECTORY.__code__
        is not _CANONICAL_FSYNC_DIRECTORY_CODE
    ):
        raise DeploymentRuntimeAuthorityError(
            "runtime authority publication durability dispatch was replaced"
        )
    try:
        before = _CANONICAL_OS_LSTAT(path)
    except OSError as exc:
        raise DeploymentRuntimeAuthorityError(
            "cannot inspect published runtime authority path"
        ) from exc
    if not _CANONICAL_STAT_ISREG(before.st_mode) or before.st_nlink != 1:
        raise DeploymentRuntimeAuthorityError(
            "published runtime authority must be a single-link regular file"
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = _CANONICAL_OS_OPEN(path, flags)
    except OSError as exc:
        raise DeploymentRuntimeAuthorityError(
            "cannot open published runtime authority for durability"
        ) from exc
    try:
        opened = _CANONICAL_OS_FSTAT(descriptor)
        if (
            not _CANONICAL_STAT_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise DeploymentRuntimeAuthorityError(
                "published runtime authority path changed before durability sync"
            )
        _CANONICAL_OS_FSYNC(descriptor)
        after = _CANONICAL_OS_LSTAT(path)
        if (
            not _CANONICAL_STAT_ISREG(after.st_mode)
            or after.st_nlink != 1
            or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise DeploymentRuntimeAuthorityError(
                "published runtime authority path changed during durability sync"
            )
    finally:
        _CANONICAL_OS_CLOSE(descriptor)
    _CANONICAL_FSYNC_DIRECTORY(path.parent)


_CANONICAL_DURABLE_PUBLICATION_FINALIZER: Final = _durably_finalize_published_path
_CANONICAL_DURABLE_PUBLICATION_FINALIZER_CODE: Final = (
    _durably_finalize_published_path.__code__
)


def _environment_payload(environment: EnvironmentIdentity) -> dict[str, object]:
    if type(environment) is not _CANONICAL_ENVIRONMENT_IDENTITY_TYPE:
        raise DeploymentRuntimeAuthorityError(
            "environment must be exact EnvironmentIdentity"
        )
    return {
        "schema": environment.schema,
        "schema_version": environment.schema_version,
        "source_id": environment.source_id,
        "config_id": environment.config_id,
        "data_id": environment.data_id,
        "protocol_id": environment.protocol_id,
        "cutoff_ts": _timestamp(environment.cutoff_ts, "environment.cutoff_ts"),
        "seed": environment.seed,
        "environment_id": environment.environment_id,
    }


def _episode_payload(episode: Episode) -> dict[str, object]:
    if type(episode) is not _CANONICAL_EPISODE_TYPE:
        raise DeploymentRuntimeAuthorityError(
            "episode must be exact Episode"
        )
    return {
        "environment_id": episode.environment_id,
        "episode_key": episode.episode_key,
        "policy_id": episode.policy_id,
        "admissible_actions": list(episode.admissible_actions),
        "episode_id": episode.episode_id,
    }


def _action_semantics_payload(
    version: str,
    meanings: tuple[tuple[str, str], ...],
) -> dict[str, object]:
    canonical_version = _text(version, "action_semantics.version")
    if type(meanings) is not tuple or not meanings:
        raise DeploymentRuntimeAuthorityError("action semantics meanings must be non-empty tuple")
    normalized: list[tuple[str, str]] = []
    for index, entry in enumerate(meanings):
        if type(entry) is not tuple or len(entry) != 2:
            raise DeploymentRuntimeAuthorityError(
                f"action semantics meanings[{index}] must be a two-item tuple"
            )
        normalized.append(
            (
                _text(entry[0], f"action semantics action[{index}]"),
                _text(entry[1], f"action semantics meaning[{index}]"),
            )
        )
    if tuple(normalized) != tuple(sorted(normalized)):
        raise DeploymentRuntimeAuthorityError("action semantics meanings must be sorted")
    names = tuple(name for name, _meaning in normalized)
    if len(names) != len(set(names)):
        raise DeploymentRuntimeAuthorityError("action semantics action names must be unique")
    definition = {
        "schema": "autosport.action_semantics",
        "schema_version": 1,
        "version": canonical_version,
        "meanings": [[name, meaning] for name, meaning in normalized],
    }
    definition_sha256 = _digest(definition)
    action_semantics_id = _digest(
        {
            "schema": "autosport.action_semantics.identity",
            "schema_version": 1,
            "version": canonical_version,
            "definition_sha256": definition_sha256,
        }
    )
    return {
        "version": canonical_version,
        "meanings": [[name, meaning] for name, meaning in normalized],
        "definition_sha256": definition_sha256,
        "action_semantics_id": action_semantics_id,
    }


def _environment_from_payload(raw: object) -> EnvironmentIdentity:
    if type(raw) is not dict:
        raise DeploymentRuntimeAuthorityError("environment payload must be an object")
    if set(raw) != {
        "schema",
        "schema_version",
        "source_id",
        "config_id",
        "data_id",
        "protocol_id",
        "cutoff_ts",
        "seed",
        "environment_id",
    }:
        raise DeploymentRuntimeAuthorityError(
            "environment payload envelope is not canonical"
        )
    seed = raw.get("seed")
    schema_version = raw.get("schema_version")
    if type(seed) is not int or type(schema_version) is not int:
        raise DeploymentRuntimeAuthorityError("environment seed/schema_version must be integers")
    environment = EnvironmentIdentity(
        source_id=_text(raw.get("source_id"), "environment.source_id"),
        config_id=_text(raw.get("config_id"), "environment.config_id"),
        data_id=_text(raw.get("data_id"), "environment.data_id"),
        protocol_id=_text(raw.get("protocol_id"), "environment.protocol_id"),
        cutoff_ts=_timestamp(raw.get("cutoff_ts"), "environment.cutoff_ts"),
        seed=seed,
        schema=_text(raw.get("schema"), "environment.schema"),
        schema_version=schema_version,
    )
    if _sha(raw.get("environment_id"), "environment.environment_id") != environment.environment_id:
        raise DeploymentRuntimeAuthorityError("environment identity digest mismatch")
    return environment


def _episode_from_payload(raw: object) -> Episode:
    if type(raw) is not dict:
        raise DeploymentRuntimeAuthorityError("episode payload must be an object")
    if set(raw) != {
        "environment_id",
        "episode_key",
        "policy_id",
        "admissible_actions",
        "episode_id",
    }:
        raise DeploymentRuntimeAuthorityError(
            "episode payload envelope is not canonical"
        )
    actions = raw.get("admissible_actions")
    if type(actions) is not list:
        raise DeploymentRuntimeAuthorityError("episode admissible_actions must be a list")
    episode = Episode(
        environment_id=_sha(raw.get("environment_id"), "episode.environment_id"),
        episode_key=_text(raw.get("episode_key"), "episode.episode_key"),
        policy_id=_text(raw.get("policy_id"), "episode.policy_id"),
        admissible_actions=tuple(_text(action, "episode admissible action") for action in actions),
    )
    if _sha(raw.get("episode_id"), "episode.episode_id") != episode.episode_id:
        raise DeploymentRuntimeAuthorityError("episode identity digest mismatch")
    return episode


def _action_semantics_from_payload(raw: object) -> tuple[str, tuple[tuple[str, str], ...], str, str]:
    if type(raw) is not dict:
        raise DeploymentRuntimeAuthorityError("action semantics payload must be an object")
    if set(raw) != {
        "version",
        "meanings",
        "definition_sha256",
        "action_semantics_id",
    }:
        raise DeploymentRuntimeAuthorityError(
            "action semantics payload envelope is not canonical"
        )
    raw_meanings = raw.get("meanings")
    if type(raw_meanings) is not list:
        raise DeploymentRuntimeAuthorityError("action semantics meanings must be a list")
    meanings: list[tuple[str, str]] = []
    for index, entry in enumerate(raw_meanings):
        if type(entry) is not list or len(entry) != 2:
            raise DeploymentRuntimeAuthorityError(
                f"action semantics meanings[{index}] must contain two items"
            )
        meanings.append(
            (
                _text(entry[0], f"action semantics action[{index}]"),
                _text(entry[1], f"action semantics meaning[{index}]"),
            )
        )
    canonical = _action_semantics_payload(
        _text(raw.get("version"), "action_semantics.version"),
        tuple(meanings),
    )
    definition_sha256 = _sha(raw.get("definition_sha256"), "action_semantics.definition_sha256")
    action_semantics_id = _sha(raw.get("action_semantics_id"), "action_semantics.action_semantics_id")
    if definition_sha256 != canonical["definition_sha256"]:
        raise DeploymentRuntimeAuthorityError("action semantics definition digest mismatch")
    if action_semantics_id != canonical["action_semantics_id"]:
        raise DeploymentRuntimeAuthorityError("action semantics identity digest mismatch")
    return (
        canonical["version"],  # type: ignore[return-value]
        tuple(meanings),
        definition_sha256,
        action_semantics_id,
    )


@dataclass(frozen=True, slots=True)
class DeploymentRuntimeAuthorityRecord:
    runtime_authority_id: str
    available_at: str
    environment: EnvironmentIdentity
    episode: Episode
    action_semantics_version: str
    action_semantics_meanings: tuple[tuple[str, str], ...]
    action_semantics_definition_sha256: str
    action_semantics_id: str
    previous_record_sha256: str
    record_sha256: str

    def __post_init__(self) -> None:
        _sha(self.runtime_authority_id, "runtime_authority_id")
        _timestamp(self.available_at, "available_at")
        if type(self.environment) is not _CANONICAL_ENVIRONMENT_IDENTITY_TYPE:
            raise DeploymentRuntimeAuthorityError(
                "environment must be exact EnvironmentIdentity"
            )
        if type(self.episode) is not _CANONICAL_EPISODE_TYPE:
            raise DeploymentRuntimeAuthorityError(
                "episode must be exact Episode"
            )
        if self.episode.environment_id != self.environment.environment_id:
            raise DeploymentRuntimeAuthorityError("episode belongs to another environment")
        semantics = _action_semantics_payload(
            self.action_semantics_version,
            self.action_semantics_meanings,
        )
        if tuple(name for name, _meaning in self.action_semantics_meanings) != self.episode.admissible_actions:
            raise DeploymentRuntimeAuthorityError(
                "action semantics must cover the exact episode admissible action universe"
            )
        if _sha(
            self.action_semantics_definition_sha256,
            "action_semantics_definition_sha256",
        ) != semantics["definition_sha256"]:
            raise DeploymentRuntimeAuthorityError("action semantics definition digest mismatch")
        if _sha(self.action_semantics_id, "action_semantics_id") != semantics["action_semantics_id"]:
            raise DeploymentRuntimeAuthorityError("action semantics identity digest mismatch")
        _sha(self.previous_record_sha256, "previous_record_sha256")
        _sha(self.record_sha256, "record_sha256")
        if self.runtime_authority_id != self.computed_runtime_authority_id:
            raise DeploymentRuntimeAuthorityError("runtime authority identity digest mismatch")
        if self.record_sha256 != self.computed_record_sha256:
            raise DeploymentRuntimeAuthorityError("runtime authority record digest mismatch")

    @property
    def identity_payload(self) -> dict[str, object]:
        return {
            "schema": RECORD_SCHEMA,
            "schema_version": RECORD_SCHEMA_VERSION,
            "environment": _environment_payload(self.environment),
            "episode": _episode_payload(self.episode),
            "action_semantics": _action_semantics_payload(
                self.action_semantics_version,
                self.action_semantics_meanings,
            ),
        }

    @property
    def computed_runtime_authority_id(self) -> str:
        return _digest(self.identity_payload)

    @property
    def record_payload(self) -> dict[str, object]:
        return {
            **self.identity_payload,
            "runtime_authority_id": self.runtime_authority_id,
            "available_at": _timestamp(self.available_at, "available_at"),
            "previous_record_sha256": self.previous_record_sha256,
        }

    @property
    def computed_record_sha256(self) -> str:
        return _digest(self.record_payload)

    def to_dict(self) -> dict[str, object]:
        return {**self.record_payload, "record_sha256": self.record_sha256}

    @classmethod
    def create(
        cls,
        *,
        environment: EnvironmentIdentity,
        episode: Episode,
        action_semantics_version: str,
        action_semantics_meanings: tuple[tuple[str, str], ...],
        available_at: str,
        previous_record_sha256: str,
    ) -> "DeploymentRuntimeAuthorityRecord":
        semantics = _action_semantics_payload(
            action_semantics_version,
            action_semantics_meanings,
        )
        identity_payload = {
            "schema": RECORD_SCHEMA,
            "schema_version": RECORD_SCHEMA_VERSION,
            "environment": _environment_payload(environment),
            "episode": _episode_payload(episode),
            "action_semantics": semantics,
        }
        runtime_authority_id = _digest(identity_payload)
        record_payload = {
            **identity_payload,
            "runtime_authority_id": runtime_authority_id,
            "available_at": _timestamp(available_at, "available_at"),
            "previous_record_sha256": _sha(previous_record_sha256, "previous_record_sha256"),
        }
        return cls(
            runtime_authority_id=runtime_authority_id,
            available_at=record_payload["available_at"],  # type: ignore[arg-type]
            environment=environment,
            episode=episode,
            action_semantics_version=semantics["version"],  # type: ignore[arg-type]
            action_semantics_meanings=action_semantics_meanings,
            action_semantics_definition_sha256=semantics["definition_sha256"],  # type: ignore[arg-type]
            action_semantics_id=semantics["action_semantics_id"],  # type: ignore[arg-type]
            previous_record_sha256=record_payload["previous_record_sha256"],  # type: ignore[arg-type]
            record_sha256=_digest(record_payload),
        )

    @classmethod
    def from_dict(cls, raw: object) -> "DeploymentRuntimeAuthorityRecord":
        if type(raw) is not dict:
            raise DeploymentRuntimeAuthorityError("runtime authority record must be an object")
        if set(raw) != {
            "schema",
            "schema_version",
            "environment",
            "episode",
            "action_semantics",
            "runtime_authority_id",
            "available_at",
            "previous_record_sha256",
            "record_sha256",
        }:
            raise DeploymentRuntimeAuthorityError(
                "runtime authority record envelope is not canonical"
            )
        if raw.get("schema") != RECORD_SCHEMA or raw.get("schema_version") != RECORD_SCHEMA_VERSION:
            raise DeploymentRuntimeAuthorityError("unsupported runtime authority record schema")
        environment = _environment_from_payload(raw.get("environment"))
        episode = _episode_from_payload(raw.get("episode"))
        version, meanings, definition_sha256, action_semantics_id = _action_semantics_from_payload(
            raw.get("action_semantics")
        )
        return cls(
            runtime_authority_id=_sha(raw.get("runtime_authority_id"), "runtime_authority_id"),
            available_at=_timestamp(raw.get("available_at"), "available_at"),
            environment=environment,
            episode=episode,
            action_semantics_version=version,
            action_semantics_meanings=meanings,
            action_semantics_definition_sha256=definition_sha256,
            action_semantics_id=action_semantics_id,
            previous_record_sha256=_sha(
                raw.get("previous_record_sha256"), "previous_record_sha256"
            ),
            record_sha256=_sha(raw.get("record_sha256"), "record_sha256"),
        )


_CANONICAL_RECORD_TYPE: Final = DeploymentRuntimeAuthorityRecord
_CANONICAL_ENVIRONMENT_IDENTITY_TYPE: Final = EnvironmentIdentity
_CANONICAL_EPISODE_TYPE: Final = Episode
_CANONICAL_LEARNING_ENVIRONMENT_MODULE: Final = _learning_environment
_CANONICAL_LEARNING_ENVIRONMENT_SCHEMA: Final = _learning_environment.ENVIRONMENT_SCHEMA
_CANONICAL_LEARNING_ENVIRONMENT_SCHEMA_VERSION: Final = (
    _learning_environment.ENVIRONMENT_SCHEMA_VERSION
)
_CANONICAL_LEARNING_ENVIRONMENT_HASHLIB: Final = _learning_environment.hashlib
_CANONICAL_LEARNING_ENVIRONMENT_JSON: Final = _learning_environment.json
_CANONICAL_LEARNING_ENVIRONMENT_DATETIME: Final = _learning_environment.datetime
_CANONICAL_LEARNING_ENVIRONMENT_TIMEZONE: Final = _learning_environment.timezone
_CANONICAL_LEARNING_ENVIRONMENT_HELPERS: Final = tuple(
    (
        name,
        helper,
        helper.__code__,
    )
    for name in (
        "_canonical_text",
        "_timestamp",
        "_timestamp_identity",
        "_sha256_hex",
        "_stable_hash",
    )
    for helper in (getattr(_learning_environment, name),)
)
_CANONICAL_ENVIRONMENT_IDENTITY_SURFACE: Final = tuple(
    (
        name,
        descriptor,
        getattr(
            getattr(descriptor, "fget", descriptor),
            "__code__",
            None,
        ),
    )
    for name in ("__init__", "__post_init__", "environment_id")
    for descriptor in (vars(EnvironmentIdentity)[name],)
)
_CANONICAL_EPISODE_SURFACE: Final = tuple(
    (
        name,
        descriptor,
        getattr(
            getattr(descriptor, "fget", descriptor),
            "__code__",
            None,
        ),
    )
    for name in ("__init__", "__post_init__", "episode_id")
    for descriptor in (vars(Episode)[name],)
)
_CANONICAL_RECORD_HELPERS: Final = (
    ("_text", _text, _text.__code__),
    ("_sha", _sha, _sha.__code__),
    ("_instant", _instant, _instant.__code__),
    ("_timestamp", _timestamp, _timestamp.__code__),
    ("_digest", _digest, _digest.__code__),
    ("_environment_payload", _environment_payload, _environment_payload.__code__),
    ("_episode_payload", _episode_payload, _episode_payload.__code__),
    ("_action_semantics_payload", _action_semantics_payload, _action_semantics_payload.__code__),
    ("_environment_from_payload", _environment_from_payload, _environment_from_payload.__code__),
    ("_episode_from_payload", _episode_from_payload, _episode_from_payload.__code__),
    ("_action_semantics_from_payload", _action_semantics_from_payload, _action_semantics_from_payload.__code__),
)
_RECORD_CODEC_NAMES: Final = (
    "__init__",
    "__post_init__",
    "identity_payload",
    "computed_runtime_authority_id",
    "record_payload",
    "computed_record_sha256",
    "to_dict",
    "create",
    "from_dict",
)
_CANONICAL_RECORD_CODEC_DESCRIPTORS: Final = tuple(
    (
        name,
        descriptor,
        getattr(
            getattr(
                getattr(descriptor, "__func__", descriptor),
                "fget",
                getattr(descriptor, "__func__", descriptor),
            ),
            "__code__",
            None,
        ),
    )
    for name in _RECORD_CODEC_NAMES
    for descriptor in (vars(DeploymentRuntimeAuthorityRecord)[name],)
)


def _assert_canonical_record_codec(
    _record_type: type = DeploymentRuntimeAuthorityRecord,
    _environment_type: type = EnvironmentIdentity,
    _episode_type: type = Episode,
    _datetime_type: type = datetime,
    _timezone_module: object = timezone,
    _learning_environment_module: object = _learning_environment,
    _learning_environment_schema: str = _learning_environment.ENVIRONMENT_SCHEMA,
    _learning_environment_schema_version: int = (
        _learning_environment.ENVIRONMENT_SCHEMA_VERSION
    ),
    _learning_environment_hashlib: object = _learning_environment.hashlib,
    _learning_environment_json: object = _learning_environment.json,
    _learning_environment_datetime: object = _learning_environment.datetime,
    _learning_environment_timezone: object = _learning_environment.timezone,
    _learning_environment_helpers: tuple[tuple[str, object, object], ...] = (
        _CANONICAL_LEARNING_ENVIRONMENT_HELPERS
    ),
    _environment_surface: tuple[tuple[str, object, object], ...] = (
        _CANONICAL_ENVIRONMENT_IDENTITY_SURFACE
    ),
    _episode_surface: tuple[tuple[str, object, object], ...] = (
        _CANONICAL_EPISODE_SURFACE
    ),
    _record_helpers: tuple[tuple[str, object, object], ...] = (
        _CANONICAL_RECORD_HELPERS
    ),
    _record_codec_descriptors: tuple[tuple[str, object, object], ...] = (
        _CANONICAL_RECORD_CODEC_DESCRIPTORS
    ),
) -> None:
    if (
        DeploymentRuntimeAuthorityRecord is not _record_type
        or _CANONICAL_RECORD_TYPE is not _record_type
    ):
        raise DeploymentRuntimeAuthorityError(
            "runtime authority record type dispatch was replaced"
        )
    if (
        EnvironmentIdentity is not _environment_type
        or _CANONICAL_ENVIRONMENT_IDENTITY_TYPE is not _environment_type
        or Episode is not _episode_type
        or _CANONICAL_EPISODE_TYPE is not _episode_type
        or datetime is not _datetime_type
        or _CANONICAL_DATETIME_TYPE is not _datetime_type
        or timezone is not _timezone_module
        or _CANONICAL_TIMEZONE_MODULE is not _timezone_module
    ):
        raise DeploymentRuntimeAuthorityError(
            "runtime authority semantic type dispatch was replaced"
        )
    if (
        _learning_environment is not _learning_environment_module
        or _CANONICAL_LEARNING_ENVIRONMENT_MODULE is not _learning_environment_module
        or _learning_environment.ENVIRONMENT_SCHEMA != _learning_environment_schema
        or _CANONICAL_LEARNING_ENVIRONMENT_SCHEMA != _learning_environment_schema
        or _learning_environment.ENVIRONMENT_SCHEMA_VERSION
        != _learning_environment_schema_version
        or _CANONICAL_LEARNING_ENVIRONMENT_SCHEMA_VERSION
        != _learning_environment_schema_version
        or _learning_environment.hashlib is not _learning_environment_hashlib
        or _CANONICAL_LEARNING_ENVIRONMENT_HASHLIB
        is not _learning_environment_hashlib
        or _learning_environment.json is not _learning_environment_json
        or _CANONICAL_LEARNING_ENVIRONMENT_JSON is not _learning_environment_json
        or _learning_environment.datetime is not _learning_environment_datetime
        or _CANONICAL_LEARNING_ENVIRONMENT_DATETIME
        is not _learning_environment_datetime
        or _learning_environment.timezone is not _learning_environment_timezone
        or _CANONICAL_LEARNING_ENVIRONMENT_TIMEZONE
        is not _learning_environment_timezone
        or _CANONICAL_LEARNING_ENVIRONMENT_HELPERS
        is not _learning_environment_helpers
        or _CANONICAL_ENVIRONMENT_IDENTITY_SURFACE is not _environment_surface
        or _CANONICAL_EPISODE_SURFACE is not _episode_surface
        or _CANONICAL_RECORD_HELPERS is not _record_helpers
        or _CANONICAL_RECORD_CODEC_DESCRIPTORS is not _record_codec_descriptors
    ):
        raise DeploymentRuntimeAuthorityError(
            "runtime authority learning-environment dependency was replaced"
        )
    for name, expected_helper, expected_code in _learning_environment_helpers:
        current_helper = getattr(_learning_environment_module, name, None)
        if (
            current_helper is not expected_helper
            or getattr(current_helper, "__code__", None) is not expected_code
        ):
            raise DeploymentRuntimeAuthorityError(
                "runtime authority learning-environment helper was replaced"
            )
    for semantic_type, surface in (
        (_environment_type, _environment_surface),
        (_episode_type, _episode_surface),
    ):
        class_dict = vars(semantic_type)
        for name, expected_descriptor, expected_code in surface:
            current = class_dict.get(name)
            current_callable = getattr(current, "fget", current)
            if (
                current is not expected_descriptor
                or (
                    expected_code is not None
                    and getattr(current_callable, "__code__", None)
                    is not expected_code
                )
            ):
                raise DeploymentRuntimeAuthorityError(
                    "runtime authority semantic type surface was replaced"
                )
    namespace = globals()
    for name, expected_helper, expected_code in _record_helpers:
        current_helper = namespace.get(name)
        if (
            current_helper is not expected_helper
            or getattr(current_helper, "__code__", None) is not expected_code
        ):
            raise DeploymentRuntimeAuthorityError(
                "runtime authority record helper dispatch was replaced"
            )
    class_dict = vars(_record_type)
    for name, expected_descriptor, expected_code in _record_codec_descriptors:
        current = class_dict.get(name)
        current_callable = getattr(
            getattr(current, "__func__", current),
            "fget",
            getattr(current, "__func__", current),
        )
        if (
            current is not expected_descriptor
            or (
                expected_code is not None
                and getattr(current_callable, "__code__", None) is not expected_code
            )
        ):
            raise DeploymentRuntimeAuthorityError(
                "runtime authority record codec dispatch was replaced"
            )


_CANONICAL_RECORD_CODEC_GUARD: Final = _assert_canonical_record_codec
_CANONICAL_RECORD_CODEC_GUARD_CODE: Final = _assert_canonical_record_codec.__code__


def _require_canonical_record_codec(
    _guard: object = _assert_canonical_record_codec,
    _guard_code: object = _assert_canonical_record_codec.__code__,
    _guard_defaults: object = _assert_canonical_record_codec.__defaults__,
) -> None:
    if (
        _assert_canonical_record_codec is not _guard
        or _CANONICAL_RECORD_CODEC_GUARD is not _guard
        or getattr(_guard, "__code__", None) is not _guard_code
        or _CANONICAL_RECORD_CODEC_GUARD_CODE is not _guard_code
        or getattr(_guard, "__defaults__", None) is not _guard_defaults
    ):
        raise DeploymentRuntimeAuthorityError(
            "runtime authority record codec guard dispatch was replaced"
        )
    _guard()


_CANONICAL_RECORD_CODEC_REQUIREMENT: Final = _require_canonical_record_codec
_CANONICAL_RECORD_CODEC_REQUIREMENT_CODE: Final = _require_canonical_record_codec.__code__


_IMMUTABLE_STORE_ROOT_SURFACE: Final = frozenset(
    {
        "__new__",
        "__init__",
        "__getattribute__",
        "__setattr__",
        "__delattr__",
        "__slots__",
        "initialize_pristine",
        "path",
        "workspace",
        "_lock",
        "_authority",
        "_semantic_binding_sha256",
        "_binding_path",
        "_binding_workspace",
        "_binding_lock",
        "_binding_authority",
        "_binding_semantic_binding_sha256",
        "_binding_workspace_identity_binding",
        "_binding_authority_root_selection_binding",
        "_binding_authority_root_selection_context",
        "_binding_root_selection_store_root",
        "_WRITE_ONCE_AUTHORITY_BINDINGS",
        "_WRITE_ONCE_AUTHORITY_STORAGE_BINDINGS",
        "_CANONICAL_DISPATCH_EXPECTATIONS",
    }
)


class _DeploymentRuntimeAuthorityStoreMeta(type):
    def __setattr__(
        cls,
        name: str,
        value: object,
        _protected: frozenset[str] = _IMMUTABLE_STORE_ROOT_SURFACE,
    ) -> None:
        if name in _protected:
            raise TypeError(
                f"runtime authority store root surface is immutable: {name}"
            )
        super().__setattr__(name, value)

    def __delattr__(
        cls,
        name: str,
        _protected: frozenset[str] = _IMMUTABLE_STORE_ROOT_SURFACE,
    ) -> None:
        if name in _protected:
            raise TypeError(
                f"runtime authority store root surface is immutable: {name}"
            )
        super().__delattr__(name)


class DeploymentRuntimeAuthorityStore(metaclass=_DeploymentRuntimeAuthorityStoreMeta):
    """Rollback-resistant append-only runtime authority file.

    The local file remains the canonical domain payload and hash chain. The
    independent monotonic workspace authority stores only opaque state digests and
    therefore detects whole-file deletion or restoration of older, locally-valid
    bytes while its separate machine-state root survives.
    """

    # Keep an instance dictionary for deliberately injected crash/falsifier seams,
    # but never store authority-bearing bindings in it. Data descriptors backed by
    # slots take precedence over __dict__, so direct vars(store)/__dict__.update()
    # cannot bypass the write-once boundary.
    __slots__ = (
        "__dict__",
        "_binding_path",
        "_binding_workspace",
        "_binding_lock",
        "_binding_authority",
        "_binding_semantic_binding_sha256",
        "_binding_workspace_identity_binding",
        "_binding_authority_root_selection_binding",
        "_binding_authority_root_selection_context",
        "_binding_root_selection_store_root",
    )

    _WRITE_ONCE_AUTHORITY_BINDINGS: Final = frozenset(
        {
            "path",
            "workspace",
            "_lock",
            "_authority",
            "_semantic_binding_sha256",
        }
    )
    _WRITE_ONCE_AUTHORITY_STORAGE_BINDINGS: Final = frozenset(
        {
            "_binding_path",
            "_binding_workspace",
            "_binding_lock",
            "_binding_authority",
            "_binding_semantic_binding_sha256",
            "_binding_workspace_identity_binding",
            "_binding_authority_root_selection_binding",
            "_binding_authority_root_selection_context",
            "_binding_root_selection_store_root",
        }
    )

    def __getattribute__(self, name: str) -> object:
        if name in _SEALED_STORE_DISPATCH_NAMES:
            if type(self) is not DeploymentRuntimeAuthorityStore:
                raise TypeError(
                    "runtime authority store must be exact DeploymentRuntimeAuthorityStore"
                )
            expectation_root = vars(DeploymentRuntimeAuthorityStore).get(
                "_CANONICAL_DISPATCH_EXPECTATIONS"
            )
            if not isinstance(expectation_root, Mapping):
                raise DeploymentRuntimeAuthorityError(
                    "runtime authority store expectation root was replaced"
                )
            expected = expectation_root.get(name)
            current = vars(DeploymentRuntimeAuthorityStore).get(name)
            if expected is None:
                raise DeploymentRuntimeAuthorityError(
                    "runtime authority store method dispatch was replaced"
                )
            (
                expected_descriptor,
                expected_code,
                expected_defaults,
                expected_kwdefaults,
            ) = expected
            current_callable = getattr(current, "__func__", current)
            current_kwdefaults = getattr(current_callable, "__kwdefaults__", None) or {}
            if (
                current is not expected_descriptor
                or (
                    expected_code is not None
                    and getattr(current_callable, "__code__", None) is not expected_code
                )
                or getattr(current_callable, "__defaults__", None)
                is not expected_defaults
                or tuple(sorted(current_kwdefaults.items())) != expected_kwdefaults
            ):
                raise DeploymentRuntimeAuthorityError(
                    "runtime authority store method dispatch was replaced"
                )
            return expected_descriptor.__get__(
                self,
                DeploymentRuntimeAuthorityStore,
            )
        return object.__getattribute__(self, name)

    @property
    def path(self) -> Path:
        return object.__getattribute__(self, "_binding_path")

    @path.setter
    def path(self, value: Path) -> None:
        try:
            object.__getattribute__(self, "_binding_path")
        except AttributeError:
            object.__setattr__(self, "_binding_path", value)
            return
        raise AttributeError("deployment runtime authority bindings are write-once")

    @property
    def workspace(self) -> Path:
        return object.__getattribute__(self, "_binding_workspace")

    @workspace.setter
    def workspace(self, value: Path) -> None:
        try:
            object.__getattribute__(self, "_binding_workspace")
        except AttributeError:
            object.__setattr__(self, "_binding_workspace", value)
            return
        raise AttributeError("deployment runtime authority bindings are write-once")

    @property
    def _lock(self) -> Any:
        return object.__getattribute__(self, "_binding_lock")

    @_lock.setter
    def _lock(self, value: object) -> None:
        try:
            object.__getattribute__(self, "_binding_lock")
        except AttributeError:
            object.__setattr__(self, "_binding_lock", value)
            return
        raise AttributeError("deployment runtime authority bindings are write-once")

    @property
    def _authority(self) -> MonotonicWorkspaceAuthority:
        return object.__getattribute__(self, "_binding_authority")

    @_authority.setter
    def _authority(self, value: MonotonicWorkspaceAuthority) -> None:
        if type(value) is not _CANONICAL_MONOTONIC_AUTHORITY_TYPE:
            raise DeploymentRuntimeAuthorityError(
                "runtime authority binding must use canonical monotonic authority"
            )
        try:
            object.__getattribute__(self, "_binding_authority")
        except AttributeError:
            object.__setattr__(self, "_binding_authority", value)
            return
        raise AttributeError("deployment runtime authority bindings are write-once")

    @property
    def _semantic_binding_sha256(self) -> str:
        return object.__getattribute__(self, "_binding_semantic_binding_sha256")

    @_semantic_binding_sha256.setter
    def _semantic_binding_sha256(self, value: str) -> None:
        try:
            object.__getattribute__(self, "_binding_semantic_binding_sha256")
        except AttributeError:
            object.__setattr__(self, "_binding_semantic_binding_sha256", value)
            return
        raise AttributeError("deployment runtime authority bindings are write-once")

    def __setattr__(self, name: str, value: object) -> None:
        storage = (
            DeploymentRuntimeAuthorityStore._WRITE_ONCE_AUTHORITY_STORAGE_BINDINGS
        )
        if name in {
            "_WRITE_ONCE_AUTHORITY_BINDINGS",
            "_WRITE_ONCE_AUTHORITY_STORAGE_BINDINGS",
        } or name in storage:
            raise AttributeError(
                "deployment runtime authority bindings are write-once"
            )
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        write_once = DeploymentRuntimeAuthorityStore._WRITE_ONCE_AUTHORITY_BINDINGS
        storage = (
            DeploymentRuntimeAuthorityStore._WRITE_ONCE_AUTHORITY_STORAGE_BINDINGS
        )
        if name in {
            "_WRITE_ONCE_AUTHORITY_BINDINGS",
            "_WRITE_ONCE_AUTHORITY_STORAGE_BINDINGS",
        } or name in write_once or name in storage:
            raise AttributeError(
                "deployment runtime authority bindings are write-once"
            )
        object.__delattr__(self, name)

    @staticmethod
    def _assert_static_authority_contract(
        _store_schema: str = STORE_SCHEMA,
        _store_schema_version: int = STORE_SCHEMA_VERSION,
        _record_schema: str = RECORD_SCHEMA,
        _record_schema_version: int = RECORD_SCHEMA_VERSION,
        _empty_chain_sha256: str = _EMPTY_CHAIN_SHA256,
        _hex: frozenset[str] = _HEX,
        _authority_domain: str = _AUTHORITY_DOMAIN,
        _authority_binding_schema: str = _AUTHORITY_BINDING_SCHEMA,
        _authority_binding_schema_version: int = _AUTHORITY_BINDING_SCHEMA_VERSION,
        _monotonic_authority_id: str = MONOTONIC_AUTHORITY_ID,
        _authority_phase_type: type = AuthorityPhase,
        _recovery_disposition_type: type = RecoveryDisposition,
        _path_type: type = Path,
    ) -> None:
        if (
            STORE_SCHEMA != _store_schema
            or _CANONICAL_STORE_SCHEMA_VALUE != _store_schema
            or STORE_SCHEMA_VERSION != _store_schema_version
            or _CANONICAL_STORE_SCHEMA_VERSION_VALUE != _store_schema_version
            or RECORD_SCHEMA != _record_schema
            or _CANONICAL_RECORD_SCHEMA_VALUE != _record_schema
            or RECORD_SCHEMA_VERSION != _record_schema_version
            or _CANONICAL_RECORD_SCHEMA_VERSION_VALUE != _record_schema_version
            or _EMPTY_CHAIN_SHA256 != _empty_chain_sha256
            or _CANONICAL_EMPTY_CHAIN_SHA256_VALUE != _empty_chain_sha256
            or _HEX != _hex
            or _CANONICAL_HEX_VALUE != _hex
            or _AUTHORITY_DOMAIN != _authority_domain
            or _CANONICAL_AUTHORITY_DOMAIN_VALUE != _authority_domain
            or _AUTHORITY_BINDING_SCHEMA != _authority_binding_schema
            or _CANONICAL_AUTHORITY_BINDING_SCHEMA_VALUE
            != _authority_binding_schema
            or _AUTHORITY_BINDING_SCHEMA_VERSION
            != _authority_binding_schema_version
            or _CANONICAL_AUTHORITY_BINDING_SCHEMA_VERSION_VALUE
            != _authority_binding_schema_version
            or MONOTONIC_AUTHORITY_ID != _monotonic_authority_id
            or _CANONICAL_MONOTONIC_AUTHORITY_ID != _monotonic_authority_id
            or AuthorityPhase is not _authority_phase_type
            or _CANONICAL_AUTHORITY_PHASE_TYPE is not _authority_phase_type
            or RecoveryDisposition is not _recovery_disposition_type
            or _CANONICAL_RECOVERY_DISPOSITION_TYPE
            is not _recovery_disposition_type
            or Path is not _path_type
            or _CANONICAL_PATH_TYPE is not _path_type
        ):
            raise DeploymentRuntimeAuthorityError(
                "runtime authority static contract was replaced"
            )

    def _configure(
        self,
        path: str | Path,
        *,
        authority_root: str | Path | None,
        _digest_helper: object = _digest,
        _digest_helper_code: object = _digest.__code__,
        _sha_validator: object = _sha,
        _sha_validator_code: object = _sha.__code__,
        _new_local_lock_helper: object = _new_local_lock,
        _new_local_lock_helper_code: object = _new_local_lock.__code__,
        _workspace_lock_helper: object = _workspace_economic_lock,
        _workspace_lock_helper_code: object = _workspace_economic_lock.__code__,
        _monotonic_constructor_helper: object = _construct_monotonic_authority,
        _monotonic_constructor_helper_code: object = _construct_monotonic_authority.__code__,
        _path_expanduser: object = _CANONICAL_PATH_EXPANDUSER,
        _path_expanduser_code: object = _CANONICAL_PATH_EXPANDUSER_CODE,
        _path_resolve: object = _CANONICAL_PATH_RESOLVE,
        _path_resolve_code: object = _CANONICAL_PATH_RESOLVE_CODE,
        _path_read_text: object = _CANONICAL_PATH_READ_TEXT,
        _path_read_text_code: object = _CANONICAL_PATH_READ_TEXT_CODE,
    ) -> None:
        self._assert_static_authority_contract()
        if (
            _digest is not _digest_helper
            or _CANONICAL_DIGEST is not _digest_helper
            or getattr(_digest_helper, "__code__", None) is not _digest_helper_code
            or _CANONICAL_DIGEST_CODE is not _digest_helper_code
            or _sha is not _sha_validator
            or _CANONICAL_SHA_VALIDATOR is not _sha_validator
            or getattr(_sha_validator, "__code__", None) is not _sha_validator_code
            or _CANONICAL_SHA_VALIDATOR_CODE is not _sha_validator_code
            or _new_local_lock is not _new_local_lock_helper
            or _CANONICAL_NEW_LOCAL_LOCK_HELPER is not _new_local_lock_helper
            or getattr(_new_local_lock_helper, "__code__", None)
            is not _new_local_lock_helper_code
            or _CANONICAL_NEW_LOCAL_LOCK_HELPER_CODE
            is not _new_local_lock_helper_code
            or _workspace_economic_lock is not _workspace_lock_helper
            or _CANONICAL_WORKSPACE_LOCK_HELPER is not _workspace_lock_helper
            or getattr(_workspace_lock_helper, "__code__", None)
            is not _workspace_lock_helper_code
            or _CANONICAL_WORKSPACE_LOCK_HELPER_CODE
            is not _workspace_lock_helper_code
            or _construct_monotonic_authority is not _monotonic_constructor_helper
            or _CANONICAL_MONOTONIC_CONSTRUCTOR_HELPER
            is not _monotonic_constructor_helper
            or getattr(_monotonic_constructor_helper, "__code__", None)
            is not _monotonic_constructor_helper_code
            or _CANONICAL_MONOTONIC_CONSTRUCTOR_HELPER_CODE
            is not _monotonic_constructor_helper_code
            or _path_expanduser is not _path_expanduser
            or _CANONICAL_PATH_EXPANDUSER_CODE is not _path_expanduser_code
            or _path_resolve is not _path_resolve
            or _CANONICAL_PATH_RESOLVE_CODE is not _path_resolve_code
            or _path_read_text is not _path_read_text
            or _CANONICAL_PATH_READ_TEXT_CODE is not _path_read_text_code
        ):
            raise DeploymentRuntimeAuthorityError(
                "runtime authority construction helper dispatch was replaced"
            )
        candidate_path = _CANONICAL_PATH_TYPE(path)
        current_path_type = type(candidate_path)
        if current_path_type is not _CANONICAL_CONCRETE_PATH_TYPE:
            raise DeploymentRuntimeAuthorityError(
                "runtime authority concrete path type was replaced"
            )
        for current, expected, expected_code in (
            (
                current_path_type.expanduser,
                _path_expanduser,
                _path_expanduser_code,
            ),
            (
                current_path_type.resolve,
                _path_resolve,
                _path_resolve_code,
            ),
            (
                current_path_type.read_text,
                _path_read_text,
                _path_read_text_code,
            ),
        ):
            if (
                current is not expected
                or (
                    expected_code is not None
                    and getattr(current, "__code__", None) is not expected_code
                )
            ):
                raise DeploymentRuntimeAuthorityError(
                    "runtime authority path dispatch was replaced"
                )
        self.path = _path_resolve(
            _path_expanduser(candidate_path),
            strict=False,
        )
        self.workspace = self.path.parent
        self._lock = _new_local_lock_helper()
        self._authority = _monotonic_constructor_helper(
            workspace=self.workspace,
            domain=_AUTHORITY_DOMAIN,
            key=self.path.name,
            authority_root=authority_root,
        )
        object.__setattr__(
            self,
            "_binding_workspace_identity_binding",
            self._authority.workspace_binding,
        )
        object.__setattr__(
            self,
            "_binding_authority_root_selection_binding",
            self._authority.authority_root_selection,
        )
        object.__setattr__(
            self,
            "_binding_authority_root_selection_context",
            self._authority.authority_root_selection.context,
        )
        object.__setattr__(
            self,
            "_binding_root_selection_store_root",
            self._authority.authority_root_selection.context.store_root,
        )
        self._semantic_binding_sha256 = _digest_helper(
            {
                "schema": _AUTHORITY_BINDING_SCHEMA,
                "schema_version": _AUTHORITY_BINDING_SCHEMA_VERSION,
                "workspace_instance_id": self._authority.workspace_instance_id,
                "store_schema": STORE_SCHEMA,
                "store_schema_version": STORE_SCHEMA_VERSION,
                "key": self.path.name,
            }
        )

    def __init__(
        self,
        path: str | Path,
        *,
        authority_root: str | Path | None = None,
    ) -> None:
        if type(self) is not DeploymentRuntimeAuthorityStore:
            raise TypeError(
                "runtime authority store must be exact DeploymentRuntimeAuthorityStore"
            )
        self._configure(path, authority_root=authority_root)
        self._assert_binding_integrity()
        with self._lock, _workspace_economic_lock(self.workspace):
            self._read_validated_records_locked()

    @classmethod
    def initialize_pristine(
        cls,
        path: str | Path,
        *,
        authority_root: str | Path | None = None,
    ) -> "DeploymentRuntimeAuthorityStore":
        if cls is not DeploymentRuntimeAuthorityStore:
            raise TypeError(
                "runtime authority store must be exact DeploymentRuntimeAuthorityStore"
            )
        if Path is not _CANONICAL_PATH_TYPE:
            raise DeploymentRuntimeAuthorityError(
                "runtime authority static contract was replaced"
            )
        candidate_path = _CANONICAL_PATH_TYPE(path)
        current_path_type = type(candidate_path)
        if current_path_type is not _CANONICAL_CONCRETE_PATH_TYPE:
            raise DeploymentRuntimeAuthorityError(
                "runtime authority concrete path type was replaced"
            )
        for current, expected, expected_code in (
            (
                current_path_type.expanduser,
                _CANONICAL_PATH_EXPANDUSER,
                _CANONICAL_PATH_EXPANDUSER_CODE,
            ),
            (
                current_path_type.resolve,
                _CANONICAL_PATH_RESOLVE,
                _CANONICAL_PATH_RESOLVE_CODE,
            ),
            (
                current_path_type.read_text,
                _CANONICAL_PATH_READ_TEXT,
                _CANONICAL_PATH_READ_TEXT_CODE,
            ),
        ):
            if (
                current is not expected
                or (
                    expected_code is not None
                    and getattr(current, "__code__", None) is not expected_code
                )
            ):
                raise DeploymentRuntimeAuthorityError(
                    "runtime authority path dispatch was replaced"
                )
        destination = _CANONICAL_PATH_RESOLVE(
            _CANONICAL_PATH_EXPANDUSER(candidate_path),
            strict=False,
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": STORE_SCHEMA,
            "schema_version": STORE_SCHEMA_VERSION,
            "records": [],
        }
        with _workspace_economic_lock(destination.parent):
            if destination.exists():
                raise DeploymentRuntimeAuthorityError(
                    "runtime authority store already exists"
                )

            store = _CANONICAL_OBJECT_NEW(cls)
            store._configure(destination, authority_root=authority_root)
            store._assert_binding_integrity()
            _CANONICAL_MONOTONIC_READ_HELPER_ASSERT()
            recovery = _CANONICAL_MONOTONIC_RECOVER_HELPER(
                store._authority,
                observed_state_sha256=None,
            )
            if recovery.committed_state_sha256 is not None:
                raise DeploymentRuntimeAuthorityError(
                    "runtime authority workspace is not pristine"
                )

            intended_sha256 = store._state_sha256(payload)
            tx_id = store._new_transaction_id()
            store._authority.prepare(
                tx_id=tx_id,
                observed_state_sha256=None,
                intended_state_sha256=intended_sha256,
                semantic_binding_sha256=store._semantic_binding_sha256,
            )
            store._write_atomic_path(destination, payload)
            store._finalize_published_path(destination)

            published = store._read_payload()
            store._records_from_payload(published)
            published_sha256 = store._state_sha256(published)
            if published_sha256 != intended_sha256:
                raise DeploymentRuntimeAuthorityError(
                    "published pristine runtime authority state does not match prepared digest"
                )
            store._authority.commit(
                tx_id=tx_id,
                observed_state_sha256=published_sha256,
                semantic_binding_sha256=store._semantic_binding_sha256,
            )
            committed_records = store._read_validated_records_locked()
            if committed_records:
                raise DeploymentRuntimeAuthorityError(
                    "pristine runtime authority gained unexpected records"
                )
            return store

    @staticmethod
    def _write_atomic_path(path: Path, payload: Mapping[str, object]) -> None:
        raw = _canonical_json(payload) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_name = handle.name
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
            _fsync_directory(path.parent)
        finally:
            if temporary_name is not None:
                try:
                    Path(temporary_name).unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _finalize_published_path(
        path: Path,
        _finalizer: object = _durably_finalize_published_path,
        _finalizer_code: object = _durably_finalize_published_path.__code__,
    ) -> None:
        if (
            _durably_finalize_published_path is not _finalizer
            or _CANONICAL_DURABLE_PUBLICATION_FINALIZER is not _finalizer
            or getattr(_finalizer, "__code__", None) is not _finalizer_code
            or _CANONICAL_DURABLE_PUBLICATION_FINALIZER_CODE
            is not _finalizer_code
        ):
            raise DeploymentRuntimeAuthorityError(
                "runtime authority publication finalizer dispatch was replaced"
            )
        _finalizer(path)

    @staticmethod
    def _state_sha256(
        payload: Mapping[str, object],
        _hashlib_module: object = hashlib,
        _sha256: object = hashlib.sha256,
        _sha256_code: object = getattr(hashlib.sha256, "__code__", None),
        _json_encoder: object = _canonical_json,
        _json_encoder_code: object = _canonical_json.__code__,
    ) -> str:
        if (
            hashlib is not _hashlib_module
            or _CANONICAL_HASHLIB_MODULE is not _hashlib_module
            or getattr(_hashlib_module, "sha256", None) is not _sha256
            or _CANONICAL_HASHLIB_SHA256 is not _sha256
            or (
                _sha256_code is not None
                and getattr(_sha256, "__code__", None) is not _sha256_code
            )
            or _CANONICAL_HASHLIB_SHA256_CODE is not _sha256_code
            or _canonical_json is not _json_encoder
            or _CANONICAL_JSON_ENCODER is not _json_encoder
            or getattr(_json_encoder, "__code__", None) is not _json_encoder_code
            or _CANONICAL_JSON_ENCODER_CODE is not _json_encoder_code
        ):
            raise DeploymentRuntimeAuthorityError(
                "runtime authority state-digest dispatch was replaced"
            )
        return _sha256(
            (_json_encoder(payload) + "\n").encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _new_transaction_id(
        _uuid_module: object = uuid,
        _uuid4: object = uuid.uuid4,
        _uuid4_code: object = getattr(uuid.uuid4, "__code__", None),
    ) -> str:
        if (
            uuid is not _uuid_module
            or _CANONICAL_UUID_MODULE is not _uuid_module
            or getattr(_uuid_module, "uuid4", None) is not _uuid4
            or _CANONICAL_UUID4 is not _uuid4
            or (
                _uuid4_code is not None
                and getattr(_uuid4, "__code__", None) is not _uuid4_code
            )
            or _CANONICAL_UUID4_CODE is not _uuid4_code
        ):
            raise DeploymentRuntimeAuthorityError(
                "runtime authority transaction-id dispatch was replaced"
            )
        return f"deployment-runtime-{_uuid4().hex}"

    def _read_payload(self) -> dict[str, object]:
        current_read_text = getattr(type(self.path), "read_text", None)
        if (
            type(self.path) is not _CANONICAL_CONCRETE_PATH_TYPE
            or current_read_text is not _CANONICAL_PATH_READ_TEXT
            or (
                _CANONICAL_PATH_READ_TEXT_CODE is not None
                and getattr(current_read_text, "__code__", None)
                is not _CANONICAL_PATH_READ_TEXT_CODE
            )
        ):
            raise DeploymentRuntimeAuthorityError(
                "runtime authority path read dispatch was replaced"
            )
        try:
            raw = _CANONICAL_PATH_READ_TEXT(self.path, encoding="utf-8")
        except FileNotFoundError as exc:
            _CANONICAL_MONOTONIC_READ_HELPER_ASSERT()
            _CANONICAL_MONOTONIC_RECOVER_HELPER(
                self._authority,
                observed_state_sha256=None,
            )
            raise DeploymentRuntimeAuthorityError(
                "cannot read runtime authority store"
            ) from exc
        except OSError as exc:
            raise DeploymentRuntimeAuthorityError(
                "cannot read runtime authority store"
            ) from exc
        if not raw.endswith("\n"):
            raise DeploymentRuntimeAuthorityError(
                "runtime authority store is not canonical text"
            )
        if (
            json is not _CANONICAL_JSON_MODULE
            or _CANONICAL_JSON_MODULE.loads is not _CANONICAL_JSON_LOADS
            or (
                _CANONICAL_JSON_LOADS_CODE is not None
                and getattr(_CANONICAL_JSON_LOADS, "__code__", None)
                is not _CANONICAL_JSON_LOADS_CODE
            )
            or _canonical_json is not _CANONICAL_JSON_ENCODER
            or _CANONICAL_JSON_ENCODER.__code__ is not _CANONICAL_JSON_ENCODER_CODE
        ):
            raise DeploymentRuntimeAuthorityError(
                "runtime authority JSON parsing dispatch was replaced"
            )
        try:
            payload = _CANONICAL_JSON_LOADS(raw)
        except _CANONICAL_JSON_DECODE_ERROR as exc:
            raise DeploymentRuntimeAuthorityError(
                "runtime authority store is not valid JSON"
            ) from exc
        if type(payload) is not dict:
            raise DeploymentRuntimeAuthorityError(
                "runtime authority store must be an object"
            )
        if set(payload) != {"schema", "schema_version", "records"}:
            raise DeploymentRuntimeAuthorityError(
                "runtime authority store envelope is not canonical"
            )
        if (
            payload.get("schema") != STORE_SCHEMA
            or payload.get("schema_version") != STORE_SCHEMA_VERSION
        ):
            raise DeploymentRuntimeAuthorityError(
                "unsupported runtime authority store schema"
            )
        if _CANONICAL_JSON_ENCODER(payload) + "\n" != raw:
            raise DeploymentRuntimeAuthorityError(
                "runtime authority store is not canonical JSON"
            )
        records = payload.get("records")
        if type(records) is not list:
            raise DeploymentRuntimeAuthorityError(
                "runtime authority records must be a list"
            )
        return payload

    @staticmethod
    def _records_from_payload(
        payload: Mapping[str, object],
    ) -> tuple[DeploymentRuntimeAuthorityRecord, ...]:
        if (
            _require_canonical_record_codec is not _CANONICAL_RECORD_CODEC_REQUIREMENT
            or _CANONICAL_RECORD_CODEC_REQUIREMENT.__code__
            is not _CANONICAL_RECORD_CODEC_REQUIREMENT_CODE
        ):
            raise DeploymentRuntimeAuthorityError(
                "runtime authority record codec requirement dispatch was replaced"
            )
        _CANONICAL_RECORD_CODEC_REQUIREMENT()
        records_raw = payload["records"]
        assert isinstance(records_raw, list)
        previous = _EMPTY_CHAIN_SHA256
        previous_available_at: datetime | None = None
        seen_ids: set[str] = set()
        records: list[DeploymentRuntimeAuthorityRecord] = []
        for raw in records_raw:
            record = DeploymentRuntimeAuthorityRecord.from_dict(raw)
            if record.previous_record_sha256 != previous:
                raise DeploymentRuntimeAuthorityError(
                    "runtime authority hash chain is broken"
                )
            current_available_at = _instant(record.available_at, "available_at")
            if (
                previous_available_at is not None
                and current_available_at < previous_available_at
            ):
                raise DeploymentRuntimeAuthorityError(
                    "runtime authority first-seen time moved backwards"
                )
            if record.runtime_authority_id in seen_ids:
                raise DeploymentRuntimeAuthorityError(
                    "duplicate runtime authority identity"
                )
            seen_ids.add(record.runtime_authority_id)
            records.append(record)
            previous = record.record_sha256
            previous_available_at = current_available_at
        return tuple(records)

    def _assert_binding_integrity(
        self,
        _monotonic_read_guard: object = _assert_monotonic_read_helpers,
        _monotonic_read_guard_code: object = _assert_monotonic_read_helpers.__code__,
        _record_codec_requirement: object = _require_canonical_record_codec,
        _record_codec_requirement_code: object = _require_canonical_record_codec.__code__,
        _record_codec_requirement_defaults: object = (
            _require_canonical_record_codec.__defaults__
        ),
        _sha_validator: object = _sha,
        _sha_validator_code: object = _sha.__code__,
        _digest_helper: object = _digest,
        _digest_helper_code: object = _digest.__code__,
    ) -> None:
        self._assert_static_authority_contract()
        if (
            _assert_monotonic_read_helpers is not _monotonic_read_guard
            or _CANONICAL_MONOTONIC_READ_HELPER_ASSERT is not _monotonic_read_guard
            or getattr(_monotonic_read_guard, "__code__", None)
            is not _monotonic_read_guard_code
            or _CANONICAL_MONOTONIC_READ_HELPER_ASSERT_CODE
            is not _monotonic_read_guard_code
        ):
            raise DeploymentRuntimeAuthorityError(
                "runtime authority monotonic read helper guard was replaced"
            )
        _monotonic_read_guard()
        if (
            _require_canonical_record_codec is not _record_codec_requirement
            or _CANONICAL_RECORD_CODEC_REQUIREMENT is not _record_codec_requirement
            or getattr(_record_codec_requirement, "__code__", None)
            is not _record_codec_requirement_code
            or _CANONICAL_RECORD_CODEC_REQUIREMENT_CODE
            is not _record_codec_requirement_code
            or getattr(_record_codec_requirement, "__defaults__", None)
            is not _record_codec_requirement_defaults
        ):
            raise DeploymentRuntimeAuthorityError(
                "runtime authority record codec requirement dispatch was replaced"
            )
        _record_codec_requirement()
        if (
            _sha is not _sha_validator
            or _CANONICAL_SHA_VALIDATOR is not _sha_validator
            or getattr(_sha_validator, "__code__", None) is not _sha_validator_code
            or _CANONICAL_SHA_VALIDATOR_CODE is not _sha_validator_code
            or _digest is not _digest_helper
            or _CANONICAL_DIGEST is not _digest_helper
            or getattr(_digest_helper, "__code__", None) is not _digest_helper_code
            or _CANONICAL_DIGEST_CODE is not _digest_helper_code
        ):
            raise DeploymentRuntimeAuthorityError(
                "runtime authority binding validator dispatch was replaced"
            )
        path = object.__getattribute__(self, "_binding_path")
        workspace = object.__getattribute__(self, "_binding_workspace")
        authority = object.__getattribute__(self, "_binding_authority")
        semantic_binding_sha256 = object.__getattribute__(
            self,
            "_binding_semantic_binding_sha256",
        )
        captured_workspace_binding = object.__getattribute__(
            self,
            "_binding_workspace_identity_binding",
        )
        captured_root_selection = object.__getattribute__(
            self,
            "_binding_authority_root_selection_binding",
        )
        captured_root_context = object.__getattribute__(
            self,
            "_binding_authority_root_selection_context",
        )
        captured_root_store = object.__getattribute__(
            self,
            "_binding_root_selection_store_root",
        )
        lock = object.__getattribute__(self, "_binding_lock")
        if (
            type(path) is not _CANONICAL_CONCRETE_PATH_TYPE
            or type(workspace) is not _CANONICAL_CONCRETE_PATH_TYPE
            or type(lock) is not _CANONICAL_RLOCK_TYPE
            or getattr(type(path), "read_text", None) is not _CANONICAL_PATH_READ_TEXT
            or getattr(type(path), "resolve", None) is not _CANONICAL_PATH_RESOLVE
        ):
            raise DeploymentRuntimeAuthorityError(
                "runtime authority binding integrity mismatch"
            )
        if type(authority) is not _CANONICAL_MONOTONIC_AUTHORITY_TYPE:
            raise DeploymentRuntimeAuthorityError(
                "runtime authority binding integrity mismatch"
            )
        workspace_binding = authority.workspace_binding
        root_selection = authority.authority_root_selection
        if (
            workspace_binding is not captured_workspace_binding
            or root_selection is not captured_root_selection
            or root_selection.context is not captured_root_context
            or root_selection.context.store_root != captured_root_store
            or type(workspace_binding) is not _CANONICAL_WORKSPACE_IDENTITY_BINDING_TYPE
            or type(root_selection)
            is not _CANONICAL_AUTHORITY_ROOT_SELECTION_BINDING_TYPE
        ):
            raise DeploymentRuntimeAuthorityError(
                "runtime authority nested binding type mismatch"
            )
        for binding_type, surface in (
            (
                _CANONICAL_WORKSPACE_IDENTITY_BINDING_TYPE,
                _CANONICAL_WORKSPACE_BINDING_SURFACE,
            ),
            (
                _CANONICAL_AUTHORITY_ROOT_SELECTION_BINDING_TYPE,
                _CANONICAL_ROOT_SELECTION_SURFACE,
            ),
        ):
            class_dict = vars(binding_type)
            for name, expected_descriptor, expected_code in surface:
                current = class_dict.get(name)
                if (
                    current is not expected_descriptor
                    or (
                        expected_code is not None
                        and getattr(current, "__code__", None) is not expected_code
                    )
                ):
                    raise DeploymentRuntimeAuthorityError(
                        "runtime authority nested binding dispatch was replaced"
                    )

        if (
            os is not _CANONICAL_OS_MODULE
            or os.path.normcase is not _CANONICAL_OS_PATH_NORMCASE
            or os.path.normpath is not _CANONICAL_OS_PATH_NORMPATH
        ):
            raise DeploymentRuntimeAuthorityError(
                "runtime authority path-normalization dispatch was replaced"
            )
        expected_workspace_locator = _CANONICAL_OS_PATH_NORMCASE(
            _CANONICAL_OS_PATH_NORMPATH(str(workspace))
        )
        expected_workspace_locator_sha256 = _CANONICAL_HASHLIB_SHA256(
            expected_workspace_locator.encode("utf-8")
        ).hexdigest()
        expected_workspace_path_binding = (
            authority.authority_root
            / "workspace-bindings"
            / expected_workspace_locator_sha256[:2]
            / f"{expected_workspace_locator_sha256}.json"
        )
        expected_root_locator = _CANONICAL_OS_PATH_NORMCASE(
            _CANONICAL_OS_PATH_NORMPATH(str(authority.authority_root))
        )
        expected_root_resolved = _CANONICAL_OS_PATH_NORMCASE(
            _CANONICAL_OS_PATH_NORMPATH(
                str(_CANONICAL_PATH_RESOLVE(authority.authority_root, strict=False))
            )
        )
        expected_root_locator_sha256 = _CANONICAL_HASHLIB_SHA256(
            expected_root_locator.encode("utf-8")
        ).hexdigest()
        expected_root_resolved_sha256 = _CANONICAL_HASHLIB_SHA256(
            expected_root_resolved.encode("utf-8")
        ).hexdigest()

        namespace_material = "\0".join(
            (
                _CANONICAL_MONOTONIC_AUTHORITY_ID,
                authority.workspace_instance_id,
                authority.domain,
                authority.key,
            )
        ).encode("utf-8")
        expected_namespace_sha256 = _CANONICAL_HASHLIB_SHA256(
            namespace_material
        ).hexdigest()
        expected_journal_dir = (
            authority.authority_root
            / "journals"
            / expected_namespace_sha256[:2]
            / expected_namespace_sha256
        )
        expected_records_dir = expected_journal_dir / "records"
        expected_namespace_marker_path = (
            authority.authority_root
            / "namespace-bindings"
            / expected_namespace_sha256[:2]
            / f"{expected_namespace_sha256}.json"
        )
        expected_activation_path = (
            root_selection.context.store_root
            / "namespace-activations"
            / expected_namespace_sha256[:2]
            / f"{expected_namespace_sha256}.json"
        )

        if (
            path.parent != workspace
            or authority.workspace != workspace
            or authority.domain != _AUTHORITY_DOMAIN
            or authority.key != path.name
            or workspace_binding.workspace != workspace
            or workspace_binding.workspace_instance_id
            != authority.workspace_instance_id
            or workspace_binding.authority_root != authority.authority_root
            or workspace_binding.workspace_locator != expected_workspace_locator
            or workspace_binding.workspace_locator_sha256
            != expected_workspace_locator_sha256
            or workspace_binding.path_binding_path
            != expected_workspace_path_binding
            or authority.workspace_binding_path
            != workspace_binding.workspace_marker_path
            or root_selection.workspace != workspace
            or root_selection.workspace_instance_id
            != authority.workspace_instance_id
            or root_selection.authority_root != authority.authority_root
            or root_selection.context.workspace_locator
            != expected_workspace_locator
            or root_selection.context.workspace_locator_sha256
            != expected_workspace_locator_sha256
            or root_selection.context.authority_root_locator
            != expected_root_locator
            or root_selection.context.authority_root_locator_sha256
            != expected_root_locator_sha256
            or root_selection.context.authority_root_resolved
            != expected_root_resolved
            or root_selection.context.authority_root_resolved_sha256
            != expected_root_resolved_sha256
            or root_selection.context.binding_path
            != (
                root_selection.context.store_root
                / "workspace-path-bindings"
                / expected_workspace_locator_sha256[:2]
                / f"{expected_workspace_locator_sha256}.json"
            )
            or authority.authority_root_binding_path
            != root_selection.context.binding_path
            or authority.namespace_sha256 != expected_namespace_sha256
            or authority.journal_dir != expected_journal_dir
            or authority.records_dir != expected_records_dir
            or authority.namespace_marker_path != expected_namespace_marker_path
            or authority.authority_root_activation_path != expected_activation_path
        ):
            raise DeploymentRuntimeAuthorityError(
                "runtime authority binding integrity mismatch"
            )
        expected_semantic_binding_sha256 = _digest_helper(
            {
                "schema": _AUTHORITY_BINDING_SCHEMA,
                "schema_version": _AUTHORITY_BINDING_SCHEMA_VERSION,
                "workspace_instance_id": authority.workspace_instance_id,
                "store_schema": STORE_SCHEMA,
                "store_schema_version": STORE_SCHEMA_VERSION,
                "key": path.name,
            }
        )
        if (
            _sha_validator(
                semantic_binding_sha256,
                "runtime authority semantic binding",
            )
            != expected_semantic_binding_sha256
        ):
            raise DeploymentRuntimeAuthorityError(
                "runtime authority binding integrity mismatch"
            )

    def _recover_state(
        self,
        payload: Mapping[str, object],
    ) -> None:
        self._assert_binding_integrity()
        state_sha256 = self._state_sha256(payload)
        _CANONICAL_MONOTONIC_READ_HELPER_ASSERT()
        history = _CANONICAL_MONOTONIC_READ_HISTORY_HELPER(self._authority)
        if any(
            record.semantic_binding_sha256 != self._semantic_binding_sha256
            for record in history
        ):
            raise DeploymentRuntimeAuthorityError(
                "runtime authority semantic binding history mismatch"
            )
        latest = history[-1] if history else None
        pending_tx_id = (
            latest.tx_id
            if latest is not None
            and latest.phase is AuthorityPhase.PREPARE
            and latest.intended_state_sha256 == state_sha256
            else None
        )
        recovery = _CANONICAL_MONOTONIC_RECOVER_HELPER(
            self._authority,
            observed_state_sha256=state_sha256,
            tx_id=pending_tx_id,
            semantic_binding_sha256=(
                self._semantic_binding_sha256
                if pending_tx_id is not None
                else None
            ),
        )
        if recovery.disposition not in {
            RecoveryDisposition.CURRENT,
            RecoveryDisposition.ABORTED_PREPARE,
            RecoveryDisposition.COMMITTED_PREPARE,
        }:
            raise DeploymentRuntimeAuthorityError(
                "runtime authority store is not a recoverable authority tip"
            )

    def _read_validated_records_locked(
        self,
    ) -> tuple[DeploymentRuntimeAuthorityRecord, ...]:
        self._assert_binding_integrity()
        payload = self._read_payload()
        records = self._records_from_payload(payload)
        self._recover_state(payload)
        return records

    def _observed_now(self) -> str:
        if (
            datetime is not _CANONICAL_DATETIME_TYPE
            or timezone is not _CANONICAL_TIMEZONE_MODULE
            or _utc_now_timestamp is not _CANONICAL_UTC_NOW_TIMESTAMP
            or _CANONICAL_UTC_NOW_TIMESTAMP.__code__
            is not _CANONICAL_UTC_NOW_TIMESTAMP_CODE
            or _timestamp is not _CANONICAL_TIMESTAMP_VALIDATOR
            or _CANONICAL_TIMESTAMP_VALIDATOR.__code__
            is not _CANONICAL_TIMESTAMP_VALIDATOR_CODE
        ):
            raise DeploymentRuntimeAuthorityError(
                "runtime authority clock dispatch was replaced"
            )
        try:
            value = _CANONICAL_UTC_NOW_TIMESTAMP()
        except Exception as exc:
            raise DeploymentRuntimeAuthorityError(
                "runtime authority store clock failed"
            ) from exc
        return _CANONICAL_TIMESTAMP_VALIDATOR(
            value,
            "runtime authority store clock",
        )

    def append(
        self,
        *,
        environment: EnvironmentIdentity,
        episode: Episode,
        action_semantics_version: str,
        action_semantics_meanings: tuple[tuple[str, str], ...],
    ) -> DeploymentRuntimeAuthorityRecord:
        self._assert_binding_integrity()
        with self._lock, _workspace_economic_lock(self.workspace):
            records = self._read_validated_records_locked()
            current_payload = {
                "schema": STORE_SCHEMA,
                "schema_version": STORE_SCHEMA_VERSION,
                "records": [record.to_dict() for record in records],
            }
            observed_sha256 = self._state_sha256(current_payload)
            previous = (
                records[-1].record_sha256 if records else _EMPTY_CHAIN_SHA256
            )

            probe = DeploymentRuntimeAuthorityRecord.create(
                environment=environment,
                episode=episode,
                action_semantics_version=action_semantics_version,
                action_semantics_meanings=action_semantics_meanings,
                available_at=(
                    records[-1].available_at
                    if records
                    else "1970-01-01T00:00:00Z"
                ),
                previous_record_sha256=previous,
            )
            for existing in records:
                if existing.runtime_authority_id == probe.runtime_authority_id:
                    return existing

            observed_at = self._observed_now()
            if records and _instant(
                observed_at, "runtime authority store clock"
            ) < _instant(
                records[-1].available_at,
                "previous runtime authority available_at",
            ):
                raise DeploymentRuntimeAuthorityError(
                    "runtime authority store clock moved backwards"
                )
            candidate = DeploymentRuntimeAuthorityRecord.create(
                environment=environment,
                episode=episode,
                action_semantics_version=action_semantics_version,
                action_semantics_meanings=action_semantics_meanings,
                available_at=observed_at,
                previous_record_sha256=previous,
            )
            payload = {
                "schema": STORE_SCHEMA,
                "schema_version": STORE_SCHEMA_VERSION,
                "records": [record.to_dict() for record in (*records, candidate)],
            }
            intended_sha256 = self._state_sha256(payload)
            tx_id = self._new_transaction_id()
            self._authority.prepare(
                tx_id=tx_id,
                observed_state_sha256=observed_sha256,
                intended_state_sha256=intended_sha256,
                semantic_binding_sha256=self._semantic_binding_sha256,
            )
            self._write_atomic_path(self.path, payload)
            self._finalize_published_path(self.path)

            published = self._read_payload()
            self._records_from_payload(published)
            published_sha256 = self._state_sha256(published)
            if published_sha256 != intended_sha256:
                raise DeploymentRuntimeAuthorityError(
                    "published runtime authority state does not match prepared digest"
                )
            self._authority.commit(
                tx_id=tx_id,
                observed_state_sha256=published_sha256,
                semantic_binding_sha256=self._semantic_binding_sha256,
            )
            committed_records = self._read_validated_records_locked()
            verified = next(
                (
                    record
                    for record in committed_records
                    if record.runtime_authority_id
                    == candidate.runtime_authority_id
                ),
                None,
            )
            if verified is None:
                raise DeploymentRuntimeAuthorityError(
                    "runtime authority append was not durable"
                )
            return verified

    def get(
        self,
        runtime_authority_id: str,
    ) -> DeploymentRuntimeAuthorityRecord | None:
        self._assert_binding_integrity()
        identity = _CANONICAL_SHA_VALIDATOR(
            runtime_authority_id,
            "runtime_authority_id",
        )
        with self._lock, _workspace_economic_lock(self.workspace):
            for record in self._read_validated_records_locked():
                if record.runtime_authority_id == identity:
                    return record
        return None

    def records(self) -> tuple[DeploymentRuntimeAuthorityRecord, ...]:
        self._assert_binding_integrity()
        with self._lock, _workspace_economic_lock(self.workspace):
            return self._read_validated_records_locked()

    # Build the expected dispatch root inside the class namespace itself. The
    # metaclass makes this attribute non-replaceable after class construction and
    # MappingProxyType makes its contents immutable, so coordinated replacement of
    # a live method plus the module-level expectation map cannot self-confirm.
    _dispatch_expectations: dict[
        str,
        tuple[object, object, object, tuple[tuple[str, object], ...]],
    ] = {}
    for _dispatch_name in _SEALED_STORE_DISPATCH_NAMES:
        _dispatch_descriptor = locals()[_dispatch_name]
        _dispatch_callable = getattr(
            _dispatch_descriptor,
            "__func__",
            _dispatch_descriptor,
        )
        _dispatch_kwdefaults = (
            getattr(_dispatch_callable, "__kwdefaults__", None) or {}
        )
        _dispatch_expectations[_dispatch_name] = (
            _dispatch_descriptor,
            getattr(_dispatch_callable, "__code__", None),
            getattr(_dispatch_callable, "__defaults__", None),
            tuple(sorted(_dispatch_kwdefaults.items())),
        )
    _CANONICAL_DISPATCH_EXPECTATIONS: Final = MappingProxyType(
        _dispatch_expectations
    )
    del _dispatch_expectations
    del _dispatch_name
    del _dispatch_descriptor
    del _dispatch_callable
    del _dispatch_kwdefaults

