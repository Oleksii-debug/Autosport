from __future__ import annotations

import hashlib
import json
import ntpath
import os
import re
import stat
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from types import FunctionType
from typing import BinaryIO

from .monotonic_workspace_authority import (
    AuthorityPhase,
    MonotonicAuthorityRecoveryRequiredError,
    MonotonicAuthorityRollbackError,
    MonotonicWorkspaceAuthority,
)
from . import secret_redaction as _secret_redaction


_ALLOWED_HEALTH_STATUSES = frozenset({"unknown", "healthy", "degraded", "failed"})
_COUNTER_FIELDS = (
    "poll_count",
    "total_received",
    "total_accepted",
    "total_rejected",
    "total_failures",
    "consecutive_failures",
    "consecutive_failure_kind_count",
)
_FAILURE_KIND_PROVIDER_UNAVAILABLE = "provider_unavailable"
_FAILURE_KIND_PROVIDER_OR_VALIDATION = "provider_or_validation"
_ALLOWED_FAILURE_KINDS = frozenset(
    {
        _FAILURE_KIND_PROVIDER_UNAVAILABLE,
        _FAILURE_KIND_PROVIDER_OR_VALIDATION,
    }
)
_SCHEMA_V1 = 1
_SCHEMA_V2 = 2
_SCHEMA_V3 = 3
_SCHEMA_V4 = 4
_HISTORY_ENTRY_V2_FIELDS = frozenset({"recorded_at", "state"})
_HISTORY_ENTRY_V3_FIELDS = frozenset({"recorded_at", "transition_order", "state"})
_SOURCE_HEALTH_AUTHORITY_DOMAIN = "autosport.source-health-store.v1"
_DURABLE_FAILURE_FALLBACK = "BaseException: exception details unavailable"


def _build_durable_failure_renderer():
    secret_module = _secret_redaction
    canonical_renderer = secret_module.safe_exception_text
    canonical_globals = canonical_renderer.__globals__
    fallback = _DURABLE_FAILURE_FALLBACK

    function_witness = tuple(
        (name, value, value.__code__)
        for name, value in canonical_globals.items()
        if type(value) is FunctionType
        and getattr(value, "__module__", None) == secret_module.__name__
    )
    referenced_names = frozenset(
        name
        for _function_name, function, _code in function_witness
        for name in function.__code__.co_names
        if name in canonical_globals
    )
    binding_witness = tuple(
        (name, canonical_globals[name])
        for name in sorted(referenced_names)
    )
    mutable_binding_witness = tuple(
        (name, tuple(sorted(value.items())))
        for name, value in binding_witness
        if type(value) is dict
    )

    def authority_current() -> bool:
        if secret_module.safe_exception_text is not canonical_renderer:
            return False
        for name, function, code in function_witness:
            if canonical_globals.get(name) is not function:
                return False
            if function.__code__ is not code:
                return False
        for name, value in binding_witness:
            if canonical_globals.get(name) is not value:
                return False
        for name, expected_items in mutable_binding_witness:
            value = canonical_globals.get(name)
            if type(value) is not dict:
                return False
            if tuple(sorted(value.items())) != expected_items:
                return False
        return True

    def render(exc: BaseException) -> str:
        if not authority_current():
            return fallback
        try:
            rendered = canonical_renderer(exc)
        except BaseException:
            return fallback
        if not authority_current() or type(rendered) is not str or not rendered:
            return fallback
        return rendered

    return render


_DURABLE_FAILURE_RENDERER = _build_durable_failure_renderer()
del _build_durable_failure_renderer


def _build_record_failure_method(renderer):
    def record_failure(
        self,
        source_id: str,
        *,
        now: str,
        error: BaseException,
        failure_kind: str | None = None,
    ):
        if failure_kind is not None and (
            type(failure_kind) is not str
            or failure_kind not in _ALLOWED_FAILURE_KINDS
        ):
            raise ValueError("invalid source health failure kind")
        with self._writer_guard():
            self._recover_current_for_write()
            state = self.get(source_id)
            state.poll_count += 1
            state.total_failures += 1
            state.consecutive_failures += 1
            state.last_error_at = now
            state.last_error = renderer(error)
            if failure_kind is None:
                state.last_failure_kind = None
                state.consecutive_failure_kind_count = 0
            elif (
                state.status == "failed"
                and state.last_failure_kind == failure_kind
                and state.consecutive_failure_kind_count > 0
            ):
                state.last_failure_kind = failure_kind
                state.consecutive_failure_kind_count += 1
            else:
                state.last_failure_kind = failure_kind
                state.consecutive_failure_kind_count = 1
            state.quality_flags = ()
            state.status = "failed"
            self._put(state, recorded_at=now)
            return state

    return record_failure


