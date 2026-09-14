from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from typing import BinaryIO


_ALLOWED_HEALTH_STATUSES = frozenset({"unknown", "healthy", "degraded", "failed"})
_COUNTER_FIELDS = (
    "poll_count",
    "total_received",
    "total_accepted",
    "total_rejected",
    "total_failures",
    "consecutive_failures",
)


def parse_source_timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError("provider source timestamp must be a non-empty trimmed string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid provider source timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise ValueError("provider source timestamps must include timezone")
    return parsed.astimezone(timezone.utc)


def _validate_source_id(value: object) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError("source_id must be a non-empty trimmed string")
    return value


def _validate_nonnegative_count(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _validate_quality_flags(value: object) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise ValueError("quality_flags must be a tuple of strings")
    seen: set[str] = set()
    for flag in value:
        if not isinstance(flag, str) or not flag or flag.strip() != flag:
            raise ValueError("quality_flags must contain non-empty trimmed strings")
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
        if isinstance(self.max_batch_size, bool) or not isinstance(self.max_batch_size, int) or self.max_batch_size <= 0:
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

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _validate_source_id(self.source_id)
        if not isinstance(self.status, str) or self.status not in _ALLOWED_HEALTH_STATUSES:
            raise ValueError("invalid source health status")
        for field_name in _COUNTER_FIELDS:
            _validate_nonnegative_count(field_name, getattr(self, field_name))
        if self.total_failures > self.poll_count:
            raise ValueError("total_failures cannot exceed poll_count")
        if self.consecutive_failures > self.total_failures:
            raise ValueError("consecutive_failures cannot exceed total_failures")
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
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{field_name} must be a string or null")

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


class _SourceHealthWriterLock:
    """Cross-process lock for one source-health JSON read/modify/write transaction."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: BinaryIO | None = None

    def __enter__(self) -> "_SourceHealthWriterLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
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
    """Durable operational projection for provider health; never used as market history."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.path.with_name(self.path.name + ".lock")
        with self._writer_guard():
            if not self.path.exists():
                self._write({"schema_version": 1, "sources": {}})
            else:
                # Validate existing operational truth while writers are excluded so a
                # session cannot race initialization against another process mutation.
                self._read()

    def get(self, source_id: str) -> SourceHealthState:
        _validate_source_id(source_id)
        raw = self._read()["sources"].get(source_id)
        if raw is None:
            return SourceHealthState(source_id=source_id)
        value = dict(raw)
        value["quality_flags"] = tuple(value["quality_flags"])
        if value.get("status") == "failed":
            # Pre-fix stores may legitimately contain the preceding successful
            # batch's flags on a later failed poll. Preserve that schema-v1 state
            # as readable input, but never expose stale batch-scoped evidence as
            # the current failed-state truth.
            value["quality_flags"] = ()
        return SourceHealthState(**value)

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
        _validate_nonnegative_count("received", received)
        _validate_nonnegative_count("accepted", accepted)
        _validate_nonnegative_count("rejected", rejected)
        if accepted + rejected > received:
            raise ValueError("accepted and rejected counts cannot exceed received")
        _validate_quality_flags(quality_flags)

        with self._writer_guard():
            state = self.get(source_id)
            state.poll_count += 1
            state.total_received += received
            state.total_accepted += accepted
            state.total_rejected += rejected
            state.consecutive_failures = 0
            state.last_success_at = now
            state.last_error = None
            state.last_cursor = cursor
            if latest_source_ts is not None:
                if state.latest_source_ts is None or (
                    parse_source_timestamp(latest_source_ts)
                    >= parse_source_timestamp(state.latest_source_ts)
                ):
                    state.latest_source_ts = latest_source_ts
            state.quality_flags = tuple(sorted(quality_flags))
            state.status = "degraded" if state.quality_flags else "healthy"
            self._put(state)
            return state

    def record_failure(self, source_id: str, *, now: str, error: BaseException) -> SourceHealthState:
        with self._writer_guard():
            state = self.get(source_id)
            state.poll_count += 1
            state.total_failures += 1
            state.consecutive_failures += 1
            state.last_error_at = now
            state.last_error = f"{type(error).__name__}: {error}"
            state.quality_flags = ()
            state.status = "failed"
            self._put(state)
            return state

    def _writer_guard(self) -> _SourceHealthWriterLock:
        return _SourceHealthWriterLock(self._lock_path)

    def _put(self, state: SourceHealthState) -> None:
        state.validate()
        raw = self._read()
        payload = asdict(state)
        payload["quality_flags"] = list(state.quality_flags)
        raw["sources"][state.source_id] = payload
        self._write(raw)

    def _read(self) -> dict:
        try:
            raw = json.loads(
                self.path.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_json_keys,
                parse_constant=_reject_nonfinite_json_constant,
            )
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("invalid source health store") from exc

        schema_version = raw.get("schema_version") if isinstance(raw, dict) else None
        if (
            not isinstance(raw, dict)
            or set(raw) != {"schema_version", "sources"}
            or isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != 1
            or not isinstance(raw.get("sources"), dict)
        ):
            raise ValueError("invalid source health store")

        for source_id, payload in raw["sources"].items():
            try:
                _validate_source_id(source_id)
                if not isinstance(payload, dict) or set(payload) != _SOURCE_STATE_FIELDS:
                    raise ValueError("invalid source health state fields")
                if payload.get("source_id") != source_id:
                    raise ValueError("source health state identity mismatch")
                if not isinstance(payload.get("quality_flags"), list):
                    raise ValueError("persisted quality_flags must be a JSON array")
                value = dict(payload)
                value["quality_flags"] = tuple(value["quality_flags"])
                SourceHealthState(**value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid source health state for {source_id!r}") from exc
        return raw

    def _write(self, raw: dict) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(raw, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
