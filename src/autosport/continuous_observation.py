from __future__ import annotations

import argparse
import json
import math
import os
import signal
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from .ingestion import CommittedIngestionHealthError
from .ingestion_health import IngestionPolicy, SourceHealthStore, parse_source_timestamp
from .integrity import atomic_write_json
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
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("continuous observation status is unreadable or invalid JSON") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != _STATUS_SCHEMA_VERSION:
        raise ValueError("continuous observation status has unsupported schema")
    run_id = raw.get("run_id")
    state = raw.get("state")
    if not isinstance(run_id, str) or not run_id or not isinstance(state, str) or not state:
        raise ValueError("continuous observation status is missing run identity/state")
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


def _provider_backoff_seconds(
    provider_unavailable_streak: int,
    config: ContinuousObservationConfig,
) -> float:
    if provider_unavailable_streak <= 0:
        raise ValueError("provider-unavailable backoff requires positive streak")
    exponent = min(provider_unavailable_streak - 1, 20)
    return min(
        config.max_backoff_seconds,
        config.interval_seconds * (2**exponent),
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
        durable_health = health_store.get(state.source_id)
        restart_backoff_remaining = 0.0
        if (
            durable_health.status == "failed"
            and durable_health.last_failure_kind == "provider_unavailable"
        ):
            if durable_health.consecutive_failure_kind_count <= 0:
                raise ValueError(
                    "typed provider-unavailable health requires positive streak"
                )
            if durable_health.last_error_at is None:
                raise ValueError(
                    "typed provider-unavailable health requires failure timestamp"
                )
            # Typed provider backoff authority is committed atomically with the
            # source-health failure transition. The lifecycle status file is only
            # an operator projection and may legitimately lag after process death.
            state.provider_unavailable_streak = (
                durable_health.consecutive_failure_kind_count
            )
            earned_backoff = _provider_backoff_seconds(
                state.provider_unavailable_streak,
                config,
            )
            restart_at = parse_source_timestamp(started_at)
            failure_at = parse_source_timestamp(durable_health.last_error_at)
            elapsed_wall = (restart_at - failure_at).total_seconds()
            # A wall-clock rollback cannot prove that any part of the earned
            # provider backoff elapsed, so it must not shorten the restart delay.
            causally_elapsed = max(0.0, elapsed_wall)
            restart_backoff_remaining = max(
                0.0,
                earned_backoff - causally_elapsed,
            )
        mirror = MarketMirror.from_store(store)
        mirror_updates = BoundedMirrorInvalidationBuffer(mirror)
        publish("running")

        enter_loop = True
        if restart_backoff_remaining > 0:
            if stopper.is_set():
                terminal_reason = "operator_stop"
                enter_loop = False
            else:
                remaining_runtime = config.max_runtime_seconds - (
                    monotonic() - started_monotonic
                )
                if remaining_runtime <= 0:
                    terminal_reason = "max_runtime"
                    enter_loop = False
                else:
                    startup_wait = min(
                        restart_backoff_remaining,
                        remaining_runtime,
                    )
                    if wait(startup_wait):
                        terminal_reason = "operator_stop"
                        enter_loop = False
                    elif startup_wait >= remaining_runtime:
                        # The bounded runtime expires no later than the backoff
                        # deadline, so first provider I/O is not permitted.
                        terminal_reason = "max_runtime"
                        enter_loop = False

        while enter_loop:
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
                full_refresh_seen = _drain_invalidation_projection(mirror_updates)
            except ProviderUnavailableError as exc:
                if _has_health_persistence_failure_note(exc):
                    state.health_status = "unknown"
                    state.last_error_kind = "local_health_failure_while_recording_provider_error"
                    state.last_error = _redacted_error(exc, redact_values)
                    terminal_reason = state.last_error_kind
                    terminal_exit = 5
                    publish("failed", stop_reason=terminal_reason)
                    break
                durable_health = health_store.get(state.source_id)
                if (
                    durable_health.status != "failed"
                    or durable_health.last_failure_kind != "provider_unavailable"
                    or durable_health.consecutive_failure_kind_count <= 0
                ):
                    state.health_status = "unknown"
                    state.last_error_kind = (
                        "local_health_failure_while_recording_provider_error"
                    )
                    state.last_error = _redacted_error(exc, redact_values)
                    terminal_reason = state.last_error_kind
                    terminal_exit = 5
                    publish("failed", stop_reason=terminal_reason)
                    break
                state.provider_unavailable_streak = (
                    durable_health.consecutive_failure_kind_count
                )
                state.health_status = durable_health.status
                state.last_error_kind = "provider_unavailable"
                state.last_error = _redacted_error(exc, redact_values)
                publish("provider_unavailable")
                if state.attempted_cycles >= config.max_cycles:
                    terminal_reason = "max_cycles_after_provider_unavailable"
                    terminal_exit = 4 if state.successful_cycles == 0 else 0
                    break
                remaining = config.max_runtime_seconds - (monotonic() - started_monotonic)
                if remaining <= 0:
                    terminal_reason = "max_runtime_after_provider_unavailable"
                    terminal_exit = 4 if state.successful_cycles == 0 else 0
                    break
                backoff = min(
                    _provider_backoff_seconds(
                        state.provider_unavailable_streak,
                        config,
                    ),
                    remaining,
                )
                if wait(backoff):
                    terminal_reason = "operator_stop"
                    break
                continue
            except CommittedIngestionHealthError as exc:
                state.health_status = "unknown"
                state.last_error_kind = "local_health_publication_failure_after_market_commit"
                state.last_error = _redacted_error(exc, redact_values)
                terminal_reason = state.last_error_kind
                terminal_exit = 5
                publish("failed", stop_reason=terminal_reason)
                break
            except (sqlite3.Error, OSError) as exc:
                state.health_status = _safe_health_status(health_store, state.source_id, "unknown")
                state.last_error_kind = "local_durable_failure"
                state.last_error = _redacted_error(exc, redact_values)
                terminal_reason = state.last_error_kind
                terminal_exit = 5
                publish("failed", stop_reason=terminal_reason)
                break
            except Exception as exc:
                state.health_status = _safe_health_status(health_store, state.source_id, "failed")
                state.last_error_kind = "fail_closed_provider_or_validation_error"
                state.last_error = _redacted_error(exc, redact_values)
                terminal_reason = state.last_error_kind
                terminal_exit = 3
                publish("failed", stop_reason=terminal_reason)
                break

            state.successful_cycles += 1
            state.total_received += stats.received
            state.total_accepted += stats.accepted
            state.total_rejected += stats.rejected
            state.last_cursor = stats.cursor
            state.health_status = stats.health_status
            state.provider_unavailable_streak = 0
            state.last_error_kind = None
            state.last_error = None
            publish("running", full_refresh=full_refresh_seen)

            if state.attempted_cycles >= config.max_cycles:
                terminal_reason = "max_cycles"
                break
            remaining = config.max_runtime_seconds - (monotonic() - started_monotonic)
            if remaining <= 0:
                terminal_reason = "max_runtime"
                break
            if wait(min(config.interval_seconds, remaining)):
                terminal_reason = "operator_stop"
                break
    except Exception as exc:
        if state.last_error_kind is None:
            state.last_error_kind = "local_startup_or_status_failure"
            state.last_error = _redacted_error(exc, redact_values)
        # If status publication itself is broken, a second write may fail too. Preserve
        # the original exception and never proceed to provider I/O after that failure.
        try:
            publish("failed", stop_reason=state.last_error_kind)
        except Exception:
            pass
        raise
    finally:
        if store is not None:
            store.close()

    publish("stopped" if terminal_exit == 0 else "failed", stop_reason=terminal_reason)
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autosport-observe-continuous",
        description="Bounded local read-only Autosport market observation loop",
    )
    parser.add_argument("workspace", type=Path)
    parser.add_argument(
        "--provider",
        choices=("parlay-table-tennis",),
        default="parlay-table-tennis",
    )
    parser.add_argument(
        "--enable-network-observation",
        action="store_true",
        help="required explicit opt-in before any provider network request",
    )
    parser.add_argument(
        "--public-preview",
        action="store_true",
        help="use the provider's public preview instead of an authenticated API key",
    )
    parser.add_argument("--max-cycles", type=int, default=60)
    parser.add_argument("--max-runtime-seconds", type=float, default=3600.0)
    parser.add_argument("--interval-seconds", type=float, default=60.0)
    parser.add_argument("--max-backoff-seconds", type=float, default=300.0)
    parser.add_argument("--max-items", type=int, default=250)
    parser.add_argument("--status-path", type=Path)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    provider_factory: ProviderFactory = ParlayApiTableTennisProvider,
) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        config = ContinuousObservationConfig(
            workspace=args.workspace,
            max_cycles=args.max_cycles,
            max_runtime_seconds=args.max_runtime_seconds,
            interval_seconds=args.interval_seconds,
            max_backoff_seconds=args.max_backoff_seconds,
            max_items=args.max_items,
            status_path=args.status_path,
        )
    except ValueError as exc:
        print(f"continuous_observation=CONFIG_ERROR error={exc}")
        return 2

    if not args.enable_network_observation:
        print(
            "continuous_observation=BLOCKED reason=network_observation_not_explicitly_enabled "
            "real_money_execution=false"
        )
        return 2

    api_key = None if args.public_preview else os.environ.get("AUTOSPORT_PARLAYAPI_KEY")
    if not args.public_preview and not api_key:
        print(
            "continuous_observation=BLOCKED reason=AUTOSPORT_PARLAYAPI_KEY_not_set "
            "real_money_execution=false"
        )
        return 2

    try:
        provider = provider_factory(api_key, public_preview=args.public_preview)
    except (ProviderPayloadError, ValueError) as exc:
        redacted_error = _redacted_error(exc, (api_key,) if api_key else ())
        print(f"continuous_observation=CONFIG_ERROR error={redacted_error}")
        return 2

    stop_event = threading.Event()
    old_handlers: dict[int, object] = {}

    def request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    for signum in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
        if signum is None:
            continue
        try:
            old_handlers[signum] = signal.signal(signum, request_stop)
        except (OSError, ValueError):
            pass

    try:
        result = run_continuous_observation(
            provider,
            config,
            stop_event=stop_event,
            redact_values=(api_key,) if api_key else (),
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"continuous_observation=FAIL_CLOSED error={_redacted_error(exc, (api_key,) if api_key else ())}")
        return 5
    finally:
        for signum, old_handler in old_handlers.items():
            try:
                signal.signal(signum, old_handler)
            except (OSError, ValueError):
                pass

    print(
        "continuous_observation=STOPPED "
        f"reason={result.stop_reason} attempted={result.attempted_cycles} "
        f"successful={result.successful_cycles} real_money_execution=false"
    )
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
