from __future__ import annotations

from contextlib import contextmanager
import json
import os
import stat
import sys
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Final, Iterator, Protocol

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
    FocusedMirrorDependency,
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


def _validate_canonical_invalidation_buffer_state(
    buffer: BoundedMirrorInvalidationBuffer,
) -> None:
    """Fail closed when the canonical invalidation buffer's internal truth is malformed."""
    dirty = buffer._dirty
    max_dirty_keys = buffer._max_dirty_keys
    full_refresh_required = buffer._full_refresh_required
    if (
        type(dirty) is not dict
        or type(max_dirty_keys) is not int
        or max_dirty_keys <= 0
        or type(full_refresh_required) is not bool
        or len(dirty) > max_dirty_keys
        or (full_refresh_required and bool(dirty))
    ):
        raise ContinuousSessionError(
            "canonical invalidation buffer state is invalid"
        )


_OUTCOME_AUTHORITY_UNSET: Final = object()


def _bind_canonical_settlement_engine(method):
    """Inject canonical settlement authorities through a closure-owned seam."""

    canonical_engine_type = SettlementEngine
    canonical_resolution_validate = SettlementResolution.validate
    canonical_replace = replace
    canonical_paper_book_save = PaperBook.save

    def guarded(self, *args, **kwargs):
        if "_settlement_engine_type" in kwargs:
            raise TypeError("settlement engine origin is internal product authority")
        if "_resolution_validate" in kwargs:
            raise TypeError("settlement validator origin is internal product authority")
        if "_replace" in kwargs:
            raise TypeError("settlement copy authority is internal product authority")
        if "_paper_book_save" in kwargs:
            raise TypeError("settlement persistence authority is internal product authority")
        kwargs["_settlement_engine_type"] = canonical_engine_type
        kwargs["_resolution_validate"] = canonical_resolution_validate
        kwargs["_replace"] = canonical_replace
        kwargs["_paper_book_save"] = canonical_paper_book_save
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
        protected = {
            "_settle",
            "_load_book",
            "_open_quote_keys_for_book",
            "_settlement_consumer_bindings_sealed",
        }
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
            "_load_book",
            "_open_quote_keys_for_book",
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
            "_load_book",
            "_open_quote_keys_for_book",
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