def _source_health_authority_key(
    path: Path,
    *,
    windows: bool | None = None,
) -> str:
    use_windows_rules = os.name == "nt" if windows is None else windows
    return ntpath.normcase(path.name) if use_windows_rules else path.name


def parse_source_timestamp(value: str) -> datetime:
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError("provider source timestamp must be a non-empty trimmed string")
    # datetime.fromisoformat() silently discards non-zero precision beyond
    # microseconds. That is not acceptable for source-health or causal as-of
    # authority because D+submicrosecond evidence could otherwise appear at D.
    for match in re.finditer(r"[.,]([0-9]+)", value):
        fractional_digits = match.group(1)
        if len(fractional_digits) > 6 and any(
            digit != "0" for digit in fractional_digits[6:]
        ):
            raise ValueError(
                "provider source timestamp precision finer than microseconds is unsupported"
            )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid provider source timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise ValueError("provider source timestamps must include timezone")
    return parsed.astimezone(timezone.utc)


def _sync_parent_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sync_existing_file(path: Path) -> None:
    with path.open("rb+") as handle:
        handle.flush()
        os.fsync(handle.fileno())


def _validate_source_id(value: object) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError("source_id must be a non-empty trimmed string")
    return value


def _validate_nonnegative_count(name: str, value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _validate_quality_flags(value: object) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise ValueError("quality_flags must be an exact tuple of strings")
    seen: set[str] = set()
    for flag in value:
        if type(flag) is not str or not flag or flag.strip() != flag:
            raise ValueError(
                "quality_flags must contain exact non-empty trimmed strings"
            )
        if flag in seen:
            raise ValueError("quality_flags must not contain duplicates")
        seen.add(flag)
    return value


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key in source health store: {key}")
        result[key] = value
    return result


def _reject_nonfinite_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant in source health store: {value}")


@dataclass(frozen=True, slots=True)
class IngestionPolicy:
    max_batch_size: int = 5000
    stale_after_seconds: float = 120.0
    max_future_skew_seconds: float = 5.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_batch_size, bool)
            or not isinstance(self.max_batch_size, int)
            or self.max_batch_size <= 0
        ):
            raise ValueError("max_batch_size must be a positive integer")
        for field_name, value in (
            ("stale_after_seconds", self.stale_after_seconds),
            ("max_future_skew_seconds", self.max_future_skew_seconds),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(value)
                or value < 0
            ):
                raise ValueError(f"{field_name} must be a finite non-negative number")


