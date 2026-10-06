from __future__ import annotations

import json
import os
import stat
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Final, Protocol

from .causal_collector import (
    CollectorDelta,
    CausalView,
    DesktopDeltaConsumer,
    GapState,
    SyncState,
)
from .collector_service import HeadlessCollectorService
from .event_lifecycle import ContinuousEventLifecycle, EventLifecycleRecord, EventPhase
from .integrity import atomic_write_json, durable_path_lock
from .json_integrity import strict_json_loads
from .market_mirror_runtime import (
    BoundedMirrorInvalidationBuffer,
    FocusedMirrorDependencyIndex,
    MirrorInvalidationBatch,
)
from .paper import PaperBook
from .settlement import SettlementEngine
from .storage import SQLiteMarketStore
from .workspace_lock import WorkspaceEconomicLock


class ContinuousSessionError(RuntimeError):
    """Base error for the durable continuous-session coordinator."""


class SessionPausedError(ContinuousSessionError):
    """Raised when work is attempted while the session is durably PAUSED."""


class SessionStoppedError(ContinuousSessionError):
    """Raised when work is attempted while the session is durably STOPPED."""


def _bind_canonical_settlement_engine(method):
    """Inject the import-time exact SettlementEngine through a closure-owned seam."""

    canonical_engine_type = SettlementEngine

    def guarded(self, *args, **kwargs):
        if "_settlement_engine_type" in kwargs:
            raise TypeError("settlement engine origin is internal product authority")
        kwargs["_settlement_engine_type"] = canonical_engine_type
        return method(self, *args, **kwargs)

    guarded.__name__ = method.__name__
    guarded.__qualname__ = method.__qualname__
    guarded.__doc__ = method.__doc__
    guarded.__annotations__ = method.__annotations__
    return guarded


def _seal_settlement_consumer_entry(method):
    """Return an immutable built-in descriptor with a closure-owned dispatch target."""

    def resolve(instance):
        return method.__get__(instance, type(instance))

    def reject_set(_instance, _value) -> None:
        raise TypeError("canonical settlement consumer entry binding is immutable")

    def reject_delete(_instance) -> None:
        raise TypeError("canonical settlement consumer entry binding is immutable")

    return property(resolve, reject_set, reject_delete, method.__doc__)


def _build_settlement_consumer_class_guard(name: str):
    """Keep type-level replacement from bypassing the installed data descriptor."""

    class SettlementConsumerClassGuard:
        __slots__ = ()

        def __get__(self, instance, owner=None):
            if instance is None:
                return self
            binding = instance.__dict__[name]
            return binding.__get__(None, instance)

        def __set__(self, _instance, _value) -> None:
            raise TypeError("canonical settlement consumer entry binding is immutable")

        def __delete__(self, _instance) -> None:
            raise TypeError("canonical settlement consumer entry binding is immutable")

    return SettlementConsumerClassGuard()


class _ContinuousSessionCoordinatorMeta(type):
    """Seal the trusted settlement consumer entry inside the process TCB."""

    def __init_subclass__(mcls, **kwargs) -> None:
        raise TypeError("canonical settlement consumer metaclass is not extensible")

    def __new__(mcls, name, bases, namespace, **kwargs):
        protected = {"_settle", "_settlement_consumer_bindings_sealed"}
        inherits_sealed_consumer = any(
            any(
                ancestor.__dict__.get(
                    "_settlement_consumer_bindings_sealed",
                    False,
                )
                for ancestor in base.__mro__
            )
            for base in bases
        )
        if inherits_sealed_consumer and protected.intersection(namespace):
            raise TypeError("canonical settlement consumer entry binding is immutable")
        return super().__new__(mcls, name, bases, namespace, **kwargs)

    def __setattr__(cls, name: str, value: object) -> None:
        sealed = any(
            ancestor.__dict__.get("_settlement_consumer_bindings_sealed", False)
            for ancestor in cls.__mro__
        )
        if sealed and name in {
            "_settle",
            "_settlement_consumer_bindings_sealed",
        }:
            raise TypeError("canonical settlement consumer entry binding is immutable")
        super().__setattr__(name, value)

    def __delattr__(cls, name: str) -> None:
        sealed = any(
            ancestor.__dict__.get("_settlement_consumer_bindings_sealed", False)
            for ancestor in cls.__mro__
        )
        if sealed and name in {
            "_settle",
            "_settlement_consumer_bindings_sealed",
        }:
            raise TypeError("canonical settlement consumer entry binding is immutable")
        super().__delattr__(name)