@dataclass(frozen=True, slots=True)
class _ContinuousSessionFailurePublication:
    """Bounded publication receipt returned by the operational failure checkpoint."""

    session_id: str
    state: SessionState
    generation: int
    cycles_completed: int
    last_success_at: str | None
    last_error_code: str
    source_gap_state: str | None
    source_sync_state: str | None


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
_EXPECTED_PROJECTION_UNSET: Final = object()


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
        _path_exists: Callable[[Path], bool] = Path.exists,
        _path_exists_code: object = Path.exists.__code__,
    ) -> None:
        if (
            getattr(_atomic_write_json, "__code__", None) is not _atomic_write_json_code
            or durable_path_lock is not _durable_path_lock
            or getattr(_durable_path_lock, "__code__", None)
            is not _durable_path_lock_code
            or Path.exists is not _path_exists
            or getattr(_path_exists, "__code__", None) is not _path_exists_code
        ):
            raise ContinuousSessionError(
                "canonical session bootstrap authority changed"
            )
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.source_id = _text(source_id, "source_id")
        self._clock = clock
        self._error_path = self.path.with_name(
            f"{self.path.name}.operational_error.json"
        )

        # Bootstrap is a read/create/read transaction on the canonical session
        # path.  Without this fence, two processes can both observe absence and
        # publish different session identities; the losing constructor could
        # then silently adopt the winner's identity.
        with _durable_path_lock(self.path):
            if _path_exists(self.path):
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
            checkpoint_token = self._checkpoint_identity_token()

            # The canonical checkpoint and bounded operational overlay are one
            # bootstrap observation. Keep both under the same session lock so a
            # concurrent writer cannot advance the main generation and sidecar
            # between reads, producing a false "sidecar ahead" bootstrap fault.
            self._session_id = raw["session_id"]
            self._generation = raw["generation"]
            self._cycles_completed = raw["cycles_completed"]
            self._last_success_at = raw["last_success_at"]
            self._state = raw["state"]
            self._last_error_code = raw["last_error_code"]
            self._source_gap_state = raw["source_gap_state"]
            self._source_sync_state = raw["source_sync_state"]
            self._checkpoint_token = checkpoint_token
            if self._error_checkpoint_present():
                error_checkpoint = self._read_error_checkpoint()
                if error_checkpoint["observed_generation"] > self._generation:
                    raise ContinuousSessionError(
                        "operational error checkpoint generation is ahead of "
                        "canonical session bootstrap state"
                    )
                same_generation = (
                    error_checkpoint["observed_generation"] == self._generation
                )
                if same_generation and (
                    error_checkpoint["observed_cycles_completed"] != self._cycles_completed
                    or error_checkpoint["observed_last_success_at"] != self._last_success_at
                    or error_checkpoint["observed_state"] != self._state
                ):
                    raise ContinuousSessionError(
                        "same-generation operational error checkpoint markers "
                        "conflict with canonical session bootstrap state"
                    )
                if same_generation and error_checkpoint["last_error_code"] is not None:
                    durable_error = raw["last_error_code"]
                    checkpoint_error = error_checkpoint["last_error_code"]
                    if durable_error is not None and durable_error != checkpoint_error:
                        raise ContinuousSessionError(
                            "continuous session error authorities conflict at bootstrap"
                        )

    @staticmethod
    def _file_identity(info: os.stat_result) -> tuple[int, int]:
        return (info.st_dev, info.st_ino)

    def _checkpoint_identity_token(
        self,
        *,
        _os_stat: Callable[..., os.stat_result] = os.stat,
    ) -> tuple[int, int, int, int, int]:
        if os.stat is not _os_stat:
            raise ContinuousSessionError(
                "canonical session checkpoint identity authority changed"
            )
        try:
            info = _os_stat(self.path)
        except OSError as exc:
            raise ContinuousSessionError(
                "cannot inspect continuous session checkpoint identity"
            ) from exc
        return (
            int(info.st_dev),
            int(info.st_ino),
            int(info.st_size),
            int(info.st_mtime_ns),
            int(info.st_ctime_ns),
        )

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
            or type(self)._bounded_descriptor_read is not _bounded_descriptor_read
            or getattr(_bounded_descriptor_read, "__code__", None)
            is not _bounded_descriptor_read_code
            or type(self)._file_identity is not _file_identity
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
            pending_error = sys.exception()
            if descriptor is not None:
                try:
                    _os_close(descriptor)
                except OSError as exc:
                    if pending_error is None:
                        raise ContinuousSessionError(
                            "cannot close continuous session operational error checkpoint"
                        ) from exc
                    try:
                        pending_error.add_note(
                            "continuous session operational error checkpoint "
                            f"descriptor close also failed: {type(exc).__name__}: {exc}"
                        )
                    except BaseException:
                        pass

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
        _text_validator: Callable[[object, str], str] = _text,
        _text_validator_code: object = _text.__code__,
        _instant_validator: Callable[[object, str], datetime] = _instant,
        _instant_validator_code: object = _instant.__code__,
    ) -> dict[str, Any]:
        if (
            getattr(_strict_json_loads, "__code__", None) is not _strict_json_loads_code
            or type(self)._read_error_checkpoint_bytes is not _read_error_checkpoint_bytes
            or getattr(_read_error_checkpoint_bytes, "__code__", None)
            is not _read_error_checkpoint_bytes_code
            or _text is not _text_validator
            or getattr(_text_validator, "__code__", None) is not _text_validator_code
            or _instant is not _instant_validator
            or getattr(_instant_validator, "__code__", None) is not _instant_validator_code
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
                _instant_validator(
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
                error_code = _text_validator(
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
        _text_validator: Callable[[object, str], str] = _text,
        _text_validator_code: object = _text.__code__,
    ) -> None:
        if (
            getattr(_json_dumps, "__code__", None) is not _json_dumps_code
            or json.dump is not _json_dump
            or getattr(_json_dump, "__code__", None) is not _json_dump_code
            or getattr(_atomic_write_json, "__code__", None)
            is not _atomic_write_json_code
            or _text is not _text_validator
            or getattr(_text_validator, "__code__", None) is not _text_validator_code
        ):
            raise ContinuousSessionError(
                "canonical operational-checkpoint writer code identity changed"
            )
        if code is not None:
            code = _text_validator(code, "code")
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
    def _validate_settlement_evidence(
        raw: object,
        *,
        _text_validator: Callable[[object, str], str] = _text,
        _text_validator_code: object = _text.__code__,
        _sha256_validator: Callable[[object, str], str] = _sha256,
        _sha256_validator_code: object = _sha256.__code__,
        _instant_validator: Callable[[object, str], datetime] = _instant,
        _instant_validator_code: object = _instant.__code__,
    ) -> tuple[dict[str, str], ...]:
        if (
            _text is not _text_validator
            or getattr(_text_validator, "__code__", None) is not _text_validator_code
            or _sha256 is not _sha256_validator
            or getattr(_sha256_validator, "__code__", None)
            is not _sha256_validator_code
            or _instant is not _instant_validator
            or getattr(_instant_validator, "__code__", None)
            is not _instant_validator_code
        ):
            raise ContinuousSessionError(
                "canonical settlement evidence parser authority changed"
            )
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
            _text_validator(item["event_identity"], "settlement_evidence event_identity")
            _text_validator(item["settlement_ref"], "settlement_evidence settlement_ref")
            evidence_id = _text_validator(
                item["evidence_id"],
                "settlement_evidence evidence_id",
            )
            if evidence_id in evidence_ids:
                raise ContinuousSessionError(
                    "settlement_evidence evidence_id values must be unique"
                )
            evidence_ids.add(evidence_id)
            _sha256_validator(
                item["evidence_sha256"],
                "settlement_evidence evidence_sha256",
            )
            _instant_validator(
                item["available_at"],
                "settlement_evidence available_at",
            )
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
        _instant_validator: Callable[[object, str], datetime] = _instant,
        _instant_validator_code: object = _instant.__code__,
        _text_validator: Callable[[object, str], str] = _text,
        _text_validator_code: object = _text.__code__,
    ) -> dict[str, Any]:
        if (
            getattr(_strict_json_loads, "__code__", None) is not _strict_json_loads_code
            or getattr(_path_read_text, "__code__", None) is not _path_read_text_code
            or getattr(_validate_settlement_evidence, "__code__", None)
            is not _validate_settlement_evidence_code
            or _instant is not _instant_validator
            or getattr(_instant_validator, "__code__", None)
            is not _instant_validator_code
            or _text is not _text_validator
            or getattr(_text_validator, "__code__", None)
            is not _text_validator_code
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
            _text_validator(raw["session_id"], "session_id")
            started_at = _instant_validator(raw["started_at"], "started_at")
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
            last_success_at = (
                None
                if raw["last_success_at"] is None
                else _instant_validator(raw["last_success_at"], "last_success_at")
            )
            last_full_refresh_at = (
                None
                if raw["last_full_refresh_at"] is None
                else _instant_validator(raw["last_full_refresh_at"], "last_full_refresh_at")
            )
            if (cycles == 0) != (last_success_at is None):
                raise ContinuousSessionError(
                    "continuous session cycle count and last_success_at disagree"
                )
            if last_success_at is not None and last_success_at < started_at:
                raise ContinuousSessionError(
                    "continuous session last_success_at precedes session start"
                )
            if last_full_refresh_at is not None and (
                last_success_at is None
                or last_full_refresh_at < started_at
                or last_full_refresh_at > last_success_at
            ):
                raise ContinuousSessionError(
                    "continuous session full-refresh timestamp is not causally valid"
                )
            if raw["last_error_code"] is not None:
                _text_validator(raw["last_error_code"], "last_error_code")
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
                _text_validator(raw["source_state_delta_id"], "source_state_delta_id")
            if raw["source_projection_stream_epoch"] is not None:
                _text_validator(
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
        _read_method: Callable[["_ContinuousSessionState"], dict[str, Any]] = _read,
        _read_method_code: object = _read.__code__,
    ) -> ContinuousSessionStatus:
        if (
            type(self)._error_checkpoint_present is not _error_checkpoint_present
            or getattr(_error_checkpoint_present, "__code__", None)
            is not _error_checkpoint_present_code
            or type(self)._read_error_checkpoint is not _read_error_checkpoint
            or getattr(_read_error_checkpoint, "__code__", None)
            is not _read_error_checkpoint_code
            or durable_path_lock is not _durable_path_lock
            or getattr(_durable_path_lock, "__code__", None)
            is not _durable_path_lock_code
            or type(self)._read is not _read_method
            or getattr(_read_method, "__code__", None) is not _read_method_code
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
            raw = _read_method(self)
            if _error_checkpoint_present(self):
                error_checkpoint = _read_error_checkpoint(self)
                if error_checkpoint["observed_generation"] > raw["generation"]:
                    raise ContinuousSessionError(
                        "operational error checkpoint generation is ahead of "
                        "canonical session state"
                    )
                same_generation = (
                    error_checkpoint["observed_generation"] == raw["generation"]
                )
                marker_matches = (
                    same_generation
                    and error_checkpoint["observed_cycles_completed"] == raw["cycles_completed"]
                    and error_checkpoint["observed_last_success_at"] == raw["last_success_at"]
                    and error_checkpoint["observed_state"] == raw["state"]
                )
                if same_generation and not marker_matches:
                    raise ContinuousSessionError(
                        "same-generation operational error checkpoint markers "
                        "conflict with canonical session state"
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

    def bounded_state(
        self,
        *,
        _durable_path_lock: Callable[..., Any] = durable_path_lock,
        _durable_path_lock_code: object = durable_path_lock.__code__,
        _checkpoint_identity_token: Callable[
            ["_ContinuousSessionState"], tuple[int, int, int, int, int]
        ] = _checkpoint_identity_token,
        _checkpoint_identity_token_code: object = _checkpoint_identity_token.__code__,
        _read_method: Callable[["_ContinuousSessionState"], dict[str, Any]] = _read,
        _read_method_code: object = _read.__code__,
    ) -> SessionState:
        if (
            durable_path_lock is not _durable_path_lock
            or getattr(_durable_path_lock, "__code__", None)
            is not _durable_path_lock_code
            or type(self)._checkpoint_identity_token is not _checkpoint_identity_token
            or getattr(_checkpoint_identity_token, "__code__", None)
            is not _checkpoint_identity_token_code
            or type(self)._read is not _read_method
            or getattr(_read_method, "__code__", None) is not _read_method_code
        ):
            raise ContinuousSessionError(
                "canonical bounded session-state authority changed"
            )
        with _durable_path_lock(self.path):
            current_token = _checkpoint_identity_token(self)
            if current_token != self._checkpoint_token:
                # Another canonical writer changed the session checkpoint.
                # Refresh full durable truth only on that external-change edge;
                # steady-state running checks remain independent of retained
                # settlement-history size.
                raw = _read_method(self)
                self._generation = raw["generation"]
                self._cycles_completed = raw["cycles_completed"]
                self._last_success_at = raw["last_success_at"]
                self._state = raw["state"]
                self._last_error_code = raw["last_error_code"]
                self._source_gap_state = raw["source_gap_state"]
                self._source_sync_state = raw["source_sync_state"]
                self._checkpoint_token = _checkpoint_identity_token(self)
            return SessionState(self._state)

    @contextmanager
    def running_fence(
        self,
        *,
        _durable_path_lock: Callable[..., Any] = durable_path_lock,
        _durable_path_lock_code: object = durable_path_lock.__code__,
        _bounded_state: Callable[["_ContinuousSessionState"], SessionState] = bounded_state,
        _bounded_state_code: object = bounded_state.__code__,
    ) -> Iterator[None]:
        if (
            durable_path_lock is not _durable_path_lock
            or getattr(_durable_path_lock, "__code__", None)
            is not _durable_path_lock_code
            or type(self).bounded_state is not _bounded_state
            or getattr(_bounded_state, "__code__", None) is not _bounded_state_code
        ):
            raise ContinuousSessionError(
                "canonical running-fence authority changed"
            )
        with _durable_path_lock(self.path):
            state = _bounded_state(self)
            if state is SessionState.PAUSED:
                raise SessionPausedError("continuous session is durably PAUSED")
            if state is SessionState.STOPPED:
                raise SessionStoppedError("continuous session is durably STOPPED")
            yield

    @property
    def session_id(self) -> str:
        # Session identity is fixed by the serialized bootstrap transaction and
        # cached for the lifetime of this state object. Reading it must remain
        # independent of retained settlement-history size.
        return self._session_id

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
        _read_method: Callable[["_ContinuousSessionState"], dict[str, Any]] = _read,
        _read_method_code: object = _read.__code__,
        _checkpoint_identity_token_method: Callable[
            ["_ContinuousSessionState"], tuple[int, int, int, int, int]
        ] = _checkpoint_identity_token,
        _checkpoint_identity_token_method_code: object = _checkpoint_identity_token.__code__,
    ) -> dict[str, Any]:
        if (
            getattr(_atomic_write_json, "__code__", None) is not _atomic_write_json_code
            or durable_path_lock is not _durable_path_lock
            or getattr(_durable_path_lock, "__code__", None)
            is not _durable_path_lock_code
            or type(self)._read is not _read_method
            or getattr(_read_method, "__code__", None) is not _read_method_code
            or type(self)._checkpoint_identity_token
            is not _checkpoint_identity_token_method
            or getattr(_checkpoint_identity_token_method, "__code__", None)
            is not _checkpoint_identity_token_method_code
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
            raw = _read_method(self)
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
                updated = _read_method(self)
            # Cache the committed canonical image before any post-commit sidecar
            # finalization. If finalization fails after the main checkpoint was
            # durably published, this in-process state must still represent the
            # committed generation; otherwise a caught cleanup error can leave
            # the instance publishing stale operational-failure receipts.
            self._generation = updated["generation"]
            self._cycles_completed = updated["cycles_completed"]
            self._last_success_at = updated["last_success_at"]
            self._state = updated["state"]
            self._last_error_code = updated["last_error_code"]
            self._source_gap_state = updated["source_gap_state"]
            self._source_sync_state = updated["source_sync_state"]
            self._checkpoint_token = _checkpoint_identity_token_method(self)
            if mutation_result is not False and finalize_under_lock is not None:
                finalize_under_lock(updated)
        return updated

    def set_state(
        self,
        state: SessionState,
        *,
        reason: str | None = None,
        _error_checkpoint_present: Callable[["_ContinuousSessionState"], bool] = (
            _error_checkpoint_present
        ),
        _error_checkpoint_present_code: object = _error_checkpoint_present.__code__,
        _read_error_checkpoint: Callable[
            ["_ContinuousSessionState"], dict[str, Any]
        ] = _read_error_checkpoint,
        _read_error_checkpoint_code: object = _read_error_checkpoint.__code__,
        _update_method: Callable[..., dict[str, Any]] = _update,
        _update_method_code: object = _update.__code__,
        _text_validator: Callable[[object, str], str] = _text,
        _text_validator_code: object = _text.__code__,
    ) -> None:
        if not isinstance(state, SessionState):
            raise TypeError("state must be SessionState")
        if (
            type(self)._error_checkpoint_present is not _error_checkpoint_present
            or getattr(_error_checkpoint_present, "__code__", None)
            is not _error_checkpoint_present_code
            or type(self)._read_error_checkpoint is not _read_error_checkpoint
            or getattr(_read_error_checkpoint, "__code__", None)
            is not _read_error_checkpoint_code
            or type(self)._update is not _update_method
            or getattr(_update_method, "__code__", None) is not _update_method_code
            or _text is not _text_validator
            or getattr(_text_validator, "__code__", None) is not _text_validator_code
        ):
            raise ContinuousSessionError(
                "canonical state-transition error authority changed"
            )

        def mutate(raw: dict[str, Any]) -> bool:
            normalized_reason = (
                None if reason is None else _text_validator(reason, "reason")
            )
            if normalized_reason is not None:
                desired_error = normalized_reason
            elif state is SessionState.RUNNING:
                # A successful operator resume establishes RUNNING as the
                # current durable state; a stop/pause reason from the
                # predecessor generation is no longer an active error.
                desired_error = None
            else:
                # Failure publication is intentionally bounded and therefore
                # lives in the same-generation sidecar rather than rewriting
                # the potentially history-sized canonical checkpoint.  A
                # PAUSE/STOP transition without a new operator reason must
                # preserve that active failure when it advances the canonical
                # generation; otherwise the tombstone written below would
                # silently erase the last operational fault.
                desired_error = raw["last_error_code"]
                if _error_checkpoint_present(self):
                    checkpoint = _read_error_checkpoint(self)
                    if checkpoint["observed_generation"] > raw["generation"]:
                        raise ContinuousSessionError(
                            "operational error checkpoint generation is ahead of "
                            "canonical session state transition"
                        )
                    marker_matches = (
                        checkpoint["observed_generation"] == raw["generation"]
                        and checkpoint["observed_cycles_completed"]
                        == raw["cycles_completed"]
                        and checkpoint["observed_last_success_at"]
                        == raw["last_success_at"]
                        and checkpoint["observed_state"] == raw["state"]
                    )
                    if (
                        checkpoint["observed_generation"] == raw["generation"]
                        and not marker_matches
                    ):
                        raise ContinuousSessionError(
                            "same-generation operational error checkpoint markers "
                            "conflict with canonical session state transition"
                        )
                    if marker_matches and checkpoint["last_error_code"] is not None:
                        checkpoint_error = checkpoint["last_error_code"]
                        if (
                            desired_error is not None
                            and desired_error != checkpoint_error
                        ):
                            raise ContinuousSessionError(
                                "continuous session error authorities conflict "
                                "during state transition"
                            )
                        desired_error = checkpoint_error

            if (
                raw["state"] == state.value
                and raw["last_error_code"] == desired_error
            ):
                return False
            raw["state"] = state.value
            raw["last_error_code"] = desired_error
            return True

        def finalize(_updated: dict[str, Any]) -> None:
            # Publish a bounded same-generation tombstone even when no prior
            # sidecar exists. Without it, a stale process that still caches the
            # predecessor generation can publish and return a stale failure
            # receipt because bounded record_failure() deliberately avoids the
            # O(history) canonical-session read.
            self._write_error_checkpoint(None)

        _update_method(
            self,
            mutate,
            advance_generation=True,
            finalize_under_lock=finalize,
        )

    @staticmethod
    def _normalized_settlement_evidence(
        evidence: SettlementResolution,
        *,
        _instant_validator: Callable[[object, str], datetime] = _instant,
        _instant_validator_code: object = _instant.__code__,
    ) -> dict[str, str]:
        if (
            _instant is not _instant_validator
            or getattr(_instant_validator, "__code__", None)
            is not _instant_validator_code
        ):
            raise ContinuousSessionError(
                "canonical settlement evidence timestamp authority changed"
            )
        return {
            "event_identity": evidence.event_identity,
            "settlement_ref": evidence.settlement_ref,
            "evidence_id": evidence.evidence_id,
            "evidence_sha256": evidence.evidence_sha256,
            "available_at": _instant_validator(
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
        _read_method: Callable[["_ContinuousSessionState"], dict[str, Any]] = _read,
        _read_method_code: object = _read.__code__,
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
        if (
            type(self)._read is not _read_method
            or getattr(_read_method, "__code__", None) is not _read_method_code
        ):
            raise ContinuousSessionError(
                "canonical settlement evidence history authority changed"
            )
        raw = _read_method(self)
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
        expected_after_delta_id: str | None | object = _EXPECTED_PROJECTION_UNSET,
        _update_method: Callable[..., dict[str, Any]] = _update,
        _update_method_code: object = _update.__code__,
        _text_validator: Callable[[object, str], str] = _text,
        _text_validator_code: object = _text.__code__,
        _delta_validate: Callable[[CollectorDelta], None] = CollectorDelta.validate,
        _delta_validate_code: object = CollectorDelta.validate.__code__,
    ) -> None:
        if (
            type(self)._update is not _update_method
            or getattr(_update_method, "__code__", None) is not _update_method_code
        ):
            raise ContinuousSessionError(
                "canonical source-projection read-modify-write authority changed"
            )
        if (
            _text is not _text_validator
            or getattr(_text_validator, "__code__", None) is not _text_validator_code
            or CollectorDelta.validate is not _delta_validate
            or getattr(_delta_validate, "__code__", None) is not _delta_validate_code
        ):
            raise ContinuousSessionError(
                "canonical source-projection validation authority changed"
            )
        if type(deltas) is not tuple:
            raise TypeError("deltas must be an exact tuple")
        if type(backlog) is not bool:
            raise TypeError("backlog must be boolean")
        if (
            expected_after_delta_id is not _EXPECTED_PROJECTION_UNSET
            and expected_after_delta_id is not None
        ):
            _text_validator(expected_after_delta_id, "expected_after_delta_id")
        if backlog and not deltas:
            raise ContinuousSessionError(
                "source-state projection backlog requires at least one delta"
            )
        seen_deltas: dict[str, CollectorDelta] = {}
        previous_epoch: str | None = None
        previous_position: int | None = None
        for delta in deltas:
            if type(delta) is not CollectorDelta:
                raise TypeError("deltas must contain exact CollectorDelta values")
            _delta_validate(delta)
            if delta.source_id != self.source_id:
                raise ContinuousSessionError(
                    "source-state projection delta belongs to another source"
                )
            if delta.delta_id in seen_deltas:
                raise ContinuousSessionError(
                    "source-state projection delta ids must be unique"
                )

            # deltas_after_commit() is an append/commit-order transport. A lawful
            # correction can therefore arrive after newer source positions and
            # legitimately move the projected cursor position backwards. Only an
            # unqualified non-revision regression is invalid here; revision
            # ancestry itself remains the canonical CollectorDeltaStore authority.
            if previous_epoch == delta.stream_epoch and previous_position is not None:
                if delta.cursor_position < previous_position and delta.revision_of is None:
                    raise ContinuousSessionError(
                        "source-state projection position moved backwards within an epoch"
                    )
                if delta.cursor_position == previous_position and delta.revision_of is None:
                    raise ContinuousSessionError(
                        "equal source-state projection position requires a revision"
                    )

            # When the revised predecessor is present in this same transport
            # batch, preserve the collector's exact local ancestry invariants.
            # A predecessor from an earlier batch cannot be reconstructed from
            # the session checkpoint alone and must not be confused with the
            # transport predecessor (expected_after_delta_id).
            if delta.revision_of is not None:
                revised = seen_deltas.get(delta.revision_of)
                if revised is not None:
                    if revised.stream_epoch != delta.stream_epoch:
                        raise ContinuousSessionError(
                            "source-state projection revision epoch does not match predecessor"
                        )
                    if revised.cursor_position != delta.cursor_position:
                        raise ContinuousSessionError(
                            "source-state projection revision position does not match predecessor"
                        )
                    if revised.source_cursor != delta.source_cursor:
                        raise ContinuousSessionError(
                            "source-state projection revision cursor does not match predecessor"
                        )
                    if revised.event_dedupe_key != delta.event_dedupe_key:
                        raise ContinuousSessionError(
                            "source-state projection revision dedupe key does not match predecessor"
                        )
                    if revised.event_id != delta.event_id:
                        raise ContinuousSessionError(
                            "source-state projection revision event does not match predecessor"
                        )
                    if (
                        revised.gap_from_cursor != delta.gap_from_cursor
                        or revised.gap_to_cursor != delta.gap_to_cursor
                    ):
                        raise ContinuousSessionError(
                            "source-state projection revision gap bounds do not match predecessor"
                        )
                    if revised.gap_state is GapState.DETECTED:
                        if delta.gap_state is not GapState.RECOVERED:
                            raise ContinuousSessionError(
                                "detected source gap can only be revised by recovery"
                            )
                    elif delta.gap_state is GapState.RECOVERED:
                        raise ContinuousSessionError(
                            "source gap recovery must revise a detected gap"
                        )
                    if delta.revision_number != revised.revision_number + 1:
                        raise ContinuousSessionError(
                            "source-state projection revision_number must advance exactly one step"
                        )

            seen_deltas[delta.delta_id] = delta
            previous_epoch = delta.stream_epoch
            previous_position = delta.cursor_position

        def mutate(raw: dict[str, Any]) -> bool:
            if raw["state"] == SessionState.PAUSED.value:
                raise SessionPausedError(
                    "source projection cannot advance while session is PAUSED"
                )
            if raw["state"] == SessionState.STOPPED.value:
                raise SessionStoppedError(
                    "source projection cannot advance while session is STOPPED"
                )
            if (
                expected_after_delta_id is not _EXPECTED_PROJECTION_UNSET
                and raw["source_state_delta_id"] != expected_after_delta_id
            ):
                raise ContinuousSessionError(
                    "source-state projection predecessor changed before publication"
                )
            before = (
                raw["source_gap_state"],
                raw["source_sync_state"],
                raw["source_state_delta_id"],
                tuple(raw["source_unresolved_gap_delta_ids"]),
                raw["source_projection_stream_epoch"],
                raw["source_state_projection_backlog"],
            )
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
            after = (
                raw["source_gap_state"],
                raw["source_sync_state"],
                raw["source_state_delta_id"],
                tuple(raw["source_unresolved_gap_delta_ids"]),
                raw["source_projection_stream_epoch"],
                raw["source_state_projection_backlog"],
            )
            return after != before

        # Source projection is durable progress, but it is not a successful
        # session generation and must not supersede an already-active
        # operational failure.  The checkpoint identity token still changes on
        # publication, so stale processes are fenced without erasing the
        # same-generation failure overlay.
        _update_method(self, mutate)

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
        _update_method: Callable[..., dict[str, Any]] = _update,
        _update_method_code: object = _update.__code__,
        _instant_validator: Callable[[object, str], datetime] = _instant,
        _instant_validator_code: object = _instant.__code__,
    ) -> int:
        if (
            type(self)._update is not _update_method
            or getattr(_update_method, "__code__", None) is not _update_method_code
        ):
            raise ContinuousSessionError(
                "canonical success read-modify-write authority changed"
            )
        if (
            _instant is not _instant_validator
            or getattr(_instant_validator, "__code__", None)
            is not _instant_validator_code
        ):
            raise ContinuousSessionError(
                "canonical success timestamp authority changed"
            )
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
        timestamp = _instant_validator(at, "at")
        if type(full_refresh) is not bool:
            raise TypeError("full_refresh must be boolean")
        if type(settlement_evidence) is not tuple:
            raise TypeError("settlement_evidence must be an exact tuple")
        for evidence in settlement_evidence:
            if type(evidence) is not SettlementResolution:
                raise TypeError(
                    "settlement_evidence must contain exact SettlementResolution values"
                )
            try:
                _validate_resolution(evidence, as_of=timestamp.isoformat())
            except (TypeError, ValueError) as exc:
                raise ContinuousSessionError(
                    "settlement evidence is invalid for success cutoff"
                ) from exc

        def mutate(raw: dict[str, Any]) -> None:
            if raw["state"] == SessionState.PAUSED.value:
                raise SessionPausedError(
                    "success cannot advance while session is PAUSED"
                )
            if raw["state"] == SessionState.STOPPED.value:
                raise SessionStoppedError(
                    "success cannot advance while session is STOPPED"
                )
            started_at = _instant_validator(raw["started_at"], "started_at")
            if timestamp < started_at:
                raise ContinuousSessionError(
                    "success timestamp precedes session start"
                )
            if raw["last_success_at"] is not None:
                previous_success = _instant_validator(
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
            # Every successful generation advance leaves a bounded tombstone
            # carrying the new generation, fencing stale record_failure()
            # publishers without rereading settlement history.
            self._write_error_checkpoint(None)

        updated = _update_method(
            self,
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
        _error_checkpoint_present: Callable[["_ContinuousSessionState"], bool] = (
            _error_checkpoint_present
        ),
        _error_checkpoint_present_code: object = _error_checkpoint_present.__code__,
        _read_error_checkpoint: Callable[
            ["_ContinuousSessionState"], dict[str, Any]
        ] = _read_error_checkpoint,
        _read_error_checkpoint_code: object = _read_error_checkpoint.__code__,
        _write_error_checkpoint: Callable[
            ["_ContinuousSessionState", str | None], None
        ] = _write_error_checkpoint,
        _write_error_checkpoint_code: object = _write_error_checkpoint.__code__,
        _checkpoint_identity_token: Callable[
            ["_ContinuousSessionState"], tuple[int, int, int, int, int]
        ] = _checkpoint_identity_token,
        _checkpoint_identity_token_code: object = _checkpoint_identity_token.__code__,
        _text_validator: Callable[[object, str], str] = _text,
        _text_validator_code: object = _text.__code__,
    ) -> _ContinuousSessionFailurePublication:
        if (
            _text is not _text_validator
            or getattr(_text_validator, "__code__", None) is not _text_validator_code
        ):
            raise ContinuousSessionError(
                "canonical failure publication lock authority changed"
            )
        code = _text_validator(code, "code")
        if (
            durable_path_lock is not _durable_path_lock
            or getattr(_durable_path_lock, "__code__", None)
            is not _durable_path_lock_code
            or type(self)._error_checkpoint_present is not _error_checkpoint_present
            or getattr(_error_checkpoint_present, "__code__", None)
            is not _error_checkpoint_present_code
            or type(self)._read_error_checkpoint is not _read_error_checkpoint
            or getattr(_read_error_checkpoint, "__code__", None)
            is not _read_error_checkpoint_code
            or type(self)._write_error_checkpoint is not _write_error_checkpoint
            or getattr(_write_error_checkpoint, "__code__", None)
            is not _write_error_checkpoint_code
            or type(self)._checkpoint_identity_token is not _checkpoint_identity_token
            or getattr(_checkpoint_identity_token, "__code__", None)
            is not _checkpoint_identity_token_code
        ):
            raise ContinuousSessionError(
                "canonical failure publication lock authority changed"
            )
        with _durable_path_lock(self.path):
            # A canonical writer can crash after publishing the main checkpoint
            # but before publishing the bounded sidecar tombstone.  Sidecar-only
            # fencing therefore cannot prove that this cached generation is still
            # current.  The already-established checkpoint identity token is a
            # bounded stat/open identity witness: reject a stale publisher without
            # performing the O(settlement-history) canonical _read().
            if _checkpoint_identity_token(self) != self._checkpoint_token:
                raise ContinuousSessionError(
                    "stale continuous session instance cannot publish operational "
                    "failure after canonical checkpoint changed"
                )
            if self._state == SessionState.PAUSED.value:
                raise SessionPausedError(
                    "operational failure cannot publish while session is PAUSED"
                )
            if self._state == SessionState.STOPPED.value:
                raise SessionStoppedError(
                    "operational failure cannot publish while session is STOPPED"
                )
            if self._last_error_code is not None and self._last_error_code != code:
                raise ContinuousSessionError(
                    "operational failure conflicts with canonical session reason"
                )
            # Keep failure publication bounded by active cached state. A full
            # canonical _read() validates every retained settlement receipt and
            # would reintroduce the exact O(history) amplification this sidecar
            # exists to remove. Before publishing, however, fence a stale
            # process from overwriting a newer bounded sidecar generation that
            # another process already made authoritative.
            if _error_checkpoint_present(self):
                existing = _read_error_checkpoint(self)
                if existing["observed_generation"] > self._generation:
                    raise ContinuousSessionError(
                        "stale continuous session instance cannot overwrite "
                        "newer operational error checkpoint"
                    )
                if existing["observed_generation"] == self._generation and (
                    existing["observed_cycles_completed"] != self._cycles_completed
                    or existing["observed_last_success_at"] != self._last_success_at
                    or existing["observed_state"] != self._state
                ):
                    raise ContinuousSessionError(
                        "same-generation operational error checkpoint markers "
                        "conflict with cached canonical session state"
                    )
            _write_error_checkpoint(self, code)
            return _ContinuousSessionFailurePublication(
                session_id=self._session_id,
                state=SessionState(self._state),
                generation=self._generation,
                cycles_completed=self._cycles_completed,
                last_success_at=self._last_success_at,
                last_error_code=code,
                source_gap_state=self._source_gap_state,
                source_sync_state=self._source_sync_state,
            )


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

    def status(
        self,
        *,
        _snapshot_method: Callable[
            ["_ContinuousSessionState"], ContinuousSessionStatus
        ] = _ContinuousSessionState.snapshot,
        _snapshot_method_code: object = _ContinuousSessionState.snapshot.__code__,
        _collector_status_method: Callable[[HeadlessCollectorService], dict[str, object]] = (
            HeadlessCollectorService.status
        ),
        _collector_status_method_code: object = HeadlessCollectorService.status.__code__,
        _invalidation_buffer_type: type[BoundedMirrorInvalidationBuffer] = (
            BoundedMirrorInvalidationBuffer
        ),
        _pending_count_descriptor: object = (
            BoundedMirrorInvalidationBuffer.__dict__["pending_count"]
        ),
        _pending_count_getter: Callable[[BoundedMirrorInvalidationBuffer], int] = (
            BoundedMirrorInvalidationBuffer.pending_count.fget
        ),
        _pending_count_getter_code: object = (
            BoundedMirrorInvalidationBuffer.pending_count.fget.__code__
        ),
        _full_refresh_descriptor: object = (
            BoundedMirrorInvalidationBuffer.__dict__["full_refresh_required"]
        ),
        _full_refresh_getter: Callable[[BoundedMirrorInvalidationBuffer], bool] = (
            BoundedMirrorInvalidationBuffer.full_refresh_required.fget
        ),
        _full_refresh_getter_code: object = (
            BoundedMirrorInvalidationBuffer.full_refresh_required.fget.__code__
        ),
        _invalidation_state_validator: Callable[
            [BoundedMirrorInvalidationBuffer], None
        ] = _validate_canonical_invalidation_buffer_state,
        _invalidation_state_validator_code: object = (
            _validate_canonical_invalidation_buffer_state.__code__
        ),
        _replace: Callable[..., ContinuousSessionStatus] = replace,
        _replace_code: object = replace.__code__,
    ) -> ContinuousSessionStatus:
        if (
            type(self._state).snapshot is not _snapshot_method
            or getattr(_snapshot_method, "__code__", None)
            is not _snapshot_method_code
            or type(self.collector).status is not _collector_status_method
            or getattr(_collector_status_method, "__code__", None)
            is not _collector_status_method_code
            or _validate_canonical_invalidation_buffer_state
            is not _invalidation_state_validator
            or getattr(_invalidation_state_validator, "__code__", None)
            is not _invalidation_state_validator_code
            or replace is not _replace
            or getattr(_replace, "__code__", None) is not _replace_code
        ):
            raise ContinuousSessionError(
                "canonical coordinator status authority changed"
            )
        snapshot = _snapshot_method(self._state)
        invalidation_buffer = self.invalidation_buffer
        if (
            isinstance(invalidation_buffer, _invalidation_buffer_type)
            and type(invalidation_buffer) is not _invalidation_buffer_type
        ):
            raise ContinuousSessionError(
                "canonical invalidation buffer subtype is not supported"
            )
        if type(invalidation_buffer) is _invalidation_buffer_type:
            _invalidation_state_validator(invalidation_buffer)
            pending_descriptor = _invalidation_buffer_type.__dict__.get(
                "pending_count"
            )
            full_refresh_descriptor = _invalidation_buffer_type.__dict__.get(
                "full_refresh_required"
            )
            if (
                BoundedMirrorInvalidationBuffer is not _invalidation_buffer_type
                or pending_descriptor is not _pending_count_descriptor
                or getattr(pending_descriptor, "fget", None)
                is not _pending_count_getter
                or getattr(_pending_count_getter, "__code__", None)
                is not _pending_count_getter_code
                or full_refresh_descriptor is not _full_refresh_descriptor
                or getattr(full_refresh_descriptor, "fget", None)
                is not _full_refresh_getter
                or getattr(_full_refresh_getter, "__code__", None)
                is not _full_refresh_getter_code
            ):
                raise ContinuousSessionError(
                    "canonical invalidation status authority changed"
                )
            invalidation_pending_count = _pending_count_getter(
                invalidation_buffer
            )
            invalidation_full_refresh_required = _full_refresh_getter(
                invalidation_buffer
            )
        else:
            invalidation_pending_count = invalidation_buffer.pending_count
            invalidation_full_refresh_required = (
                invalidation_buffer.full_refresh_required
            )
        if (
            type(invalidation_pending_count) is not int
            or invalidation_pending_count < 0
            or type(invalidation_full_refresh_required) is not bool
        ):
            raise ContinuousSessionError(
                "invalidation buffer status state is invalid"
            )
        source_status = _collector_status_method(self.collector)
        if type(source_status) is not dict:
            raise ContinuousSessionError(
                "collector status authority returned non-canonical mapping"
            )
        source_last_success = source_status.get("last_success_at")
        source_last_error = source_status.get("last_error_code")
        if source_last_success is not None and not isinstance(source_last_success, str):
            raise ContinuousSessionError("collector last_success_at must be a string or None")
        if source_last_error is not None and not isinstance(source_last_error, str):
            raise ContinuousSessionError("collector last_error_code must be a string or None")
        return _replace(
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
            invalidation_pending_count=invalidation_pending_count,
            invalidation_full_refresh_required=invalidation_full_refresh_required,
        )

    def pause(
        self,
        *,
        _set_state: Callable[..., None] = _ContinuousSessionState.set_state,
        _set_state_code: object = _ContinuousSessionState.set_state.__code__,
    ) -> None:
        if (
            type(self._state).set_state is not _set_state
            or getattr(_set_state, "__code__", None) is not _set_state_code
        ):
            raise ContinuousSessionError(
                "canonical coordinator operator-control authority changed"
            )
        _set_state(self._state, SessionState.PAUSED)

    def stop(
        self,
        reason: str = "operator_stop",
        *,
        _set_state: Callable[..., None] = _ContinuousSessionState.set_state,
        _set_state_code: object = _ContinuousSessionState.set_state.__code__,
    ) -> None:
        if (
            type(self._state).set_state is not _set_state
            or getattr(_set_state, "__code__", None) is not _set_state_code
        ):
            raise ContinuousSessionError(
                "canonical coordinator operator-control authority changed"
            )
        _set_state(self._state, SessionState.STOPPED, reason=reason)

    def resume(
        self,
        *,
        _bounded_state: Callable[["_ContinuousSessionState"], SessionState] = (
            _ContinuousSessionState.bounded_state
        ),
        _bounded_state_code: object = _ContinuousSessionState.bounded_state.__code__,
        _set_state: Callable[..., None] = _ContinuousSessionState.set_state,
        _set_state_code: object = _ContinuousSessionState.set_state.__code__,
    ) -> None:
        if (
            type(self._state).bounded_state is not _bounded_state
            or getattr(_bounded_state, "__code__", None) is not _bounded_state_code
            or type(self._state).set_state is not _set_state
            or getattr(_set_state, "__code__", None) is not _set_state_code
        ):
            raise ContinuousSessionError(
                "canonical coordinator operator-control authority changed"
            )
        current = _bounded_state(self._state)
        if current not in {SessionState.PAUSED, SessionState.STOPPED}:
            return
        _set_state(self._state, SessionState.RUNNING)

    def _require_running(
        self,
        *,
        _bounded_state: Callable[["_ContinuousSessionState"], SessionState] = (
            _ContinuousSessionState.bounded_state
        ),
        _bounded_state_code: object = _ContinuousSessionState.bounded_state.__code__,
    ) -> None:
        if (
            type(self._state).bounded_state is not _bounded_state
            or getattr(_bounded_state, "__code__", None) is not _bounded_state_code
        ):
            raise ContinuousSessionError(
                "canonical coordinator running-state authority changed"
            )
        state = _bounded_state(self._state)
        if state is SessionState.PAUSED:
            raise SessionPausedError("continuous session is durably PAUSED")
        if state is SessionState.STOPPED:
            raise SessionStoppedError("continuous session is durably STOPPED")

    def _register_input(
        self,
        input_id: str,
        *,
        dependency_index: FocusedMirrorDependencyIndex | None = None,
        register_input: Callable[..., object] | None = None,
        _dependency_index_type: type[FocusedMirrorDependencyIndex] = (
            FocusedMirrorDependencyIndex
        ),
        _dependency_type: type[FocusedMirrorDependency] = FocusedMirrorDependency,
        _selector_normalizer: Callable[..., frozenset[str] | None] = (
            FocusedMirrorDependencyIndex._selector
        ),
        _selector_normalizer_code: object = FocusedMirrorDependencyIndex._selector.__code__,
        _input_id_validator: Callable[[str], str] = FocusedMirrorDependencyIndex._input_id,
        _input_id_validator_code: object = FocusedMirrorDependencyIndex._input_id.__code__,
        _input_ids_descriptor: object = (
            FocusedMirrorDependencyIndex.__dict__["input_ids"]
        ),
        _input_ids_getter: Callable[
            [FocusedMirrorDependencyIndex], tuple[str, ...]
        ] = FocusedMirrorDependencyIndex.input_ids.fget,
        _input_ids_getter_code: object = (
            FocusedMirrorDependencyIndex.input_ids.fget.__code__
        ),
        _dependency_reader: Callable[
            [FocusedMirrorDependencyIndex, str], FocusedMirrorDependency
        ] = FocusedMirrorDependencyIndex._dependency,
        _dependency_reader_code: object = FocusedMirrorDependencyIndex._dependency.__code__,
        _matching_keys_reader: Callable[..., tuple[object, ...]] = (
            FocusedMirrorDependencyIndex.matching_keys
        ),
        _matching_keys_reader_code: object = FocusedMirrorDependencyIndex.matching_keys.__code__,
        **selectors: object,
    ) -> bool:
        def require_canonical_dependency_helpers() -> None:
            if (
                FocusedMirrorDependencyIndex is not _dependency_index_type
                or FocusedMirrorDependency is not _dependency_type
                or _dependency_index_type._selector is not _selector_normalizer
                or getattr(_selector_normalizer, "__code__", None)
                is not _selector_normalizer_code
                or _dependency_index_type._input_id is not _input_id_validator
                or getattr(_input_id_validator, "__code__", None)
                is not _input_id_validator_code
                or _dependency_index_type.__dict__.get("input_ids")
                is not _input_ids_descriptor
                or getattr(_input_ids_descriptor, "fget", None)
                is not _input_ids_getter
                or getattr(_input_ids_getter, "__code__", None)
                is not _input_ids_getter_code
                or _dependency_index_type._dependency is not _dependency_reader
                or getattr(_dependency_reader, "__code__", None)
                is not _dependency_reader_code
                or _dependency_index_type.matching_keys is not _matching_keys_reader
                or getattr(_matching_keys_reader, "__code__", None)
                is not _matching_keys_reader_code
            ):
                raise ContinuousSessionError(
                    "canonical dependency lifecycle verification authority changed"
                )

        require_canonical_dependency_helpers()
        dependency_index = (
            self.dependency_index if dependency_index is None else dependency_index
        )
        if (
            isinstance(dependency_index, _dependency_index_type)
            and type(dependency_index) is not _dependency_index_type
        ):
            raise ContinuousSessionError(
                "canonical dependency index subtype is not supported"
            )
        register_input = (
            getattr(dependency_index, "register", None)
            if register_input is None
            else register_input
        )
        dependency_index_was_canonical = (
            type(dependency_index) is _dependency_index_type
        )

        def require_dependency_index_type_authority() -> None:
            if (
                dependency_index_was_canonical
                and type(dependency_index) is not _dependency_index_type
            ):
                try:
                    object.__setattr__(
                        dependency_index,
                        "__class__",
                        _dependency_index_type,
                    )
                except (AttributeError, TypeError):
                    pass
                raise ContinuousSessionError(
                    "canonical dependency index type changed during lifecycle registration"
                )

        def read_input_ids() -> tuple[str, ...] | object:
            require_dependency_index_type_authority()
            if type(dependency_index) is _dependency_index_type:
                return _input_ids_getter(dependency_index)
            return dependency_index.input_ids

        before_ids = read_input_ids()
        if (
            type(before_ids) is not tuple
            or any(
                type(value) is not str
                or not value
                or value.strip() != value
                for value in before_ids
            )
            or len(set(before_ids)) != len(before_ids)
        ):
            raise ContinuousSessionError(
                "dependency index input identity state is invalid"
            )
        if (
            type(input_id) is not str
            or not input_id
            or input_id.strip() != input_id
        ):
            raise ContinuousSessionError(
                "dependency index registration input id is invalid"
            )
        canonical_selector_names = {
            "source_ids",
            "sports",
            "event_ids",
            "market_ids",
            "selection_ids",
        }
        if any(name not in canonical_selector_names for name in selectors):
            raise ContinuousSessionError(
                "dependency index registration selectors are invalid"
            )
        expected_dependency: FocusedMirrorDependency | None = None
        before_dependencies: tuple[tuple[object, ...], ...] | None = None
        before_matching_keys: tuple[tuple[str, tuple[object, ...]], ...] | None = None

        def dependency_fingerprint(
            dependency: FocusedMirrorDependency,
        ) -> tuple[object, ...]:
            if type(dependency) is not _dependency_type:
                raise ContinuousSessionError(
                    "dependency index published non-canonical dependency state"
                )
            return (
                dependency.input_id,
                dependency.source_ids,
                dependency.sports,
                dependency.event_ids,
                dependency.market_ids,
                dependency.selection_ids,
            )
        dependency_mirror: object | None = None
        dependency_storage: object | None = None
        matched_keys_storage: object | None = None
        dependency_lock: object | None = None
        if isinstance(dependency_index, _dependency_index_type):
            try:
                expected_dependency = _dependency_type(
                    input_id=input_id,
                    source_ids=_selector_normalizer(
                        selectors.get("source_ids"),
                        name="source_ids",
                    ),
                    sports=_selector_normalizer(
                        selectors.get("sports"),
                        name="sports",
                    ),
                    event_ids=_selector_normalizer(
                        selectors.get("event_ids"),
                        name="event_ids",
                    ),
                    market_ids=_selector_normalizer(
                        selectors.get("market_ids"),
                        name="market_ids",
                    ),
                    selection_ids=_selector_normalizer(
                        selectors.get("selection_ids"),
                        name="selection_ids",
                    ),
                )
            except (TypeError, ValueError) as exc:
                raise ContinuousSessionError(
                    "dependency index registration selectors are invalid"
                ) from exc
            before_dependencies = tuple(
                dependency_fingerprint(
                    _dependency_reader(
                        dependency_index,
                        existing_input_id,
                    )
                )
                for existing_input_id in before_ids
            )
            before_matching_keys = tuple(
                (
                    existing_input_id,
                    _matching_keys_reader(dependency_index, existing_input_id),
                )
                for existing_input_id in before_ids
            )
            dependency_mirror = dependency_index._mirror
            dependency_storage = dependency_index._dependencies
            matched_keys_storage = dependency_index._matched_keys
            dependency_lock = dependency_index._lock
        if input_id in before_ids:
            if expected_dependency is not None:
                existing_dependency = _dependency_reader(
                    dependency_index,
                    input_id,
                )
                if dependency_fingerprint(existing_dependency) != dependency_fingerprint(
                    expected_dependency
                ):
                    raise ContinuousSessionError(
                        "existing dependency selectors conflict with lifecycle registration"
                    )
            return False
        if not callable(register_input):
            raise ContinuousSessionError(
                "dependency index registration authority is unavailable"
            )
        register_input(input_id, **selectors)
        require_dependency_index_type_authority()
        require_canonical_dependency_helpers()
        if before_dependencies is not None and (
            dependency_index._dependencies is not dependency_storage
            or dependency_index._matched_keys is not matched_keys_storage
            or dependency_index._lock is not dependency_lock
        ):
            raise ContinuousSessionError(
                "dependency index registration changed state authority"
            )
        after_ids = read_input_ids()
        if (
            type(after_ids) is not tuple
            or any(
                type(value) is not str
                or not value
                or value.strip() != value
                for value in after_ids
            )
            or len(set(after_ids)) != len(after_ids)
        ):
            raise ContinuousSessionError(
                "dependency index input identity state is invalid"
            )
        if input_id not in after_ids:
            raise ContinuousSessionError(
                "dependency index registration did not publish the input"
            )
        if after_ids != (*before_ids, input_id):
            raise ContinuousSessionError(
                "dependency index registration changed unrelated input identities"
            )
        if before_dependencies is not None:
            if dependency_index._mirror is not dependency_mirror:
                raise ContinuousSessionError(
                    "dependency index registration changed mirror authority"
                )
            current_dependencies = tuple(
                dependency_fingerprint(
                    _dependency_reader(
                        dependency_index,
                        existing_input_id,
                    )
                )
                for existing_input_id in before_ids
            )
            if current_dependencies != before_dependencies:
                raise ContinuousSessionError(
                    "dependency index registration changed unrelated dependency selectors"
                )
            current_matching_keys = tuple(
                (
                    existing_input_id,
                    _matching_keys_reader(dependency_index, existing_input_id),
                )
                for existing_input_id in before_ids
            )
            if current_matching_keys != before_matching_keys:
                raise ContinuousSessionError(
                    "dependency index registration changed unrelated matched-key routing"
                )
        if expected_dependency is not None:
            published_dependency = _dependency_reader(
                dependency_index,
                input_id,
            )
            if dependency_fingerprint(published_dependency) != dependency_fingerprint(
                expected_dependency
            ):
                raise ContinuousSessionError(
                    "dependency index registration selectors do not match lifecycle request"
                )
        return True

    def _retire_input(
        self,
        input_id: str,
        *,
        dependency_index: FocusedMirrorDependencyIndex | None = None,
        unregister_input: Callable[[str], object] | None = None,
        _dependency_index_type: type[FocusedMirrorDependencyIndex] = (
            FocusedMirrorDependencyIndex
        ),
        _dependency_type: type[FocusedMirrorDependency] = FocusedMirrorDependency,
        _input_id_validator: Callable[[str], str] = FocusedMirrorDependencyIndex._input_id,
        _input_id_validator_code: object = FocusedMirrorDependencyIndex._input_id.__code__,
        _input_ids_descriptor: object = (
            FocusedMirrorDependencyIndex.__dict__["input_ids"]
        ),
        _input_ids_getter: Callable[
            [FocusedMirrorDependencyIndex], tuple[str, ...]
        ] = FocusedMirrorDependencyIndex.input_ids.fget,
        _input_ids_getter_code: object = (
            FocusedMirrorDependencyIndex.input_ids.fget.__code__
        ),
        _dependency_reader: Callable[
            [FocusedMirrorDependencyIndex, str], FocusedMirrorDependency
        ] = FocusedMirrorDependencyIndex._dependency,
        _dependency_reader_code: object = FocusedMirrorDependencyIndex._dependency.__code__,
        _matching_keys_reader: Callable[..., tuple[object, ...]] = (
            FocusedMirrorDependencyIndex.matching_keys
        ),
        _matching_keys_reader_code: object = FocusedMirrorDependencyIndex.matching_keys.__code__,
    ) -> bool:
        def require_canonical_dependency_helpers() -> None:
            if (
                FocusedMirrorDependencyIndex is not _dependency_index_type
                or FocusedMirrorDependency is not _dependency_type
                or _dependency_index_type._input_id is not _input_id_validator
                or getattr(_input_id_validator, "__code__", None)
                is not _input_id_validator_code
                or _dependency_index_type.__dict__.get("input_ids")
                is not _input_ids_descriptor
                or getattr(_input_ids_descriptor, "fget", None)
                is not _input_ids_getter
                or getattr(_input_ids_getter, "__code__", None)
                is not _input_ids_getter_code
                or _dependency_index_type._dependency is not _dependency_reader
                or getattr(_dependency_reader, "__code__", None)
                is not _dependency_reader_code
                or _dependency_index_type.matching_keys is not _matching_keys_reader
                or getattr(_matching_keys_reader, "__code__", None)
                is not _matching_keys_reader_code
            ):
                raise ContinuousSessionError(
                    "canonical dependency lifecycle verification authority changed"
                )

        require_canonical_dependency_helpers()
        dependency_index = (
            self.dependency_index if dependency_index is None else dependency_index
        )
        if (
            isinstance(dependency_index, _dependency_index_type)
            and type(dependency_index) is not _dependency_index_type
        ):
            raise ContinuousSessionError(
                "canonical dependency index subtype is not supported"
            )
        unregister_input = (
            getattr(dependency_index, "unregister", None)
            if unregister_input is None
            else unregister_input
        )
        dependency_index_was_canonical = (
            type(dependency_index) is _dependency_index_type
        )

        def require_dependency_index_type_authority() -> None:
            if (
                dependency_index_was_canonical
                and type(dependency_index) is not _dependency_index_type
            ):
                try:
                    object.__setattr__(
                        dependency_index,
                        "__class__",
                        _dependency_index_type,
                    )
                except (AttributeError, TypeError):
                    pass
                raise ContinuousSessionError(
                    "canonical dependency index type changed during lifecycle retirement"
                )

        def read_input_ids() -> tuple[str, ...] | object:
            require_dependency_index_type_authority()
            if type(dependency_index) is _dependency_index_type:
                return _input_ids_getter(dependency_index)
            return dependency_index.input_ids

        if (
            type(input_id) is not str
            or not input_id
            or input_id.strip() != input_id
        ):
            raise ContinuousSessionError(
                "dependency index retirement input id is invalid"
            )
        if not callable(unregister_input):
            raise ContinuousSessionError(
                "dependency index retirement authority is unavailable"
            )
        before_ids = read_input_ids()
        if (
            type(before_ids) is not tuple
            or any(
                type(value) is not str
                or not value
                or value.strip() != value
                for value in before_ids
            )
            or len(set(before_ids)) != len(before_ids)
        ):
            raise ContinuousSessionError(
                "dependency index input identity state is invalid"
            )
        before_dependencies: tuple[tuple[str, tuple[object, ...]], ...] | None = None
        before_matching_keys: tuple[tuple[str, tuple[object, ...]], ...] | None = None

        def dependency_fingerprint(
            dependency: FocusedMirrorDependency,
        ) -> tuple[object, ...]:
            if type(dependency) is not _dependency_type:
                raise ContinuousSessionError(
                    "dependency index published non-canonical dependency state"
                )
            return (
                dependency.input_id,
                dependency.source_ids,
                dependency.sports,
                dependency.event_ids,
                dependency.market_ids,
                dependency.selection_ids,
            )
        dependency_mirror: object | None = None
        dependency_storage: object | None = None
        matched_keys_storage: object | None = None
        dependency_lock: object | None = None
        if isinstance(dependency_index, _dependency_index_type):
            before_dependencies = tuple(
                (
                    existing_input_id,
                    dependency_fingerprint(
                        _dependency_reader(
                            dependency_index,
                            existing_input_id,
                        )
                    ),
                )
                for existing_input_id in before_ids
            )
            before_matching_keys = tuple(
                (
                    existing_input_id,
                    _matching_keys_reader(dependency_index, existing_input_id),
                )
                for existing_input_id in before_ids
            )
            dependency_mirror = dependency_index._mirror
            dependency_storage = dependency_index._dependencies
            matched_keys_storage = dependency_index._matched_keys
            dependency_lock = dependency_index._lock
        removed = unregister_input(input_id)
        require_dependency_index_type_authority()
        require_canonical_dependency_helpers()
        if before_dependencies is not None and (
            dependency_index._dependencies is not dependency_storage
            or dependency_index._matched_keys is not matched_keys_storage
            or dependency_index._lock is not dependency_lock
        ):
            raise ContinuousSessionError(
                "dependency index retirement changed state authority"
            )
        if type(removed) is not bool:
            raise ContinuousSessionError(
                "dependency index retirement receipt is invalid"
            )
        after_ids = read_input_ids()
        if (
            type(after_ids) is not tuple
            or any(
                type(value) is not str
                or not value
                or value.strip() != value
                for value in after_ids
            )
            or len(set(after_ids)) != len(after_ids)
        ):
            raise ContinuousSessionError(
                "dependency index input identity state is invalid"
            )
        if removed and input_id in after_ids:
            raise ContinuousSessionError(
                "dependency index retirement did not remove the input"
            )
        was_present = input_id in before_ids
        if removed is not was_present:
            raise ContinuousSessionError(
                "dependency index retirement receipt conflicts with identity state"
            )
        expected_after_ids = tuple(
            value for value in before_ids if value != input_id
        )
        if after_ids != expected_after_ids:
            raise ContinuousSessionError(
                "dependency index retirement changed unrelated input identities"
            )
        if before_dependencies is not None:
            if dependency_index._mirror is not dependency_mirror:
                raise ContinuousSessionError(
                    "dependency index retirement changed mirror authority"
                )
            expected_remaining_dependencies = tuple(
                item
                for item in before_dependencies
                if item[0] != input_id
            )
            current_remaining_dependencies = tuple(
                (
                    remaining_input_id,
                    dependency_fingerprint(
                        _dependency_reader(
                            dependency_index,
                            remaining_input_id,
                        )
                    ),
                )
                for remaining_input_id in after_ids
            )
            if current_remaining_dependencies != expected_remaining_dependencies:
                raise ContinuousSessionError(
                    "dependency index retirement changed unrelated dependency selectors"
                )
            expected_remaining_matching_keys = tuple(
                item
                for item in before_matching_keys
                if item[0] != input_id
            )
            current_remaining_matching_keys = tuple(
                (
                    remaining_input_id,
                    _matching_keys_reader(dependency_index, remaining_input_id),
                )
                for remaining_input_id in after_ids
            )
            if current_remaining_matching_keys != expected_remaining_matching_keys:
                raise ContinuousSessionError(
                    "dependency index retirement changed unrelated matched-key routing"
                )
        return removed

    def _drain_invalidations(
        self,
        *,
        invalidation_buffer: BoundedMirrorInvalidationBuffer | None = None,
        dependency_index: FocusedMirrorDependencyIndex | None = None,
        drain_invalidation: Callable[..., MirrorInvalidationBatch] | None = None,
        affected_inputs: Callable[[MirrorInvalidationBatch], tuple[str, ...]] | None = None,
        max_batches: int | None = None,
        max_items: int | None = None,
        _dependency_index_type: type[FocusedMirrorDependencyIndex] = (
            FocusedMirrorDependencyIndex
        ),
        _input_ids_descriptor: object = (
            FocusedMirrorDependencyIndex.__dict__["input_ids"]
        ),
        _input_ids_getter: Callable[
            [FocusedMirrorDependencyIndex], tuple[str, ...]
        ] = FocusedMirrorDependencyIndex.input_ids.fget,
        _input_ids_getter_code: object = (
            FocusedMirrorDependencyIndex.input_ids.fget.__code__
        ),
        _dependency_reader: Callable[
            [FocusedMirrorDependencyIndex, str], FocusedMirrorDependency
        ] = FocusedMirrorDependencyIndex._dependency,
        _dependency_reader_code: object = FocusedMirrorDependencyIndex._dependency.__code__,
        _matching_keys_reader: Callable[..., tuple[object, ...]] = (
            FocusedMirrorDependencyIndex.matching_keys
        ),
        _matching_keys_reader_code: object = FocusedMirrorDependencyIndex.matching_keys.__code__,
        _dependency_type: type[FocusedMirrorDependency] = FocusedMirrorDependency,
        _dependency_matches: Callable[[FocusedMirrorDependency, object], bool] = (
            FocusedMirrorDependency.matches
        ),
        _dependency_matches_code: object = FocusedMirrorDependency.matches.__code__,
        _dependency_equals: Callable[[FocusedMirrorDependency, object], object] = (
            FocusedMirrorDependency.__eq__
        ),
        _dependency_equals_code: object = FocusedMirrorDependency.__eq__.__code__,
        _invalidation_buffer_type: type[BoundedMirrorInvalidationBuffer] = (
            BoundedMirrorInvalidationBuffer
        ),
        _invalidation_drain_method: Callable[..., MirrorInvalidationBatch] = (
            BoundedMirrorInvalidationBuffer.drain
        ),
        _invalidation_drain_method_code: object = (
            BoundedMirrorInvalidationBuffer.drain.__code__
        ),
        _pending_count_descriptor: object = (
            BoundedMirrorInvalidationBuffer.__dict__["pending_count"]
        ),
        _pending_count_getter: Callable[[BoundedMirrorInvalidationBuffer], int] = (
            BoundedMirrorInvalidationBuffer.pending_count.fget
        ),
        _pending_count_getter_code: object = (
            BoundedMirrorInvalidationBuffer.pending_count.fget.__code__
        ),
        _full_refresh_descriptor: object = (
            BoundedMirrorInvalidationBuffer.__dict__["full_refresh_required"]
        ),
        _full_refresh_getter: Callable[[BoundedMirrorInvalidationBuffer], bool] = (
            BoundedMirrorInvalidationBuffer.full_refresh_required.fget
        ),
        _full_refresh_getter_code: object = (
            BoundedMirrorInvalidationBuffer.full_refresh_required.fget.__code__
        ),
        _force_full_refresh: Callable[[BoundedMirrorInvalidationBuffer], None] = (
            BoundedMirrorInvalidationBuffer.force_full_refresh
        ),
        _force_full_refresh_code: object = (
            BoundedMirrorInvalidationBuffer.force_full_refresh.__code__
        ),
        _invalidation_state_validator: Callable[
            [BoundedMirrorInvalidationBuffer], None
        ] = _validate_canonical_invalidation_buffer_state,
        _invalidation_state_validator_code: object = (
            _validate_canonical_invalidation_buffer_state.__code__
        ),
    ) -> tuple[
        tuple[str, ...],
        bool,
        bool,
    ]:
        invalidation_buffer = (
            self.invalidation_buffer
            if invalidation_buffer is None
            else invalidation_buffer
        )
        dependency_index = (
            self.dependency_index if dependency_index is None else dependency_index
        )
        if isinstance(dependency_index, _dependency_index_type):
            if FocusedMirrorDependencyIndex is not _dependency_index_type:
                raise ContinuousSessionError(
                    "canonical dependency routing index authority changed"
                )
            if type(dependency_index) is not _dependency_index_type:
                raise ContinuousSessionError(
                    "canonical dependency index subtype is not supported"
                )
        dependency_index_was_canonical = (
            type(dependency_index) is _dependency_index_type
        )

        def restore_dependency_index_type_authority() -> bool:
            if (
                not dependency_index_was_canonical
                or type(dependency_index) is _dependency_index_type
            ):
                return False
            try:
                object.__setattr__(
                    dependency_index,
                    "__class__",
                    _dependency_index_type,
                )
            except (AttributeError, TypeError):
                pass
            return True

        def require_dependency_index_type_authority(message: str) -> None:
            if restore_dependency_index_type_authority():
                raise ContinuousSessionError(message)

        def require_dependency_identity_dispatch(message: str) -> None:
            require_dependency_index_type_authority(message)
            if type(dependency_index) is not _dependency_index_type:
                return
            descriptor = _dependency_index_type.__dict__.get("input_ids")
            if (
                FocusedMirrorDependencyIndex is not _dependency_index_type
                or descriptor is not _input_ids_descriptor
                or getattr(descriptor, "fget", None) is not _input_ids_getter
                or getattr(_input_ids_getter, "__code__", None)
                is not _input_ids_getter_code
            ):
                raise ContinuousSessionError(message)

        def read_dependency_input_ids() -> object:
            require_dependency_identity_dispatch(
                "canonical dependency routing identity authority changed"
            )
            if type(dependency_index) is _dependency_index_type:
                return _input_ids_getter(dependency_index)
            return dependency_index.input_ids

        require_dependency_identity_dispatch(
            "canonical dependency routing identity authority changed"
        )
        drain_invalidation = (
            getattr(invalidation_buffer, "drain", None)
            if drain_invalidation is None
            else drain_invalidation
        )
        affected_inputs = (
            getattr(dependency_index, "affected_inputs", None)
            if affected_inputs is None
            else affected_inputs
        )
        max_batches = (
            self.max_invalidation_batches_per_tick
            if max_batches is None
            else max_batches
        )
        max_items = (
            self.max_invalidation_items_per_batch
            if max_items is None
            else max_items
        )
        if (
            not callable(drain_invalidation)
            or not callable(affected_inputs)
        ):
            raise ContinuousSessionError(
                "continuous-session invalidation routing authority is unavailable"
            )
        if (
            isinstance(invalidation_buffer, _invalidation_buffer_type)
            and type(invalidation_buffer) is not _invalidation_buffer_type
        ):
            raise ContinuousSessionError(
                "canonical invalidation buffer subtype is not supported"
            )
        invalidation_buffer_was_canonical = (
            type(invalidation_buffer) is _invalidation_buffer_type
        )

        def restore_invalidation_buffer_type_authority() -> bool:
            if (
                not invalidation_buffer_was_canonical
                or type(invalidation_buffer) is _invalidation_buffer_type
            ):
                return False
            try:
                object.__setattr__(
                    invalidation_buffer,
                    "__class__",
                    _invalidation_buffer_type,
                )
            except (AttributeError, TypeError):
                pass
            return True

        def require_invalidation_buffer_type_authority(message: str) -> None:
            if restore_invalidation_buffer_type_authority():
                raise ContinuousSessionError(message)

        if type(invalidation_buffer) is _invalidation_buffer_type:
            if (
                _validate_canonical_invalidation_buffer_state
                is not _invalidation_state_validator
                or getattr(_invalidation_state_validator, "__code__", None)
                is not _invalidation_state_validator_code
            ):
                raise ContinuousSessionError(
                    "canonical invalidation state validation authority changed"
                )
            _invalidation_state_validator(invalidation_buffer)
            pending_descriptor = _invalidation_buffer_type.__dict__.get(
                "pending_count"
            )
            full_refresh_descriptor = _invalidation_buffer_type.__dict__.get(
                "full_refresh_required"
            )
            if (
                BoundedMirrorInvalidationBuffer is not _invalidation_buffer_type
                or getattr(drain_invalidation, "__self__", None)
                is not invalidation_buffer
                or getattr(drain_invalidation, "__func__", None)
                is not _invalidation_drain_method
                or getattr(_invalidation_drain_method, "__code__", None)
                is not _invalidation_drain_method_code
                or pending_descriptor is not _pending_count_descriptor
                or getattr(pending_descriptor, "fget", None)
                is not _pending_count_getter
                or getattr(_pending_count_getter, "__code__", None)
                is not _pending_count_getter_code
                or full_refresh_descriptor is not _full_refresh_descriptor
                or getattr(full_refresh_descriptor, "fget", None)
                is not _full_refresh_getter
                or getattr(_full_refresh_getter, "__code__", None)
                is not _full_refresh_getter_code
            ):
                raise ContinuousSessionError(
                    "canonical invalidation buffer dispatch authority changed"
                )
        if (
            type(max_batches) is not int
            or max_batches <= 0
            or type(max_items) is not int
            or max_items <= 0
        ):
            raise ContinuousSessionError(
                "continuous-session invalidation bounds are invalid"
            )
        indexed_input_ids = read_dependency_input_ids()
        if (
            type(indexed_input_ids) is not tuple
            or any(
                type(input_id) is not str
                or not input_id
                or input_id.strip() != input_id
                for input_id in indexed_input_ids
            )
            or len(set(indexed_input_ids)) != len(indexed_input_ids)
        ):
            raise ContinuousSessionError(
                "dependency index input identity state is invalid"
            )
        def require_dependency_model_dispatch(message: str) -> None:
            if (
                FocusedMirrorDependency is not _dependency_type
                or _dependency_type.matches is not _dependency_matches
                or getattr(_dependency_matches, "__code__", None)
                is not _dependency_matches_code
                or _dependency_type.__eq__ is not _dependency_equals
                or getattr(_dependency_equals, "__code__", None)
                is not _dependency_equals_code
            ):
                raise ContinuousSessionError(message)

        require_dependency_model_dispatch(
            "canonical dependency routing model authority changed"
        )

        def dependency_fingerprint(
            dependency: FocusedMirrorDependency,
        ) -> tuple[object, ...]:
            if type(dependency) is not _dependency_type:
                raise ContinuousSessionError(
                    "dependency index published non-canonical dependency state"
                )
            return (
                dependency.input_id,
                dependency.source_ids,
                dependency.sports,
                dependency.event_ids,
                dependency.market_ids,
                dependency.selection_ids,
            )

        dependency_authority: tuple[tuple[object, ...], ...] | None = None
        dependency_entries: tuple[
            tuple[str, FocusedMirrorDependency, tuple[object, ...]], ...
        ] | None = None
        dependency_mirror: object | None = None
        dependency_storage: object | None = None
        matched_keys_storage: object | None = None
        dependency_lock: object | None = None
        if type(dependency_index) is _dependency_index_type:
            if (
                _dependency_index_type._dependency is not _dependency_reader
                or getattr(_dependency_reader, "__code__", None)
                is not _dependency_reader_code
                or _dependency_index_type.matching_keys
                is not _matching_keys_reader
                or getattr(_matching_keys_reader, "__code__", None)
                is not _matching_keys_reader_code
            ):
                raise ContinuousSessionError(
                    "canonical dependency routing reader authority changed"
                )
            dependency_entries = tuple(
                (
                    input_id,
                    _dependency_reader(dependency_index, input_id),
                    dependency_fingerprint(
                        _dependency_reader(dependency_index, input_id)
                    ),
                )
                for input_id in indexed_input_ids
            )
            dependency_authority = tuple(
                fingerprint
                for _input_id, _dependency, fingerprint in dependency_entries
            )
            dependency_mirror = dependency_index._mirror
            dependency_storage = dependency_index._dependencies
            matched_keys_storage = dependency_index._matched_keys
            dependency_lock = dependency_index._lock

        def restore_dependency_authority() -> None:
            if (
                dependency_entries is None
                or dependency_storage is None
                or matched_keys_storage is None
                or dependency_lock is None
            ):
                return
            object.__setattr__(dependency_index, "_mirror", dependency_mirror)
            object.__setattr__(dependency_index, "_dependencies", dependency_storage)
            object.__setattr__(dependency_index, "_matched_keys", matched_keys_storage)
            object.__setattr__(dependency_index, "_lock", dependency_lock)
            with dependency_lock:
                dependency_storage.clear()
                for input_id, dependency, fingerprint in dependency_entries:
                    for field_name, value in zip(
                        (
                            "input_id",
                            "source_ids",
                            "sports",
                            "event_ids",
                            "market_ids",
                            "selection_ids",
                        ),
                        fingerprint,
                    ):
                        object.__setattr__(dependency, field_name, value)
                    dependency_storage[input_id] = dependency

        def require_dependency_authority(message: str) -> None:
            require_dependency_model_dispatch(message)
            require_dependency_identity_dispatch(message)
            if dependency_authority is None:
                return
            if (
                dependency_index._mirror is not dependency_mirror
                or dependency_index._dependencies is not dependency_storage
                or dependency_index._matched_keys is not matched_keys_storage
                or dependency_index._lock is not dependency_lock
            ):
                restore_dependency_authority()
                raise ContinuousSessionError(message)
            try:
                current = tuple(
                    dependency_fingerprint(
                        _dependency_reader(dependency_index, input_id)
                    )
                    for input_id in indexed_input_ids
                )
            except Exception as exc:
                restore_dependency_authority()
                raise ContinuousSessionError(message) from exc
            if current != dependency_authority:
                restore_dependency_authority()
                raise ContinuousSessionError(message)

        def matching_keys_authority() -> tuple[
            tuple[str, tuple[object, ...]], ...
        ] | None:
            if dependency_authority is None:
                return None
            return tuple(
                (
                    input_id,
                    _matching_keys_reader(dependency_index, input_id),
                )
                for input_id in indexed_input_ids
            )

        def capture_matching_key_state() -> tuple[
            tuple[str, set[object], tuple[object, ...]], ...
        ] | None:
            if dependency_authority is None or matched_keys_storage is None:
                return None
            state: list[tuple[str, set[object], tuple[object, ...]]] = []
            for input_id in indexed_input_ids:
                matched_set = matched_keys_storage.get(input_id)
                if type(matched_set) is not set:
                    raise ContinuousSessionError(
                        "dependency index matched-key storage is invalid"
                    )
                state.append(
                    (
                        input_id,
                        matched_set,
                        _matching_keys_reader(dependency_index, input_id),
                    )
                )
            return tuple(state)

        def restore_matching_key_state(
            snapshot: tuple[
                tuple[str, set[object], tuple[object, ...]], ...
            ] | None,
        ) -> None:
            if (
                snapshot is None
                or matched_keys_storage is None
                or dependency_lock is None
            ):
                return
            object.__setattr__(dependency_index, "_matched_keys", matched_keys_storage)
            with dependency_lock:
                matched_keys_storage.clear()
                for input_id, matched_set, keys in snapshot:
                    matched_set.clear()
                    matched_set.update(keys)
                    matched_keys_storage[input_id] = matched_set

        @contextmanager
        def consumed_batch_recovery() -> Iterator[None]:
            try:
                yield
            except Exception as exc:
                dependency_type_changed = restore_dependency_index_type_authority()
                invalidation_type_changed = (
                    restore_invalidation_buffer_type_authority()
                )
                if dependency_type_changed:
                    try:
                        exc.add_note(
                            "canonical dependency index runtime type was changed "
                            "during invalidation consumption and was restored"
                        )
                    except BaseException:
                        pass
                if invalidation_type_changed:
                    try:
                        exc.add_note(
                            "canonical invalidation buffer runtime type was changed "
                            "during invalidation consumption and was restored"
                        )
                    except BaseException:
                        pass
                if type(invalidation_buffer) is _invalidation_buffer_type:
                    try:
                        if (
                            BoundedMirrorInvalidationBuffer is not _invalidation_buffer_type
                            or _invalidation_buffer_type.force_full_refresh
                            is not _force_full_refresh
                            or getattr(_force_full_refresh, "__code__", None)
                            is not _force_full_refresh_code
                        ):
                            raise ContinuousSessionError(
                                "canonical invalidation recovery authority changed"
                            )
                        _force_full_refresh(invalidation_buffer)
                    except Exception as recovery_exc:
                        try:
                            exc.add_note(
                                "consumed invalidation batch could not be promoted "
                                "to full-refresh recovery: "
                                f"{type(recovery_exc).__name__}: {recovery_exc}"
                            )
                        except BaseException:
                            pass
                raise

        affected: list[str] = []
        full_refresh_required = False
        last_has_more = False

        for _ in range(max_batches):
            if read_dependency_input_ids() != indexed_input_ids:
                raise ContinuousSessionError(
                    "dependency index input identity state changed between invalidation batches"
                )
            matching_state_before_drain = capture_matching_key_state()
            matching_keys_before_drain = matching_keys_authority()
            with consumed_batch_recovery():
                batch = drain_invalidation(max_items=max_items)
                require_invalidation_buffer_type_authority(
                    "canonical invalidation buffer type changed during invalidation routing"
                )
                require_dependency_index_type_authority(
                    "canonical dependency index type changed during invalidation routing"
                )
                if read_dependency_input_ids() != indexed_input_ids:
                    restore_dependency_authority()
                    restore_matching_key_state(matching_state_before_drain)
                    raise ContinuousSessionError(
                        "invalidation drain mutated dependency index input identity state"
                    )
                try:
                    require_dependency_authority(
                        "dependency index routing authority changed during invalidation drain"
                    )
                    current_matching_keys = matching_keys_authority()
                except Exception:
                    restore_matching_key_state(matching_state_before_drain)
                    raise
                if current_matching_keys != matching_keys_before_drain:
                    restore_matching_key_state(matching_state_before_drain)
                    raise ContinuousSessionError(
                        "dependency index matched-key routing changed during invalidation drain"
                    )
                if (
                    type(batch) is not MirrorInvalidationBatch
                    or type(batch.changed_keys) is not tuple
                    or type(batch.full_refresh_required) is not bool
                    or type(batch.has_more) is not bool
                    or any(
                        type(key) is not tuple
                        or len(key) != 2
                        or any(
                            type(part) is not str
                            or not part
                            or part.strip() != part
                            for part in key
                        )
                        for key in batch.changed_keys
                    )
                    or len(set(batch.changed_keys)) != len(batch.changed_keys)
                    or (
                        batch.full_refresh_required
                        and (bool(batch.changed_keys) or batch.has_more)
                    )
                ):
                    raise ContinuousSessionError(
                        "invalidation buffer returned an invalid batch"
                    )
                batch_changed_keys = batch.changed_keys
                batch_full_refresh_required = batch.full_refresh_required
                batch_has_more = batch.has_more
                routed = affected_inputs(batch)
                require_invalidation_buffer_type_authority(
                    "canonical invalidation buffer type changed during invalidation routing"
                )
                require_dependency_index_type_authority(
                    "canonical dependency index type changed during invalidation routing"
                )
                if (
                    batch.changed_keys != batch_changed_keys
                    or batch.full_refresh_required is not batch_full_refresh_required
                    or batch.has_more is not batch_has_more
                ):
                    raise ContinuousSessionError(
                        "dependency index routing mutated invalidation batch truth"
                    )
                if read_dependency_input_ids() != indexed_input_ids:
                    raise ContinuousSessionError(
                        "dependency index routing mutated input identity state"
                    )
                require_dependency_authority(
                    "dependency index routing authority changed during affected-input routing"
                )
                if (
                    type(routed) is not tuple
                    or any(
                        type(input_id) is not str
                        or not input_id
                        or input_id.strip() != input_id
                        for input_id in routed
                    )
                    or len(set(routed)) != len(routed)
                ):
                    raise ContinuousSessionError(
                        "dependency index returned invalid affected inputs"
                    )
                if any(input_id not in indexed_input_ids for input_id in routed):
                    raise ContinuousSessionError(
                        "dependency index routed an unregistered input"
                    )
                if not batch_full_refresh_required:
                    routed_ids = set(routed)
                    expected_routed = tuple(
                        input_id
                        for input_id in indexed_input_ids
                        if input_id in routed_ids
                    )
                    if routed != expected_routed:
                        raise ContinuousSessionError(
                            "dependency index affected input routing is reordered"
                        )
                if (
                    batch_full_refresh_required
                    and routed != indexed_input_ids
                ):
                    raise ContinuousSessionError(
                        "dependency index full refresh routing is incomplete or reordered"
                    )
                affected.extend(routed)
                full_refresh_required = (
                    full_refresh_required or batch_full_refresh_required
                )
                last_has_more = batch_has_more
                if not batch_has_more:
                    break

        matching_state_before_backlog = capture_matching_key_state()
        matching_keys_before_backlog = matching_keys_authority()
        require_invalidation_buffer_type_authority(
            "canonical invalidation buffer type changed before backlog inspection"
        )
        require_dependency_index_type_authority(
            "canonical dependency index type changed before backlog inspection"
        )
        if type(invalidation_buffer) is _invalidation_buffer_type:
            pending_count = _pending_count_getter(invalidation_buffer)
            pending_full_refresh = _full_refresh_getter(invalidation_buffer)
        else:
            pending_count = invalidation_buffer.pending_count
            pending_full_refresh = invalidation_buffer.full_refresh_required
        if (
            type(pending_count) is not int
            or pending_count < 0
            or type(pending_full_refresh) is not bool
        ):
            raise ContinuousSessionError(
                "invalidation buffer backlog state is invalid"
            )
        if read_dependency_input_ids() != indexed_input_ids:
            restore_dependency_authority()
            restore_matching_key_state(matching_state_before_backlog)
            raise ContinuousSessionError(
                "invalidation backlog inspection mutated dependency index input identity state"
            )
        try:
            require_dependency_authority(
                "dependency index routing authority changed during invalidation backlog inspection"
            )
            current_matching_keys = matching_keys_authority()
        except Exception:
            restore_matching_key_state(matching_state_before_backlog)
            raise
        if current_matching_keys != matching_keys_before_backlog:
            restore_matching_key_state(matching_state_before_backlog)
            raise ContinuousSessionError(
                "dependency index matched-key routing changed during invalidation backlog inspection"
            )
        backlog = last_has_more or pending_count > 0 or pending_full_refresh
        affected_ids = set(affected)
        ordered_affected = tuple(
            input_id
            for input_id in indexed_input_ids
            if input_id in affected_ids
        )
        return ordered_affected, full_refresh_required, backlog

    def _refresh_source_state_projection(
        self,
        *,
        state: _ContinuousSessionState | None = None,
        collector: HeadlessCollectorService | None = None,
        source_id: str | None = None,
        delta_store: Any | None = None,
        read_deltas: Callable[..., tuple[CollectorDelta, ...]] | None = None,
        max_items: int | None = None,
        _snapshot_method: Callable[
            ["_ContinuousSessionState"], ContinuousSessionStatus
        ] = _ContinuousSessionState.snapshot,
        _snapshot_method_code: object = _ContinuousSessionState.snapshot.__code__,
        _record_source_projection_method: Callable[..., None] = (
            _ContinuousSessionState.record_source_projection
        ),
        _record_source_projection_method_code: object = (
            _ContinuousSessionState.record_source_projection.__code__
        ),
    ) -> ContinuousSessionStatus:
        state = self._state if state is None else state
        if (
            type(state).snapshot is not _snapshot_method
            or getattr(_snapshot_method, "__code__", None)
            is not _snapshot_method_code
            or type(state).record_source_projection
            is not _record_source_projection_method
            or getattr(_record_source_projection_method, "__code__", None)
            is not _record_source_projection_method_code
        ):
            raise ContinuousSessionError(
                "canonical source-projection coordinator authority changed"
            )
        collector = self.collector if collector is None else collector
        source_id = collector.source_id if source_id is None else source_id
        delta_store = collector.delta_store if delta_store is None else delta_store
        read_deltas = (
            getattr(delta_store, "deltas_after_commit", None)
            if read_deltas is None
            else read_deltas
        )
        max_items = collector.config.max_items if max_items is None else max_items
        if (
            type(source_id) is not str
            or not source_id
            or source_id.strip() != source_id
            or type(max_items) is not int
            or max_items <= 0
            or not callable(read_deltas)
        ):
            raise ContinuousSessionError(
                "collector projection configuration is invalid"
            )
        snapshot = _snapshot_method(state)
        deltas = read_deltas(
            source_id=source_id,
            after_delta_id=snapshot.source_state_delta_id,
            max_items=max_items + 1,
        )
        if (
            type(deltas) is not tuple
            or any(type(delta) is not CollectorDelta for delta in deltas)
        ):
            raise ContinuousSessionError(
                "collector projection returned invalid deltas"
            )
        backlog = len(deltas) > max_items
        selected = deltas[:max_items]
        _record_source_projection_method(
            state,
            deltas=selected,
            backlog=backlog,
            expected_after_delta_id=snapshot.source_state_delta_id,
        )
        return _snapshot_method(state)

    def _settlement_resolutions(
        self,
        *,
        as_of: str,
        lifecycle: ContinuousEventLifecycle | None = None,
        outcome_authority: SettlementOutcomeAuthority | None | object = (
            _OUTCOME_AUTHORITY_UNSET
        ),
        records_reader: Callable[[], tuple[EventLifecycleRecord, ...]] | None = None,
        resolve_outcome: Callable[
            ..., SettlementResolution | None
        ] | None | object = _OUTCOME_AUTHORITY_UNSET,
        _outcome_authority_unset: object = _OUTCOME_AUTHORITY_UNSET,
        _resolution_validate: Callable[..., None] = SettlementResolution.validate,
        _resolution_validate_code: object = SettlementResolution.validate.__code__,
        _replace: Callable[..., SettlementResolution] = replace,
        _replace_code: object = replace.__code__,
        _instant_validator: Callable[[object, str], datetime] = _instant,
        _instant_validator_code: object = _instant.__code__,
    ) -> tuple[SettlementResolution, ...]:
        if (
            SettlementResolution.validate is not _resolution_validate
            or getattr(_resolution_validate, "__code__", None)
            is not _resolution_validate_code
            or replace is not _replace
            or getattr(_replace, "__code__", None) is not _replace_code
            or _instant is not _instant_validator
            or getattr(_instant_validator, "__code__", None)
            is not _instant_validator_code
        ):
            raise ContinuousSessionError(
                "canonical settlement resolution validator authority changed"
            )
        lifecycle = self.lifecycle if lifecycle is None else lifecycle
        if resolve_outcome is _outcome_authority_unset:
            if outcome_authority is _outcome_authority_unset:
                outcome_authority = self.outcome_authority
            if outcome_authority is None:
                return ()
            resolve_outcome = getattr(outcome_authority, "resolve", None)
        elif resolve_outcome is None:
            return ()
        if not callable(resolve_outcome):
            raise ContinuousSessionError(
                "settlement outcome authority resolver is not callable"
            )
        if records_reader is None:
            records_reader = getattr(lifecycle, "records", None)
        if not callable(records_reader):
            raise ContinuousSessionError(
                "lifecycle settlement records authority is unavailable"
            )
        records = records_reader()
        if type(records) is not tuple:
            raise ContinuousSessionError(
                "lifecycle settlement records must be an exact tuple"
            )
        cutoff = _instant_validator(as_of, "as_of")
        resolutions: list[SettlementResolution] = []
        evidence_by_id: dict[str, SettlementResolution] = {}
        outcome_by_settlement: dict[tuple[str, str], dict[str, str]] = {}
        for record in records:
            if record.phase is not EventPhase.COMPLETED or record.settlement_ref is None:
                continue
            # Settlement truth is causal only after product state discovered both
            # completion and the settlement reference. tick() intentionally fixes
            # as_of before provider I/O, so a record advanced during that I/O must
            # not become settleable against the older cutoff.
            completion_discovered_at = getattr(
                record,
                "completion_discovered_at",
                None,
            )
            settlement_discovered_at = getattr(
                record,
                "settlement_discovered_at",
                None,
            )
            if (
                completion_discovered_at is None
                or settlement_discovered_at is None
            ):
                continue
            try:
                completion_discovered = _instant_validator(
                    completion_discovered_at,
                    "completion_discovered_at",
                )
                settlement_discovered = _instant_validator(
                    settlement_discovered_at,
                    "settlement_discovered_at",
                )
            except (TypeError, ValueError) as exc:
                raise ContinuousSessionError(
                    "lifecycle settlement discovery timestamp is invalid"
                ) from exc
            if completion_discovered > cutoff or settlement_discovered > cutoff:
                continue
            # Snapshot lifecycle causality before entering the external outcome
            # authority.  The authority receives the live record for protocol
            # compatibility, but it must not be able to rewrite the identity or
            # settlement reference that product state made authoritative before
            # the callback.
            record_phase = record.phase
            record_identity = record.identity
            record_settlement_ref = record.settlement_ref
            resolution = resolve_outcome(record, as_of=as_of)
            if (
                record.phase is not record_phase
                or record.identity != record_identity
                or record.settlement_ref != record_settlement_ref
            ):
                raise ContinuousSessionError(
                    "outcome authority mutated lifecycle settlement identity"
                )
            if (
                getattr(record, "completion_discovered_at", None)
                != completion_discovered_at
                or getattr(record, "settlement_discovered_at", None)
                != settlement_discovered_at
            ):
                raise ContinuousSessionError(
                    "outcome authority mutated lifecycle settlement causality"
                )
            if resolution is None:
                continue
            if type(resolution) is not SettlementResolution:
                raise ContinuousSessionError(
                    "outcome authority must return exact SettlementResolution or None"
                )
            if (
                type(resolution.event_identity) is not str
                or type(resolution.settlement_ref) is not str
                or type(resolution.evidence_id) is not str
                or type(resolution.evidence_sha256) is not str
                or type(resolution.available_at) is not str
            ):
                raise ContinuousSessionError(
                    "outcome authority returned malformed settlement resolution fields"
                )
            if resolution.event_identity != record_identity:
                raise ContinuousSessionError(
                    "settlement evidence event identity does not match lifecycle identity"
                )
            if resolution.settlement_ref != record_settlement_ref:
                raise ContinuousSessionError(
                    "settlement evidence reference does not match lifecycle evidence"
                )
            # Reject non-canonical mapping types before snapshotting: coercing
            # an arbitrary mapping with dict(...) can execute attacker-controlled
            # iteration or collapse non-canonical input before validation.
            if type(resolution.quote_outcomes) is not dict:
                raise ContinuousSessionError(
                    "settlement resolution quote_outcomes must be an exact dict"
                )
            # Detach mutable quote_outcomes before validation.  Validating the
            # authority-owned mapping and copying it afterwards leaves a TOCTOU
            # window where external mutation can change already-validated
            # settlement truth before product state takes ownership.
            resolution = _replace(
                resolution,
                quote_outcomes=resolution.quote_outcomes.copy(),
            )
            try:
                _resolution_validate(resolution, as_of=as_of)
            except (TypeError, ValueError) as exc:
                raise ContinuousSessionError(
                    "outcome authority returned invalid settlement resolution"
                ) from exc
            settlement_key = (
                resolution.event_identity,
                resolution.settlement_ref,
            )
            existing_outcomes = outcome_by_settlement.get(settlement_key)
            if (
                existing_outcomes is not None
                and existing_outcomes != resolution.quote_outcomes
            ):
                raise ContinuousSessionError(
                    "outcome authority returned conflicting settlement outcomes"
                )
            outcome_by_settlement.setdefault(
                settlement_key,
                dict(resolution.quote_outcomes),
            )
            existing = evidence_by_id.get(resolution.evidence_id)
            if existing is not None:
                if existing != resolution:
                    raise ContinuousSessionError(
                        "outcome authority returned conflicting duplicate evidence_id"
                    )
                continue
            evidence_by_id[resolution.evidence_id] = resolution
            resolutions.append(resolution)
        return tuple(resolutions)

    @staticmethod
    def _detached_settlement_resolutions(
        resolutions: tuple[SettlementResolution, ...],
        _replace: Callable[..., SettlementResolution] = replace,
        _replace_code: object = replace.__code__,
    ) -> tuple[SettlementResolution, ...]:
        if (
            replace is not _replace
            or getattr(_replace, "__code__", None) is not _replace_code
        ):
            raise ContinuousSessionError(
                "settlement callback copy authority changed"
            )
        return tuple(
            _replace(
                resolution,
                quote_outcomes=resolution.quote_outcomes.copy(),
            )
            for resolution in resolutions
        )

    def _load_book(
        self,
        *,
        paper_book_path: Path | None = None,
        initial_bankroll: str | None = None,
        _paper_book_type: type[PaperBook] = PaperBook,
        _paper_book_load: Callable[..., PaperBook] = PaperBook.load,
        _paper_book_load_descriptor: object = PaperBook.__dict__["load"],
        _path_exists: Callable[[Path], bool] = Path.exists,
        _path_exists_code: object = Path.exists.__code__,
    ) -> PaperBook:
        current_load = PaperBook.__dict__.get("load")
        if (
            PaperBook is not _paper_book_type
            or current_load is not _paper_book_load_descriptor
            or not isinstance(current_load, classmethod)
            or current_load.__func__ is not getattr(
                _paper_book_load_descriptor,
                "__func__",
                None,
            )
            or Path.exists is not _path_exists
            or getattr(_path_exists, "__code__", None) is not _path_exists_code
        ):
            raise ContinuousSessionError(
                "settlement book loader authority changed"
            )
        paper_book_path = (
            self.paper_book_path if paper_book_path is None else paper_book_path
        )
        initial_bankroll = (
            self.initial_bankroll if initial_bankroll is None else initial_bankroll
        )
        if _path_exists(paper_book_path):
            return _paper_book_load(paper_book_path)
        return _paper_book_type(initial_bankroll)

    @_seal_settlement_consumer_entry
    @_bind_canonical_settlement_engine
    def _settle(
        self,
        *,
        resolutions: tuple[SettlementResolution, ...],
        workspace: Path | None = None,
        paper_book_path: Path | None = None,
        initial_bankroll: str | None = None,
        _settlement_engine_type: type[SettlementEngine],
        _resolution_validate: Callable[..., None],
        _replace: Callable[..., SettlementResolution],
        _paper_book_save: Callable[..., None],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if not resolutions:
            return (), ()
        if SettlementEngine is not _settlement_engine_type:
            raise ContinuousSessionError(
                "settlement engine constructor origin changed"
            )
        if SettlementResolution.validate is not _resolution_validate:
            raise ContinuousSessionError(
                "settlement consumer validator authority changed"
            )
        if replace is not _replace:
            raise ContinuousSessionError(
                "settlement consumer copy authority changed"
            )
        if PaperBook.save is not _paper_book_save:
            raise ContinuousSessionError(
                "settlement consumer persistence authority changed"
            )
        if type(resolutions) is not tuple:
            raise TypeError("resolutions must be an exact tuple")
        workspace = self.workspace if workspace is None else workspace
        paper_book_path = (
            self.paper_book_path if paper_book_path is None else paper_book_path
        )
        initial_bankroll = (
            self.initial_bankroll if initial_bankroll is None else initial_bankroll
        )
        unique: dict[str, SettlementResolution] = {}
        outcome_by_settlement: dict[tuple[str, str], dict[str, str]] = {}
        for resolution in resolutions:
            if type(resolution) is not SettlementResolution:
                raise ContinuousSessionError(
                    "settlement consumer requires exact SettlementResolution values"
                )
            if type(resolution.quote_outcomes) is not dict:
                raise ContinuousSessionError(
                    "settlement consumer quote_outcomes must be an exact dict"
                )
            # Take ownership of the only mutable field before any economic I/O.
            # A caller retaining the input resolution must not be able to alter
            # outcomes after the consumer has accepted the batch.
            resolution = _replace(
                resolution,
                quote_outcomes=resolution.quote_outcomes.copy(),
            )
            try:
                # The collector already enforces the external causal cutoff.
                # At this lower economic boundary, self-cutoff validation
                # independently rejects malformed identity/evidence/outcomes
                # before workspace or PaperBook I/O.
                _resolution_validate(
                    resolution,
                    as_of=resolution.available_at,
                )
            except (TypeError, ValueError) as exc:
                raise ContinuousSessionError(
                    "settlement consumer received invalid settlement resolution"
                ) from exc
            settlement_key = (
                resolution.event_identity,
                resolution.settlement_ref,
            )
            existing_outcomes = outcome_by_settlement.get(settlement_key)
            if (
                existing_outcomes is not None
                and existing_outcomes != resolution.quote_outcomes
            ):
                raise ContinuousSessionError(
                    "settlement consumer received conflicting settlement outcomes"
                )
            outcome_by_settlement.setdefault(
                settlement_key,
                dict(resolution.quote_outcomes),
            )
            existing = unique.get(resolution.evidence_id)
            if existing is not None:
                if existing != resolution:
                    raise ContinuousSessionError(
                        "settlement consumer received conflicting duplicate evidence_id"
                    )
                continue
            unique[resolution.evidence_id] = resolution

        with WorkspaceEconomicLock(workspace):
            book = ContinuousSessionCoordinator._load_book(
                self,
                paper_book_path=paper_book_path,
                initial_bankroll=initial_bankroll,
            )
            engine = _settlement_engine_type()
            if type(engine) is not _settlement_engine_type:
                raise ContinuousSessionError(
                    "settlement engine constructor returned non-canonical type"
                )
            for resolution in unique.values():
                allowed = ContinuousSessionCoordinator._open_quote_keys_for_book(
                    book,
                    resolution.event_identity,
                )
                scoped = {
                    quote_key: outcome
                    for quote_key, outcome in resolution.quote_outcomes.items()
                    if quote_key in allowed
                }
                if scoped:
                    engine.record(scoped)
            settled = tuple(engine.settle_ready(book))
            if settled:
                _paper_book_save(book, paper_book_path)

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

    def tick(
        self,
        *,
        _running_fence: Callable[..., Any] = _ContinuousSessionState.running_fence,
        _running_fence_code: object = _ContinuousSessionState.running_fence.__code__,
        _require_running_method: Callable[["ContinuousSessionCoordinator"], None] = (
            _require_running
        ),
        _require_running_method_code: object = _require_running.__code__,
        _record_failure_method: Callable[
            ["_ContinuousSessionState"], _ContinuousSessionFailurePublication
        ] = _ContinuousSessionState.record_failure,
        _record_failure_method_code: object = _ContinuousSessionState.record_failure.__code__,
        _record_success_method: Callable[..., int] = _ContinuousSessionState.record_success,
        _record_success_method_code: object = _ContinuousSessionState.record_success.__code__,
        _validate_settlement_evidence_method: Callable[..., None] = (
            _ContinuousSessionState.validate_settlement_evidence
        ),
        _validate_settlement_evidence_method_code: object = (
            _ContinuousSessionState.validate_settlement_evidence.__code__
        ),
        _refresh_source_state_projection_method: Callable[
            ["ContinuousSessionCoordinator"], ContinuousSessionStatus
        ] = _refresh_source_state_projection,
        _refresh_source_state_projection_method_code: object = (
            _refresh_source_state_projection.__code__
        ),
        _settlement_resolutions_method: Callable[
            ..., tuple[SettlementResolution, ...]
        ] = _settlement_resolutions,
        _settlement_resolutions_method_code: object = _settlement_resolutions.__code__,
        _drain_invalidations_method: Callable[
            ["ContinuousSessionCoordinator"],
            tuple[tuple[str, ...], bool, bool],
        ] = _drain_invalidations,
        _drain_invalidations_method_code: object = _drain_invalidations.__code__,
        _register_input_method: Callable[..., bool] = _register_input,
        _register_input_method_code: object = _register_input.__code__,
        _retire_input_method: Callable[..., bool] = _retire_input,
        _retire_input_method_code: object = _retire_input.__code__,
        _detached_settlement_resolutions_method: Callable[
            [tuple[SettlementResolution, ...]],
            tuple[SettlementResolution, ...],
        ] = _detached_settlement_resolutions.__func__,
        _detached_settlement_resolutions_method_code: object = (
            _detached_settlement_resolutions.__func__.__code__
        ),
        _instant_validator: Callable[[object, str], datetime] = _instant,
        _instant_validator_code: object = _instant.__code__,
        _dependency_index_type: type[FocusedMirrorDependencyIndex] = (
            FocusedMirrorDependencyIndex
        ),
        _dependency_type: type[FocusedMirrorDependency] = FocusedMirrorDependency,
        _dependency_matches: Callable[[FocusedMirrorDependency, object], bool] = (
            FocusedMirrorDependency.matches
        ),
        _dependency_matches_code: object = FocusedMirrorDependency.matches.__code__,
        _dependency_equals: Callable[[FocusedMirrorDependency, object], object] = (
            FocusedMirrorDependency.__eq__
        ),
        _dependency_equals_code: object = FocusedMirrorDependency.__eq__.__code__,
        _dependency_reader: Callable[
            [FocusedMirrorDependencyIndex, str], FocusedMirrorDependency
        ] = FocusedMirrorDependencyIndex._dependency,
        _dependency_reader_code: object = FocusedMirrorDependencyIndex._dependency.__code__,
        _matching_keys_reader: Callable[..., tuple[object, ...]] = (
            FocusedMirrorDependencyIndex.matching_keys
        ),
        _matching_keys_reader_code: object = FocusedMirrorDependencyIndex.matching_keys.__code__,
        _dependency_register_method: Callable[..., FocusedMirrorDependency] = (
            FocusedMirrorDependencyIndex.register
        ),
        _dependency_register_method_code: object = FocusedMirrorDependencyIndex.register.__code__,
        _dependency_unregister_method: Callable[..., bool] = (
            FocusedMirrorDependencyIndex.unregister
        ),
        _dependency_unregister_method_code: object = FocusedMirrorDependencyIndex.unregister.__code__,
        _dependency_affected_inputs_method: Callable[..., tuple[str, ...]] = (
            FocusedMirrorDependencyIndex.affected_inputs
        ),
        _dependency_affected_inputs_method_code: object = (
            FocusedMirrorDependencyIndex.affected_inputs.__code__
        ),
        _dependency_input_ids_descriptor: object = (
            FocusedMirrorDependencyIndex.__dict__["input_ids"]
        ),
        _dependency_input_ids_getter_code: object = (
            FocusedMirrorDependencyIndex.input_ids.fget.__code__
        ),
        _invalidation_buffer_type: type[BoundedMirrorInvalidationBuffer] = (
            BoundedMirrorInvalidationBuffer
        ),
        _invalidation_drain_method: Callable[..., MirrorInvalidationBatch] = (
            BoundedMirrorInvalidationBuffer.drain
        ),
        _invalidation_drain_method_code: object = (
            BoundedMirrorInvalidationBuffer.drain.__code__
        ),
        _invalidation_pending_descriptor: object = (
            BoundedMirrorInvalidationBuffer.__dict__["pending_count"]
        ),
        _invalidation_pending_getter: Callable[
            [BoundedMirrorInvalidationBuffer], int
        ] = BoundedMirrorInvalidationBuffer.pending_count.fget,
        _invalidation_pending_getter_code: object = (
            BoundedMirrorInvalidationBuffer.pending_count.fget.__code__
        ),
        _invalidation_full_refresh_descriptor: object = (
            BoundedMirrorInvalidationBuffer.__dict__["full_refresh_required"]
        ),
        _invalidation_full_refresh_getter: Callable[
            [BoundedMirrorInvalidationBuffer], bool
        ] = BoundedMirrorInvalidationBuffer.full_refresh_required.fget,
        _invalidation_full_refresh_getter_code: object = (
            BoundedMirrorInvalidationBuffer.full_refresh_required.fget.__code__
        ),
        _invalidation_state_validator: Callable[
            [BoundedMirrorInvalidationBuffer], None
        ] = _validate_canonical_invalidation_buffer_state,
        _invalidation_state_validator_code: object = (
            _validate_canonical_invalidation_buffer_state.__code__
        ),
        _lifecycle_type: type[ContinuousEventLifecycle] = ContinuousEventLifecycle,
        _lifecycle_register_eligible_method: Callable[..., tuple[str, ...]] = (
            ContinuousEventLifecycle.register_eligible
        ),
        _lifecycle_register_eligible_method_code: object = (
            ContinuousEventLifecycle.register_eligible.__code__
        ),
        _lifecycle_records_method: Callable[..., tuple[EventLifecycleRecord, ...]] = (
            ContinuousEventLifecycle.records
        ),
        _lifecycle_records_method_code: object = ContinuousEventLifecycle.records.__code__,
    ) -> ContinuousTickResult:
        state = self._state
        if (
            type(state).running_fence is not _running_fence
            or getattr(_running_fence, "__code__", None) is not _running_fence_code
            or type(self)._require_running is not _require_running_method
            or getattr(_require_running_method, "__code__", None)
            is not _require_running_method_code
            or type(state).record_failure is not _record_failure_method
            or getattr(_record_failure_method, "__code__", None)
            is not _record_failure_method_code
            or type(state).record_success is not _record_success_method
            or getattr(_record_success_method, "__code__", None)
            is not _record_success_method_code
            or type(state).validate_settlement_evidence
            is not _validate_settlement_evidence_method
            or getattr(_validate_settlement_evidence_method, "__code__", None)
            is not _validate_settlement_evidence_method_code
            or type(self)._refresh_source_state_projection
            is not _refresh_source_state_projection_method
            or getattr(_refresh_source_state_projection_method, "__code__", None)
            is not _refresh_source_state_projection_method_code
            or type(self)._settlement_resolutions is not _settlement_resolutions_method
            or getattr(_settlement_resolutions_method, "__code__", None)
            is not _settlement_resolutions_method_code
            or type(self)._drain_invalidations is not _drain_invalidations_method
            or getattr(_drain_invalidations_method, "__code__", None)
            is not _drain_invalidations_method_code
            or type(self)._register_input is not _register_input_method
            or getattr(_register_input_method, "__code__", None)
            is not _register_input_method_code
            or type(self)._retire_input is not _retire_input_method
            or getattr(_retire_input_method, "__code__", None)
            is not _retire_input_method_code
            or type(self)._detached_settlement_resolutions
            is not _detached_settlement_resolutions_method
            or getattr(
                _detached_settlement_resolutions_method,
                "__code__",
                None,
            )
            is not _detached_settlement_resolutions_method_code
            or _instant is not _instant_validator
            or getattr(_instant_validator, "__code__", None)
            is not _instant_validator_code
            or FocusedMirrorDependencyIndex is not _dependency_index_type
            or FocusedMirrorDependency is not _dependency_type
            or _dependency_type.matches is not _dependency_matches
            or getattr(_dependency_matches, "__code__", None)
            is not _dependency_matches_code
            or _dependency_type.__eq__ is not _dependency_equals
            or getattr(_dependency_equals, "__code__", None)
            is not _dependency_equals_code
            or _dependency_index_type._dependency is not _dependency_reader
            or getattr(_dependency_reader, "__code__", None)
            is not _dependency_reader_code
            or _dependency_index_type.matching_keys is not _matching_keys_reader
            or getattr(_matching_keys_reader, "__code__", None)
            is not _matching_keys_reader_code
            or _validate_canonical_invalidation_buffer_state
            is not _invalidation_state_validator
            or getattr(_invalidation_state_validator, "__code__", None)
            is not _invalidation_state_validator_code
        ):
            raise ContinuousSessionError(
                "canonical coordinator running-fence authority changed"
            )
        _require_running_method(self)
        observation_token = state._checkpoint_token
        collector = self.collector
        collector_run_cycle = getattr(collector, "run_cycle", None)
        collector_source_id = getattr(collector, "source_id", None)
        if (
            type(collector_source_id) is not str
            or not collector_source_id
            or collector_source_id.strip() != collector_source_id
            or collector_source_id != state.source_id
        ):
            raise ContinuousSessionError(
                "collector source identity does not match continuous session"
            )
        collector_delta_store = getattr(collector, "delta_store", None)
        collector_delta_reader = getattr(
            collector_delta_store,
            "deltas_after_commit",
            None,
        )
        collector_config = getattr(collector, "config", None)
        collector_max_items = getattr(collector_config, "max_items", None)
        lifecycle = self.lifecycle
        if (
            isinstance(lifecycle, _lifecycle_type)
            and type(lifecycle) is not _lifecycle_type
        ):
            raise ContinuousSessionError(
                "canonical event lifecycle subtype is not supported"
            )
        lifecycle_was_canonical = type(lifecycle) is _lifecycle_type
        lifecycle_register_eligible = getattr(lifecycle, "register_eligible", None)
        lifecycle_records = getattr(lifecycle, "records", None)
        market_store = self.market_store
        desktop_consumer = self.desktop_consumer
        desktop_drain = getattr(desktop_consumer, "drain", None)
        invalidation_buffer = self.invalidation_buffer
        if (
            isinstance(invalidation_buffer, _invalidation_buffer_type)
            and type(invalidation_buffer) is not _invalidation_buffer_type
        ):
            raise ContinuousSessionError(
                "canonical invalidation buffer subtype is not supported"
            )
        invalidation_buffer_was_canonical = (
            type(invalidation_buffer) is _invalidation_buffer_type
        )
        invalidation_drain = getattr(invalidation_buffer, "drain", None)
        invalidation_buffer_mirror: object | None = None
        invalidation_dirty_storage: object | None = None
        invalidation_buffer_lock: object | None = None
        invalidation_max_dirty_keys: object | None = None
        if type(invalidation_buffer) is _invalidation_buffer_type:
            _invalidation_state_validator(invalidation_buffer)
            invalidation_buffer_mirror = invalidation_buffer._mirror
            invalidation_dirty_storage = invalidation_buffer._dirty
            invalidation_buffer_lock = invalidation_buffer._lock
            invalidation_max_dirty_keys = invalidation_buffer._max_dirty_keys
        dependency_index = self.dependency_index
        if (
            isinstance(dependency_index, _dependency_index_type)
            and type(dependency_index) is not _dependency_index_type
        ):
            raise ContinuousSessionError(
                "canonical dependency index subtype is not supported"
            )
        dependency_index_was_canonical = (
            type(dependency_index) is _dependency_index_type
        )
        dependency_affected_inputs = getattr(
            dependency_index,
            "affected_inputs",
            None,
        )
        dependency_register = getattr(dependency_index, "register", None)
        dependency_unregister = getattr(dependency_index, "unregister", None)
        max_invalidation_batches = self.max_invalidation_batches_per_tick
        max_invalidation_items = self.max_invalidation_items_per_batch
        causal_view = self.causal_view
        required_history = self.required_history
        clock = self.clock
        outcome_authority = self.outcome_authority
        outcome_resolver = (
            None
            if outcome_authority is None
            else getattr(outcome_authority, "resolve", None)
        )
        if outcome_resolver is not None and not callable(outcome_resolver):
            raise ContinuousSessionError(
                "settlement outcome authority resolver is not callable"
            )
        if outcome_resolver is not None and not callable(lifecycle_records):
            raise ContinuousSessionError(
                "lifecycle settlement records authority is unavailable"
            )
        learning_handoff = self.settlement_learning_handoff
        prepare_settlement = None
        reconcile_after_settlement = None
        if learning_handoff is not None:
            prepare_settlement = getattr(
                learning_handoff,
                "prepare_settlement",
                None,
            )
            reconcile_after_settlement = getattr(
                learning_handoff,
                "reconcile_after_settlement",
                None,
            )
            if prepare_settlement is not None and not callable(prepare_settlement):
                raise ContinuousSessionError(
                    "settlement learning prepare callback is not callable"
                )
            if not callable(reconcile_after_settlement):
                raise ContinuousSessionError(
                    "settlement learning reconcile callback is not callable"
                )
        workspace = self.workspace
        paper_book_path = self.paper_book_path
        initial_bankroll = self.initial_bankroll

        if not callable(collector_run_cycle):
            raise ContinuousSessionError(
                "collector cycle authority is unavailable"
            )
        if not callable(collector_delta_reader):
            raise ContinuousSessionError(
                "collector projection authority is unavailable"
            )
        if (
            type(collector_max_items) is not int
            or collector_max_items <= 0
        ):
            raise ContinuousSessionError(
                "collector projection configuration is invalid"
            )
        if not callable(lifecycle_register_eligible):
            raise ContinuousSessionError(
                "lifecycle registration authority is unavailable"
            )
        if not callable(desktop_drain):
            raise ContinuousSessionError(
                "desktop delivery authority is unavailable"
            )
        if not callable(invalidation_drain) or not callable(
            dependency_affected_inputs
        ):
            raise ContinuousSessionError(
                "continuous-session invalidation routing authority is unavailable"
            )
        if (
            type(max_invalidation_batches) is not int
            or max_invalidation_batches <= 0
            or type(max_invalidation_items) is not int
            or max_invalidation_items <= 0
        ):
            raise ContinuousSessionError(
                "continuous-session invalidation bounds are invalid"
            )
        if type(causal_view) is not CausalView:
            raise ContinuousSessionError(
                "continuous-session causal view is invalid"
            )
        if (
            not isinstance(required_history, timedelta)
            or required_history.total_seconds() < 0
        ):
            raise ContinuousSessionError(
                "continuous-session required history is invalid"
            )
        if not callable(clock):
            raise ContinuousSessionError(
                "continuous-session clock authority is unavailable"
            )

        def restore_state_identity() -> bool:
            if self._state is state:
                return False
            self._state = state
            return True

        def require_state_identity() -> None:
            if restore_state_identity():
                raise ContinuousSessionError(
                    "continuous session state authority changed during tick"
                )

        def restore_dependency_index_type_authority() -> bool:
            if (
                not dependency_index_was_canonical
                or type(dependency_index) is _dependency_index_type
            ):
                return False
            try:
                object.__setattr__(
                    dependency_index,
                    "__class__",
                    _dependency_index_type,
                )
            except (AttributeError, TypeError):
                pass
            return True

        def restore_lifecycle_type_authority() -> bool:
            if not lifecycle_was_canonical or type(lifecycle) is _lifecycle_type:
                return False
            try:
                object.__setattr__(
                    lifecycle,
                    "__class__",
                    _lifecycle_type,
                )
            except (AttributeError, TypeError):
                pass
            return True

        def restore_invalidation_buffer_type_authority() -> bool:
            if (
                not invalidation_buffer_was_canonical
                or type(invalidation_buffer) is _invalidation_buffer_type
            ):
                return False
            try:
                object.__setattr__(
                    invalidation_buffer,
                    "__class__",
                    _invalidation_buffer_type,
                )
            except (AttributeError, TypeError):
                pass
            return True

        def restore_dependency_index_identity() -> bool:
            type_changed = restore_dependency_index_type_authority()
            if self.dependency_index is dependency_index:
                return type_changed
            self.dependency_index = dependency_index
            return True

        def require_dependency_index_identity() -> None:
            if restore_dependency_index_identity():
                raise ContinuousSessionError(
                    "continuous-session dependency index authority changed during tick"
                )

        def restore_invalidation_buffer_identity() -> bool:
            type_changed = restore_invalidation_buffer_type_authority()
            if self.invalidation_buffer is invalidation_buffer:
                return type_changed
            self.invalidation_buffer = invalidation_buffer
            return True

        def require_invalidation_buffer_identity() -> None:
            if restore_invalidation_buffer_identity():
                raise ContinuousSessionError(
                    "continuous-session invalidation buffer authority changed during tick"
                )

        def restore_invalidation_buffer_structure_authority() -> bool:
            type_changed = restore_invalidation_buffer_type_authority()
            if type(invalidation_buffer) is not _invalidation_buffer_type:
                return type_changed
            changed = type_changed or (
                invalidation_buffer._mirror is not invalidation_buffer_mirror
                or invalidation_buffer._dirty is not invalidation_dirty_storage
                or invalidation_buffer._lock is not invalidation_buffer_lock
                or invalidation_buffer._max_dirty_keys != invalidation_max_dirty_keys
            )
            object.__setattr__(
                invalidation_buffer,
                "_mirror",
                invalidation_buffer_mirror,
            )
            object.__setattr__(
                invalidation_buffer,
                "_dirty",
                invalidation_dirty_storage,
            )
            object.__setattr__(
                invalidation_buffer,
                "_lock",
                invalidation_buffer_lock,
            )
            object.__setattr__(
                invalidation_buffer,
                "_max_dirty_keys",
                invalidation_max_dirty_keys,
            )
            return changed

        def require_invalidation_buffer_structure_authority() -> None:
            if restore_invalidation_buffer_structure_authority():
                raise ContinuousSessionError(
                    "continuous-session invalidation buffer structure changed during tick"
                )

        def require_invalidation_buffer_dispatch_authority() -> None:
            if type(invalidation_buffer) is not _invalidation_buffer_type:
                return
            pending_descriptor = _invalidation_buffer_type.__dict__.get(
                "pending_count"
            )
            full_refresh_descriptor = _invalidation_buffer_type.__dict__.get(
                "full_refresh_required"
            )
            if (
                BoundedMirrorInvalidationBuffer is not _invalidation_buffer_type
                or getattr(invalidation_drain, "__self__", None)
                is not invalidation_buffer
                or getattr(invalidation_drain, "__func__", None)
                is not _invalidation_drain_method
                or getattr(_invalidation_drain_method, "__code__", None)
                is not _invalidation_drain_method_code
                or pending_descriptor is not _invalidation_pending_descriptor
                or getattr(pending_descriptor, "fget", None)
                is not _invalidation_pending_getter
                or getattr(_invalidation_pending_getter, "__code__", None)
                is not _invalidation_pending_getter_code
                or full_refresh_descriptor
                is not _invalidation_full_refresh_descriptor
                or getattr(full_refresh_descriptor, "fget", None)
                is not _invalidation_full_refresh_getter
                or getattr(_invalidation_full_refresh_getter, "__code__", None)
                is not _invalidation_full_refresh_getter_code
            ):
                raise ContinuousSessionError(
                    "canonical invalidation buffer dispatch authority changed"
                )

        def require_lifecycle_dispatch_authority() -> None:
            if restore_dependency_index_type_authority():
                raise ContinuousSessionError(
                    "canonical dependency index type changed during tick"
                )
            if restore_lifecycle_type_authority():
                raise ContinuousSessionError(
                    "canonical event lifecycle type changed during tick"
                )
            if type(dependency_index) is _dependency_index_type:
                descriptor = _dependency_index_type.__dict__.get("input_ids")
                if (
                    FocusedMirrorDependencyIndex is not _dependency_index_type
                    or FocusedMirrorDependency is not _dependency_type
                    or getattr(dependency_register, "__self__", None)
                    is not dependency_index
                    or getattr(dependency_register, "__func__", None)
                    is not _dependency_register_method
                    or getattr(_dependency_register_method, "__code__", None)
                    is not _dependency_register_method_code
                    or getattr(dependency_unregister, "__self__", None)
                    is not dependency_index
                    or getattr(dependency_unregister, "__func__", None)
                    is not _dependency_unregister_method
                    or getattr(_dependency_unregister_method, "__code__", None)
                    is not _dependency_unregister_method_code
                    or _dependency_type.matches is not _dependency_matches
                    or getattr(_dependency_matches, "__code__", None)
                    is not _dependency_matches_code
                    or _dependency_type.__eq__ is not _dependency_equals
                    or getattr(_dependency_equals, "__code__", None)
                    is not _dependency_equals_code
                    or _dependency_index_type._dependency is not _dependency_reader
                    or getattr(_dependency_reader, "__code__", None)
                    is not _dependency_reader_code
                    or _dependency_index_type.matching_keys is not _matching_keys_reader
                    or getattr(_matching_keys_reader, "__code__", None)
                    is not _matching_keys_reader_code
                    or getattr(dependency_affected_inputs, "__self__", None)
                    is not dependency_index
                    or getattr(dependency_affected_inputs, "__func__", None)
                    is not _dependency_affected_inputs_method
                    or getattr(_dependency_affected_inputs_method, "__code__", None)
                    is not _dependency_affected_inputs_method_code
                    or descriptor is not _dependency_input_ids_descriptor
                    or getattr(descriptor, "fget", None) is None
                    or getattr(descriptor.fget, "__code__", None)
                    is not _dependency_input_ids_getter_code
                ):
                    raise ContinuousSessionError(
                        "canonical dependency lifecycle dispatch authority changed"
                    )
            if type(lifecycle) is _lifecycle_type:
                if (
                    ContinuousEventLifecycle is not _lifecycle_type
                    or getattr(lifecycle_register_eligible, "__self__", None)
                    is not lifecycle
                    or getattr(lifecycle_register_eligible, "__func__", None)
                    is not _lifecycle_register_eligible_method
                    or getattr(_lifecycle_register_eligible_method, "__code__", None)
                    is not _lifecycle_register_eligible_method_code
                    or getattr(lifecycle_records, "__self__", None) is not lifecycle
                    or getattr(lifecycle_records, "__func__", None)
                    is not _lifecycle_records_method
                    or getattr(_lifecycle_records_method, "__code__", None)
                    is not _lifecycle_records_method_code
                ):
                    raise ContinuousSessionError(
                        "canonical event lifecycle dispatch authority changed"
                    )

        require_invalidation_buffer_structure_authority()
        require_invalidation_buffer_dispatch_authority()
        require_lifecycle_dispatch_authority()

        def dependency_fingerprint(
            dependency: FocusedMirrorDependency,
        ) -> tuple[object, ...]:
            if type(dependency) is not _dependency_type:
                raise ContinuousSessionError(
                    "dependency index published non-canonical dependency state"
                )
            return (
                dependency.input_id,
                dependency.source_ids,
                dependency.sports,
                dependency.event_ids,
                dependency.market_ids,
                dependency.selection_ids,
            )

        tick_dependency_mirror: object | None = None
        tick_dependency_storage: object | None = None
        tick_matched_keys_storage: object | None = None
        tick_dependency_lock: object | None = None
        tick_dependency_input_ids: tuple[str, ...] | None = None
        tick_dependency_fingerprints: tuple[
            tuple[str, tuple[object, ...]], ...
        ] | None = None
        tick_dependency_entries: tuple[
            tuple[str, FocusedMirrorDependency, tuple[object, ...]], ...
        ] | None = None
        tick_matching_keys: tuple[
            tuple[str, tuple[object, ...]], ...
        ] | None = None
        tick_matching_key_sets: tuple[
            tuple[str, set[object], tuple[object, ...]], ...
        ] | None = None

        if type(dependency_index) is _dependency_index_type:
            tick_dependency_mirror = dependency_index._mirror
            tick_dependency_storage = dependency_index._dependencies
            tick_matched_keys_storage = dependency_index._matched_keys
            tick_dependency_lock = dependency_index._lock

        def refresh_tick_dependency_routing_authority() -> None:
            require_invalidation_buffer_identity()
            require_invalidation_buffer_structure_authority()
            nonlocal tick_dependency_input_ids
            nonlocal tick_dependency_fingerprints
            nonlocal tick_dependency_entries
            nonlocal tick_matching_keys
            nonlocal tick_matching_key_sets
            require_lifecycle_dispatch_authority()
            if type(dependency_index) is not _dependency_index_type:
                tick_dependency_input_ids = None
                tick_dependency_fingerprints = None
                tick_dependency_entries = None
                tick_matching_keys = None
                tick_matching_key_sets = None
                return
            input_ids = dependency_index.input_ids
            if (
                type(input_ids) is not tuple
                or any(
                    type(input_id) is not str
                    or not input_id
                    or input_id.strip() != input_id
                    for input_id in input_ids
                )
                or len(set(input_ids)) != len(input_ids)
            ):
                raise ContinuousSessionError(
                    "dependency index input identity state is invalid"
                )
            tick_dependency_input_ids = input_ids
            tick_dependency_entries = tuple(
                (
                    input_id,
                    _dependency_reader(dependency_index, input_id),
                    dependency_fingerprint(
                        _dependency_reader(dependency_index, input_id)
                    ),
                )
                for input_id in input_ids
            )
            tick_dependency_fingerprints = tuple(
                (input_id, fingerprint)
                for input_id, _dependency, fingerprint in tick_dependency_entries
            )
            tick_matching_key_sets = tuple(
                (
                    input_id,
                    dependency_index._matched_keys[input_id],
                    _matching_keys_reader(dependency_index, input_id),
                )
                for input_id in input_ids
            )
            tick_matching_keys = tuple(
                (input_id, keys)
                for input_id, _matched_set, keys in tick_matching_key_sets
            )

        def restore_tick_dependency_routing_authority() -> bool:
            if (
                type(dependency_index) is not _dependency_index_type
                or tick_dependency_entries is None
                or tick_matching_key_sets is None
                or tick_dependency_storage is None
                or tick_matched_keys_storage is None
                or tick_dependency_lock is None
            ):
                return False
            changed = (
                dependency_index._mirror is not tick_dependency_mirror
                or dependency_index._dependencies is not tick_dependency_storage
                or dependency_index._matched_keys is not tick_matched_keys_storage
                or dependency_index._lock is not tick_dependency_lock
                or tuple(dependency_index._dependencies) != tick_dependency_input_ids
            )
            object.__setattr__(dependency_index, "_mirror", tick_dependency_mirror)
            object.__setattr__(
                dependency_index,
                "_dependencies",
                tick_dependency_storage,
            )
            object.__setattr__(
                dependency_index,
                "_matched_keys",
                tick_matched_keys_storage,
            )
            object.__setattr__(dependency_index, "_lock", tick_dependency_lock)
            with tick_dependency_lock:
                tick_dependency_storage.clear()
                for input_id, dependency, fingerprint in tick_dependency_entries:
                    for field_name, value in zip(
                        (
                            "input_id",
                            "source_ids",
                            "sports",
                            "event_ids",
                            "market_ids",
                            "selection_ids",
                        ),
                        fingerprint,
                    ):
                        if getattr(dependency, field_name) != value:
                            changed = True
                            object.__setattr__(dependency, field_name, value)
                    tick_dependency_storage[input_id] = dependency

                tick_matched_keys_storage.clear()
                for input_id, matched_set, keys in tick_matching_key_sets:
                    if matched_set != set(keys):
                        changed = True
                    matched_set.clear()
                    matched_set.update(keys)
                    tick_matched_keys_storage[input_id] = matched_set
            return changed

        def require_tick_dependency_routing_authority(message: str) -> None:
            require_invalidation_buffer_identity()
            require_invalidation_buffer_structure_authority()
            require_lifecycle_dispatch_authority()
            if type(dependency_index) is not _dependency_index_type:
                return
            if (
                dependency_index._mirror is not tick_dependency_mirror
                or dependency_index._dependencies is not tick_dependency_storage
                or dependency_index._matched_keys is not tick_matched_keys_storage
                or dependency_index._lock is not tick_dependency_lock
            ):
                restore_tick_dependency_routing_authority()
                raise ContinuousSessionError(message)
            input_ids = dependency_index.input_ids
            if input_ids != tick_dependency_input_ids:
                restore_tick_dependency_routing_authority()
                raise ContinuousSessionError(message)
            try:
                current_dependencies = tuple(
                    (
                        input_id,
                        dependency_fingerprint(
                            _dependency_reader(dependency_index, input_id)
                        ),
                    )
                    for input_id in input_ids
                )
                current_matching_keys = tuple(
                    (
                        input_id,
                        _matching_keys_reader(dependency_index, input_id),
                    )
                    for input_id in input_ids
                )
            except Exception as exc:
                restore_tick_dependency_routing_authority()
                raise ContinuousSessionError(message) from exc
            if (
                current_dependencies != tick_dependency_fingerprints
                or current_matching_keys != tick_matching_keys
            ):
                restore_tick_dependency_routing_authority()
                raise ContinuousSessionError(message)

        refresh_tick_dependency_routing_authority()

        def require_economic_context() -> None:
            if (
                self.workspace != workspace
                or self.paper_book_path != paper_book_path
                or self.initial_bankroll != initial_bankroll
            ):
                raise ContinuousSessionError(
                    "settlement economic configuration changed during tick"
                )

        now = clock()
        _instant_validator(now, "now")

        try:
            cycle = collector_run_cycle()
            cycle_source_id = cycle.source_id
            cycle_provider_unavailable = cycle.provider_unavailable
            cycle_committed_delta_ids = cycle.committed_delta_ids
            if (
                type(cycle_source_id) is not str
                or not cycle_source_id
                or cycle_source_id.strip() != cycle_source_id
                or cycle_source_id != collector_source_id
                or type(cycle_provider_unavailable) is not bool
                or type(cycle_committed_delta_ids) is not tuple
                or any(
                    type(delta_id) is not str
                    or not delta_id
                    or delta_id.strip() != delta_id
                    for delta_id in cycle_committed_delta_ids
                )
                or len(set(cycle_committed_delta_ids))
                != len(cycle_committed_delta_ids)
                or (
                    cycle_provider_unavailable
                    and bool(cycle_committed_delta_ids)
                )
            ):
                raise ContinuousSessionError(
                    "collector returned invalid continuous-session cycle metadata"
                )
            require_state_identity()
            require_dependency_index_identity()
            require_invalidation_buffer_structure_authority()
            require_invalidation_buffer_dispatch_authority()
            require_lifecycle_dispatch_authority()
            require_tick_dependency_routing_authority(
                "dependency routing authority changed during collector observation"
            )
        except Exception as exc:
            state_was_rebound = restore_state_identity()
            dependency_index_was_rebound = restore_dependency_index_identity()
            lifecycle_type_was_changed = restore_lifecycle_type_authority()
            invalidation_buffer_was_rebound = restore_invalidation_buffer_identity()
            invalidation_structure_was_rebound = (
                restore_invalidation_buffer_structure_authority()
            )
            if lifecycle_type_was_changed:
                try:
                    exc.add_note(
                        "canonical event lifecycle runtime type was changed "
                        "during collector observation and was restored"
                    )
                except BaseException:
                    pass
            if invalidation_structure_was_rebound:
                try:
                    exc.add_note(
                        "continuous-session invalidation buffer structure was changed "
                        "during collector observation and was restored"
                    )
                except BaseException:
                    pass
            if invalidation_buffer_was_rebound:
                try:
                    exc.add_note(
                        "continuous-session invalidation buffer authority was rebound "
                        "during collector observation and was restored"
                    )
                except BaseException:
                    pass
            if dependency_index_was_rebound:
                try:
                    exc.add_note(
                        "continuous-session dependency index authority was rebound "
                        "during collector observation and was restored"
                    )
                except BaseException:
                    pass
            if state_was_rebound:
                try:
                    exc.add_note(
                        "continuous session state authority was rebound during "
                        "collector observation and was restored"
                    )
                except BaseException:
                    pass
            try:
                with _running_fence(state):
                    # Collector observation happens outside the long-lived product
                    # fence so operator pause remains responsive during provider I/O.
                    # Publish its failure only if no newer canonical generation won
                    # while that observation was in flight.
                    if state._checkpoint_token == observation_token:
                        _record_failure_method(
                            state,
                            code=type(exc).__name__,
                        )
            except (SessionPausedError, SessionStoppedError):
                # An operator transition supersedes the in-flight observation error.
                pass
            except Exception as checkpoint_exc:
                try:
                    exc.add_note(
                        "operational failure checkpoint could not be persisted: "
                        f"{type(checkpoint_exc).__name__}: {checkpoint_exc}"
                    )
                except BaseException:
                    pass
            raise

        if cycle_provider_unavailable:
            with _running_fence(state):
                if state._checkpoint_token != observation_token:
                    raise ContinuousSessionError(
                        "provider-unavailable observation was superseded by "
                        "a newer canonical session generation"
                    )
                # A provider-unavailable collector cycle commits no source deltas,
                # so there is no new source projection to publish. Avoid the full
                # continuous-session snapshot path here: retained settlement history
                # must not amplify an operational provider failure into O(history).
                try:
                    require_invalidation_buffer_structure_authority()
                    require_invalidation_buffer_dispatch_authority()
                    if type(invalidation_buffer) is _invalidation_buffer_type:
                        pending_count = _invalidation_pending_getter(
                            invalidation_buffer
                        )
                        pending_full_refresh = _invalidation_full_refresh_getter(
                            invalidation_buffer
                        )
                    else:
                        pending_count = invalidation_buffer.pending_count
                        pending_full_refresh = invalidation_buffer.full_refresh_required
                    if (
                        type(pending_count) is not int
                        or pending_count < 0
                        or type(pending_full_refresh) is not bool
                    ):
                        raise ContinuousSessionError(
                            "invalidation buffer backlog state is invalid"
                        )
                    require_state_identity()
                    require_dependency_index_identity()
                    require_tick_dependency_routing_authority(
                        "dependency routing authority changed during provider-unavailable backlog inspection"
                    )
                    failure = _record_failure_method(
                        state,
                        code="ProviderUnavailableError",
                    )
                except Exception as exc:
                    # This fast path intentionally does not replace malformed local
                    # backlog truth with ProviderUnavailableError. It must still
                    # restore coordinator authority if a backlog accessor mutates
                    # state while the provider-unavailable result is being handled.
                    state_was_rebound = restore_state_identity()
                    dependency_index_was_rebound = restore_dependency_index_identity()
                    lifecycle_type_was_changed = restore_lifecycle_type_authority()
                    invalidation_buffer_was_rebound = (
                        restore_invalidation_buffer_identity()
                    )
                    if lifecycle_type_was_changed:
                        try:
                            exc.add_note(
                                "canonical event lifecycle runtime type was changed "
                                "during provider-unavailable handling and was restored"
                            )
                        except BaseException:
                            pass
                    invalidation_structure_was_rebound = (
                        restore_invalidation_buffer_structure_authority()
                    )
                    if invalidation_structure_was_rebound:
                        try:
                            exc.add_note(
                                "continuous-session invalidation buffer structure was "
                                "changed during provider-unavailable backlog inspection "
                                "and was restored"
                            )
                        except BaseException:
                            pass
                    if invalidation_buffer_was_rebound:
                        try:
                            exc.add_note(
                                "continuous-session invalidation buffer authority was "
                                "rebound during provider-unavailable backlog inspection "
                                "and was restored"
                            )
                        except BaseException:
                            pass
                    if dependency_index_was_rebound:
                        try:
                            exc.add_note(
                                "continuous-session dependency index authority was "
                                "rebound during provider-unavailable backlog inspection "
                                "and was restored"
                            )
                        except BaseException:
                            pass
                    if state_was_rebound:
                        try:
                            exc.add_note(
                                "continuous session state authority was rebound during "
                                "provider-unavailable backlog inspection and was restored"
                            )
                        except BaseException:
                            pass
                    raise
            return ContinuousTickResult(
                session_id=failure.session_id,
                cycle_index=failure.cycles_completed,
                source_id=cycle_source_id,
                source_provider_unavailable=True,
                source_gap_states=(
                    ()
                    if failure.source_gap_state is None
                    else (failure.source_gap_state,)
                ),
                source_sync_states=(
                    ()
                    if failure.source_sync_state is None
                    else (failure.source_sync_state,)
                ),
                committed_delta_ids=cycle_committed_delta_ids,
                delivered_delta_ids=(),
                affected_input_ids=(),
                registered_input_ids=(),
                retired_input_ids=(),
                full_refresh_required=pending_full_refresh,
                invalidation_backlog=(
                    pending_count > 0 or pending_full_refresh
                ),
                settled_ticket_ids=(),
                settlement_evidence_ids=(),
                last_success_at=failure.last_success_at,
            )

        with _running_fence(state):
            if state._checkpoint_token != observation_token:
                raise ContinuousSessionError(
                    "collector observation was superseded by a newer "
                    "canonical session generation"
                )
            try:
                require_economic_context()
                source_snapshot = _refresh_source_state_projection_method(
                    self,
                    state=state,
                    collector=collector,
                    source_id=collector_source_id,
                    delta_store=collector_delta_store,
                    read_deltas=collector_delta_reader,
                    max_items=collector_max_items,
                )
                require_tick_dependency_routing_authority(
                    "dependency routing authority changed during source projection"
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
                require_state_identity()
                require_dependency_index_identity()
                delivered = desktop_drain(
                    as_of=now,
                    view=causal_view,
                )
                require_state_identity()
                require_dependency_index_identity()
                require_tick_dependency_routing_authority(
                    "dependency routing authority changed during desktop delivery"
                )
                if (
                    type(delivered) is not tuple
                    or any(
                        type(delta_id) is not str
                        or not delta_id
                        or delta_id.strip() != delta_id
                        for delta_id in delivered
                    )
                    or len(set(delivered)) != len(delivered)
                ):
                    raise ContinuousSessionError(
                        "desktop consumer returned invalid delivered delta ids"
                    )
                require_lifecycle_dispatch_authority()
                affected, full_refresh, backlog = _drain_invalidations_method(
                    self,
                    invalidation_buffer=invalidation_buffer,
                    dependency_index=dependency_index,
                    drain_invalidation=invalidation_drain,
                    affected_inputs=dependency_affected_inputs,
                    max_batches=max_invalidation_batches,
                    max_items=max_invalidation_items,
                )
                require_state_identity()
                require_dependency_index_identity()
                refresh_tick_dependency_routing_authority()

                lifecycle_index_before = dependency_index.input_ids
                if (
                    type(lifecycle_index_before) is not tuple
                    or any(
                        type(value) is not str
                        or not value
                        or value.strip() != value
                        for value in lifecycle_index_before
                    )
                    or len(set(lifecycle_index_before))
                    != len(lifecycle_index_before)
                ):
                    raise ContinuousSessionError(
                        "dependency index input identity state is invalid"
                    )
                expected_lifecycle_index_ids = list(lifecycle_index_before)
                lifecycle_expected_dependencies: dict[
                    str, tuple[object, ...]
                ] | None = None
                lifecycle_expected_matching_keys: dict[
                    str, tuple[object, ...]
                ] | None = None
                lifecycle_dependency_mirror: object | None = None
                lifecycle_dependency_storage: object | None = None
                lifecycle_matched_keys_storage: object | None = None
                lifecycle_dependency_lock: object | None = None
                if isinstance(dependency_index, _dependency_index_type):
                    lifecycle_expected_dependencies = {
                        input_id: dependency_fingerprint(
                            _dependency_reader(dependency_index, input_id)
                        )
                        for input_id in lifecycle_index_before
                    }
                    lifecycle_expected_matching_keys = {
                        input_id: _matching_keys_reader(
                            dependency_index,
                            input_id,
                        )
                        for input_id in lifecycle_index_before
                    }
                    lifecycle_dependency_mirror = dependency_index._mirror
                    lifecycle_dependency_storage = dependency_index._dependencies
                    lifecycle_matched_keys_storage = dependency_index._matched_keys
                    lifecycle_dependency_lock = dependency_index._lock
                registration_callback_ids: list[str] = []
                newly_registered: list[str] = []
                retired: list[str] = []

                def register(input_id: str, **selectors: object) -> None:
                    require_lifecycle_dispatch_authority()
                    registered_now = _register_input_method(
                        self,
                        input_id,
                        dependency_index=dependency_index,
                        register_input=dependency_register,
                        **selectors,
                    )
                    registration_callback_ids.append(input_id)
                    if registered_now:
                        if input_id in expected_lifecycle_index_ids:
                            raise ContinuousSessionError(
                                "lifecycle changed dependency index outside "
                                "coordinator callbacks"
                            )
                        newly_registered.append(input_id)
                        expected_lifecycle_index_ids.append(input_id)
                        if lifecycle_expected_dependencies is not None:
                            lifecycle_expected_dependencies[input_id] = (
                                dependency_fingerprint(
                                    _dependency_reader(dependency_index, input_id)
                                )
                            )
                            lifecycle_expected_matching_keys[input_id] = (
                                _matching_keys_reader(
                                    dependency_index,
                                    input_id,
                                )
                            )

                def retire(input_id: str) -> None:
                    require_lifecycle_dispatch_authority()
                    if _retire_input_method(
                        self,
                        input_id,
                        dependency_index=dependency_index,
                        unregister_input=dependency_unregister,
                    ):
                        if input_id not in expected_lifecycle_index_ids:
                            raise ContinuousSessionError(
                                "lifecycle changed dependency index outside "
                                "coordinator callbacks"
                            )
                        retired.append(input_id)
                        expected_lifecycle_index_ids.remove(input_id)
                        if lifecycle_expected_dependencies is not None:
                            lifecycle_expected_dependencies.pop(input_id, None)
                            lifecycle_expected_matching_keys.pop(input_id, None)

                require_lifecycle_dispatch_authority()
                registered = lifecycle_register_eligible(
                    market_store,
                    as_of=now,
                    required_history=required_history,
                    register_input=register,
                    retire_input=retire,
                )
                require_state_identity()
                require_dependency_index_identity()
                require_lifecycle_dispatch_authority()
                if (
                    type(registered) is not tuple
                    or any(
                        type(input_id) is not str
                        or not input_id
                        or input_id.strip() != input_id
                        for input_id in registered
                    )
                    or len(set(registered)) != len(registered)
                ):
                    raise ContinuousSessionError(
                        "lifecycle returned invalid registered input ids"
                    )
                # The lifecycle is canonical about eligibility; the index is canonical
                # about dependency routing. Keep both outputs for auditability, but never
                # report a lifecycle registration that is absent from the routing index.
                indexed_input_ids = dependency_index.input_ids
                if (
                    type(indexed_input_ids) is not tuple
                    or any(
                        type(value) is not str
                        or not value
                        or value.strip() != value
                        for value in indexed_input_ids
                    )
                    or len(set(indexed_input_ids)) != len(indexed_input_ids)
                ):
                    raise ContinuousSessionError(
                        "dependency index input identity state is invalid"
                    )
                if indexed_input_ids != tuple(expected_lifecycle_index_ids):
                    raise ContinuousSessionError(
                        "lifecycle changed dependency index outside coordinator callbacks"
                    )
                if lifecycle_expected_dependencies is not None:
                    if (
                        FocusedMirrorDependencyIndex is not _dependency_index_type
                        or _dependency_index_type._dependency is not _dependency_reader
                        or getattr(_dependency_reader, "__code__", None)
                        is not _dependency_reader_code
                        or _dependency_index_type.matching_keys
                        is not _matching_keys_reader
                        or getattr(_matching_keys_reader, "__code__", None)
                        is not _matching_keys_reader_code
                        or dependency_index._mirror is not lifecycle_dependency_mirror
                        or dependency_index._dependencies
                        is not lifecycle_dependency_storage
                        or dependency_index._matched_keys
                        is not lifecycle_matched_keys_storage
                        or dependency_index._lock is not lifecycle_dependency_lock
                    ):
                        raise ContinuousSessionError(
                            "lifecycle changed dependency routing authority outside "
                            "coordinator callbacks"
                        )
                    current_dependencies = tuple(
                        dependency_fingerprint(
                            _dependency_reader(dependency_index, input_id)
                        )
                        for input_id in indexed_input_ids
                    )
                    expected_dependencies = tuple(
                        lifecycle_expected_dependencies[input_id]
                        for input_id in indexed_input_ids
                    )
                    current_matching_keys = tuple(
                        (
                            input_id,
                            _matching_keys_reader(dependency_index, input_id),
                        )
                        for input_id in indexed_input_ids
                    )
                    expected_matching_keys = tuple(
                        (
                            input_id,
                            lifecycle_expected_matching_keys[input_id],
                        )
                        for input_id in indexed_input_ids
                    )
                    if (
                        current_dependencies != expected_dependencies
                        or current_matching_keys != expected_matching_keys
                    ):
                        raise ContinuousSessionError(
                            "lifecycle changed dependency routing authority outside "
                            "coordinator callbacks"
                        )
                for input_id in registered:
                    if input_id not in indexed_input_ids:
                        raise ContinuousSessionError(
                            "lifecycle reported an input absent from dependency index"
                        )
                    if input_id not in newly_registered:
                        newly_registered.append(input_id)
                if registered != tuple(registration_callback_ids):
                    raise ContinuousSessionError(
                        "lifecycle registration receipt conflicts with coordinator callbacks"
                    )

                refresh_tick_dependency_routing_authority()
                require_lifecycle_dispatch_authority()
                resolutions = _settlement_resolutions_method(
                    self,
                    as_of=now,
                    lifecycle=lifecycle,
                    outcome_authority=outcome_authority,
                    records_reader=lifecycle_records,
                    resolve_outcome=outcome_resolver,
                )
                require_state_identity()
                require_dependency_index_identity()
                require_tick_dependency_routing_authority(
                    "dependency routing authority changed during settlement resolution"
                )
                _validate_settlement_evidence_method(
                    state,
                    settlement_evidence=resolutions,
                )
                require_economic_context()
                if prepare_settlement is not None:
                    prepare_settlement(
                        paper_book_path=paper_book_path,
                        resolutions=_detached_settlement_resolutions_method(
                            resolutions
                        ),
                        at=now,
                    )
                    require_state_identity()
                    require_dependency_index_identity()
                    require_tick_dependency_routing_authority(
                        "dependency routing authority changed during settlement preparation"
                    )
                    require_economic_context()
                settled, evidence_ids = self._settle(
                    resolutions=resolutions,
                    workspace=workspace,
                    paper_book_path=paper_book_path,
                    initial_bankroll=initial_bankroll,
                )
                require_state_identity()
                require_dependency_index_identity()
                require_tick_dependency_routing_authority(
                    "dependency routing authority changed during settlement application"
                )
                require_economic_context()
                if reconcile_after_settlement is not None:
                    reconcile_after_settlement(
                        paper_book_path=paper_book_path,
                        resolutions=_detached_settlement_resolutions_method(
                            resolutions
                        ),
                        settled_ticket_ids=settled,
                        at=now,
                    )
                    require_state_identity()
                    require_dependency_index_identity()
                    require_tick_dependency_routing_authority(
                        "dependency routing authority changed during settlement reconciliation"
                    )
                    require_economic_context()

                cycle_index = _record_success_method(
                    state,
                    at=now,
                    full_refresh=full_refresh,
                    settlement_evidence=resolutions,
                )
                committed_last_success_at = state._last_success_at
            except Exception as exc:
                # We are already inside a RUNNING durable fence here. A downstream
                # callback is not allowed to manufacture SessionPausedError or
                # SessionStoppedError as an operator-control bypass: genuine durable
                # PAUSED/STOPPED state is rejected by running_fence before this block
                # is entered. Treat every in-fence callback failure uniformly so
                # authority restoration and bounded failure publication still run.
                # Keep the coordinator pinned to the canonical state and dependency
                # routing objects even when a downstream callback replaces either
                # authority before failing.
                state_was_rebound = restore_state_identity()
                dependency_index_was_rebound = restore_dependency_index_identity()
                lifecycle_type_was_changed = restore_lifecycle_type_authority()
                invalidation_buffer_was_rebound = restore_invalidation_buffer_identity()
                invalidation_structure_was_rebound = (
                    restore_invalidation_buffer_structure_authority()
                )
                if lifecycle_type_was_changed:
                    try:
                        exc.add_note(
                            "canonical event lifecycle runtime type was changed "
                            "during tick effects and was restored"
                        )
                    except BaseException:
                        pass
                if invalidation_structure_was_rebound:
                    try:
                        exc.add_note(
                            "continuous-session invalidation buffer structure was changed "
                            "during tick effects and was restored"
                        )
                    except BaseException:
                        pass
                if invalidation_buffer_was_rebound:
                    try:
                        exc.add_note(
                            "continuous-session invalidation buffer authority was rebound "
                            "during tick effects and was restored"
                        )
                    except BaseException:
                        pass
                if dependency_index_was_rebound:
                    try:
                        exc.add_note(
                            "continuous-session dependency index authority was rebound "
                            "during tick effects and was restored"
                        )
                    except BaseException:
                        pass
                if state_was_rebound:
                    try:
                        exc.add_note(
                            "continuous session state authority was rebound during "
                            "tick effects and was restored"
                        )
                    except BaseException:
                        pass
                # The running fence is still held here, so no other canonical
                # generation can overtake this failure publication.
                try:
                    _record_failure_method(
                        state,
                        code=type(exc).__name__,
                    )
                except Exception as publication_error:
                    try:
                        exc.add_note(
                            "continuous session failure publication also failed: "
                            f"{type(publication_error).__name__}: {publication_error}"
                        )
                    except BaseException:
                        pass
                raise

        return ContinuousTickResult(
            session_id=state.session_id,
            cycle_index=cycle_index,
            source_id=cycle_source_id,
            source_provider_unavailable=False,
            source_gap_states=source_gap_states,
            source_sync_states=source_sync_states,
            committed_delta_ids=cycle_committed_delta_ids,
            delivered_delta_ids=delivered,
            affected_input_ids=affected,
            registered_input_ids=tuple(newly_registered),
            retired_input_ids=tuple(retired),
            full_refresh_required=full_refresh,
            invalidation_backlog=backlog,
            settled_ticket_ids=settled,
            settlement_evidence_ids=evidence_ids,
            last_success_at=committed_last_success_at or now,
        )

# Seal the consumer entry after class creation. The metaclass data descriptor also
# makes direct type.__setattr__/type.__delattr__ respect the same class-level fence.
_ContinuousSessionCoordinatorMeta._settle = _build_settlement_consumer_class_guard(
    "_settle"
)
ContinuousSessionCoordinator._settlement_consumer_bindings_sealed = True