@dataclass(slots=True)
class SourceHealthState:
    source_id: str
    status: str = "unknown"
    poll_count: int = 0
    total_received: int = 0
    total_accepted: int = 0
    total_rejected: int = 0
    total_failures: int = 0
    consecutive_failures: int = 0
    last_success_at: str | None = None
    last_error_at: str | None = None
    last_error: str | None = None
    last_cursor: str | None = None
    latest_source_ts: str | None = None
    quality_flags: tuple[str, ...] = field(default_factory=tuple)
    last_failure_kind: str | None = None
    consecutive_failure_kind_count: int = 0

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _validate_source_id(self.source_id)
        if type(self.status) is not str or self.status not in _ALLOWED_HEALTH_STATUSES:
            raise ValueError("invalid source health status")
        for field_name in _COUNTER_FIELDS:
            _validate_nonnegative_count(field_name, getattr(self, field_name))
        if self.total_failures > self.poll_count:
            raise ValueError("total_failures cannot exceed poll_count")
        if self.consecutive_failures > self.total_failures:
            raise ValueError("consecutive_failures cannot exceed total_failures")
        if self.consecutive_failure_kind_count > self.consecutive_failures:
            raise ValueError(
                "consecutive_failure_kind_count cannot exceed consecutive_failures"
            )
        if self.last_failure_kind is None:
            if self.consecutive_failure_kind_count != 0:
                raise ValueError(
                    "untyped source failure cannot retain typed consecutive count"
                )
        else:
            if (
                type(self.last_failure_kind) is not str
                or self.last_failure_kind not in _ALLOWED_FAILURE_KINDS
            ):
                raise ValueError("invalid source health failure kind")
            if self.status != "failed" or self.consecutive_failure_kind_count <= 0:
                raise ValueError(
                    "typed source failure requires failed status and positive typed count"
                )
        if self.total_accepted + self.total_rejected > self.total_received:
            raise ValueError("accepted and rejected source totals cannot exceed total_received")

        for field_name in ("last_success_at", "last_error_at", "latest_source_ts"):
            value = getattr(self, field_name)
            if value is not None:
                try:
                    parse_source_timestamp(value)
                except ValueError as exc:
                    raise ValueError(f"invalid {field_name} in source health state") from exc

        for field_name in ("last_error", "last_cursor"):
            value = getattr(self, field_name)
            if value is not None and type(value) is not str:
                raise ValueError(
                    f"{field_name} must be an exact string or null"
                )

        _validate_quality_flags(self.quality_flags)

        successful_polls = self.poll_count - self.total_failures
        if successful_polls == 0:
            if self.total_failures > 0 and self.consecutive_failures != self.total_failures:
                raise ValueError(
                    "source health without successful polls requires all failures to be consecutive"
                )
            if self.last_success_at is not None:
                raise ValueError("source health without successful polls cannot have last_success_at")
            if self.total_received or self.total_accepted or self.total_rejected:
                raise ValueError("source health without successful polls cannot have received event totals")
            if self.quality_flags:
                raise ValueError("source health without successful polls cannot have quality flags")
            if self.last_cursor is not None:
                raise ValueError("source health without successful polls cannot have last_cursor")
            if self.latest_source_ts is not None:
                raise ValueError("source health without successful polls cannot have latest_source_ts")
        elif self.last_success_at is None:
            raise ValueError("successful poll history requires last_success_at")

        if self.total_failures == 0:
            if self.last_error_at is not None:
                raise ValueError("source health without failures cannot have last_error_at")
        elif self.last_error_at is None:
            raise ValueError("failure history requires last_error_at")

        if self.status == "unknown":
            if (
                self.poll_count != 0
                or self.consecutive_failures != 0
                or self.last_error is not None
                or self.last_cursor is not None
                or self.latest_source_ts is not None
            ):
                raise ValueError("unknown source health must be pristine")
            return

        if self.status == "failed":
            if self.consecutive_failures == 0:
                raise ValueError("failed source health requires a positive consecutive failure count")
            if not isinstance(self.last_error, str) or not self.last_error.strip():
                raise ValueError("failed source health requires non-empty last error evidence")
        else:
            if self.consecutive_failures != 0:
                raise ValueError("successful source health cannot retain consecutive failures")
            if self.last_error is not None:
                raise ValueError("successful source health cannot retain last_error")
            if successful_polls == 0:
                raise ValueError("successful source health requires at least one successful poll")

        if self.status == "healthy" and self.quality_flags:
            raise ValueError("healthy source health cannot retain quality flags")
        if self.status == "degraded" and not self.quality_flags:
            raise ValueError("degraded source health requires quality flags")


_SOURCE_STATE_FIELDS = frozenset(item.name for item in fields(SourceHealthState))
_LEGACY_SOURCE_STATE_FIELDS = _SOURCE_STATE_FIELDS - {
    "last_failure_kind",
    "consecutive_failure_kind_count",
}