class SessionState(StrEnum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"


@dataclass(frozen=True, slots=True)
class SettlementResolution:
    """One externally-authoritative, causally available settlement resolution."""

    event_identity: str
    settlement_ref: str
    quote_outcomes: dict[str, str]
    evidence_id: str
    evidence_sha256: str
    available_at: str

    def validate(self, *, as_of: str) -> None:
        _text(self.event_identity, "event_identity")
        _text(self.settlement_ref, "settlement_ref")
        _text(self.evidence_id, "evidence_id")
        _sha256(self.evidence_sha256, "evidence_sha256")
        cutoff = _instant(as_of, "as_of")
        available = _instant(self.available_at, "available_at")
        if available > cutoff:
            raise ValueError("settlement evidence is not causally available at session cutoff")
        if type(self.quote_outcomes) is not dict or not self.quote_outcomes:
            raise ValueError("quote_outcomes must be a non-empty exact dict")
        for quote_key, outcome in self.quote_outcomes.items():
            _text(quote_key, "quote_outcomes quote_key")
            if type(outcome) is not str or outcome not in {"win", "loss", "void"}:
                raise ValueError("quote_outcomes contains unsupported outcome")


class SettlementOutcomeAuthority(Protocol):
    """External outcome authority; Autosport never derives outcomes from lifecycle state."""

    def resolve(
        self,
        record: EventLifecycleRecord,
        *,
        as_of: str,
    ) -> SettlementResolution | None:
        ...


class SettlementLearningHandoff(Protocol):
    """Optional durable PAPER settlement seam for causal learning."""

    def prepare_settlement(
        self,
        *,
        paper_book_path: Path,
        resolutions: tuple[SettlementResolution, ...],
        at: str,
    ) -> tuple[str, ...]:
        ...

    def reconcile_after_settlement(
        self,
        *,
        paper_book_path: Path,
        resolutions: tuple[SettlementResolution, ...],
        settled_ticket_ids: tuple[str, ...],
        at: str,
    ) -> tuple[str, ...]:
        ...


@dataclass(frozen=True, slots=True)
class ContinuousTickResult:
    session_id: str
    cycle_index: int
    source_id: str
    source_provider_unavailable: bool
    source_gap_states: tuple[str, ...]
    source_sync_states: tuple[str, ...]
    committed_delta_ids: tuple[str, ...]
    delivered_delta_ids: tuple[str, ...]
    affected_input_ids: tuple[str, ...]
    registered_input_ids: tuple[str, ...]
    retired_input_ids: tuple[str, ...]
    full_refresh_required: bool
    invalidation_backlog: bool
    settled_ticket_ids: tuple[str, ...]
    settlement_evidence_ids: tuple[str, ...]
    last_success_at: str | None


@dataclass(frozen=True, slots=True)
class ContinuousSessionStatus:
    session_id: str
    source_id: str
    state: SessionState
    cycles_completed: int
    last_success_at: str | None
    last_error_code: str | None
    last_full_refresh_at: str | None
    settlement_evidence: tuple[dict[str, str], ...]
    source_provider_unavailable: bool = False
    source_last_success_at: str | None = None
    source_last_error_code: str | None = None
    source_gap_state: str | None = None
    source_sync_state: str | None = None
    source_state_delta_id: str | None = None
    source_unresolved_gap_delta_ids: tuple[str, ...] = ()
    source_projection_stream_epoch: str | None = None
    source_state_projection_backlog: bool = False
    invalidation_pending_count: int = 0
    invalidation_full_refresh_required: bool = False


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError(f"{field} must be a non-empty trimmed string")
    return value


def _instant(value: object, field: str) -> datetime:
    raw = _text(value, field)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware ISO-8601")
    return parsed.astimezone(timezone.utc)


def _sha256(value: object, field: str) -> str:
    if type(value) is not str or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 hex digest")
    return value


_CONTINUOUS_SESSION_SCHEMA: Final = "autosport.continuous_session"
_CONTINUOUS_SESSION_VERSION: Final = 3
_CONTINUOUS_SESSION_LEGACY_VERSION: Final = 2
_CONTINUOUS_SESSION_ERROR_SCHEMA: Final = "autosport.continuous_session.operational_error"
_CONTINUOUS_SESSION_ERROR_VERSION: Final = 3
_CONTINUOUS_SESSION_ERROR_MAX_BYTES: Final = 16 * 1024
_CONTINUOUS_SESSION_ERROR_MAX_CODE_CHARS: Final = 512
_CONTINUOUS_SESSION_ERROR_FIELDS: Final = frozenset(
    {
        "schema",
        "schema_version",
        "session_id",
        "source_id",
        "observed_generation",
        "observed_cycles_completed",
        "observed_last_success_at",
        "observed_state",
        "last_error_code",
    }
)
_CONTINUOUS_SESSION_FIELDS: Final = frozenset(
    {
        "schema",
        "schema_version",
        "session_id",
        "source_id",
        "state",
        "started_at",
        "generation",
        "cycles_completed",
        "last_success_at",
        "last_error_code",
        "last_full_refresh_at",
        "settlement_evidence",
        "source_gap_state",
        "source_sync_state",
        "source_state_delta_id",
        "source_unresolved_gap_delta_ids",
        "source_projection_stream_epoch",
        "source_state_projection_backlog",
    }
)
_CONTINUOUS_SESSION_LEGACY_FIELDS: Final = frozenset(
    _CONTINUOUS_SESSION_FIELDS - {"generation"}
)


class _ContinuousSessionState:
    _SCHEMA = _CONTINUOUS_SESSION_SCHEMA
    _VERSION = _CONTINUOUS_SESSION_VERSION
    _ERROR_SCHEMA = _CONTINUOUS_SESSION_ERROR_SCHEMA
    _ERROR_VERSION = _CONTINUOUS_SESSION_ERROR_VERSION
    _MAX_ERROR_CHECKPOINT_BYTES = _CONTINUOUS_SESSION_ERROR_MAX_BYTES
    _MAX_ERROR_CODE_CHARS = _CONTINUOUS_SESSION_ERROR_MAX_CODE_CHARS
    _ERROR_FIELDS = _CONTINUOUS_SESSION_ERROR_FIELDS
    _FIELDS = _CONTINUOUS_SESSION_FIELDS

    def __init__(
        self,
        path: str | Path,
        *,
        session_id: str | None,
        source_id: str,
        clock: Callable[[], str],
        _schema: str = _CONTINUOUS_SESSION_SCHEMA,
        _version: int = _CONTINUOUS_SESSION_VERSION,
        _atomic_write_json: Callable[..., Any] = atomic_write_json,
        _atomic_write_json_code: object = atomic_write_json.__code__,
        _durable_path_lock: Callable[..., Any] = durable_path_lock,
        _durable_path_lock_code: object = durable_path_lock.__code__,
    ) -> None:
        if (
            getattr(_atomic_write_json, "__code__", None) is not _atomic_write_json_code
            or durable_path_lock is not _durable_path_lock
            or getattr(_durable_path_lock, "__code__", None)
            is not _durable_path_lock_code
        ):
            raise ContinuousSessionError(
                "canonical session bootstrap authority changed"
            )
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.source_id = _text(source_id, "source_id")
        self._clock = clock

        # Bootstrap is a read/create/read transaction on the canonical session
        # path.  Without this fence, two processes can both observe absence and
        # publish different session identities; the losing constructor could
        # then silently adopt the winner's identity.
        with _durable_path_lock(self.path):
            if self.path.exists():
                raw = self._read()
            else:
                resolved_id = _text(
                    session_id or str(uuid.uuid4()),
                    "session_id",
                )
                started_at = clock()
                _instant(started_at, "started_at")
                _atomic_write_json(
                    self.path,
                    {
                        "schema": _schema,
                        "schema_version": _version,
                        "session_id": resolved_id,
                        "source_id": self.source_id,
                        "state": SessionState.RUNNING.value,
                        "started_at": started_at,
                        "generation": 0,
                        "cycles_completed": 0,
                        "last_success_at": None,
                        "last_error_code": None,
                        "last_full_refresh_at": None,
                        "settlement_evidence": [],
                        "source_gap_state": None,
                        "source_sync_state": None,
                        "source_state_delta_id": None,
                        "source_unresolved_gap_delta_ids": [],
                        "source_projection_stream_epoch": None,
                        "source_state_projection_backlog": False,
                    },
                )
                raw = self._read()

            existing_source = raw["source_id"]
            if existing_source != self.source_id:
                raise ContinuousSessionError(
                    "durable session source_id does not match configured source"
                )
            if session_id is not None and raw["session_id"] != _text(
                session_id, "session_id"
            ):
                raise ContinuousSessionError(
                    "durable session_id does not match configured session"
                )

        self._session_id = raw["session_id"]
        self._generation = raw["generation"]
        self._cycles_completed = raw["cycles_completed"]
        self._last_success_at = raw["last_success_at"]
        self._state = raw["state"]
        self._error_path = self.path.with_name(
            f"{self.path.name}.operational_error.json"
        )
        if self._error_checkpoint_present():
            self._read_error_checkpoint()

    @staticmethod
    def _file_identity(info: os.stat_result) -> tuple[int, int]:
        return (info.st_dev, info.st_ino)

    def _error_checkpoint_present(
        self,
        *,
        _path_lstat: Callable[[Path], os.stat_result] = Path.lstat,
        _path_lstat_code: object = Path.lstat.__code__,
    ) -> bool:
        if (
            Path.lstat is not _path_lstat
            or getattr(Path.lstat, "__code__", None) is not _path_lstat_code
        ):
            raise ContinuousSessionError(
                "canonical operational-checkpoint presence authority changed"
            )
        try:
            _path_lstat(self._error_path)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise ContinuousSessionError(
                "cannot inspect continuous session operational error checkpoint"
            ) from exc
        return True

    @staticmethod
    def _bounded_descriptor_read(
        descriptor: int,
        limit: int,
        *,
        _os_read: Callable[[int, int], bytes] = os.read,
    ) -> bytes:
        chunks: list[bytes] = []
        remaining = limit
        while remaining > 0:
            chunk = _os_read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _read_error_checkpoint_bytes(
        self,
        *,
        _max_bytes: int = _CONTINUOUS_SESSION_ERROR_MAX_BYTES,
        _os_open: Callable[..., int] = os.open,
        _os_fstat: Callable[..., os.stat_result] = os.fstat,
        _os_lseek: Callable[..., int] = os.lseek,
        _os_read: Callable[..., bytes] = os.read,
        _os_close: Callable[[int], None] = os.close,
        _os_o_rdonly: int = os.O_RDONLY,
        _os_o_binary: int = getattr(os, "O_BINARY", 0),
        _os_o_nofollow: int = getattr(os, "O_NOFOLLOW", 0),
        _os_o_nonblock: int = getattr(os, "O_NONBLOCK", 0),
        _os_seek_set: int = os.SEEK_SET,
        _path_lstat: Callable[[Path], os.stat_result] = Path.lstat,
        _path_lstat_code: object = Path.lstat.__code__,
        _stat_islnk: Callable[[int], bool] = stat.S_ISLNK,
        _stat_isreg: Callable[[int], bool] = stat.S_ISREG,
        _bounded_descriptor_read: Callable[..., bytes] = _bounded_descriptor_read.__func__,
        _bounded_descriptor_read_code: object = _bounded_descriptor_read.__func__.__code__,
        _file_identity: Callable[[os.stat_result], tuple[int, int]] = _file_identity.__func__,
        _file_identity_code: object = _file_identity.__func__.__code__,
    ) -> bytes:
        if (
            os.open is not _os_open
            or os.fstat is not _os_fstat
            or os.lseek is not _os_lseek
            or os.read is not _os_read
            or os.close is not _os_close
            or Path.lstat is not _path_lstat
            or getattr(Path.lstat, "__code__", None) is not _path_lstat_code
            or os.O_RDONLY != _os_o_rdonly
            or getattr(os, "O_BINARY", 0) != _os_o_binary
            or getattr(os, "O_NOFOLLOW", 0) != _os_o_nofollow
            or getattr(os, "O_NONBLOCK", 0) != _os_o_nonblock
            or os.SEEK_SET != _os_seek_set
            or stat.S_ISLNK is not _stat_islnk
            or stat.S_ISREG is not _stat_isreg
            or getattr(_bounded_descriptor_read, "__code__", None)
            is not _bounded_descriptor_read_code
            or getattr(_file_identity, "__code__", None) is not _file_identity_code
        ):
            raise ContinuousSessionError(
                "canonical operational-checkpoint filesystem authority changed"
            )
        flags = _os_o_rdonly | _os_o_binary
        flags |= _os_o_nofollow
        # A path can be replaced after lstat() but before open().  On POSIX,
        # opening a FIFO/device-like replacement without O_NONBLOCK could hang
        # the coordinator before descriptor-type verification gets a chance to
        # fail closed.  Regular files ignore this flag.
        flags |= _os_o_nonblock
        descriptor: int | None = None
        try:
            before = _path_lstat(self._error_path)
            if _stat_islnk(before.st_mode) or not _stat_isreg(before.st_mode):
                raise ContinuousSessionError(
                    "continuous session operational error checkpoint "
                    "must be a regular file"
                )
            if before.st_nlink != 1:
                raise ContinuousSessionError(
                    "continuous session operational error checkpoint "
                    "must not have multiple hard links"
                )

            descriptor = _os_open(self._error_path, flags)
            opened = _os_fstat(descriptor)
            if not _stat_isreg(opened.st_mode) or opened.st_nlink != 1:
                raise ContinuousSessionError(
                    "continuous session operational error checkpoint "
                    "file identity is not trustworthy"
                )
            if _file_identity(opened) != _file_identity(before):
                raise ContinuousSessionError(
                    "continuous session operational error checkpoint "
                    "was replaced before verification"
                )

            read_limit = _max_bytes + 1
            _os_lseek(descriptor, 0, _os_seek_set)
            first_image = _bounded_descriptor_read(descriptor, read_limit)
            _os_lseek(descriptor, 0, _os_seek_set)
            encoded = _bounded_descriptor_read(descriptor, read_limit)
            if encoded != first_image:
                raise ContinuousSessionError(
                    "continuous session operational error checkpoint "
                    "changed during bounded read"
                )

            current = _os_fstat(descriptor)
            after = _path_lstat(self._error_path)
            if (
                _file_identity(current) != _file_identity(opened)
                or _file_identity(after) != _file_identity(opened)
                or _stat_islnk(after.st_mode)
                or not _stat_isreg(after.st_mode)
                or after.st_nlink != 1
            ):
                raise ContinuousSessionError(
                    "continuous session operational error checkpoint "
                    "changed during verification"
                )
            return encoded
        except ContinuousSessionError:
            raise
        except OSError as exc:
            raise ContinuousSessionError(
                "cannot verify continuous session operational error checkpoint file"
            ) from exc
        finally:
            if descriptor is not None:
                try:
                    _os_close(descriptor)
                except OSError as exc:
                    raise ContinuousSessionError(
                        "cannot close continuous session operational error checkpoint"
                    ) from exc

    def _read_error_checkpoint(
        self,
        *,
        _fields: frozenset[str] = _CONTINUOUS_SESSION_ERROR_FIELDS,
        _strict_json_loads: Callable[..., Any] = strict_json_loads,
        _strict_json_loads_code: object = strict_json_loads.__code__,
        _schema: str = _CONTINUOUS_SESSION_ERROR_SCHEMA,
        _version: int = _CONTINUOUS_SESSION_ERROR_VERSION,
        _max_bytes: int = _CONTINUOUS_SESSION_ERROR_MAX_BYTES,
        _max_code_chars: int = _CONTINUOUS_SESSION_ERROR_MAX_CODE_CHARS,
        _read_error_checkpoint_bytes: Callable[..., bytes] = _read_error_checkpoint_bytes,
        _read_error_checkpoint_bytes_code: object = _read_error_checkpoint_bytes.__code__,
    ) -> dict[str, Any]:
        if (
            getattr(_strict_json_loads, "__code__", None) is not _strict_json_loads_code
            or getattr(_read_error_checkpoint_bytes, "__code__", None)
            is not _read_error_checkpoint_bytes_code
        ):
            raise ContinuousSessionError(
                "canonical operational-checkpoint parser code identity changed"
            )
        try:
            encoded = _read_error_checkpoint_bytes(self, _max_bytes=_max_bytes)
            if len(encoded) > _max_bytes:
                raise ContinuousSessionError(
                    "continuous session operational error checkpoint "
                    "exceeds resource limit"
                )
            raw = _strict_json_loads(encoded.decode("utf-8"))
        except ContinuousSessionError:
            raise
        except (
            OSError,
            UnicodeDecodeError,
            TypeError,
            ValueError,
            RecursionError,
        ) as exc:
            raise ContinuousSessionError(
                "cannot verify continuous session operational error checkpoint"
            ) from exc
        if (
            type(raw) is not dict
            or set(raw) != _fields
            or raw["schema"] != _schema
            or type(raw["schema_version"]) is not int
            or raw["schema_version"] != _version
            or raw["session_id"] != self._session_id
            or raw["source_id"] != self.source_id
        ):
            raise ContinuousSessionError(
                "continuous session operational error checkpoint mismatch"
            )
        observed_generation = raw["observed_generation"]
        if (
            isinstance(observed_generation, bool)
            or not isinstance(observed_generation, int)
            or observed_generation < 0
        ):
            raise ContinuousSessionError(
                "operational error observed_generation must be non-negative"
            )
        observed_cycles = raw["observed_cycles_completed"]
        if (
            isinstance(observed_cycles, bool)
            or not isinstance(observed_cycles, int)
            or observed_cycles < 0
        ):
            raise ContinuousSessionError(
                "operational error observed_cycles_completed must be non-negative"
            )
        try:
            if raw["observed_last_success_at"] is not None:
                _instant(
                    raw["observed_last_success_at"],
                    "operational error observed_last_success_at",
                )
            observed_state = raw["observed_state"]
            if type(observed_state) is not str or observed_state not in {
                "RUNNING",
                "PAUSED",
                "STOPPED",
            }:
                raise ContinuousSessionError(
                    "operational error observed_state is unsupported"
                )
            if raw["last_error_code"] is not None:
                error_code = _text(
                    raw["last_error_code"],
                    "operational error last_error_code",
                )
                if len(error_code) > _max_code_chars:
                    raise ContinuousSessionError(
                        "operational error last_error_code exceeds resource limit"
                    )
        except ContinuousSessionError:
            raise
        except ValueError as exc:
            raise ContinuousSessionError(
                "continuous session operational error checkpoint contains invalid field"
            ) from exc
        return raw

    def _write_error_checkpoint(
        self,
        code: str | None,
        *,
        _schema: str = _CONTINUOUS_SESSION_ERROR_SCHEMA,
        _version: int = _CONTINUOUS_SESSION_ERROR_VERSION,
        _max_bytes: int = _CONTINUOUS_SESSION_ERROR_MAX_BYTES,
        _max_code_chars: int = _CONTINUOUS_SESSION_ERROR_MAX_CODE_CHARS,
        _json_dumps: Callable[..., str] = json.dumps,
        _json_dump: Callable[..., Any] = json.dump,
        _atomic_write_json: Callable[[str | Path, dict[str, Any]], None] = atomic_write_json,
        _json_dumps_code: object = json.dumps.__code__,
        _json_dump_code: object = json.dump.__code__,
        _atomic_write_json_code: object = atomic_write_json.__code__,
    ) -> None:
        if (
            getattr(_json_dumps, "__code__", None) is not _json_dumps_code
            or json.dump is not _json_dump
            or getattr(_json_dump, "__code__", None) is not _json_dump_code
            or getattr(_atomic_write_json, "__code__", None)
            is not _atomic_write_json_code
        ):
            raise ContinuousSessionError(
                "canonical operational-checkpoint writer code identity changed"
            )
        if code is not None:
            code = _text(code, "code")
            if len(code) > _max_code_chars:
                raise ValueError("code exceeds operational error resource limit")
        payload = {
            "schema": _schema,
            "schema_version": _version,
            "session_id": self._session_id,
            "source_id": self.source_id,
            "observed_generation": self._generation,
            "observed_cycles_completed": self._cycles_completed,
            "observed_last_success_at": self._last_success_at,
            "observed_state": self._state,
            "last_error_code": code,
        }
        encoded = (
            _json_dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        if len(encoded) > _max_bytes:
            raise ContinuousSessionError(
                "continuous session operational error checkpoint exceeds resource limit"
            )
        _atomic_write_json(self._error_path, payload)

    @staticmethod
    def _validate_settlement_evidence(raw: object) -> tuple[dict[str, str], ...]:
        if type(raw) is not list:
            raise ContinuousSessionError("settlement_evidence must be a list")
        values: list[dict[str, str]] = []
        evidence_ids: set[str] = set()
        for item in raw:
            if type(item) is not dict:
                raise ContinuousSessionError(
                    "settlement_evidence entries must be objects"
                )
            if set(item) != {
                "event_identity",
                "settlement_ref",
                "evidence_id",
                "evidence_sha256",
                "available_at",
            }:
                raise ContinuousSessionError(
                    "settlement_evidence entry fields mismatch"
                )
            _text(item["event_identity"], "settlement_evidence event_identity")
            _text(item["settlement_ref"], "settlement_evidence settlement_ref")
            evidence_id = _text(
                item["evidence_id"],
                "settlement_evidence evidence_id",
            )
            if evidence_id in evidence_ids:
                raise ContinuousSessionError(
                    "settlement_evidence evidence_id values must be unique"
                )
            evidence_ids.add(evidence_id)
            _sha256(item["evidence_sha256"], "settlement_evidence evidence_sha256")
            _instant(item["available_at"], "settlement_evidence available_at")
            values.append(
                {
                    "event_identity": item["event_identity"],
                    "settlement_ref": item["settlement_ref"],
                    "evidence_id": item["evidence_id"],
                    "evidence_sha256": item["evidence_sha256"],
                    "available_at": item["available_at"],
                }
            )
        return tuple(values)

    def _read(
        self,
        *,
        _fields: frozenset[str] = _CONTINUOUS_SESSION_FIELDS,
        _legacy_fields: frozenset[str] = _CONTINUOUS_SESSION_LEGACY_FIELDS,
        _legacy_version: int = _CONTINUOUS_SESSION_LEGACY_VERSION,
        _strict_json_loads: Callable[..., Any] = strict_json_loads,
        _strict_json_loads_code: object = strict_json_loads.__code__,
        _path_read_text: Callable[..., str] = Path.read_text,
        _path_read_text_code: object = Path.read_text.__code__,
        _validate_settlement_evidence: Callable[
            [object], tuple[dict[str, str], ...]
        ] = _validate_settlement_evidence.__func__,
        _validate_settlement_evidence_code: object = (
            _validate_settlement_evidence.__func__.__code__
        ),
        _schema: str = _CONTINUOUS_SESSION_SCHEMA,
        _version: int = _CONTINUOUS_SESSION_VERSION,
    ) -> dict[str, Any]:
        if (
            getattr(_strict_json_loads, "__code__", None) is not _strict_json_loads_code
            or getattr(_path_read_text, "__code__", None) is not _path_read_text_code
            or getattr(_validate_settlement_evidence, "__code__", None)
            is not _validate_settlement_evidence_code
        ):
            raise ContinuousSessionError(
                "canonical session-reader code identity changed"
            )
        try:
            raw = _strict_json_loads(_path_read_text(self.path, encoding="utf-8"))
        except (OSError, TypeError, ValueError, RecursionError) as exc:
            raise ContinuousSessionError(
                "cannot verify continuous session state"
            ) from exc
        if (
            type(raw) is not dict
            or raw.get("schema") != _schema
            or raw.get("source_id") != self.source_id
        ):
            raise ContinuousSessionError("continuous session state schema/identity mismatch")
        field_set = set(raw)
        schema_version = raw.get("schema_version")
        current_shape = (
            field_set == _fields
            and type(schema_version) is int
            and schema_version == _version
        )
        legacy_shape = (
            field_set == _legacy_fields
            and type(schema_version) is int
            and schema_version == _legacy_version
        )
        if not current_shape and not legacy_shape:
            raise ContinuousSessionError("continuous session state schema/identity mismatch")
        if legacy_shape:
            raw["schema_version"] = _version
            raw["generation"] = 0
        try:
            _text(raw["session_id"], "session_id")
            _instant(raw["started_at"], "started_at")
        except (TypeError, ValueError) as exc:
            raise ContinuousSessionError(
                "continuous session state contains invalid identity/timestamp field"
            ) from exc
        try:
            state = SessionState(raw["state"])
        except ValueError as exc:
            raise ContinuousSessionError("unsupported continuous session state") from exc
        generation = raw["generation"]
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise ContinuousSessionError("generation must be a non-negative integer")
        cycles = raw["cycles_completed"]
        if isinstance(cycles, bool) or not isinstance(cycles, int) or cycles < 0:
            raise ContinuousSessionError("cycles_completed must be a non-negative integer")
        try:
            for name in ("last_success_at", "last_full_refresh_at"):
                if raw[name] is not None:
                    _instant(raw[name], name)
            if raw["last_error_code"] is not None:
                _text(raw["last_error_code"], "last_error_code")
            evidence = _validate_settlement_evidence(raw["settlement_evidence"])
        except ContinuousSessionError:
            raise
        except (TypeError, ValueError, RecursionError) as exc:
            raise ContinuousSessionError(
                "continuous session state contains invalid durable field"
            ) from exc
        gap_state = raw["source_gap_state"]
        sync_state = raw["source_sync_state"]
        if (gap_state is None) != (sync_state is None):
            raise ContinuousSessionError(
                "source gap/sync projection must be present or absent together"
            )
        if gap_state is not None:
            try:
                GapState(gap_state)
                SyncState(sync_state)
            except ValueError as exc:
                raise ContinuousSessionError(
                    "source gap/sync projection contains an unsupported state"
                ) from exc
        try:
            if raw["source_state_delta_id"] is not None:
                _text(raw["source_state_delta_id"], "source_state_delta_id")
            if raw["source_projection_stream_epoch"] is not None:
                _text(
                    raw["source_projection_stream_epoch"],
                    "source_projection_stream_epoch",
                )
        except (TypeError, ValueError) as exc:
            raise ContinuousSessionError(
                "continuous session source projection contains invalid identity"
            ) from exc
        if (raw["source_state_delta_id"] is None) != (
            raw["source_projection_stream_epoch"] is None
        ):
            raise ContinuousSessionError(
                "source projection identity is incomplete"
            )
        if raw["source_state_delta_id"] is None and gap_state is not None:
            raise ContinuousSessionError(
                "source projection state requires a canonical delta identity"
            )
        unresolved = raw["source_unresolved_gap_delta_ids"]
        if (
            type(unresolved) is not list
            or any(type(item) is not str or not item.strip() for item in unresolved)
            or len(set(unresolved)) != len(unresolved)
        ):
            raise ContinuousSessionError(
                "source_unresolved_gap_delta_ids must contain unique non-empty strings"
            )
        if type(raw["source_state_projection_backlog"]) is not bool:
            raise ContinuousSessionError(
                "source_state_projection_backlog must be boolean"
            )
        if unresolved and (
            gap_state != GapState.DETECTED.value
            or sync_state != SyncState.GAP_DETECTED.value
        ):
            raise ContinuousSessionError(
                "unresolved source gaps require DETECTED/GAP_DETECTED projection"
            )
        raw["state"] = state.value
        raw["settlement_evidence"] = [dict(item) for item in evidence]
        return raw

    def snapshot(
        self,
        *,
        _error_checkpoint_present: Callable[["_ContinuousSessionState"], bool] = (
            _error_checkpoint_present
        ),
        _error_checkpoint_present_code: object = _error_checkpoint_present.__code__,
        _read_error_checkpoint: Callable[
            ["_ContinuousSessionState"], dict[str, Any]
        ] = _read_error_checkpoint,
        _read_error_checkpoint_code: object = _read_error_checkpoint.__code__,
        _durable_path_lock: Callable[..., Any] = durable_path_lock,
        _durable_path_lock_code: object = durable_path_lock.__code__,
    ) -> ContinuousSessionStatus:
        if (
            getattr(_error_checkpoint_present, "__code__", None)
            is not _error_checkpoint_present_code
            or getattr(_read_error_checkpoint, "__code__", None)
            is not _read_error_checkpoint_code
            or durable_path_lock is not _durable_path_lock
            or getattr(_durable_path_lock, "__code__", None)
            is not _durable_path_lock_code
        ):
            raise ContinuousSessionError(
                "canonical operational-checkpoint snapshot authority changed"
            )
        # Read the canonical session image and its bounded failure overlay under
        # the same session lock used by all publishers.  The generation markers
        # reject stale sidecars, while this lock prevents a snapshot from
        # returning a main image and sidecar image observed on opposite sides of
        # a concurrent state/success/failure transaction.
        with _durable_path_lock(self.path):
            raw = self._read()
            if _error_checkpoint_present(self):
                error_checkpoint = _read_error_checkpoint(self)
                marker_matches = (
                    error_checkpoint["observed_generation"] == raw["generation"]
                    and error_checkpoint["observed_cycles_completed"] == raw["cycles_completed"]
                    and error_checkpoint["observed_last_success_at"] == raw["last_success_at"]
                    and error_checkpoint["observed_state"] == raw["state"]
                )
                if marker_matches and error_checkpoint["last_error_code"] is not None:
                    durable_error = raw["last_error_code"]
                    checkpoint_error = error_checkpoint["last_error_code"]
                    if durable_error is not None and durable_error != checkpoint_error:
                        raise ContinuousSessionError(
                            "continuous session error authorities conflict"
                        )
                    raw["last_error_code"] = checkpoint_error
        return ContinuousSessionStatus(
            session_id=raw["session_id"],
            source_id=raw["source_id"],
            state=SessionState(raw["state"]),
            cycles_completed=raw["cycles_completed"],
            last_success_at=raw["last_success_at"],
            last_error_code=raw["last_error_code"],
            last_full_refresh_at=raw["last_full_refresh_at"],
            settlement_evidence=tuple(raw["settlement_evidence"]),
            source_gap_state=raw["source_gap_state"],
            source_sync_state=raw["source_sync_state"],
            source_state_delta_id=raw["source_state_delta_id"],
            source_unresolved_gap_delta_ids=tuple(
                raw["source_unresolved_gap_delta_ids"]
            ),
            source_projection_stream_epoch=raw["source_projection_stream_epoch"],
            source_state_projection_backlog=raw[
                "source_state_projection_backlog"
            ],
        )

    @property
    def session_id(self) -> str:
        return self._read()["session_id"]

    def _update(
        self,
        mutate: Callable[[dict[str, Any]], bool | None],
        *,
        advance_generation: bool = False,
        finalize_under_lock: Callable[[dict[str, Any]], None] | None = None,
        _atomic_write_json: Callable[[str | Path, dict[str, Any]], None] = atomic_write_json,
        _atomic_write_json_code: object = atomic_write_json.__code__,
        _durable_path_lock: Callable[..., Any] = durable_path_lock,
        _durable_path_lock_code: object = durable_path_lock.__code__,
    ) -> dict[str, Any]:
        if (
            getattr(_atomic_write_json, "__code__", None) is not _atomic_write_json_code
            or durable_path_lock is not _durable_path_lock
            or getattr(_durable_path_lock, "__code__", None)
            is not _durable_path_lock_code
        ):
            raise ContinuousSessionError(
                "canonical session read-modify-write authority changed"
            )
        if finalize_under_lock is not None and not callable(finalize_under_lock):
            raise TypeError("finalize_under_lock must be callable or None")
        # The generation is a monotonic durable authority only if reading the
        # predecessor image, applying the mutation, publishing the successor,
        # and rereading it are one serialized transaction. atomic_write_json()
        # already re-enters this canonical lock for publication.
        with _durable_path_lock(self.path):
            raw = self._read()
            mutation_result = mutate(raw)
            if mutation_result is False:
                # Explicit no-op mutations do not manufacture a new generation
                # or clear a failure sidecar.  This is used for idempotent state
                # commands whose target already matches durable truth.
                updated = raw
            else:
                if advance_generation:
                    raw["generation"] = int(raw["generation"]) + 1
                _atomic_write_json(self.path, raw)
                updated = self._read()
                if finalize_under_lock is not None:
                    finalize_under_lock(updated)
            self._generation = updated["generation"]
            self._cycles_completed = updated["cycles_completed"]
            self._last_success_at = updated["last_success_at"]
            self._state = updated["state"]
        return updated

    def set_state(self, state: SessionState, *, reason: str | None = None) -> None:
        if not isinstance(state, SessionState):
            raise TypeError("state must be SessionState")

        def mutate(raw: dict[str, Any]) -> bool:
            normalized_reason = None if reason is None else _text(reason, "reason")
            if normalized_reason is not None:
                desired_error = normalized_reason
            elif state is SessionState.RUNNING:
                # A successful operator resume establishes RUNNING as the
                # current durable state; a stop/pause reason from the
                # predecessor generation is no longer an active error.
                desired_error = None
            else:
                desired_error = raw["last_error_code"]

            if (
                raw["state"] == state.value
                and raw["last_error_code"] == desired_error
            ):
                return False
            raw["state"] = state.value
            raw["last_error_code"] = desired_error
            return True

        def finalize(_updated: dict[str, Any]) -> None:
            # Any committed state transition supersedes an operational failure
            # observed in the predecessor state. Keep cleanup under the same
            # session lock so a newer failure cannot be erased after commit.
            if self._error_checkpoint_present():
                self._write_error_checkpoint(None)

        self._update(
            mutate,
            advance_generation=True,
            finalize_under_lock=finalize,
        )

    @staticmethod
    def _normalized_settlement_evidence(
        evidence: SettlementResolution,
    ) -> dict[str, str]:
        return {
            "event_identity": evidence.event_identity,
            "settlement_ref": evidence.settlement_ref,
            "evidence_id": evidence.evidence_id,
            "evidence_sha256": evidence.evidence_sha256,
            "available_at": _instant(
                evidence.available_at,
                "available_at",
            ).isoformat(),
        }

    def validate_settlement_evidence(
        self,
        *,
        settlement_evidence: tuple[SettlementResolution, ...],
        _normalized_settlement_evidence: Callable[
            [SettlementResolution], dict[str, str]
        ] = _normalized_settlement_evidence.__func__,
        _normalized_settlement_evidence_code: object = (
            _normalized_settlement_evidence.__func__.__code__
        ),
    ) -> None:
        if (
            getattr(
                _normalized_settlement_evidence,
                "__code__",
                None,
            )
            is not _normalized_settlement_evidence_code
        ):
            raise ContinuousSessionError(
                "canonical settlement evidence normalizer code identity changed"
            )
        raw = self._read()
        known = {
            item["evidence_id"]: item
            for item in raw["settlement_evidence"]
        }
        for evidence in settlement_evidence:
            normalized = _normalized_settlement_evidence(evidence)
            existing = known.get(evidence.evidence_id)
            if existing is not None and existing != normalized:
                raise ContinuousSessionError(
                    "settlement evidence id conflicts with durable evidence"
                )
            known[evidence.evidence_id] = normalized

    def record_source_projection(
        self,
        *,
        deltas: tuple[CollectorDelta, ...],
        backlog: bool,
    ) -> None:
        if type(backlog) is not bool:
            raise TypeError("backlog must be boolean")
        for delta in deltas:
            if not isinstance(delta, CollectorDelta):
                raise TypeError("deltas must contain CollectorDelta values")
            delta.validate()
            if delta.source_id != self.source_id:
                raise ContinuousSessionError(
                    "source-state projection delta belongs to another source"
                )

        def mutate(raw: dict[str, Any]) -> None:
            unresolved = set(raw["source_unresolved_gap_delta_ids"])
            projection_epoch = raw["source_projection_stream_epoch"]
            for delta in deltas:
                if projection_epoch != delta.stream_epoch:
                    unresolved.clear()
                    projection_epoch = delta.stream_epoch
                if delta.gap_state is GapState.DETECTED:
                    unresolved.add(delta.delta_id)
                elif delta.gap_state is GapState.RECOVERED:
                    if delta.revision_of is None:
                        raise ContinuousSessionError(
                            "recovered gap projection requires revision_of"
                        )
                    unresolved.discard(delta.revision_of)
                elif delta.gap_state is GapState.CURSOR_RESET:
                    unresolved.clear()

                raw["source_state_delta_id"] = delta.delta_id
                raw["source_projection_stream_epoch"] = projection_epoch
                if unresolved:
                    raw["source_gap_state"] = GapState.DETECTED.value
                    raw["source_sync_state"] = SyncState.GAP_DETECTED.value
                else:
                    raw["source_gap_state"] = delta.gap_state.value
                    raw["source_sync_state"] = delta.sync_state.value

            raw["source_unresolved_gap_delta_ids"] = sorted(unresolved)
            raw["source_state_projection_backlog"] = backlog

        self._update(mutate)

    def record_success(
        self,
        *,
        at: str,
        full_refresh: bool,
        settlement_evidence: tuple[SettlementResolution, ...],
        _normalized_settlement_evidence: Callable[
            [SettlementResolution], dict[str, str]
        ] = _normalized_settlement_evidence.__func__,
        _normalized_settlement_evidence_code: object = (
            _normalized_settlement_evidence.__func__.__code__
        ),
        _validate_resolution: Callable[..., None] = SettlementResolution.validate,
        _validate_resolution_code: object = SettlementResolution.validate.__code__,
    ) -> int:
        if (
            getattr(
                _normalized_settlement_evidence,
                "__code__",
                None,
            )
            is not _normalized_settlement_evidence_code
            or getattr(_validate_resolution, "__code__", None)
            is not _validate_resolution_code
        ):
            raise ContinuousSessionError(
                "canonical settlement evidence validation authority changed"
            )
        timestamp = _instant(at, "at")
        if type(full_refresh) is not bool:
            raise TypeError("full_refresh must be boolean")
        if type(settlement_evidence) is not tuple:
            raise TypeError("settlement_evidence must be an exact tuple")
        for evidence in settlement_evidence:
            if type(evidence) is not SettlementResolution:
                raise TypeError(
                    "settlement_evidence must contain exact SettlementResolution values"
                )
            _validate_resolution(evidence, as_of=timestamp.isoformat())

        def mutate(raw: dict[str, Any]) -> None:
            started_at = _instant(raw["started_at"], "started_at")
            if timestamp < started_at:
                raise ContinuousSessionError(
                    "success timestamp precedes session start"
                )
            if raw["last_success_at"] is not None:
                previous_success = _instant(
                    raw["last_success_at"],
                    "last_success_at",
                )
                if timestamp < previous_success:
                    raise ContinuousSessionError(
                        "success timestamp would roll back durable session time"
                    )
            raw["cycles_completed"] = int(raw["cycles_completed"]) + 1
            raw["last_success_at"] = timestamp.isoformat()
            raw["last_error_code"] = None
            if full_refresh:
                raw["last_full_refresh_at"] = timestamp.isoformat()

            known = {
                item["evidence_id"]: item
                for item in raw["settlement_evidence"]
            }
            for evidence in settlement_evidence:
                existing = known.get(evidence.evidence_id)
                normalized = _normalized_settlement_evidence(evidence)
                if existing is not None:
                    if existing != normalized:
                        raise ContinuousSessionError(
                            "settlement evidence id conflicts with durable evidence"
                        )
                    continue
                known[evidence.evidence_id] = normalized
            raw["settlement_evidence"] = list(
                sorted(known.values(), key=lambda item: item["evidence_id"])
            )

        def finalize(_updated: dict[str, Any]) -> None:
            if self._error_checkpoint_present():
                self._write_error_checkpoint(None)

        updated = self._update(
            mutate,
            advance_generation=True,
            finalize_under_lock=finalize,
        )
        return int(updated["cycles_completed"])

    def record_failure(
        self,
        *,
        code: str,
        _durable_path_lock: Callable[..., Any] = durable_path_lock,
        _durable_path_lock_code: object = durable_path_lock.__code__,
    ) -> None:
        code = _text(code, "code")
        if (
            durable_path_lock is not _durable_path_lock
            or getattr(_durable_path_lock, "__code__", None)
            is not _durable_path_lock_code
        ):
            raise ContinuousSessionError(
                "canonical failure publication lock authority changed"
            )
        with _durable_path_lock(self.path):
            # Keep failure publication bounded by active cached state.  A full
            # canonical _read() validates every retained settlement receipt and
            # would reintroduce the exact O(history) amplification this sidecar
            # exists to remove.  If another instance has advanced the canonical
            # generation, these cached markers make this sidecar stale and
            # snapshot() fails closed by refusing to overlay it.
            self._write_error_checkpoint(code)


class ContinuousSessionCoordinator(metaclass=_ContinuousSessionCoordinatorMeta):
    """Compose existing collector/lifecycle/mirror/settlement authorities into one durable loop.

    The coordinator owns only session identity/checkpoint and sequencing. It never becomes
    a market store, delta store, lifecycle store, scheduler, outcome authority, or LLM
    decision engine.
    """

    _settlement_consumer_bindings_sealed = False

    def __init__(
        self,
        *,
        workspace: str | Path,
        collector: HeadlessCollectorService,
        lifecycle: ContinuousEventLifecycle,
        market_store: SQLiteMarketStore,
        desktop_consumer: DesktopDeltaConsumer,
        invalidation_buffer: BoundedMirrorInvalidationBuffer,
        dependency_index: FocusedMirrorDependencyIndex,
        paper_book_path: str | Path | None = None,
        outcome_authority: SettlementOutcomeAuthority | None = None,
        settlement_learning_handoff: SettlementLearningHandoff | None = None,
        session_id: str | None = None,
        clock: Callable[[], str] | None = None,
        required_history: timedelta = timedelta(0),
        max_invalidation_batches_per_tick: int = 4,
        max_invalidation_items_per_batch: int = 250,
        causal_view: CausalView = CausalView.AS_KNOWN_AT_DECISION,
        initial_bankroll: str = "10000",
    ) -> None:
        if not isinstance(workspace, (str, Path)):
            raise TypeError("workspace must be a path-like value")
        if not isinstance(collector, HeadlessCollectorService):
            raise TypeError("collector must be HeadlessCollectorService")
        if not isinstance(lifecycle, ContinuousEventLifecycle):
            raise TypeError("lifecycle must be ContinuousEventLifecycle")
        if not isinstance(market_store, SQLiteMarketStore):
            raise TypeError("market_store must be SQLiteMarketStore")
        if not isinstance(desktop_consumer, DesktopDeltaConsumer):
            raise TypeError("desktop_consumer must be DesktopDeltaConsumer")
        if not isinstance(invalidation_buffer, BoundedMirrorInvalidationBuffer):
            raise TypeError(
                "invalidation_buffer must be BoundedMirrorInvalidationBuffer"
            )
        if not isinstance(dependency_index, FocusedMirrorDependencyIndex):
            raise TypeError("dependency_index must be FocusedMirrorDependencyIndex")
        if outcome_authority is not None and not callable(
            getattr(outcome_authority, "resolve", None)
        ):
            raise TypeError("outcome_authority.resolve must be callable")
        if settlement_learning_handoff is not None:
            if not callable(
                getattr(settlement_learning_handoff, "reconcile_after_settlement", None)
            ):
                raise TypeError(
                    "settlement_learning_handoff.reconcile_after_settlement must be callable"
                )
            prepare = getattr(settlement_learning_handoff, "prepare_settlement", None)
            if prepare is not None and not callable(prepare):
                raise TypeError(
                    "settlement_learning_handoff.prepare_settlement must be callable"
                )

        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.collector = collector
        self.lifecycle = lifecycle
        self.market_store = market_store
        self.desktop_consumer = desktop_consumer
        self.invalidation_buffer = invalidation_buffer
        self.dependency_index = dependency_index
        self.paper_book_path = (
            self.workspace / "paper_book.json"
            if paper_book_path is None
            else Path(paper_book_path)
        )
        self.outcome_authority = outcome_authority
        self.settlement_learning_handoff = settlement_learning_handoff
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
        if isinstance(required_history, timedelta) and required_history.total_seconds() < 0:
            raise ValueError("required_history cannot be negative")
        if not isinstance(required_history, timedelta):
            raise TypeError("required_history must be timedelta")
        self.required_history = required_history
        for name, value in (
            ("max_invalidation_batches_per_tick", max_invalidation_batches_per_tick),
            ("max_invalidation_items_per_batch", max_invalidation_items_per_batch),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
            ):
                raise ValueError(f"{name} must be a positive integer")
        self.max_invalidation_batches_per_tick = max_invalidation_batches_per_tick
        self.max_invalidation_items_per_batch = max_invalidation_items_per_batch
        try:
            self.causal_view = CausalView(causal_view)
        except ValueError as exc:
            raise ValueError("unsupported causal_view") from exc
        try:
            initial_bankroll = str(initial_bankroll)
            PaperBook(initial_bankroll)
        except Exception as exc:
            raise ValueError("initial_bankroll must construct a valid PaperBook") from exc
        self.initial_bankroll = initial_bankroll
        self._state = _ContinuousSessionState(
            self.workspace / "continuous_session.json",
            session_id=session_id,
            source_id=collector.source_id,
            clock=self.clock,
        )

    @property
    def session_id(self) -> str:
        return self._state.session_id

    def status(self) -> ContinuousSessionStatus:
        snapshot = self._state.snapshot()
        source_status = self.collector.status()
        source_last_success = source_status.get("last_success_at")
        source_last_error = source_status.get("last_error_code")
        if source_last_success is not None and not isinstance(source_last_success, str):
            raise ContinuousSessionError("collector last_success_at must be a string or None")
        if source_last_error is not None and not isinstance(source_last_error, str):
            raise ContinuousSessionError("collector last_error_code must be a string or None")
        return replace(
            snapshot,
            source_provider_unavailable=source_last_error == "ProviderUnavailableError",
            source_last_success_at=source_last_success,
            source_last_error_code=source_last_error,
            source_gap_state=snapshot.source_gap_state,
            source_sync_state=snapshot.source_sync_state,
            source_state_delta_id=snapshot.source_state_delta_id,
            source_unresolved_gap_delta_ids=snapshot.source_unresolved_gap_delta_ids,
            source_projection_stream_epoch=snapshot.source_projection_stream_epoch,
            source_state_projection_backlog=snapshot.source_state_projection_backlog,
            invalidation_pending_count=self.invalidation_buffer.pending_count,
            invalidation_full_refresh_required=bool(
                self.invalidation_buffer.full_refresh_required
            ),
        )

    def pause(self) -> None:
        self._state.set_state(SessionState.PAUSED)

    def stop(self, reason: str = "operator_stop") -> None:
        self._state.set_state(SessionState.STOPPED, reason=reason)

    def resume(self) -> None:
        current = self._state.snapshot().state
        if current not in {SessionState.PAUSED, SessionState.STOPPED}:
            return
        self._state.set_state(SessionState.RUNNING)

    def _require_running(self) -> None:
        state = self._state.snapshot().state
        if state is SessionState.PAUSED:
            raise SessionPausedError("continuous session is durably PAUSED")
        if state is SessionState.STOPPED:
            raise SessionStoppedError("continuous session is durably STOPPED")

    def _register_input(self, input_id: str, **selectors: object) -> None:
        if input_id in self.dependency_index.input_ids:
            return
        self.dependency_index.register(input_id, **selectors)

    def _retire_input(self, input_id: str) -> None:
        self.dependency_index.unregister(input_id)

    def _drain_invalidations(self) -> tuple[
        tuple[str, ...],
        bool,
        bool,
    ]:
        affected: list[str] = []
        full_refresh_required = False
        backlog = False

        for _ in range(self.max_invalidation_batches_per_tick):
            batch = self.invalidation_buffer.drain(
                max_items=self.max_invalidation_items_per_batch
            )
            if not isinstance(batch, MirrorInvalidationBatch):
                raise ContinuousSessionError(
                    "invalidation buffer returned an invalid batch"
                )
            routed = self.dependency_index.affected_inputs(batch)
            affected.extend(routed)
            full_refresh_required = full_refresh_required or batch.full_refresh_required
            if not batch.has_more:
                break
        else:
            backlog = self.invalidation_buffer.pending_count > 0 or (
                self.invalidation_buffer.full_refresh_required
            )
        return tuple(dict.fromkeys(affected)), full_refresh_required, backlog

    def _refresh_source_state_projection(self) -> ContinuousSessionStatus:
        snapshot = self._state.snapshot()
        deltas = self.collector.delta_store.deltas_after_commit(
            source_id=self.collector.source_id,
            after_delta_id=snapshot.source_state_delta_id,
            max_items=self.collector.config.max_items + 1,
        )
        backlog = len(deltas) > self.collector.config.max_items
        selected = deltas[: self.collector.config.max_items]
        self._state.record_source_projection(
            deltas=selected,
            backlog=backlog,
        )
        return self._state.snapshot()

    def _settlement_resolutions(
        self,
        *,
        as_of: str,
    ) -> tuple[SettlementResolution, ...]:
        if self.outcome_authority is None:
            return ()
        resolutions: list[SettlementResolution] = []
        for record in self.lifecycle.records():
            if record.phase is not EventPhase.COMPLETED or record.settlement_ref is None:
                continue
            resolution = self.outcome_authority.resolve(record, as_of=as_of)
            if resolution is None:
                continue
            if not isinstance(resolution, SettlementResolution):
                raise ContinuousSessionError(
                    "outcome authority must return SettlementResolution or None"
                )
            if resolution.event_identity != record.identity:
                raise ContinuousSessionError(
                    "settlement evidence event identity does not match lifecycle identity"
                )
            if resolution.settlement_ref != record.settlement_ref:
                raise ContinuousSessionError(
                    "settlement evidence reference does not match lifecycle evidence"
                )
            resolution.validate(as_of=as_of)
            resolutions.append(resolution)
        return tuple(resolutions)

    def _load_book(self) -> PaperBook:
        if self.paper_book_path.exists():
            return PaperBook.load(self.paper_book_path)
        return PaperBook(self.initial_bankroll)

    @_seal_settlement_consumer_entry
    @_bind_canonical_settlement_engine
    def _settle(
        self,
        *,
        resolutions: tuple[SettlementResolution, ...],
        _settlement_engine_type: type[SettlementEngine],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if not resolutions:
            return (), ()
        if SettlementEngine is not _settlement_engine_type:
            raise ContinuousSessionError(
                "settlement engine constructor origin changed"
            )
        unique: dict[str, SettlementResolution] = {}
        for resolution in resolutions:
            unique.setdefault(resolution.evidence_id, resolution)

        with WorkspaceEconomicLock(self.workspace):
            book = self._load_book()
            engine = _settlement_engine_type()
            if type(engine) is not _settlement_engine_type:
                raise ContinuousSessionError(
                    "settlement engine constructor returned non-canonical type"
                )
            for resolution in unique.values():
                allowed = self._open_quote_keys_for_book(book, resolution.event_identity)
                scoped = {
                    quote_key: outcome
                    for quote_key, outcome in resolution.quote_outcomes.items()
                    if quote_key in allowed
                }
                if scoped:
                    engine.record(scoped)
            settled = tuple(engine.settle_ready(book))
            if settled:
                book.save(self.paper_book_path)

        return settled, tuple(unique)

    @staticmethod
    def _open_quote_keys_for_book(
        book: PaperBook,
        event_identity: str,
    ) -> set[str]:
        parts = {event_identity}
        if ":" in event_identity:
            parts.add(event_identity.split(":", 1)[1])
        return {
            leg.quote_key
            for ticket in book.tickets.values()
            if ticket.status.value == "open"
            for leg in ticket.legs
            if leg.event_id in parts
        }

    def tick(self) -> ContinuousTickResult:
        self._require_running()
        now = self.clock()
        _instant(now, "now")
        try:
            cycle = self.collector.run_cycle()
            source_snapshot = self._refresh_source_state_projection()
            if cycle.provider_unavailable:
                self._state.record_failure(code="ProviderUnavailableError")
                snapshot = self._state.snapshot()
                return ContinuousTickResult(
                    session_id=self.session_id,
                    cycle_index=snapshot.cycles_completed,
                    source_id=cycle.source_id,
                    source_provider_unavailable=True,
                    source_gap_states=(
                        ()
                        if source_snapshot.source_gap_state is None
                        else (source_snapshot.source_gap_state,)
                    ),
                    source_sync_states=(
                        ()
                        if source_snapshot.source_sync_state is None
                        else (source_snapshot.source_sync_state,)
                    ),
                    committed_delta_ids=cycle.committed_delta_ids,
                    delivered_delta_ids=(),
                    affected_input_ids=(),
                    registered_input_ids=(),
                    retired_input_ids=(),
                    full_refresh_required=bool(
                        self.invalidation_buffer.full_refresh_required
                    ),
                    invalidation_backlog=(
                        self.invalidation_buffer.pending_count > 0
                        or self.invalidation_buffer.full_refresh_required
                    ),
                    settled_ticket_ids=(),
                    settlement_evidence_ids=(),
                    last_success_at=snapshot.last_success_at,
                )

            source_gap_states = (
                ()
                if source_snapshot.source_gap_state is None
                else (source_snapshot.source_gap_state,)
            )
            source_sync_states = (
                ()
                if source_snapshot.source_sync_state is None
                else (source_snapshot.source_sync_state,)
            )
            delivered = self.desktop_consumer.drain(
                as_of=now,
                view=self.causal_view,
            )
            affected, full_refresh, backlog = self._drain_invalidations()

            newly_registered: list[str] = []
            retired: list[str] = []

            def register(input_id: str, **selectors: object) -> None:
                before = input_id in self.dependency_index.input_ids
                self._register_input(input_id, **selectors)
                if not before:
                    newly_registered.append(input_id)

            def retire(input_id: str) -> None:
                before = input_id in self.dependency_index.input_ids
                self._retire_input(input_id)
                if before:
                    retired.append(input_id)

            registered = self.lifecycle.register_eligible(
                self.market_store,
                as_of=now,
                required_history=self.required_history,
                register_input=register,
                retire_input=retire,
            )
            # The lifecycle is canonical about eligibility; the index is canonical
            # about dependency routing. Keep both outputs for auditability.
            for input_id in registered:
                if input_id not in newly_registered:
                    newly_registered.append(input_id)

            resolutions = self._settlement_resolutions(as_of=now)
            self._state.validate_settlement_evidence(
                settlement_evidence=resolutions
            )
            if self.settlement_learning_handoff is not None:
                prepare = getattr(
                    self.settlement_learning_handoff,
                    "prepare_settlement",
                    None,
                )
                if prepare is not None:
                    prepare(
                        paper_book_path=self.paper_book_path,
                        resolutions=resolutions,
                        at=now,
                    )
            settled, evidence_ids = self._settle(resolutions=resolutions)
            if self.settlement_learning_handoff is not None:
                self.settlement_learning_handoff.reconcile_after_settlement(
                    paper_book_path=self.paper_book_path,
                    resolutions=resolutions,
                    settled_ticket_ids=settled,
                    at=now,
                )

            cycle_index = self._state.record_success(
                at=now,
                full_refresh=full_refresh,
                settlement_evidence=resolutions,
            )
            return ContinuousTickResult(
                session_id=self.session_id,
                cycle_index=cycle_index,
                source_id=cycle.source_id,
                source_provider_unavailable=False,
                source_gap_states=source_gap_states,
                source_sync_states=source_sync_states,
                committed_delta_ids=cycle.committed_delta_ids,
                delivered_delta_ids=delivered,
                affected_input_ids=affected,
                registered_input_ids=tuple(newly_registered),
                retired_input_ids=tuple(retired),
                full_refresh_required=full_refresh,
                invalidation_backlog=backlog,
                settled_ticket_ids=settled,
                settlement_evidence_ids=evidence_ids,
                last_success_at=self._state.snapshot().last_success_at or now,
            )
        except Exception as exc:
            self._state.record_failure(code=type(exc).__name__)
            raise

# Seal the consumer entry after class creation. The metaclass data descriptor also
# makes direct type.__setattr__/type.__delattr__ respect the same class-level fence.
_ContinuousSessionCoordinatorMeta._settle = _build_settlement_consumer_class_guard(
    "_settle"
)
ContinuousSessionCoordinator._settlement_consumer_bindings_sealed = True
