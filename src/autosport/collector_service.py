from __future__ import annotations

import argparse
import importlib
import json
import math
import random
import signal
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol, Sequence

from .causal_collector import (
    CollectorDelta,
    CollectorDeltaStore,
    CollectorStorageBackpressureError,
    RemoteCollectorAdapter,
    StreamCheckpoint,
)
from .event_lifecycle import (
    CatalogCheckpoint,
    CatalogPage,
    ContinuousEventLifecycle,
    EventLifecycleRecord,
)
from .integrity import atomic_write_json
from .json_integrity import strict_json_loads
from .providers import ProviderUnavailableError


_MAX_DELTA_PAGE_ITEMS = 5000
_MAX_PROVIDER_RETRY_ATTEMPTS = 10
_DEFAULT_MAX_STORE_BYTES = 1_073_741_824


class CollectorServiceError(RuntimeError):
    """Base error for the bounded headless collector runtime."""


class CollectorStorageLimitError(CollectorServiceError):
    """Raised when the configured durable collector storage budget is exhausted."""


class CollectorRetentionRequiredError(CollectorStorageLimitError):
    """Recoverable backpressure requiring safe retention before more intake."""

    code = "RETENTION_REQUIRED"


class CollectorServiceStoppedError(CollectorServiceError):
    """Raised when a durably stopped run is used without explicit resume."""


class _StopRequested(RuntimeError):
    """Internal control-flow signal for a requested bounded stop."""


