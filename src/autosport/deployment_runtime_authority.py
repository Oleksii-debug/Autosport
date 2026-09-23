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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Final, Mapping

from .integrity import durable_path_lock
from .learning_environment import EnvironmentIdentity, Episode


STORE_SCHEMA: Final = "autosport.deployment_runtime_authority_store"
STORE_SCHEMA_VERSION: Final = 2
RECORD_SCHEMA: Final = "autosport.deployment_runtime_authority"
RECORD_SCHEMA_VERSION: Final = 2
_EMPTY_CHAIN_SHA256: Final = hashlib.sha256(b"").hexdigest()
_HEX: Final = frozenset("0123456789abcdef")


class DeploymentRuntimeAuthorityError(ValueError):
    """Durable runtime authority is missing, malformed, or conflicting."""


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
    """One local durable append-only authority file with full-read validation."""

    def __init__(self, path: str | Path) -> None:
        candidate = Path(path)
        try:
            canonical = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise DeploymentRuntimeAuthorityError(
                "cannot resolve runtime authority store path"
            ) from exc
        self.path = canonical
        self._lock = RLock()
        self._require_single_link()
        self._read_validated_records()

    def _require_single_link(self) -> None:
        """Fail closed if the canonical store inode has another pathname."""

        try:
            link_count = self.path.stat().st_nlink
        except (OSError, RuntimeError) as exc:
            raise DeploymentRuntimeAuthorityError(
                "cannot stat runtime authority store path"
            ) from exc
        if link_count != 1:
            raise DeploymentRuntimeAuthorityError(
                "runtime authority store must not be hard-linked"
            )

    @classmethod
    def initialize_pristine(
        cls,
        path: str | Path,
    ) -> "DeploymentRuntimeAuthorityStore":
        requested = Path(path)
        requested.parent.mkdir(parents=True, exist_ok=True)
        try:
            destination = requested.parent.resolve(strict=True) / requested.name
        except (OSError, RuntimeError) as exc:
            raise DeploymentRuntimeAuthorityError(
                "cannot resolve runtime authority store parent path"
            ) from exc
        with durable_path_lock(destination):
            if destination.exists():
                raise DeploymentRuntimeAuthorityError("runtime authority store already exists")
            cls._write_atomic_path(
                destination,
                {
                    "schema": STORE_SCHEMA,
                    "schema_version": STORE_SCHEMA_VERSION,
                    "records": [],
                },
            )
        return cls(destination)

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
        finally:
            if temporary_name is not None:
                try:
                    Path(temporary_name).unlink(missing_ok=True)
                except OSError:
                    pass

    def _read_payload(self) -> dict[str, object]:
        # The constructor fence is insufficient: another process can create a hard
        # link after this Store instance is opened. Revalidate both sides of the
        # filesystem read so a late alias cannot become silently trusted state.
        self._require_single_link()
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise DeploymentRuntimeAuthorityError("cannot read runtime authority store") from exc
        self._require_single_link()
        if not raw.endswith("\n"):
            raise DeploymentRuntimeAuthorityError("runtime authority store is not canonical text")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DeploymentRuntimeAuthorityError("runtime authority store is not valid JSON") from exc
        if type(payload) is not dict:
            raise DeploymentRuntimeAuthorityError("runtime authority store must be an object")
        if set(payload) != {"schema", "schema_version", "records"}:
            raise DeploymentRuntimeAuthorityError("runtime authority store envelope is not canonical")
        if payload.get("schema") != STORE_SCHEMA or payload.get("schema_version") != STORE_SCHEMA_VERSION:
            raise DeploymentRuntimeAuthorityError("unsupported runtime authority store schema")
        if _canonical_json(payload) + "\n" != raw:
            raise DeploymentRuntimeAuthorityError("runtime authority store is not canonical JSON")
        records = payload.get("records")
        if type(records) is not list:
            raise DeploymentRuntimeAuthorityError("runtime authority records must be a list")
        return payload

    def _read_validated_records(self) -> tuple[DeploymentRuntimeAuthorityRecord, ...]:
        payload = self._read_payload()
        records_raw = payload["records"]
        assert isinstance(records_raw, list)
        previous = _EMPTY_CHAIN_SHA256
        previous_available_at: datetime | None = None
        seen_ids: set[str] = set()
        records: list[DeploymentRuntimeAuthorityRecord] = []
        for raw in records_raw:
            record = DeploymentRuntimeAuthorityRecord.from_dict(raw)
            if record.previous_record_sha256 != previous:
                raise DeploymentRuntimeAuthorityError("runtime authority hash chain is broken")
            current_available_at = _instant(record.available_at, "available_at")
            if previous_available_at is not None and current_available_at < previous_available_at:
                raise DeploymentRuntimeAuthorityError(
                    "runtime authority first-seen time moved backwards"
                )
            if record.runtime_authority_id in seen_ids:
                raise DeploymentRuntimeAuthorityError("duplicate runtime authority identity")
            seen_ids.add(record.runtime_authority_id)
            records.append(record)
            previous = record.record_sha256
            previous_available_at = current_available_at
        return tuple(records)

    def _observed_now(self) -> str:
        try:
            value = _utc_now_timestamp()
        except Exception as exc:
            raise DeploymentRuntimeAuthorityError("runtime authority store clock failed") from exc
        return _timestamp(value, "runtime authority store clock")

    def append(
        self,
        *,
        environment: EnvironmentIdentity,
        episode: Episode,
        action_semantics_version: str,
        action_semantics_meanings: tuple[tuple[str, str], ...],
    ) -> DeploymentRuntimeAuthorityRecord:
        # The instance RLock protects callers sharing one Store object.  A product
        # restart or two independent runtimes can hold distinct Store instances for
        # the same path, so the whole read/modify/write transaction also needs the
        # canonical durable path fence.  Locking only os.replace() would still allow
        # two writers to derive children from the same hash-chain head and silently
        # erase one another with last-writer-wins publication.
        with self._lock:
            with durable_path_lock(self.path):
                records = self._read_validated_records()
                previous = records[-1].record_sha256 if records else _EMPTY_CHAIN_SHA256

                # Compute the immutable semantic identity before consulting the clock so an
                # exact retry returns the original first-seen record even if time advanced.
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
                    "records": [
                        record.to_dict() for record in (*records, candidate)
                    ],
                }
                # Recheck immediately before publication. A hard link created after
                # the validated read must not let os.replace split one accepted
                # authority history into two independently readable pathnames.
                self._require_single_link()
                self._write_atomic_path(self.path, payload)
                verified = self.get(candidate.runtime_authority_id)
                if verified is None:
                    raise DeploymentRuntimeAuthorityError(
                        "runtime authority append was not durable"
                    )
                return verified

    def get(self, runtime_authority_id: str) -> DeploymentRuntimeAuthorityRecord | None:
        identity = _sha(runtime_authority_id, "runtime_authority_id")
        with self._lock:
            for record in self._read_validated_records():
                if record.runtime_authority_id == identity:
                    return record
        return None

    def records(self) -> tuple[DeploymentRuntimeAuthorityRecord, ...]:
        with self._lock:
            return self._read_validated_records()
