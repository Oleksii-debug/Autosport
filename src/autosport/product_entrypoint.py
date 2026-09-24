from __future__ import annotations

import argparse
import json
import math
import signal
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Sequence

from .collector_service import _load_source_factory
from .product_runtime import AutonomousProductRuntime, build_autonomous_product_runtime


class ProductEntrypointError(RuntimeError):
    """The supported product command cannot safely construct or run the product."""


class ProductRuntimeError(ProductEntrypointError):
    """The product failed only after the canonical runtime had started."""

    def __init__(self, error_type: str) -> None:
        super().__init__("product runtime failed after start")
        self.error_type = error_type


class _SignalStopRequest:
    def __init__(self) -> None:
        self.signal_number: int | None = None
        self._event = threading.Event()

    def handle(self, signum: int, _frame: object) -> None:
        self.signal_number = signum
        self._event.set()

    def wait(self, timeout: float) -> bool:
        """Wait for a stop request, returning early when a signal handler fires."""
        return self._event.wait(timeout)

    @property
    def requested(self) -> bool:
        return self.signal_number is not None

    @property
    def reason(self) -> str:
        if self.signal_number is None:
            return "operator_stop"
        try:
            name = signal.Signals(self.signal_number).name
        except ValueError:
            name = str(self.signal_number)
        return f"signal:{name}"

    @property
    def exit_code(self) -> int:
        if self.signal_number is None:
            return 0
        return 128 + self.signal_number


def _product_stop_signals() -> tuple[int, ...]:
    """Return console stop signals supported by the running platform.

    Windows delivers Ctrl+Break as SIGBREAK rather than SIGINT.  Register it when
    available so that the supported product boundary records a durable stop instead
    of letting the process terminate outside the runtime shutdown path.
    """

    signals = [int(signal.SIGINT), int(signal.SIGTERM)]
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None and int(sigbreak) not in signals:
        signals.append(int(sigbreak))
    return tuple(signals)


def _normalized_workspace(value: object, *, label: str) -> Path:
    try:
        return Path(value).expanduser().resolve(strict=False)
    except (TypeError, ValueError, OSError, RuntimeError) as exc:
        raise ProductEntrypointError(f"{label} workspace cannot be resolved") from exc


def _validated_source(source_factory: str, *, workspace: str | Path) -> object:
    factory = _load_source_factory(source_factory)
    source = factory()
    for field in ("source_id", "stream_epoch"):
        value = getattr(source, field, None)
        if type(value) is not str or not value or value.strip() != value:
            raise ProductEntrypointError(
                f"product source {field} must be a non-empty trimmed string"
            )
    for method in ("fetch_catalog_page", "fetch_deltas", "resolve_event"):
        if not callable(getattr(source, method, None)):
            raise ProductEntrypointError(
                f"product source must provide callable {method}"
            )

    # A source that owns durable product state must be bound to the same canonical
    # workspace as the supported runtime before the composition root creates any
    # runtime files. Generic stateless/external source factories remain compatible.
    source_workspace = getattr(source, "workspace", None)
    if source_workspace is not None:
        expected_workspace = _normalized_workspace(workspace, label="product runtime")
        observed_workspace = _normalized_workspace(
            source_workspace, label="product source"
        )
        if observed_workspace != expected_workspace:
            raise ProductEntrypointError(
                "product source workspace must match product runtime workspace"
            )
    return source


