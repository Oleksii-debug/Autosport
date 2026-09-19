from __future__ import annotations

import argparse
import importlib
import json
import math
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol, Sequence

from .causal_collector import (
    CollectorDelta,
    CollectorDeltaStore,
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


class CollectorServiceError(RuntimeError):
    """Base error for the bounded headless collector runtime."""


class CollectorStorageLimitError(CollectorServiceError):
    """Raised when the configured durable collector storage budget is exhausted."""


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
    retry_attempts: int = 3
    initial_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 30.0
    jitter_fraction: float = 0.20
    max_store_bytes: int = 1_073_741_824

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_items, bool)
            or not isinstance(self.max_items, int)
            or self.max_items <= 0
        ):
            raise ValueError("max_items must be a positive integer")
        if (
            isinstance(self.retry_attempts, bool)
            or not isinstance(self.retry_attempts, int)
            or self.retry_attempts <= 0
        ):
            raise ValueError("retry_attempts must be a positive integer")
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
        ):
            raise ValueError("max_items must be a positive integer")
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
            raw["cycles_attempted"] = int(raw["cycles_attempted"]) + 1
            raw["last_cycle_at"] = at
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
        self.config = config or CollectorServiceConfig()
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
        self.sleep = sleep or time.sleep
        self.random_value = random_value or random.random
        self.stop_requested = stop_requested or (lambda: False)
        self._adapter = RemoteCollectorAdapter(self.delta_store.append)
        started_at = self.clock()
        _CollectorServiceState._instant(started_at, "started_at")
        self._state = _CollectorServiceState(
            state_path,
            run_id=run_id,
            source_id=source_id,
            started_at=started_at,
        )

    @property
    def source_id(self) -> str:
        return self.source.source_id

    def status(self) -> dict[str, object]:
        """Durable operator-readable state; provider messages/secrets are excluded."""
        return self._state.snapshot()

    def _bounded_provider_call(self, action: Callable[[], object]) -> object:
        delay = self.config.initial_backoff_seconds
        for attempt in range(self.config.retry_attempts):
            try:
                return action()
            except ProviderUnavailableError:
                if attempt + 1 >= self.config.retry_attempts:
                    raise
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
                self.sleep(min(self.config.max_backoff_seconds, jittered))
                delay = min(self.config.max_backoff_seconds, delay * 2)
        raise AssertionError("unreachable retry loop")

    def _check_storage_budget(self) -> None:
        try:
            size = self.delta_store.path.stat().st_size
        except FileNotFoundError:
            size = 0
        if size >= self.config.max_store_bytes:
            raise CollectorStorageLimitError(
                "collector durable store reached configured byte budget"
            )

    def run_cycle(self) -> CollectorCycleResult:
        attempt_at = self.clock()
        _CollectorServiceState._instant(attempt_at, "attempt_at")
        self._state.record_attempt(at=attempt_at)
        try:
            self._check_storage_budget()
            catalog_changes = self._bounded_provider_call(
                lambda: self.lifecycle.refresh_once(
                    self.source.fetch_catalog_page,
                    source_id=self.source_id,
                    discovered_at=self.clock(),
                )
            )
            if not isinstance(catalog_changes, tuple):
                raise TypeError("lifecycle refresh must return a tuple")
            records = tuple(
                item
                for item in self.lifecycle.records()
                if item.source_id == self.source_id
            )
            checkpoint = self.delta_store.stream_checkpoint(
                self.source_id, self.source.stream_epoch
            )
            raw_deltas = self._bounded_provider_call(
                lambda: self.source.fetch_deltas(
                    checkpoint,
                    records,
                    self.config.max_items,
                )
            )
            if not isinstance(raw_deltas, tuple):
                raise TypeError("source.fetch_deltas must return a tuple")
            if len(raw_deltas) > self.config.max_items:
                raise CollectorServiceError(
                    "source returned more deltas than the configured batch bound"
                )

            discovered_event_ids = {item.identity for item in records}
            committed: list[str] = []
            duplicates: list[str] = []
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
                if delta.stream_epoch != self.source.stream_epoch:
                    raise CollectorServiceError(
                        "collector source returned a delta for another stream_epoch"
                    )
                if delta.event_id not in discovered_event_ids:
                    raise CollectorServiceError(
                        "collector delta event is absent from durable event lifecycle"
                    )
                if self._adapter.submit_committed_delta(delta):
                    committed.append(delta.delta_id)
                else:
                    duplicates.append(delta.delta_id)
                self._check_storage_budget()

            completed_at = self.clock()
            _CollectorServiceState._instant(completed_at, "completed_at")
            self._state.record_success(
                at=completed_at,
                committed=len(committed),
                duplicates=len(duplicates),
            )
            return CollectorCycleResult(
                source_id=self.source_id,
                catalog_changes=tuple(catalog_changes),
                committed_delta_ids=tuple(committed),
                duplicate_delta_ids=tuple(duplicates),
            )
        except ProviderUnavailableError as exc:
            self._state.record_provider_failure(code=type(exc).__name__)
            return CollectorCycleResult(
                source_id=self.source_id,
                catalog_changes=(),
                committed_delta_ids=(),
                duplicate_delta_ids=(),
                provider_unavailable=True,
            )
        except BaseException as exc:
            self._state.record_local_failure(code=type(exc).__name__)
            raise

    def stop(self, reason: str = "operator_stop") -> None:
        self._state.stop(at=self.clock(), reason=reason)

    def run(self, *, max_cycles: int | None = None) -> tuple[CollectorCycleResult, ...]:
        if max_cycles is not None and (
            isinstance(max_cycles, bool)
            or not isinstance(max_cycles, int)
            or max_cycles <= 0
        ):
            raise ValueError("max_cycles must be a positive integer or None")
        results: list[CollectorCycleResult] = []
        while max_cycles is None or len(results) < max_cycles:
            if self.stop_requested():
                self.stop("stop_requested")
                break
            results.append(self.run_cycle())
            if max_cycles is not None and len(results) >= max_cycles:
                self.stop("max_cycles_reached")
                break
            if self.stop_requested():
                self.stop("stop_requested")
                break
            self.sleep(self.config.poll_interval_seconds)
        return tuple(results)


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
    parser.add_argument("--max-cycles", type=int)
    parser.add_argument("--max-items", type=int, default=250)
    parser.add_argument("--poll-seconds", type=float, default=30.0)
    parser.add_argument("--retry-attempts", type=int, default=3)
    parser.add_argument("--initial-backoff-seconds", type=float, default=1.0)
    parser.add_argument("--max-backoff-seconds", type=float, default=30.0)
    parser.add_argument("--jitter-fraction", type=float, default=0.20)
    parser.add_argument("--max-store-bytes", type=int, default=1_073_741_824)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = Path(args.workspace)
    root.mkdir(parents=True, exist_ok=True)
    factory = _load_source_factory(args.source_factory)
    source = factory()
    service = HeadlessCollectorService(
        delta_store=CollectorDeltaStore(root / "collector_deltas.json"),
        lifecycle=ContinuousEventLifecycle(root / "collector_catalog.json"),
        source=source,
        state_path=root / "collector_service_state.json",
        run_id=args.run_id,
        config=CollectorServiceConfig(
            max_items=args.max_items,
            poll_interval_seconds=args.poll_seconds,
            retry_attempts=args.retry_attempts,
            initial_backoff_seconds=args.initial_backoff_seconds,
            max_backoff_seconds=args.max_backoff_seconds,
            jitter_fraction=args.jitter_fraction,
            max_store_bytes=args.max_store_bytes,
        ),
    )
    try:
        service.run(max_cycles=args.max_cycles)
    except KeyboardInterrupt:
        service.stop("operator_interrupt")
        print(json.dumps(service.status(), ensure_ascii=False, sort_keys=True))
        return 130
    print(json.dumps(service.status(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
