from __future__ import annotations

import argparse
import math
import os
import signal
import sqlite3
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from .ingestion import CommittedIngestionHealthError
from .ingestion_health import IngestionPolicy, SourceHealthStore
from .integrity import atomic_write_json
from .json_integrity import strict_json_loads
from .live_observation import poll_open_market_store_once
from .market_mirror import MarketMirror
from .market_mirror_runtime import BoundedMirrorInvalidationBuffer
from .parlayapi_provider import ParlayApiTableTennisProvider, ProviderPayloadError
from .providers import MarketProvider, ProviderUnavailableError
from .storage import SQLiteMarketStore


MonotonicClock = Callable[[], float]
WallClock = Callable[[], str]
Waiter = Callable[[float], bool]
Reporter = Callable[[str], None]
ProviderFactory = Callable[..., MarketProvider]

_STATUS_SCHEMA_VERSION = 1
_STATUS_KIND = "autosport_continuous_local_observation"
_STATUS_LIFECYCLE_STATES = frozenset(
    {"starting", "running", "attempting", "provider_unavailable", "failed", "stopped"}
)
# The durable status is a small control envelope, not a provider payload. Bound the
# restart read before UTF-8/JSON decoding so a corrupted or replaced artifact cannot
# turn startup classification into an unbounded memory allocation.
_STATUS_MAX_BYTES = 1024 * 1024
_MAX_CYCLES = 100_000
_MAX_RUNTIME_SECONDS = 7 * 24 * 60 * 60
_MAX_INTERVAL_SECONDS = 60 * 60
_MAX_BACKOFF_SECONDS = 6 * 60 * 60
_MAX_ITEMS = 5_000


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _positive_int(value: object, *, field: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{field} must be an integer between 1 and {maximum}")
    return value


def _positive_finite(value: object, *, field: str, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite positive number")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric <= 0 or numeric > maximum:
        raise ValueError(f"{field} must be > 0 and <= {maximum:g}")
    return numeric


@dataclass(frozen=True, slots=True)
class ContinuousObservationConfig:
    workspace: Path
    max_cycles: int = 60
    max_runtime_seconds: float = 3600.0
    interval_seconds: float = 60.0
    max_backoff_seconds: float = 300.0
    max_items: int = 250
    status_path: Path | None = None

    def __post_init__(self) -> None:
        workspace = Path(self.workspace)
        status_path = Path(self.status_path) if self.status_path is not None else None
        object.__setattr__(self, "workspace", workspace)
        object.__setattr__(self, "status_path", status_path)
        _positive_int(self.max_cycles, field="max_cycles", maximum=_MAX_CYCLES)
        _positive_finite(
            self.max_runtime_seconds,
            field="max_runtime_seconds",
            maximum=_MAX_RUNTIME_SECONDS,
        )
        _positive_finite(
            self.interval_seconds,
            field="interval_seconds",
            maximum=_MAX_INTERVAL_SECONDS,
        )
        _positive_finite(
            self.max_backoff_seconds,
            field="max_backoff_seconds",
            maximum=_MAX_BACKOFF_SECONDS,
        )
        _positive_int(self.max_items, field="max_items", maximum=_MAX_ITEMS)

    @property
    def resolved_status_path(self) -> Path:
        return self.status_path or self.workspace / "continuous_observation_status.json"


@dataclass(frozen=True, slots=True)
class ContinuousObservationResult:
    run_id: str
    stop_reason: str
    attempted_cycles: int
    successful_cycles: int
    total_received: int
    total_accepted: int
    total_rejected: int
    last_error_kind: str | None
    last_error: str | None
    exit_code: int


@dataclass(slots=True)
class _LoopState:
    run_id: str
    source_id: str
    started_at: str
    attempted_cycles: int = 0
    successful_cycles: int = 0
    total_received: int = 0
    total_accepted: int = 0
    total_rejected: int = 0
    last_cursor: str | None = None
    health_status: str = "unknown"
    last_error_kind: str | None = None
    last_error: str | None = None
    provider_unavailable_streak: int = 0


def _redacted_error(exc: BaseException, secrets: Sequence[str]) -> str:
    message = f"{type(exc).__name__}: {exc}"
    for secret in secrets:
        if isinstance(secret, str) and secret:
            message = message.replace(secret, "[REDACTED]")
    return message


def _read_previous_status(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        with path.open("rb") as handle:
            payload = handle.read(_STATUS_MAX_BYTES + 1)
        if len(payload) > _STATUS_MAX_BYTES:
            raise ValueError("continuous observation status exceeds size limit")
        text = payload.decode("utf-8")
        raw = strict_json_loads(text)
    except (OSError, UnicodeError) as exc:
        raise ValueError("continuous observation status is unreadable or invalid JSON") from exc
    except ValueError as exc:
        if str(exc) == "continuous observation status exceeds size limit":
            raise
        raise ValueError("continuous observation status is unreadable or invalid JSON") from exc
    if (
        type(raw) is not dict
        or type(raw.get("schema_version")) is not int
        or raw["schema_version"] != _STATUS_SCHEMA_VERSION
    ):
        raise ValueError("continuous observation status has unsupported schema")
    if raw.get("kind") != _STATUS_KIND:
        raise ValueError("continuous observation status has unsupported kind")
    run_id = raw.get("run_id")
    state = raw.get("state")
    if not isinstance(run_id, str) or not run_id or not isinstance(state, str) or not state:
        raise ValueError("continuous observation status is missing run identity/state")
    if state not in _STATUS_LIFECYCLE_STATES:
        raise ValueError("continuous observation status has unsupported lifecycle state")
    return raw


def _status_payload(
    state: _LoopState,
    *,
    lifecycle_state: str,
    updated_at: str,
    stop_reason: str | None,
    previous_run_id: str | None,
    previous_state: str | None,
    previous_unclean_shutdown: bool,
    mirror_full_refresh_required: bool = False,
) -> dict[str, object]:
    return {
        "schema_version": _STATUS_SCHEMA_VERSION,
        "kind": "autosport_continuous_local_observation",
        "run_id": state.run_id,
        "source_id": state.source_id,
        "state": lifecycle_state,
        "started_at": state.started_at,
        "updated_at": updated_at,
        "attempted_cycles": state.attempted_cycles,
        "successful_cycles": state.successful_cycles,
        "total_received": state.total_received,
        "total_accepted": state.total_accepted,
        "total_rejected": state.total_rejected,
        "last_cursor": state.last_cursor,
        "health_status": state.health_status,
        "provider_unavailable_streak": state.provider_unavailable_streak,
        "last_error_kind": state.last_error_kind,
        "last_error": state.last_error,
        "stop_reason": stop_reason,
        "previous_run_id": previous_run_id,
        "previous_state": previous_state,
        "previous_unclean_shutdown": previous_unclean_shutdown,
        "mirror_full_refresh_required": mirror_full_refresh_required,
        "market_universe_complete": False,
        "off_pc_24x7_proven": False,
        "real_money_execution": False,
    }


def _format_status(payload: dict[str, object]) -> str:
    parts = [
        f"observation_loop={payload['state']}",
        f"run_id={payload['run_id']}",
        f"source={payload['source_id']}",
        f"attempted={payload['attempted_cycles']}",
        f"successful={payload['successful_cycles']}",
        f"health={payload['health_status']}",
        f"received={payload['total_received']}",
        f"accepted={payload['total_accepted']}",
    ]
    if payload.get("last_cursor") is not None:
        parts.append(f"cursor={payload['last_cursor']}")
    if payload.get("last_error_kind") is not None:
        parts.append(f"error_kind={payload['last_error_kind']}")
    if payload.get("stop_reason") is not None:
        parts.append(f"stop_reason={payload['stop_reason']}")
    parts.extend(("market_universe_complete=false", "real_money_execution=false"))
    return " ".join(parts)


def _publish_status(
    path: Path,
    payload: dict[str, object],
    *,
    reporter: Reporter | None,
) -> None:
    atomic_write_json(path, payload)
    if reporter is not None:
        try:
            reporter(_format_status(payload))
        except (BrokenPipeError, OSError):
            # Console output is observability only. Durable status and market/source
            # stores remain authoritative and must not be rolled back by a closed pipe.
            pass


def _drain_invalidation_projection(buffer: BoundedMirrorInvalidationBuffer) -> bool:
    """Bound non-authoritative invalidation memory after each collector cycle."""
    full_refresh_seen = False
    while True:
        batch = buffer.drain(max_items=buffer.max_dirty_keys)
        full_refresh_seen = full_refresh_seen or batch.full_refresh_required
        if not batch.has_more:
            return full_refresh_seen


def _safe_health_status(store: SourceHealthStore, source_id: str, fallback: str) -> str:
    try:
        return store.get(source_id).status
    except Exception:
        return fallback


def _has_health_persistence_failure_note(exc: BaseException) -> bool:
    return any(
        isinstance(note, str) and note.startswith("source health failure persistence also failed:")
        for note in getattr(exc, "__notes__", ())
    )


def run_continuous_observation(
    provider: MarketProvider,
    config: ContinuousObservationConfig,
    *,
    stop_event: threading.Event | None = None,
    policy: IngestionPolicy | None = None,
    ingestion_clock: Callable[[], str] | None = None,
    monotonic: MonotonicClock = time.monotonic,
    wall_clock: WallClock = _utc_now_iso,
    waiter: Waiter | None = None,
    reporter: Reporter | None = print,
    redact_values: Sequence[str] = (),
    run_id: str | None = None,
) -> ContinuousObservationResult:
    """Run bounded repeated local observation without adding a second market authority.

    Every acquisition flows through 'poll_open_market_store_once' and its existing
    persist-first market/source-health semantics. Provider unavailability may retry
    only after bounded backoff. Local durability or ambiguous committed-health errors
    stop the run immediately rather than replaying an uncertain durable boundary.
    """
    if not hasattr(provider, "read_batch") or not isinstance(getattr(provider, "source_id", None), str):
        raise TypeError("provider must satisfy MarketProvider")
    if not provider.source_id or provider.source_id != provider.source_id.strip():
        raise ValueError("provider source_id must be non-empty and trimmed")

    root = config.workspace
    root.mkdir(parents=True, exist_ok=True)
    status_path = config.resolved_status_path
    previous = _read_previous_status(status_path)
    previous_run_id = previous.get("run_id") if previous else None
    previous_state = previous.get("state") if previous else None
    previous_unclean = previous_state in {"starting", "running", "attempting", "provider_unavailable"}

    current_run_id = run_id or uuid.uuid4().hex
    if not isinstance(current_run_id, str) or not current_run_id or current_run_id != current_run_id.strip():
        raise ValueError("run_id must be a non-empty trimmed string")
    started_at = wall_clock()
    state = _LoopState(current_run_id, provider.source_id, started_at)
    stopper = stop_event or threading.Event()
    wait = waiter or stopper.wait

    def publish(lifecycle_state: str, *, stop_reason: str | None = None, full_refresh: bool = False) -> None:
        payload = _status_payload(
            state,
            lifecycle_state=lifecycle_state,
            updated_at=wall_clock(),
            stop_reason=stop_reason,
            previous_run_id=previous_run_id if isinstance(previous_run_id, str) else None,
            previous_state=previous_state if isinstance(previous_state, str) else None,
            previous_unclean_shutdown=previous_unclean,
            mirror_full_refresh_required=full_refresh,
        )
        _publish_status(status_path, payload, reporter=reporter)

    publish("starting")
    started_monotonic = monotonic()
    terminal_reason = "max_cycles"
    terminal_exit = 0

    store: SQLiteMarketStore | None = None
    try:
        store = SQLiteMarketStore(root / "market.db")
        health_store = SourceHealthStore(root / "source_health.json")
        mirror = MarketMirror.from_store(store)
        mirror_updates = BoundedMirrorInvalidationBuffer(mirror)
        publish("running")

        while True:
            elapsed = monotonic() - started_monotonic
            if stopper.is_set():
                terminal_reason = "operator_stop"
                break
            if state.attempted_cycles >= config.max_cycles:
                terminal_reason = "max_cycles"
                break
            if elapsed >= config.max_runtime_seconds:
                terminal_reason = "max_runtime"
                break

            state.attempted_cycles += 1
            publish("attempting")
            full_refresh_seen = False
            try:
                stats = poll_open_market_store_once(
                    store,
                    health_store,
                    provider,
                    mirror_updates=mirror_updates,
                    max_items=config.max_items,
                    policy=policy,
                    clock=ingestion_clock,
                )
            except ProviderUnavailableError as exc:
                full_refresh_seen = _drain_invalidation_projection(mirror_updates)
                state.provider_unavailable_streak += 1
                state.last_error_kind = "provider_unavailable"
                state.last_error = _redacted_error(exc, redact_values)
                state.health_status = _safe_health_status(health_store, provider.source_id, "unknown")
                publish("provider_unavailable", full_refresh=full_refresh_seen)
                if state.attempted_cycles >= config.max_cycles:
                    terminal_reason = "max_cycles_after_provider_unavailable"
                    terminal_exit = 4
                    break
                delay = min(
                    config.max_backoff_seconds,
                    config.interval_seconds * (2 ** min(state.provider_unavailable_streak - 1, 20)),
                )
                if wait(delay):
                    terminal_reason = "operator_stop"
                    break
            except (OSError, sqlite3.Error, ValueError, CommittedIngestionHealthError) as exc:
                full_refresh_seen = _drain_invalidation_projection(mirror_updates)
                state.last_error_kind = "local_durable_failure"
                state.last_error = _redacted_error(exc, redact_values)
                state.health_status = _safe_health_status(health_store, provider.source_id, "unknown")
                terminal_reason = "local_durable_failure"
                terminal_exit = 5
                publish("failed", stop_reason=terminal_reason, full_refresh=full_refresh_seen)
                break
            else:
                full_refresh_seen = _drain_invalidation_projection(mirror_updates)
                state.provider_unavailable_streak = 0
                state.successful_cycles += 1
                state.total_received += stats.received
                state.total_accepted += stats.accepted
                state.total_rejected += stats.rejected
                state.last_cursor = stats.cursor
                state.health_status = stats.health_status
                state.last_error_kind = None
                state.last_error = None

                publish("running", full_refresh=full_refresh_seen)
                if state.attempted_cycles >= config.max_cycles:
                    terminal_reason = "max_cycles"
                    break
                if wait(config.interval_seconds):
                    terminal_reason = "operator_stop"
                    break
    finally:
        primary_failure = sys.exc_info()[1]
        if store is not None:
            try:
                store.close()
            except BaseException as close_exc:
                if primary_failure is not None:
                    try:
                        primary_failure.add_note(f"market store close also failed: {close_exc}")
                    except Exception:
                        pass
                else:
                    raise

    publish("stopped", stop_reason=terminal_reason)
    return ContinuousObservationResult(
        run_id=state.run_id,
        stop_reason=terminal_reason,
        attempted_cycles=state.attempted_cycles,
        successful_cycles=state.successful_cycles,
        total_received=state.total_received,
        total_accepted=state.total_accepted,
        total_rejected=state.total_rejected,
        last_error_kind=state.last_error_kind,
        last_error=state.last_error,
        exit_code=terminal_exit,
    )