def _print_record(kind: str, *, runtime: AutonomousProductRuntime, value: object) -> None:
    print(
        json.dumps(
            {
                "kind": kind,
                "paper_only": True,
                "real_money_execution": False,
                "source_id": runtime.manifest.source_id,
                "workspace": str(runtime.workspace),
                "value": asdict(value),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _print_failure(*, kind: str, error_code: str, error_type: str) -> None:
    print(
        json.dumps(
            {
                "kind": kind,
                "paper_only": True,
                "real_money_execution": False,
                "error_code": error_code,
                "error_type": error_type,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def run_product(
    *,
    workspace: str | Path,
    source_factory: str,
    initial_bankroll: str = "10000",
    max_cycles: int | None = None,
    poll_seconds: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
    install_signal_handlers: bool = True,
) -> int:
    """Run the canonical headless PAPER product from one supported boundary.

    Provider credentials and acquisition policy live behind ``source_factory``. This
    command owns no provider truth, market store, PAPER book, settlement, or learning
    authority; it only constructs the integrated product composition root and drives
    its canonical ticks.
    """

    if max_cycles is not None and (
        isinstance(max_cycles, bool)
        or not isinstance(max_cycles, int)
        or max_cycles <= 0
    ):
        raise ValueError("max_cycles must be a positive integer or None")
    if (
        isinstance(poll_seconds, bool)
        or not isinstance(poll_seconds, (int, float))
        or not math.isfinite(float(poll_seconds))
        or poll_seconds < 0
    ):
        raise ValueError("poll_seconds must be a finite non-negative number")
    if max_cycles is None and float(poll_seconds) == 0.0:
        raise ValueError("unbounded product run requires a positive poll interval")

    # Validate the complete production source capability before the composition root
    # creates a workspace or durable manifest. Missing event resolution or a split
    # source/runtime workspace must never be hidden by runtime initialization.
    source = _validated_source(source_factory, workspace=workspace)
    runtime = build_autonomous_product_runtime(
        workspace=workspace,
        source=source,
        initial_bankroll=initial_bankroll,
    )
    stop_request = _SignalStopRequest()
    previous_handlers: dict[int, object] = {}
    installed_handlers: list[int] = []
    started = False
    try:
        if install_signal_handlers:
            previous_handlers = {
                signum: signal.getsignal(signum)
                for signum in _product_stop_signals()
            }
            for signum in previous_handlers:
                signal.signal(signum, stop_request.handle)
                installed_handlers.append(signum)

        start_status = runtime.start()
        started = True
        _print_record("product_status", runtime=runtime, value=start_status)
        cycles = 0
        while max_cycles is None or cycles < max_cycles:
            if stop_request.requested:
                _print_record(
                    "product_status",
                    runtime=runtime,
                    value=runtime.stop(stop_request.reason),
                )
                break

            result = runtime.tick()
            cycles += 1
            _print_record("product_tick", runtime=runtime, value=result)

            if max_cycles is not None and cycles >= max_cycles:
                _print_record(
                    "product_status",
                    runtime=runtime,
                    value=runtime.stop("max_cycles_reached"),
                )
                break
            if stop_request.requested:
                _print_record(
                    "product_status",
                    runtime=runtime,
                    value=runtime.stop(stop_request.reason),
                )
                break
            if install_signal_handlers and sleep is time.sleep:
                stop_request.wait(float(poll_seconds))
            else:
                sleep(float(poll_seconds))
        return stop_request.exit_code
    except Exception as exc:
        if started:
            if isinstance(exc, ProductRuntimeError):
                raise
            raise ProductRuntimeError(type(exc).__name__) from exc
        raise
    finally:
        try:
            runtime.close()
        except Exception as exc:
            if started:
                raise ProductRuntimeError(type(exc).__name__) from exc
            raise
        finally:
            for signum in installed_handlers:
                signal.signal(signum, previous_handlers[signum])


def run_product_command(
    *,
    workspace: Path,
    source_factory: str,
    initial_bankroll: str,
    max_cycles: int | None,
    poll_seconds: float,
) -> int:
    try:
        return run_product(
            workspace=workspace,
            source_factory=source_factory,
            initial_bankroll=initial_bankroll,
            max_cycles=max_cycles,
            poll_seconds=poll_seconds,
        )
    except ProductRuntimeError as exc:
        _print_failure(
            kind="product_runtime_failure",
            error_code="product_runtime_failed",
            error_type=exc.error_type,
        )
        return 4
    except Exception as exc:
        # Product stdout is a public/machine-readable boundary. Arbitrary exception
        # messages may contain provider credentials, response bodies or other secrets,
        # so only stable classification is emitted here. Detailed diagnostics belong
        # behind an explicitly secret-safe internal logging boundary.
        _print_failure(
            kind="product_start_failure",
            error_code="product_start_failed",
            error_type=type(exc).__name__,
        )
        return 3


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autosport-product",
        description=(
            "Run the canonical durable Autosport PAPER product. Provider credentials "
            "remain external to Autosport and are never accepted as CLI arguments."
        ),
    )
    parser.add_argument("--workspace", type=Path, default=Path(".autosport-product"))
    parser.add_argument(
        "--source-factory",
        required=True,
        help="external product source factory in module:function form",
    )
    parser.add_argument("--bankroll", default="10000")
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=None,
        help="optional bounded cycle count for qualification/supervised runs",
    )
    parser.add_argument("--poll-seconds", type=float, default=30.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return run_product_command(
        workspace=args.workspace,
        source_factory=args.source_factory,
        initial_bankroll=args.bankroll,
        max_cycles=args.max_cycles,
        poll_seconds=args.poll_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
