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
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Final, Mapping

from .learning_environment import EnvironmentIdentity, Episode
from .monotonic_workspace_authority import (
    AuthorityPhase,
    MonotonicWorkspaceAuthority,
    RecoveryDisposition,
)
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
_CANONICAL_MONOTONIC_AUTHORITY_TYPE: Final = MonotonicWorkspaceAuthority
_CANONICAL_RLOCK_FACTORY: Final = RLock
_CANONICAL_WORKSPACE_ECONOMIC_LOCK_TYPE: Final = WorkspaceEconomicLock
_CANONICAL_MONOTONIC_AUTHORITY_INIT: Final = MonotonicWorkspaceAuthority.__init__
_CANONICAL_OBJECT_NEW: Final = object.__new__
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


def _construct_monotonic_authority(
    *,
    workspace: Path,
    domain: str,
    key: str,
    authority_root: str | Path | None,
) -> MonotonicWorkspaceAuthority:
    _assert_canonical_monotonic_authority_constructor()
    authority = _CANONICAL_OBJECT_NEW(_CANONICAL_MONOTONIC_AUTHORITY_TYPE)
    _CANONICAL_MONOTONIC_AUTHORITY_INIT(
        authority,
        workspace=workspace,
        domain=domain,
        key=key,
        authority_root=authority_root,
    )
    _assert_canonical_monotonic_authority_constructor()
    if type(authority) is not _CANONICAL_MONOTONIC_AUTHORITY_TYPE:
        raise DeploymentRuntimeAuthorityError(
            "monotonic workspace authority construction returned noncanonical type"
        )
    return authority


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


def _utc_now_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise DeploymentRuntimeAuthorityError("runtime authority is not canonical JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _environment_payload(environment: EnvironmentIdentity) -> dict[str, object]:
    if not isinstance(environment, EnvironmentIdentity):
        raise DeploymentRuntimeAuthorityError("environment must be EnvironmentIdentity")
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
    if not isinstance(episode, Episode):
        raise DeploymentRuntimeAuthorityError("episode must be Episode")
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
        if not isinstance(self.environment, EnvironmentIdentity):
            raise DeploymentRuntimeAuthorityError("environment must be EnvironmentIdentity")
        if not isinstance(self.episode, Episode):
            raise DeploymentRuntimeAuthorityError("episode must be Episode")
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


class DeploymentRuntimeAuthorityStore:
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
        }
    )

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

    def _configure(
        self,
        path: str | Path,
        *,
        authority_root: str | Path | None,
    ) -> None:
        self.path = Path(path).expanduser().resolve(strict=False)
        self.workspace = self.path.parent
        self._lock = _new_local_lock()
        self._authority = _construct_monotonic_authority(
            workspace=self.workspace,
            domain=_AUTHORITY_DOMAIN,
            key=self.path.name,
            authority_root=authority_root,
        )
        self._semantic_binding_sha256 = _digest(
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
        destination = Path(path).expanduser().resolve(strict=False)
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

            store = cls.__new__(cls)
            store._configure(destination, authority_root=authority_root)
            recovery = store._authority.recover(observed_state_sha256=None)
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
    def _state_sha256(payload: Mapping[str, object]) -> str:
        return hashlib.sha256(
            (_canonical_json(payload) + "\n").encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _new_transaction_id() -> str:
        return f"deployment-runtime-{uuid.uuid4().hex}"

    def _read_payload(self) -> dict[str, object]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            self._authority.recover(observed_state_sha256=None)
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
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
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
        if _canonical_json(payload) + "\n" != raw:
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

    def _recover_state(
        self,
        payload: Mapping[str, object],
    ) -> None:
        state_sha256 = self._state_sha256(payload)
        history = self._authority.read_history()
        latest = history[-1] if history else None
        pending_tx_id = (
            latest.tx_id
            if latest is not None
            and latest.phase is AuthorityPhase.PREPARE
            and latest.intended_state_sha256 == state_sha256
            else None
        )
        recovery = self._authority.recover(
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
        payload = self._read_payload()
        records = self._records_from_payload(payload)
        self._recover_state(payload)
        return records

    def _observed_now(self) -> str:
        try:
            value = _utc_now_timestamp()
        except Exception as exc:
            raise DeploymentRuntimeAuthorityError(
                "runtime authority store clock failed"
            ) from exc
        return _timestamp(value, "runtime authority store clock")

    def append(
        self,
        *,
        environment: EnvironmentIdentity,
        episode: Episode,
        action_semantics_version: str,
        action_semantics_meanings: tuple[tuple[str, str], ...],
    ) -> DeploymentRuntimeAuthorityRecord:
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

            published = self._read_payload()
            published_records = self._records_from_payload(published)
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
            verified = next(
                (
                    record
                    for record in published_records
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
        identity = _sha(runtime_authority_id, "runtime_authority_id")
        with self._lock, _workspace_economic_lock(self.workspace):
            for record in self._read_validated_records_locked():
                if record.runtime_authority_id == identity:
                    return record
        return None

    def records(self) -> tuple[DeploymentRuntimeAuthorityRecord, ...]:
        with self._lock, _workspace_economic_lock(self.workspace):
            return self._read_validated_records_locked()