class _SignalStopRequest:
    """Signal handler target that performs no I/O and defers STOP to safe code."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._signal_number: int | None = None

    def __call__(self) -> bool:
        return self._event.is_set()

    def handle(self, signum: int, _frame: object) -> None:
        self._signal_number = signum
        self._event.set()

    def reason(self) -> str:
        if self._signal_number is None:
            return "stop_requested"
        try:
            name = signal.Signals(self._signal_number).name
        except ValueError:
            name = str(self._signal_number)
        return f"signal:{name}"

    @property
    def exit_code(self) -> int | None:
        if self._signal_number is None:
            return None
        return 128 + int(self._signal_number)


class CollectorServiceSource(Protocol):
    """Provider-specific acquisition port; canonical Autosport authorities stay external."""

    source_id: str
    stream_epoch: str

    def fetch_catalog_page(
        self, checkpoint: CatalogCheckpoint | None
    ) -> CatalogPage: ...

    def fetch_deltas(
        self,
        checkpoint: StreamCheckpoint | None,
        records: tuple[EventLifecycleRecord, ...],
        max_items: int,
    ) -> tuple[CollectorDelta, ...]: ...


@dataclass(frozen=True, slots=True)
class CollectorServiceConfig:
    max_items: int = 250
    poll_interval_seconds: float = 30.0
    evaluation_slot_count: int | None = None
    retry_attempts: int = 3
    initial_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 30.0
    jitter_fraction: float = 0.20
    max_store_bytes: int = _DEFAULT_MAX_STORE_BYTES

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_items, bool)
            or not isinstance(self.max_items, int)
            or self.max_items <= 0
            or self.max_items > _MAX_DELTA_PAGE_ITEMS
        ):
            raise ValueError(
                f"max_items must be in 1..{_MAX_DELTA_PAGE_ITEMS}"
            )
        if self.evaluation_slot_count is not None and (
            isinstance(self.evaluation_slot_count, bool)
            or not isinstance(self.evaluation_slot_count, int)
            or self.evaluation_slot_count <= 0
        ):
            raise ValueError(
                "evaluation_slot_count must be a positive integer or None"
            )
        if (
            isinstance(self.retry_attempts, bool)
            or not isinstance(self.retry_attempts, int)
            or self.retry_attempts <= 0
            or self.retry_attempts > _MAX_PROVIDER_RETRY_ATTEMPTS
        ):
            raise ValueError(
                f"retry_attempts must be in 1..{_MAX_PROVIDER_RETRY_ATTEMPTS}"
            )
        if (
            isinstance(self.max_store_bytes, bool)
            or not isinstance(self.max_store_bytes, int)
            or self.max_store_bytes <= 0
        ):
            raise ValueError("max_store_bytes must be a positive integer")
        for name, value, allow_zero in (
            ("poll_interval_seconds", self.poll_interval_seconds, False),
            ("initial_backoff_seconds", self.initial_backoff_seconds, False),
            ("max_backoff_seconds", self.max_backoff_seconds, False),
            ("jitter_fraction", self.jitter_fraction, True),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
                or (not allow_zero and value == 0)
            ):
                qualifier = "non-negative" if allow_zero else "positive"
                raise ValueError(f"{name} must be a finite {qualifier} number")
        if self.max_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError(
                "max_backoff_seconds cannot be below initial_backoff_seconds"
            )
        if self.jitter_fraction > 1:
            raise ValueError("jitter_fraction cannot exceed 1")


@dataclass(frozen=True, slots=True)
class CollectorCycleResult:
    source_id: str
    catalog_changes: tuple[str, ...]
    committed_delta_ids: tuple[str, ...]
    duplicate_delta_ids: tuple[str, ...]
    provider_unavailable: bool = False


@dataclass(frozen=True, slots=True)
class CollectorRunResult:
    cycles_executed: int
    last_cycle: CollectorCycleResult | None


@dataclass(frozen=True, slots=True)
class DeltaFeedPage:
    source_id: str
    deltas: tuple[CollectorDelta, ...]
    next_after_delta_id: str | None
    has_more: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "deltas": [item.to_dict() for item in self.deltas],
            "next_after_delta_id": self.next_after_delta_id,
            "has_more": self.has_more,
        }


class ReadOnlyCollectorDeltaFeed:
    """Bounded append-order feed for missing-only desktop synchronization.

    The transport cursor is an immutable delta_id in durable commit order. It is
    separate from provider cursor position, so a late correction at an older source
    position is still delivered after the desktop's last transport token.
    """

    def __init__(self, store: CollectorDeltaStore, *, source_id: str) -> None:
        if not isinstance(store, CollectorDeltaStore):
            raise TypeError("store must be CollectorDeltaStore")
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("source_id must be a non-empty string")
        self.store = store
        self.source_id = source_id

    def read_page(
        self,
        *,
        after_delta_id: str | None = None,
        max_items: int = 250,
    ) -> DeltaFeedPage:
        if (
            isinstance(max_items, bool)
            or not isinstance(max_items, int)
            or max_items <= 0
            or max_items > _MAX_DELTA_PAGE_ITEMS
        ):
            raise ValueError(
                f"max_items must be in 1..{_MAX_DELTA_PAGE_ITEMS}"
            )
        candidates = self.store.deltas_after_commit(
            source_id=self.source_id,
            after_delta_id=after_delta_id,
            max_items=max_items + 1,
        )
        has_more = len(candidates) > max_items
        selected = candidates[:max_items]
        next_token = selected[-1].delta_id if selected else after_delta_id
        return DeltaFeedPage(
            source_id=self.source_id,
            deltas=selected,
            next_after_delta_id=next_token,
            has_more=has_more,
        )


class _CollectorServiceState:
    _SCHEMA = "autosport.headless_collector_service"
    _VERSION = 1

    def __init__(
        self,
        path: str | Path,
        *,
        run_id: str,
        source_id: str,
        started_at: str,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = self._text(run_id, "run_id")
        self.source_id = self._text(source_id, "source_id")
        self._instant(started_at, "started_at")
        if not self.path.exists():
            atomic_write_json(
                self.path,
                {
                    "schema": self._SCHEMA,
                    "schema_version": self._VERSION,
                    "run_id": self.run_id,
                    "source_id": self.source_id,
                    "started_at": started_at,
                    "cycles_attempted": 0,
                    "cycles_succeeded": 0,
                    "deltas_committed": 0,
                    "duplicate_deltas": 0,
                    "provider_failures": 0,
                    "last_cycle_at": None,
                    "last_success_at": None,
                    "last_error_code": None,
                    "stopped_at": None,
                    "stop_reason": None,
                },
            )
        self._read()

    @staticmethod
    def _text(value: object, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
        return value

    @staticmethod
    def _instant(value: object, name: str) -> datetime:
        raw = _CollectorServiceState._text(value, name)
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{name} must be valid ISO-8601") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")
        return parsed

    def _read(self) -> dict[str, object]:
        try:
            raw = strict_json_loads(self.path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            raise CollectorServiceError(
                "cannot verify collector service state"
            ) from exc
        expected = {
            "schema",
            "schema_version",
            "run_id",
            "source_id",
            "started_at",
            "cycles_attempted",
            "cycles_succeeded",
            "deltas_committed",
            "duplicate_deltas",
            "provider_failures",
            "last_cycle_at",
            "last_success_at",
            "last_error_code",
            "stopped_at",
            "stop_reason",
        }
        if (
            not isinstance(raw, dict)
            or set(raw) != expected
            or raw.get("schema") != self._SCHEMA
            or raw.get("schema_version") != self._VERSION
            or raw.get("run_id") != self.run_id
            or raw.get("source_id") != self.source_id
        ):
            raise CollectorServiceError(
                "collector service state identity/schema mismatch"
            )
        self._instant(raw["started_at"], "started_at")
        for name in (
            "cycles_attempted",
            "cycles_succeeded",
            "deltas_committed",
            "duplicate_deltas",
            "provider_failures",
        ):
            value = raw[name]
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            ):
                raise CollectorServiceError(
                    f"collector service state has invalid {name}"
                )
        if raw["cycles_succeeded"] > raw["cycles_attempted"]:
            raise CollectorServiceError(
                "collector service successful cycles exceed attempts"
            )
        for name in ("last_cycle_at", "last_success_at", "stopped_at"):
            if raw[name] is not None:
                self._instant(raw[name], name)
        for name in ("last_error_code", "stop_reason"):
            if raw[name] is not None and (
                not isinstance(raw[name], str) or not raw[name].strip()
            ):
                raise CollectorServiceError(
                    f"collector service state has invalid {name}"
                )
        if (raw["stopped_at"] is None) != (raw["stop_reason"] is None):
            raise CollectorServiceError(
                "collector service STOP state is incomplete"
            )
        return raw

    def snapshot(self) -> dict[str, object]:
        return dict(self._read())

    def _update(self, mutate: Callable[[dict[str, object]], None]) -> None:
        raw = self._read()
        mutate(raw)
        atomic_write_json(self.path, raw)
        self._read()

    def record_attempt(self, *, at: str) -> None:
        self._instant(at, "at")

        def mutate(raw: dict[str, object]) -> None:
            if raw["stopped_at"] is not None:
                raise CollectorServiceStoppedError(
                    "collector run is durably STOPPED; explicit resume is required"
                )
            raw["cycles_attempted"] = int(raw["cycles_attempted"]) + 1
            raw["last_cycle_at"] = at

        self._update(mutate)

    def resume(self, *, at: str) -> None:
        """Explicitly authorize a previously durably stopped run to continue."""
        self._instant(at, "at")

        def mutate(raw: dict[str, object]) -> None:
            if raw["stopped_at"] is None:
                return
            raw["stopped_at"] = None
            raw["stop_reason"] = None

        self._update(mutate)

    def record_success(
        self,
        *,
        at: str,
        committed: int,
        duplicates: int,
    ) -> None:
        self._instant(at, "at")
        if committed < 0 or duplicates < 0:
            raise ValueError("delta counts must be non-negative")

        def mutate(raw: dict[str, object]) -> None:
            raw["cycles_succeeded"] = int(raw["cycles_succeeded"]) + 1
            raw["deltas_committed"] = int(raw["deltas_committed"]) + committed
            raw["duplicate_deltas"] = int(raw["duplicate_deltas"]) + duplicates
            raw["last_success_at"] = at
            raw["last_error_code"] = None

        self._update(mutate)

    def record_provider_failure(self, *, code: str) -> None:
        code = self._text(code, "code")

        def mutate(raw: dict[str, object]) -> None:
            raw["provider_failures"] = int(raw["provider_failures"]) + 1
            raw["last_error_code"] = code

        self._update(mutate)

    def record_local_failure(self, *, code: str) -> None:
        code = self._text(code, "code")

        def mutate(raw: dict[str, object]) -> None:
            raw["last_error_code"] = code

        self._update(mutate)

    def stop(self, *, at: str, reason: str) -> None:
        self._instant(at, "at")
        reason = self._text(reason, "reason")

        def mutate(raw: dict[str, object]) -> None:
            raw["stopped_at"] = at
            raw["stop_reason"] = reason

        self._update(mutate)


class HeadlessCollectorService:
    """Restart-safe collector process without a second market/scheduler authority."""

    def __init__(
        self,
        *,
        delta_store: CollectorDeltaStore,
        lifecycle: ContinuousEventLifecycle,
        source: CollectorServiceSource,
        state_path: str | Path,
        run_id: str,
        config: CollectorServiceConfig | None = None,
        clock: Callable[[], str] | None = None,
        sleep: Callable[[float], None] | None = None,
        random_value: Callable[[], float] | None = None,
        stop_requested: Callable[[], bool] | None = None,
        stop_reason: Callable[[], str] | None = None,
        wait_for_stop: Callable[[float], bool] | None = None,
    ) -> None:
        if not isinstance(delta_store, CollectorDeltaStore):
            raise TypeError("delta_store must be CollectorDeltaStore")
        if not isinstance(lifecycle, ContinuousEventLifecycle):
            raise TypeError("lifecycle must be ContinuousEventLifecycle")
        source_id = getattr(source, "source_id", None)
        stream_epoch = getattr(source, "stream_epoch", None)
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("source.source_id must be a non-empty string")
        if not isinstance(stream_epoch, str) or not stream_epoch.strip():
            raise ValueError("source.stream_epoch must be a non-empty string")
        if not callable(getattr(source, "fetch_catalog_page", None)):
            raise TypeError("source.fetch_catalog_page must be callable")
        if not callable(getattr(source, "fetch_deltas", None)):
            raise TypeError("source.fetch_deltas must be callable")
        self.delta_store = delta_store
        self.lifecycle = lifecycle
        self.source = source
        self._source_identity = source
        self._source_id = source_id
        self.config = config or CollectorServiceConfig()
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
        self.sleep = sleep or time.sleep
        self.random_value = random_value or random.random
        self.stop_requested = stop_requested or (lambda: False)
        self.stop_reason = stop_reason or (lambda: "stop_requested")
        if wait_for_stop is not None and not callable(wait_for_stop):
            raise TypeError("wait_for_stop must be callable or None")
        self.wait_for_stop = wait_for_stop
        self._adapter = RemoteCollectorAdapter(self._append_admitted_delta)
        started_at = self.clock()
        _CollectorServiceState._instant(started_at, "started_at")
        self._state = _CollectorServiceState(
            state_path,
            run_id=run_id,
            source_id=source_id,
            started_at=started_at,
        )
        try:
            self.delta_store._bootstrap_or_recover_runtime_stream_epoch(
                source_id=self._source_id,
                stream_epoch=stream_epoch,
                activated_at=started_at,
            )
        except (TypeError, ValueError) as exc:
            raise CollectorServiceError(
                "cannot establish collector active-epoch authority"
            ) from exc

    def _require_source_identity(
        self,
        *,
        expected_stream_epoch: str | None = None,
    ) -> CollectorServiceSource:
        """Return the configured source only while its product identity is intact."""

        source = self._source_identity
        if self.source is not source:
            raise CollectorServiceError(
                "collector source instance cannot be replaced during this service run"
            )
        source_id = getattr(source, "source_id", None)
        stream_epoch = getattr(source, "stream_epoch", None)
        if source_id != self._source_id:
            raise CollectorServiceError(
                "source.source_id changed after collector service construction"
            )
        if not isinstance(stream_epoch, str) or not stream_epoch.strip():
            raise CollectorServiceError(
                "source.stream_epoch must remain a non-empty string"
            )
        if (
            expected_stream_epoch is not None
            and stream_epoch != expected_stream_epoch
        ):
            raise CollectorServiceError(
                "source.stream_epoch changed during active collector cycle"
            )
        return source

    def _append_admitted_delta(self, delta: CollectorDelta) -> bool:
        """Commit a validated provider delta and its epoch authority atomically."""

        if not isinstance(delta, CollectorDelta):
            raise TypeError("delta must be CollectorDelta")
        delta.validate()
        self._require_source_identity(
            expected_stream_epoch=delta.stream_epoch
        )
        if delta.source_id != self._source_id:
            raise CollectorServiceError(
                "collector source returned a delta for another source_id"
            )
        activated_at = self.clock()
        _CollectorServiceState._instant(activated_at, "activated_at")
        try:
            return self.delta_store._append_with_runtime_stream_epoch(
                delta,
                activated_at=activated_at,
            )
        except CollectorStorageBackpressureError as exc:
            raise CollectorRetentionRequiredError(
                "RETENTION_REQUIRED: collector native SQLite allocation ceiling was reached; "
                "run explicit pin-aware compaction, then retry"
            ) from exc

    @property
    def source_id(self) -> str:
        return self._source_id

    def status(self) -> dict[str, object]:
        """Durable operator-readable state; provider messages/secrets are excluded."""
        return self._state.snapshot()

    def resume(self) -> None:
        """Explicitly resume the same durable run after an operator STOP."""
        self._state.resume(at=self.clock())

    def _requested_stop_reason(self) -> str | None:
        if not self.stop_requested():
            return None
        reason = self.stop_reason()
        if not isinstance(reason, str) or not reason.strip():
            raise CollectorServiceError(
                "stop_reason must return a non-empty string when STOP is requested"
            )
        return reason

    def _stop_if_requested(self) -> None:
        reason = self._requested_stop_reason()
        if reason is None:
            return
        self.stop(reason)
        raise _StopRequested(reason)

    def _wait_or_stop(self, seconds: float) -> None:
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not math.isfinite(float(seconds))
            or seconds < 0
        ):
            raise CollectorServiceError(
                "wait duration must be a finite non-negative number"
            )
        self._stop_if_requested()
        duration = float(seconds)
        if self.wait_for_stop is None:
            self.sleep(duration)
        else:
            interrupted = self.wait_for_stop(duration)
            if not isinstance(interrupted, bool):
                raise CollectorServiceError("wait_for_stop must return bool")
            if interrupted and not self.stop_requested():
                raise CollectorServiceError(
                    "wait_for_stop reported STOP without stop_requested authority"
                )
        self._stop_if_requested()

    def _bounded_provider_call(self, action: Callable[[], object]) -> object:
        delay = self.config.initial_backoff_seconds
        for attempt in range(self.config.retry_attempts):
            self._stop_if_requested()
            try:
                return action()
            except ProviderUnavailableError:
                if attempt + 1 >= self.config.retry_attempts:
                    raise
                self._stop_if_requested()
                random_value = self.random_value()
                if (
                    isinstance(random_value, bool)
                    or not isinstance(random_value, (int, float))
                    or not math.isfinite(random_value)
                    or not 0 <= random_value <= 1
                ):
                    raise CollectorServiceError(
                        "random_value must return a finite number in [0, 1]"
                    )
                jittered = delay * (
                    1 + self.config.jitter_fraction * float(random_value)
                )
                self._wait_or_stop(
                    min(self.config.max_backoff_seconds, jittered)
                )
                delay = min(self.config.max_backoff_seconds, delay * 2)
        raise AssertionError("unreachable retry loop")

    def _check_storage_budget(self) -> None:
        try:
            size = self.delta_store.path.stat().st_size
        except FileNotFoundError:
            size = 0
        if size >= self.config.max_store_bytes:
            raise CollectorRetentionRequiredError(
                "RETENTION_REQUIRED: collector durable store reached configured byte budget; "
                "run explicit pin-aware compaction or enlarge the budget, then retry"
            )

    def run_cycle(
        self,
        *,
        _schedule_slot: dict[str, object] | None = None,
    ) -> CollectorCycleResult:
        attempt_at = self.clock()
        _CollectorServiceState._instant(attempt_at, "attempt_at")
        self._state.record_attempt(at=attempt_at)

        # Reserve immutable source-observation evidence immediately before the first
        # provider-facing operation. A crash after this point leaves an explicit
        # pending cycle rather than silently shrinking a future evidence denominator.
        cycle_source = self._require_source_identity()
        cycle_stream_epoch = cycle_source.stream_epoch
        if _schedule_slot is None:
            cycle_seq = self.delta_store._begin_collector_cycle(
                source_id=self.source_id,
                run_id=self._state.run_id,
                stream_epoch=cycle_stream_epoch,
                attempted_at=attempt_at,
            )
        else:
            if not isinstance(_schedule_slot, dict):
                raise CollectorServiceError(
                    "scheduled collector cycle requires canonical slot evidence"
                )
            frozen_max_items = _schedule_slot.get("max_items")
            if frozen_max_items != self.config.max_items:
                raise CollectorServiceError(
                    "scheduled collector cycle max_items does not match frozen schedule"
                )
            try:
                cycle_seq = self.delta_store._begin_scheduled_collector_cycle(
                    source_id=self.source_id,
                    run_id=self._state.run_id,
                    stream_epoch=cycle_stream_epoch,
                    max_items=self.config.max_items,
                    slot_ordinal=_schedule_slot.get("slot_ordinal"),
                    due_at=_schedule_slot.get("due_at"),
                    attempted_at=attempt_at,
                )
            except (TypeError, ValueError) as exc:
                raise CollectorServiceError(
                    "cannot bind scheduled collector cycle to canonical due slot"
                ) from exc
        catalog_changes: tuple[str, ...] = ()
        observed: list[str] = []
        committed: list[str] = []
        duplicates: list[str] = []

        def finish_cycle(status: str, *, error_code: str | None = None) -> str:
            completed_at = self.clock()
            _CollectorServiceState._instant(completed_at, "completed_at")
            self.delta_store._finish_collector_cycle(
                source_id=self.source_id,
                cycle_seq=cycle_seq,
                status=status,
                completed_at=completed_at,
                catalog_changes=catalog_changes,
                observed_delta_ids=tuple(observed),
                committed_delta_ids=tuple(committed),
                duplicate_delta_ids=tuple(duplicates),
                error_code=error_code,
            )
            return completed_at

        try:
            self._check_storage_budget()
            refreshed = self._bounded_provider_call(
                lambda: self.lifecycle.refresh_once(
                    cycle_source.fetch_catalog_page,
                    source_id=self.source_id,
                    discovered_at=self.clock(),
                )
            )
            self._require_source_identity(
                expected_stream_epoch=cycle_stream_epoch
            )
            if not isinstance(refreshed, tuple):
                raise TypeError("lifecycle refresh must return a tuple")
            catalog_changes = tuple(refreshed)
            records = tuple(
                item
                for item in self.lifecycle.records()
                if item.source_id == self.source_id
            )
            checkpoint = self.delta_store.stream_checkpoint(
                self.source_id, cycle_stream_epoch
            )
            raw_deltas = self._bounded_provider_call(
                lambda: cycle_source.fetch_deltas(
                    checkpoint,
                    records,
                    self.config.max_items,
                )
            )
            self._require_source_identity(
                expected_stream_epoch=cycle_stream_epoch
            )
            if not isinstance(raw_deltas, tuple):
                raise TypeError("source.fetch_deltas must return a tuple")
            if len(raw_deltas) > self.config.max_items:
                raise CollectorServiceError(
                    "source returned more deltas than the configured batch bound"
                )

            discovered_event_ids = {item.identity for item in records}
            for delta in raw_deltas:
                if not isinstance(delta, CollectorDelta):
                    raise TypeError(
                        "source.fetch_deltas must return CollectorDelta values"
                    )
                delta.validate()
                if delta.source_id != self.source_id:
                    raise CollectorServiceError(
                        "collector source returned a delta for another source_id"
                    )
                if delta.stream_epoch != cycle_stream_epoch:
                    raise CollectorServiceError(
                        "collector source returned a delta for another stream_epoch"
                    )
                if delta.event_id not in discovered_event_ids:
                    raise CollectorServiceError(
                        "collector delta event is absent from durable event lifecycle"
                    )
                observed.append(delta.delta_id)
                if self._adapter.submit_committed_delta(delta):
                    committed.append(delta.delta_id)
                else:
                    duplicates.append(delta.delta_id)
                self._check_storage_budget()

            self._require_source_identity(
                expected_stream_epoch=cycle_stream_epoch
            )
            completed_at = finish_cycle("SUCCESS")
            self._state.record_success(
                at=completed_at,
                committed=len(committed),
                duplicates=len(duplicates),
            )
            return CollectorCycleResult(
                source_id=self.source_id,
                catalog_changes=catalog_changes,
                committed_delta_ids=tuple(committed),
                duplicate_delta_ids=tuple(duplicates),
            )
        except _StopRequested as exc:
            try:
                finish_cycle("STOP_REQUESTED", error_code="STOP_REQUESTED")
            except BaseException as evidence_error:
                try:
                    exc.add_note(
                        "collector cycle STOP evidence also failed: "
                        f"{type(evidence_error).__name__}: {evidence_error}"
                    )
                except BaseException:
                    pass
            raise
        except ProviderUnavailableError as exc:
            finish_cycle(
                "PROVIDER_UNAVAILABLE",
                error_code=type(exc).__name__,
            )
            self._state.record_provider_failure(code=type(exc).__name__)
            return CollectorCycleResult(
                source_id=self.source_id,
                catalog_changes=(),
                committed_delta_ids=(),
                duplicate_delta_ids=(),
                provider_unavailable=True,
            )
        except BaseException as exc:
            try:
                finish_cycle("LOCAL_FAILURE", error_code=type(exc).__name__)
            except BaseException as evidence_error:
                try:
                    exc.add_note(
                        "collector cycle failure evidence also failed: "
                        f"{type(evidence_error).__name__}: {evidence_error}"
                    )
                except BaseException:
                    pass
            try:
                self._state.record_local_failure(code=type(exc).__name__)
            except BaseException as state_error:
                try:
                    exc.add_note(
                        "collector service failure projection also failed: "
                        f"{type(state_error).__name__}: {state_error}"
                    )
                except BaseException:
                    pass
            raise

    def stop(self, reason: str = "operator_stop") -> None:
        self._state.stop(at=self.clock(), reason=reason)

    def _ensure_prospective_schedule(self) -> None:
        """Create or re-resolve the one durable prospective schedule for this run."""

        schedule_anchor = self.clock()
        _CollectorServiceState._instant(schedule_anchor, "schedule_anchor")
        schedule_source = self._require_source_identity()
        schedule_stream_epoch = schedule_source.stream_epoch
        try:
            self.delta_store._ensure_collector_schedule(
                source_id=self.source_id,
                run_id=self._state.run_id,
                stream_epoch=schedule_stream_epoch,
                anchor_at=schedule_anchor,
                interval_seconds=self.config.poll_interval_seconds,
                max_items=self.config.max_items,
                evaluation_start_slot_ordinal=(
                    0 if self.config.evaluation_slot_count is not None else None
                ),
                evaluation_end_slot_ordinal=(
                    self.config.evaluation_slot_count - 1
                    if self.config.evaluation_slot_count is not None
                    else None
                ),
            )
        except (TypeError, ValueError) as exc:
            raise CollectorServiceError(
                "cannot establish prospective collector schedule authority"
            ) from exc

    def _wait_for_next_scheduled_slot(self) -> dict[str, object]:
        """Resolve the next canonical due slot and wait until it is due."""

        reason = self._requested_stop_reason()
        if reason is not None:
            self.stop(reason)
            raise _StopRequested(reason)
        try:
            schedule_slot = self.delta_store._next_collector_schedule_slot(
                source_id=self.source_id,
                run_id=self._state.run_id,
            )
        except (TypeError, ValueError) as exc:
            raise CollectorServiceError(
                "cannot resolve next canonical collector due slot"
            ) from exc

        due_at = _CollectorServiceState._instant(
            schedule_slot.get("due_at"),
            "due_at",
        )
        now = _CollectorServiceState._instant(self.clock(), "clock")
        while now < due_at:
            self._wait_or_stop((due_at - now).total_seconds())
            now = _CollectorServiceState._instant(self.clock(), "clock")
        return schedule_slot

    def _run_scheduled_cycle(self) -> CollectorCycleResult:
        self._ensure_prospective_schedule()
        schedule_slot = self._wait_for_next_scheduled_slot()
        return self.run_cycle(_schedule_slot=schedule_slot)

    def run_scheduled_cycle(self) -> CollectorCycleResult:
        """Run one provider-facing cycle bound to the frozen prospective schedule."""

        try:
            return self._run_scheduled_cycle()
        except _StopRequested as exc:
            raise CollectorServiceStoppedError(
                "collector STOP requested before scheduled cycle completed"
            ) from exc

    def run(self, *, max_cycles: int | None = None) -> CollectorRunResult:
        """Run against one frozen prospective cadence without shifting missed slots."""
        if max_cycles is not None and (
            isinstance(max_cycles, bool)
            or not isinstance(max_cycles, int)
            or max_cycles <= 0
        ):
            raise ValueError("max_cycles must be a positive integer or None")

        cycles_executed = 0
        last_cycle: CollectorCycleResult | None = None
        while max_cycles is None or cycles_executed < max_cycles:
            try:
                last_cycle = self._run_scheduled_cycle()
            except _StopRequested:
                break
            cycles_executed += 1
            if max_cycles is not None and cycles_executed >= max_cycles:
                self.stop("max_cycles_reached")
                break
            reason = self._requested_stop_reason()
            if reason is not None:
                self.stop(reason)
                break
        return CollectorRunResult(
            cycles_executed=cycles_executed,
            last_cycle=last_cycle,
        )


def _load_source_factory(spec: str) -> Callable[[], CollectorServiceSource]:
    if ":" not in spec:
        raise ValueError("source factory must use module:function syntax")
    module_name, function_name = spec.split(":", 1)
    if not module_name or not function_name:
        raise ValueError("source factory must use module:function syntax")
    module = importlib.import_module(module_name)
    factory = getattr(module, function_name, None)
    if not callable(factory):
        raise TypeError("source factory target must be callable")
    return factory


def _open_collector_store_with_effective_budget(
    path: Path,
    requested_max_bytes: int | None,
) -> tuple[CollectorDeltaStore, int]:
    """Open the canonical store and resolve one durable operator byte budget.

    CLI omission is assertion-free: an existing durable budget is adopted. A fresh
    or previously unbound workspace establishes the product default. Explicit
    values remain assertions and the canonical store rejects any durable conflict.
    """

    requested = requested_max_bytes
    if requested is None and not path.exists():
        requested = _DEFAULT_MAX_STORE_BYTES

    store = CollectorDeltaStore(path, max_bytes=requested)
    effective = store.configured_max_bytes
    if effective is None:
        # Existing unbound stores adopt the product default through the same
        # canonical durable authority. Never duplicate or bypass its conflict fence.
        store = CollectorDeltaStore(path, max_bytes=_DEFAULT_MAX_STORE_BYTES)
        effective = store.configured_max_bytes
    if effective is None:
        raise CollectorServiceError(
            "collector durable store did not resolve a max_store_bytes authority"
        )
    return store, effective


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m autosport.collector_service",
        description=(
            "Run the bounded headless causal collector. Provider credentials remain "
            "external to Autosport and are never accepted as CLI arguments."
        ),
    )
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--source-factory", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--resume-stopped-run",
        action="store_true",
        help="Explicitly resume this durable run_id after a persisted STOP.",
    )
    parser.add_argument("--max-cycles", type=int)
    parser.add_argument(
        "--evaluation-slots",
        type=int,
        help=(
            "Prospectively freeze the finite scientific evaluation window for "
            "this durable run. This is separate from per-invocation --max-cycles."
        ),
    )
    parser.add_argument("--max-items", type=int, default=250)
    parser.add_argument("--poll-seconds", type=float, default=30.0)
    parser.add_argument("--retry-attempts", type=int, default=3)
    parser.add_argument("--initial-backoff-seconds", type=float, default=1.0)
    parser.add_argument("--max-backoff-seconds", type=float, default=30.0)
    parser.add_argument("--jitter-fraction", type=float, default=0.20)
    parser.add_argument("--max-store-bytes", type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = Path(args.workspace)
    root.mkdir(parents=True, exist_ok=True)
    signal_stop = _SignalStopRequest()
    previous_handlers = {
        signum: signal.getsignal(signum)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    for signum in previous_handlers:
        signal.signal(signum, signal_stop.handle)

    try:
        factory = _load_source_factory(args.source_factory)
        source = factory()
        delta_store, effective_max_store_bytes = (
            _open_collector_store_with_effective_budget(
                root / "collector_deltas.json",
                args.max_store_bytes,
            )
        )
        service = HeadlessCollectorService(
            delta_store=delta_store,
            lifecycle=ContinuousEventLifecycle(root / "collector_catalog.json"),
            source=source,
            state_path=root / "collector_service_state.json",
            run_id=args.run_id,
            config=CollectorServiceConfig(
                max_items=args.max_items,
                poll_interval_seconds=args.poll_seconds,
                evaluation_slot_count=args.evaluation_slots,
                retry_attempts=args.retry_attempts,
                initial_backoff_seconds=args.initial_backoff_seconds,
                max_backoff_seconds=args.max_backoff_seconds,
                jitter_fraction=args.jitter_fraction,
                max_store_bytes=effective_max_store_bytes,
            ),
            stop_requested=signal_stop,
            stop_reason=signal_stop.reason,
        )
        if args.resume_stopped_run:
            service.resume()
        try:
            service.run(max_cycles=args.max_cycles)
        except CollectorServiceStoppedError:
            print(json.dumps(service.status(), ensure_ascii=False, sort_keys=True))
            return 3
        except CollectorRetentionRequiredError:
            print(json.dumps(service.status(), ensure_ascii=False, sort_keys=True))
            return 4
        print(json.dumps(service.status(), ensure_ascii=False, sort_keys=True))
        return signal_stop.exit_code or 0
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
