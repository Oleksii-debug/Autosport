from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from typing import BinaryIO


def parse_source_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid provider source timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise ValueError("provider source timestamps must include timezone")
    return parsed.astimezone(timezone.utc)


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

    def get(self, source_id: str) -> SourceHealthState:
        raw = self._read()["sources"].get(source_id)
        if raw is None:
            return SourceHealthState(source_id=source_id)
        value = dict(raw)
        value["quality_flags"] = tuple(value.get("quality_flags", ()))
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
            state.quality_flags = tuple(sorted(set(quality_flags)))
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
            state.status = "failed"
            self._put(state)
            return state

    def _writer_guard(self) -> _SourceHealthWriterLock:
        return _SourceHealthWriterLock(self._lock_path)

    def _put(self, state: SourceHealthState) -> None:
        raw = self._read()
        payload = asdict(state)
        payload["quality_flags"] = list(state.quality_flags)
        raw["sources"][state.source_id] = payload
        self._write(raw)

    def _read(self) -> dict:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if raw.get("schema_version") != 1 or not isinstance(raw.get("sources"), dict):
            raise ValueError("invalid source health store")
        return raw

    def _write(self, raw: dict) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(raw, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
