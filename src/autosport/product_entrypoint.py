from __future__ import annotations

import argparse
import json
import math
import signal
import time
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Sequence

from .collector_service import _load_source_factory
from .product_runtime import AutonomousProductRuntime, build_autonomous_product_runtime


class ProductEntrypointError(RuntimeError):
    """The supported product command cannot safely construct or run the product."""


class _SignalStopRequest:
    def __init__(self) -> None:
        self.signal_number: int | None = None

    def handle(self, signum: int, _frame: object) -> None:
        self.signal_number = signum

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


def _validated_source(source_factory: str) -> object:
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
    # creates a workspace or durable manifest. Missing event resolution must never be
    # hidden by a synthesized MarketEvent or a test-only fallback.
    source = _validated_source(source_factory)
    runtime = build_autonomous_product_runtime(
        workspace=workspace,
        source=source,
        initial_bankroll=initial_bankroll,
    )
    stop_request = _SignalStopRequest()
    previous_handlers: dict[signal.Signals, object] = {}
    if install_signal_handlers:
        previous_handlers = {
            signum: signal.getsignal(signum)
            for signum in (signal.SIGINT, signal.SIGTERM)
        }
        for signum in previous_handlers:
            signal.signal(signum, stop_request.handle)

    try:
        _print_record("product_status", runtime=runtime, value=runtime.start())
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
            sleep(float(poll_seconds))
        return stop_request.exit_code
    finally:
        runtime.close()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


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
    except Exception as exc:
        # Product stdout is a public/machine-readable boundary. Arbitrary exception
        # messages may contain provider credentials, response bodies or other secrets,
        # so only stable classification is emitted here. Detailed diagnostics belong
        # behind an explicitly secret-safe internal logging boundary.
        print(
            json.dumps(
                {
                    "kind": "product_start_failure",
                    "paper_only": True,
                    "real_money_execution": False,
                    "error_code": "product_start_failed",
                    "error_type": type(exc).__name__,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
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