class _SourceHealthWriterLock:
    """Cross-process lock for one source-health JSON read/modify/write transaction."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: BinaryIO | None = None

    def __enter__(self) -> "_SourceHealthWriterLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            existing = os.lstat(self.path)
        except FileNotFoundError:
            existing = None
        except OSError as exc:
            raise RuntimeError(
                "source health writer-lock path is unavailable"
            ) from exc
        if existing is not None and (
            stat.S_ISLNK(existing.st_mode)
            or not stat.S_ISREG(existing.st_mode)
            or getattr(existing, "st_nlink", 1) != 1
        ):
            raise RuntimeError(
                "source health writer-lock path must be one regular non-symlink file"
            )
        flags = os.O_CREAT | os.O_RDWR
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise RuntimeError(
                "source health writer-lock path is unavailable"
            ) from exc
        handle = os.fdopen(fd, "a+b", closefd=True)
        try:
            info = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or getattr(info, "st_nlink", 1) != 1
            ):
                raise RuntimeError(
                    "source health writer-lock path must be one regular file"
                )
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            self._lock_handle(handle)
        except BaseException:
            handle.close()
            raise
        self._handle = handle
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            self._unlock_handle(handle)
        finally:
            handle.close()
            self._handle = None

    @staticmethod
    def _lock_handle(handle: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            return

        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

    @staticmethod
    def _unlock_handle(handle: BinaryIO) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return

        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class SourceHealthStore:
    """Durable provider-health projection plus causal append-only state history.

    Schema v4 extends the v3 causal transition history with a product-owned failure
    kind and same-kind consecutive suffix count in the exact same durable mutation as
    generic health counters. Legacy schema-v1/v2/v3 stores remain readable as untyped
    failure history and are upgraded on the first successful mutation; legacy error
    text is never guessed into typed provider authority.
    """

    def __init__(self, path: str | Path) -> None:
        requested_path = Path(path)
        requested_path.parent.mkdir(parents=True, exist_ok=True)
        self.path = requested_path.resolve(strict=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._path_authority = self.path
        self._lock_path = self.path.with_name(self.path.name + ".lock")
        self._lock_path_authority = self._lock_path
        self._temporary_path_authority = self.path.with_suffix(self.path.suffix + ".tmp")
        parent_info = os.stat(self.path.parent)
        self._parent_identity_authority = (parent_info.st_dev, parent_info.st_ino)
        with self._writer_guard():
            if not self.path.exists():
                self._write({"schema_version": _SCHEMA_V4, "sources": {}, "history": {}})
            else:
                _, observed = self._read_snapshot(verify_authority=False)
                self._recover_or_bootstrap_authority(observed)
            self._read()

    def _monotonic_authority(self) -> MonotonicWorkspaceAuthority:
        return MonotonicWorkspaceAuthority(
            workspace=self._path_authority.parent.resolve(strict=False),
            domain=_SOURCE_HEALTH_AUTHORITY_DOMAIN,
            key=_source_health_authority_key(self._path_authority),
        )

    @staticmethod
    def _sha256_bytes(value: bytes) -> str:
        return hashlib.sha256(value).hexdigest()

    def _current_state_sha256(self) -> str | None:
        if not self._path_authority.exists():
            return None
        self._assert_target_shape()
        return self._sha256_bytes(self._path_authority.read_bytes())

    def _authority_binding(
        self,
        observed: str | None,
        intended: str,
        *,
        kind: str,
    ) -> str:
        material = "\0".join(
            (
                _SOURCE_HEALTH_AUTHORITY_DOMAIN,
                kind,
                _source_health_authority_key(self._path_authority),
                observed or "<PRISTINE>",
                intended,
            )
        ).encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    def _bootstrap_validated_authority_state(
        self,
        authority: MonotonicWorkspaceAuthority,
        observed: str,
    ) -> None:
        self._assert_target_shape()
        _sync_existing_file(self._path_authority)
        _sync_parent_directory(self._path_authority.parent)
        if self._current_state_sha256() != observed:
            raise MonotonicAuthorityRollbackError(
                "source health state changed during authority bootstrap durability barrier"
            )
        binding = self._authority_binding(None, observed, kind="BOOTSTRAP")
        tx_id = self._next_authority_tx_id(
            authority,
            None,
            observed,
            binding,
        )
        authority.prepare(
            tx_id=tx_id,
            observed_state_sha256=None,
            intended_state_sha256=observed,
            semantic_binding_sha256=binding,
        )
        authority.commit(
            tx_id=tx_id,
            observed_state_sha256=observed,
            semantic_binding_sha256=binding,
        )

    def _recover_or_bootstrap_authority(
        self,
        observed: str | None,
    ) -> MonotonicWorkspaceAuthority:
        authority = self._monotonic_authority()
        history = authority.read_history()
        if not history:
            if observed is not None:
                self._bootstrap_validated_authority_state(authority, observed)
            return authority

        pending = history[-1] if history[-1].phase is AuthorityPhase.PREPARE else None
        if pending is not None:
            if observed == pending.intended_state_sha256:
                self._assert_target_shape()
                _sync_existing_file(self._path_authority)
                _sync_parent_directory(self._path_authority.parent)
                observed = self._current_state_sha256()
            authority.recover(
                observed_state_sha256=observed,
                tx_id=pending.tx_id,
                semantic_binding_sha256=pending.semantic_binding_sha256,
            )
            return authority

        has_committed_state = any(
            record.phase is AuthorityPhase.COMMIT for record in history
        )
        if not has_committed_state and observed is not None:
            self._bootstrap_validated_authority_state(authority, observed)
            return authority

        authority.recover(observed_state_sha256=observed)
        return authority

    def _recover_current_for_write(self) -> None:
        self._recover_or_bootstrap_authority(self._current_state_sha256())

    def _next_authority_tx_id(
        self,
        authority: MonotonicWorkspaceAuthority,
        observed: str | None,
        intended: str,
        semantic_binding_sha256: str,
    ) -> str:
        history = authority.read_history()
        authority_tip = history[-1].record_sha256 if history else "<PRISTINE>"
        material = "\0".join(
            (
                _source_health_authority_key(self._path_authority),
                authority_tip,
                observed or "<PRISTINE>",
                intended,
                semantic_binding_sha256,
            )
        ).encode("utf-8")
        return f"source-health-{hashlib.sha256(material).hexdigest()}"

    def _verify_authority_current(self, observed: str) -> None:
        authority = self._monotonic_authority()
        history = authority.read_history()
        if not history:
            raise MonotonicAuthorityRollbackError(
                "source health state is missing independent monotonic authority"
            )
        pending = history[-1] if history[-1].phase is AuthorityPhase.PREPARE else None
        if pending is not None:
            if observed == pending.previous_committed_state_sha256:
                return
            if observed == pending.intended_state_sha256:
                raise MonotonicAuthorityRecoveryRequiredError(
                    "source health publication requires monotonic commit recovery"
                )
            raise MonotonicAuthorityRollbackError(
                "source health state matches neither committed nor prepared authority"
            )
        authority.recover(observed_state_sha256=observed)

    def _assert_persistence_authority(self) -> None:
        if (
            self.path != self._path_authority
            or self._lock_path != self._lock_path_authority
            or self._temporary_path_authority
            != self._path_authority.with_suffix(self._path_authority.suffix + ".tmp")
        ):
            raise RuntimeError(
                "source health persistence authority changed after construction"
            )
        try:
            parent_info = os.stat(self._path_authority.parent)
        except OSError as exc:
            raise RuntimeError(
                "source health persistence directory authority is unavailable"
            ) from exc
        if (parent_info.st_dev, parent_info.st_ino) != self._parent_identity_authority:
            raise RuntimeError(
                "source health persistence directory authority changed after construction"
            )

    def _assert_target_shape(self) -> None:
        try:
            info = os.lstat(self._path_authority)
        except OSError as exc:
            raise RuntimeError(
                "source health persistence target is unavailable"
            ) from exc
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or getattr(info, "st_nlink", 1) != 1
        ):
            raise RuntimeError(
                "source health persistence target must be one regular non-symlink file"
            )

    def _sync_parent_directory(self) -> None:
        _sync_parent_directory(self._path_authority.parent)

    def assert_persistence_authority(self) -> None:
        self._assert_persistence_authority()
        if self._path_authority.exists() or self._path_authority.is_symlink():
            self._assert_target_shape()

    @staticmethod
    def _state_from_payload(payload: dict, *, normalize_failed_flags: bool = True) -> SourceHealthState:
        value = dict(payload)
        value.setdefault("last_failure_kind", None)
        value.setdefault("consecutive_failure_kind_count", 0)
        value["quality_flags"] = tuple(value["quality_flags"])
        if normalize_failed_flags and value.get("status") == "failed":
            # Old schema-v1 stores could retain the preceding successful batch's
            # quality flags on a later failed poll. Those flags are not current
            # failed-state evidence, so normalize them at the public boundary.
            value["quality_flags"] = ()
        return SourceHealthState(**value)

    @staticmethod
    def _payload(state: SourceHealthState) -> dict:
        payload = asdict(state)
        payload["quality_flags"] = list(state.quality_flags)
        return payload

    @staticmethod
    def _transition_at(state: SourceHealthState) -> str | None:
        if state.status == "failed":
            return state.last_error_at
        if state.status in {"healthy", "degraded"}:
            return state.last_success_at
        return None

    @staticmethod
    def _as_of(value: datetime) -> datetime:
        if type(value) is not datetime:
            raise TypeError("as_of must be an exact datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        return value.astimezone(timezone.utc)

    def get(self, source_id: str) -> SourceHealthState:
        _validate_source_id(source_id)
        raw = self._read()["sources"].get(source_id)
        if raw is None:
            return SourceHealthState(source_id=source_id)
        return self._state_from_payload(raw)

    def get_as_of(self, source_id: str, *, as_of: datetime) -> SourceHealthState:
        """Return health state known at ``as_of`` without consulting future transitions."""
        _validate_source_id(source_id)
        boundary = self._as_of(as_of)
        raw = self._read()

        if raw["schema_version"] == _SCHEMA_V1:
            payload = raw["sources"].get(source_id)
            if payload is None:
                return SourceHealthState(source_id=source_id)
            state = self._state_from_payload(payload)
            recorded_at = self._transition_at(state)
            if recorded_at is None or parse_source_timestamp(recorded_at) > boundary:
                return SourceHealthState(source_id=source_id)
            return state

        entries = raw["history"].get(source_id, ())
        selected: dict | None = None
        for entry in entries:
            if parse_source_timestamp(entry["recorded_at"]) <= boundary:
                selected = entry["state"]
            else:
                break
        if selected is None:
            return SourceHealthState(source_id=source_id)
        return self._state_from_payload(selected, normalize_failed_flags=False)

    @staticmethod
    def _validate_success_update(
        *,
        received: int,
        accepted: int,
        rejected: int,
        quality_flags: tuple[str, ...],
    ) -> None:
        _validate_nonnegative_count("received", received)
        _validate_nonnegative_count("accepted", accepted)
        _validate_nonnegative_count("rejected", rejected)
        if accepted + rejected > received:
            raise ValueError("accepted and rejected counts cannot exceed received")
        _validate_quality_flags(quality_flags)

    def _record_success_locked(
        self,
        state: SourceHealthState,
        *,
        now: str,
        received: int,
        accepted: int,
        rejected: int,
        cursor: str | None,
        latest_source_ts: str | None,
        quality_flags: tuple[str, ...],
    ) -> SourceHealthState:
        state.poll_count += 1
        state.total_received += received
        state.total_accepted += accepted
        state.total_rejected += rejected
        state.consecutive_failures = 0
        state.last_success_at = now
        state.last_error = None
        state.last_failure_kind = None
        state.consecutive_failure_kind_count = 0
        state.last_cursor = cursor
        effective_flags = set(quality_flags)
        if latest_source_ts is not None:
            if state.latest_source_ts is not None and (
                parse_source_timestamp(latest_source_ts)
                < parse_source_timestamp(state.latest_source_ts)
            ):
                effective_flags.add("SOURCE_TIME_REGRESSION")
            if state.latest_source_ts is None or (
                parse_source_timestamp(latest_source_ts)
                >= parse_source_timestamp(state.latest_source_ts)
            ):
                state.latest_source_ts = latest_source_ts
        state.quality_flags = tuple(sorted(effective_flags))
        state.status = "degraded" if state.quality_flags else "healthy"
        self._put(state, recorded_at=now)
        return state

    def record_success(
        self,
        source_id: str,
        *,
        now: str,
        received: int,
        accepted: int,
        rejected: int,
        cursor: str | None,
        latest_source_ts: str | None,
        quality_flags: tuple[str, ...],
    ) -> SourceHealthState:
        self._validate_success_update(
            received=received,
            accepted=accepted,
            rejected=rejected,
            quality_flags=quality_flags,
        )

        with self._writer_guard():
            self._recover_current_for_write()
            state = self.get(source_id)
            return self._record_success_locked(
                state,
                now=now,
                received=received,
                accepted=accepted,
                rejected=rejected,
                cursor=cursor,
                latest_source_ts=latest_source_ts,
                quality_flags=quality_flags,
            )

    def record_success_if_current(
        self,
        expected_before: SourceHealthState,
        *,
        ambiguous_after: SourceHealthState | None = None,
        now: str,
        received: int,
        accepted: int,
        rejected: int,
        cursor: str | None,
        latest_source_ts: str | None,
        quality_flags: tuple[str, ...],
    ) -> SourceHealthState:
        """Apply one success only if the durable state still equals expected_before."""
        if type(expected_before) is not SourceHealthState:
            raise TypeError("expected_before must be exact SourceHealthState")
        expected_before.validate()
        expected_snapshot = self._state_from_payload(
            self._payload(expected_before),
            normalize_failed_flags=False,
        )
        ambiguous_snapshot: SourceHealthState | None = None
        if ambiguous_after is not None:
            if type(ambiguous_after) is not SourceHealthState:
                raise TypeError(
                    "ambiguous_after must be exact SourceHealthState or null"
                )
            ambiguous_after.validate()
            ambiguous_snapshot = self._state_from_payload(
                self._payload(ambiguous_after),
                normalize_failed_flags=False,
            )
            if ambiguous_snapshot.source_id != expected_snapshot.source_id:
                raise ValueError("ambiguous_after source_id must match expected_before")
        self._validate_success_update(
            received=received,
            accepted=accepted,
            rejected=rejected,
            quality_flags=quality_flags,
        )

        with self._writer_guard():
            self._recover_current_for_write()
            current = self.get(expected_snapshot.source_id)
            if ambiguous_snapshot is not None and current == ambiguous_snapshot:
                raise RuntimeError(
                    "source health matches the expected post-state but this outcome "
                    "cannot prove it performed that durable mutation; refusing ambiguous retry"
                )
            if current != expected_snapshot:
                raise RuntimeError(
                    "source health changed since the committed ingestion outcome; "
                    "refusing ambiguous retry"
                )
            return self._record_success_locked(
                current,
                now=now,
                received=received,
                accepted=accepted,
                rejected=rejected,
                cursor=cursor,
                latest_source_ts=latest_source_ts,
                quality_flags=quality_flags,
            )

    record_failure = _build_record_failure_method(_DURABLE_FAILURE_RENDERER)

    def _writer_guard(self) -> _SourceHealthWriterLock:
        self._assert_persistence_authority()
        return _SourceHealthWriterLock(self._lock_path_authority)

    def _upgrade_to_v4(self, raw: dict) -> dict:
        if raw["schema_version"] == _SCHEMA_V4:
            return raw
        upgraded = {"schema_version": _SCHEMA_V4, "sources": {}, "history": {}}

        if raw["schema_version"] == _SCHEMA_V1:
            for source_id, payload in raw["sources"].items():
                state = self._state_from_payload(payload)
                normalized = self._payload(state)
                upgraded["sources"][source_id] = normalized
                recorded_at = self._transition_at(state)
                if recorded_at is None:
                    raise ValueError(
                        "persisted non-pristine source health requires transition timestamp"
                    )
                upgraded["history"][source_id] = [
                    {
                        "recorded_at": recorded_at,
                        "transition_order": 1,
                        "state": normalized,
                    }
                ]
            return upgraded

        for source_id, payload in raw["sources"].items():
            upgraded["sources"][source_id] = self._payload(
                self._state_from_payload(payload, normalize_failed_flags=False)
            )
            entries: list[dict] = []
            for index, entry in enumerate(raw["history"][source_id], start=1):
                transition_order = (
                    entry["transition_order"]
                    if raw["schema_version"] == _SCHEMA_V3
                    else index
                )
                entries.append(
                    {
                        "recorded_at": entry["recorded_at"],
                        "transition_order": transition_order,
                        "state": self._payload(
                            self._state_from_payload(
                                entry["state"], normalize_failed_flags=False
                            )
                        ),
                    }
                )
            upgraded["history"][source_id] = entries
        return upgraded

    def _put(self, state: SourceHealthState, *, recorded_at: str) -> None:
        state.validate()
        recorded = parse_source_timestamp(recorded_at)
        transition_at = self._transition_at(state)
        if transition_at is None or parse_source_timestamp(transition_at) != recorded:
            raise ValueError("source health transition timestamp mismatch")

        raw = self._upgrade_to_v4(self._read())
        entries = raw["history"].setdefault(state.source_id, [])
        if entries and parse_source_timestamp(entries[-1]["recorded_at"]) > recorded:
            raise ValueError("source health transitions cannot move backwards in evidence time")

        payload = self._payload(state)
        transition_order = entries[-1]["transition_order"] + 1 if entries else 1
        entries.append(
            {
                "recorded_at": recorded_at,
                "transition_order": transition_order,
                "state": payload,
            }
        )
        raw["sources"][state.source_id] = payload
        self._write(raw)

    @staticmethod
    def _validate_persisted_state(
        source_id: str,
        payload: object,
        *,
        schema_version: int,
    ) -> None:
        _validate_source_id(source_id)
        expected_state_fields = (
            _SOURCE_STATE_FIELDS
            if schema_version == _SCHEMA_V4
            else _LEGACY_SOURCE_STATE_FIELDS
        )
        if not isinstance(payload, dict) or set(payload) != expected_state_fields:
            raise ValueError("invalid source health state fields")
        if payload.get("source_id") != source_id:
            raise ValueError("source health state identity mismatch")
        if not isinstance(payload.get("quality_flags"), list):
            raise ValueError("persisted quality_flags must be a JSON array")
        value = dict(payload)
        value.setdefault("last_failure_kind", None)
        value.setdefault("consecutive_failure_kind_count", 0)
        value["quality_flags"] = tuple(value["quality_flags"])
        SourceHealthState(**value)

    def _read_snapshot(
        self,
        *,
        verify_authority: bool = True,
    ) -> tuple[dict, str]:
        self._assert_persistence_authority()
        self._assert_target_shape()
        try:
            raw_bytes = self._path_authority.read_bytes()
            raw = json.loads(
                raw_bytes.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_json_keys,
                parse_constant=_reject_nonfinite_json_constant,
            )
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("invalid source health store") from exc

        schema_version = raw.get("schema_version") if isinstance(raw, dict) else None
        if (
            not isinstance(raw, dict)
            or isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version not in {_SCHEMA_V1, _SCHEMA_V2, _SCHEMA_V3, _SCHEMA_V4}
            or not isinstance(raw.get("sources"), dict)
        ):
            raise ValueError("invalid source health store")

        expected_fields = (
            {"schema_version", "sources"}
            if schema_version == _SCHEMA_V1
            else {"schema_version", "sources", "history"}
        )
        if set(raw) != expected_fields:
            raise ValueError("invalid source health store")
        if schema_version in {_SCHEMA_V2, _SCHEMA_V3, _SCHEMA_V4} and not isinstance(
            raw.get("history"), dict
        ):
            raise ValueError("invalid source health store")

        try:
            for source_id, payload in raw["sources"].items():
                self._validate_persisted_state(
                    source_id,
                    payload,
                    schema_version=schema_version,
                )

            if schema_version in {_SCHEMA_V2, _SCHEMA_V3, _SCHEMA_V4}:
                if set(raw["history"]) != set(raw["sources"]):
                    raise ValueError("source health history/projection identity mismatch")
                for source_id, entries in raw["history"].items():
                    if not isinstance(entries, list) or not entries:
                        raise ValueError("source health history must be a non-empty array")
                    previous_recorded: datetime | None = None
                    previous_order = 0
                    for entry in entries:
                        expected_entry_fields = (
                            _HISTORY_ENTRY_V2_FIELDS
                            if schema_version == _SCHEMA_V2
                            else _HISTORY_ENTRY_V3_FIELDS
                        )
                        if not isinstance(entry, dict) or set(entry) != expected_entry_fields:
                            raise ValueError("invalid source health history entry")
                        recorded_at = parse_source_timestamp(entry["recorded_at"])
                        if schema_version == _SCHEMA_V2:
                            if previous_recorded is not None and recorded_at <= previous_recorded:
                                raise ValueError("source health history is not strictly increasing")
                        else:
                            order = entry["transition_order"]
                            if (
                                isinstance(order, bool)
                                or not isinstance(order, int)
                                or order != previous_order + 1
                            ):
                                raise ValueError("source health transition order is not contiguous")
                            if previous_recorded is not None and recorded_at < previous_recorded:
                                raise ValueError("source health history evidence time moved backwards")
                            previous_order = order
                        previous_recorded = recorded_at
                        self._validate_persisted_state(
                            source_id,
                            entry["state"],
                            schema_version=schema_version,
                        )
                        state = self._state_from_payload(
                            entry["state"], normalize_failed_flags=False
                        )
                        transition_at = self._transition_at(state)
                        if (
                            transition_at is None
                            or parse_source_timestamp(transition_at) != recorded_at
                        ):
                            raise ValueError("source health history timestamp mismatch")
                    if entries[-1]["state"] != raw["sources"][source_id]:
                        raise ValueError("source health latest projection/history mismatch")
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid source health state/history") from exc
        digest = self._sha256_bytes(raw_bytes)
        if verify_authority:
            self._verify_authority_current(digest)
        return raw, digest

    def _read(self, *, verify_authority: bool = True) -> dict:
        raw, _ = self._read_snapshot(verify_authority=verify_authority)
        return raw

    def _write(self, raw: dict) -> None:
        self._assert_persistence_authority()
        temporary = self._temporary_path_authority
        try:
            if temporary.exists() or temporary.is_symlink():
                raise RuntimeError(
                    "source health temporary persistence path already exists"
                )
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(temporary, flags, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n", closefd=True) as handle:
                info = os.fstat(handle.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or getattr(info, "st_nlink", 1) != 1
                ):
                    raise RuntimeError(
                        "source health temporary persistence path must be one regular file"
                    )
                json.dump(raw, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            intended = self._sha256_bytes(temporary.read_bytes())
            observed = self._current_state_sha256()
            authority = self._recover_or_bootstrap_authority(observed)
            binding = self._authority_binding(observed, intended, kind="PUBLISH")
            tx_id = self._next_authority_tx_id(
                authority,
                observed,
                intended,
                binding,
            )
            authority.prepare(
                tx_id=tx_id,
                observed_state_sha256=observed,
                intended_state_sha256=intended,
                semantic_binding_sha256=binding,
            )
            self._assert_persistence_authority()
            os.replace(temporary, self._path_authority)
            self._sync_parent_directory()
            published = self._current_state_sha256()
            if published != intended:
                raise RuntimeError(
                    "published source health bytes do not match prepared authority digest"
                )
            authority.commit(
                tx_id=tx_id,
                observed_state_sha256=intended,
                semantic_binding_sha256=binding,
            )
            self._assert_persistence_authority()
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


# record_failure retains the sealed renderer and fallback through lexical cells.
del _DURABLE_FAILURE_RENDERER
del _DURABLE_FAILURE_FALLBACK
del _build_record_failure_method
del _secret_redaction
