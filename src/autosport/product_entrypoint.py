from __future__ import annotations

import argparse
import json
import math
import signal
import sys
import threading
import time
import unicodedata
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .collector_service import _load_source_factory
from .product_runtime import AutonomousProductRuntime, build_autonomous_product_runtime


_OUTPUT_FORMATS = frozenset({"json", "text"})
_TEXT_FIELD_ORDER = (
    "kind",
    "paper_only",
    "real_money_execution",
    "source_id",
    "workspace",
    "error_code",
    "error_type",
    "value",
)


class ProductEntrypointError(RuntimeError):
    """The supported product command cannot safely construct or run the product."""


class ProductRuntimeError(ProductEntrypointError):
    """The product failed only after the canonical runtime had started."""

    def __init__(self, error_type: str) -> None:
        super().__init__("product runtime failed after start")
        self.error_type = error_type


class _SecretSafeArgumentParser(argparse.ArgumentParser):
    """Argument parser that never echoes rejected caller-controlled values."""

    def error(self, _message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(
            2,
            f"{self.prog}: error: invalid command-line arguments; use --help\n",
        )


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
    """Return console stop signals supported by the running platform."""

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


def _validated_output_format(output_format: str) -> str:
    if output_format not in _OUTPUT_FORMATS:
        raise ValueError("output_format must be one of: json, text")
    return output_format


def _text_atom(value: object) -> str:
    """Render one value without allowing terminal-shaping Unicode controls through."""

    rendered = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    safe: list[str] = []
    for character in rendered:
        category = unicodedata.category(character)
        if category.startswith("C") or category in {"Zl", "Zp"}:
            codepoint = ord(character)
            if codepoint <= 0xFFFF:
                safe.append(f"\\u{codepoint:04x}")
            else:
                safe.append(f"\\U{codepoint:08x}")
        else:
            safe.append(character)
    return "".join(safe)


def _text_mapping_key_identity(key: object) -> tuple[str, str]:
    """Return a deterministic identity for JSON-object-compatible mapping keys."""

    if type(key) is str:
        return ("str", key)
    if type(key) is bool:
        return ("bool", "true" if key else "false")
    if type(key) is int:
        return ("int", str(key))
    if type(key) is float:
        if not math.isfinite(key):
            raise ValueError("text output mapping float keys must be finite")
        return (
            "float",
            json.dumps(
                key,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
            ),
        )
    if key is None:
        return ("null", "")
    raise TypeError(
        "text output mapping keys must be str, bool, int, finite float, or None"
    )


def _text_label_child(prefix: str, key: object) -> str:
    """Append one collision-free mapping-key path component."""

    kind, component = _text_mapping_key_identity(key)
    if kind == "str":
        if component and all(
            character.isalnum() or character in "_-" for character in component
        ):
            return f"{prefix}.{component}" if prefix else component

        encoded = _text_atom(component)
        return f"{prefix}[{encoded}]" if prefix else f"[{encoded}]"

    encoded = "null" if kind == "null" else f"{kind}={component}"
    return f"{prefix}[{encoded}]" if prefix else f"[{encoded}]"


def _append_text_lines(lines: list[str], prefix: str, value: object) -> None:
    if isinstance(value, Mapping):
        if not value:
            lines.append(f"{prefix}: empty mapping")
            return
        for key in sorted(value, key=_text_mapping_key_identity):
            _append_text_lines(lines, _text_label_child(prefix, key), value[key])
        return
    if isinstance(value, (list, tuple)):
        if not value:
            lines.append(f"{prefix}: empty list")
            return
        for index, item in enumerate(value):
            _append_text_lines(lines, f"{prefix}[{index}]", item)
        return
    lines.append(f"{prefix}: {_text_atom(value)}")


def _format_text_record(record: Mapping[str, object]) -> str:
    """Return a stable line-oriented record suitable for keyboard/screen-reader use."""

    lines = ["AUTOSPORT RECORD"]
    handled: set[str] = set()
    for field in _TEXT_FIELD_ORDER:
        if field in record:
            _append_text_lines(lines, field, record[field])
            handled.add(field)
    for field in sorted(set(record) - handled):
        _append_text_lines(lines, field, record[field])
    lines.append("END AUTOSPORT RECORD")
    return "\n".join(lines)


def _print_payload(record: Mapping[str, object], *, output_format: str) -> None:
    output_format = _validated_output_format(output_format)
    if output_format == "text":
        print(_format_text_record(record))
        return
    print(
        json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _print_record(
    kind: str,
    *,
    runtime: AutonomousProductRuntime,
    value: object,
    output_format: str = "json",
) -> None:
    _print_payload(
        {
            "kind": kind,
            "paper_only": True,
            "real_money_execution": False,
            "source_id": runtime.manifest.source_id,
            "workspace": str(runtime.workspace),
            "value": asdict(value),
        },
        output_format=output_format,
    )


def _print_failure(
    *,
    kind: str,
    error_code: str,
    error_type: str,
    output_format: str = "json",
) -> None:
    _print_payload(
        {
            "kind": kind,
            "paper_only": True,
            "real_money_execution": False,
            "error_code": error_code,
            "error_type": error_type,
        },
        output_format=output_format,
    )


def run_product(
    *,
    workspace: str | Path,
    source_factory: str,
    initial_bankroll: str = "10000",
    max_cycles: int | None = None,
    poll_seconds: float = 30.0,
    output_format: str = "json",
    sleep: Callable[[float], None] = time.sleep,
    install_signal_handlers: bool = True,
) -> int:
    """Run the canonical headless PAPER product from one supported boundary."""

    output_format = _validated_output_format(output_format)
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
    terminalized = False
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
        _print_record(
            "product_status",
            runtime=runtime,
            value=start_status,
            output_format=output_format,
        )
        cycles = 0
        exit_code = 0
        while max_cycles is None or cycles < max_cycles:
            if stop_request.requested:
                exit_code = stop_request.exit_code
                stop_status = runtime.stop(stop_request.reason)
                terminalized = True
                _print_record(
                    "product_status",
                    runtime=runtime,
                    value=stop_status,
                    output_format=output_format,
                )
                break

            result = runtime.tick()
            cycles += 1
            _print_record(
                "product_tick",
                runtime=runtime,
                value=result,
                output_format=output_format,
            )

            if stop_request.requested:
                exit_code = stop_request.exit_code
                stop_status = runtime.stop(stop_request.reason)
                terminalized = True
                _print_record(
                    "product_status",
                    runtime=runtime,
                    value=stop_status,
                    output_format=output_format,
                )
                break
            if max_cycles is not None and cycles >= max_cycles:
                stop_status = runtime.stop("max_cycles_reached")
                terminalized = True
                _print_record(
                    "product_status",
                    runtime=runtime,
                    value=stop_status,
                    output_format=output_format,
                )
                break
            if install_signal_handlers and sleep is time.sleep:
                stop_request.wait(float(poll_seconds))
            else:
                sleep(float(poll_seconds))
        return exit_code
    except Exception as exc:
        if started:
            if isinstance(exc, ProductRuntimeError):
                raise
            raise ProductRuntimeError(type(exc).__name__) from exc
        raise
    finally:
        primary_failure = sys.exc_info()[1]
        cleanup_failure: BaseException | None = None

        if started and not terminalized and primary_failure is not None:
            try:
                runtime.stop("runtime_error")
                terminalized = True
            except BaseException as stop_error:
                try:
                    primary_failure.add_note(
                        "runtime STOP also failed during exceptional cleanup: "
                        f"{type(stop_error).__name__}: {stop_error}"
                    )
                except BaseException:
                    pass

        try:
            runtime.close()
        except BaseException as exc:
            if primary_failure is None:
                cleanup_failure = exc
            else:
                try:
                    primary_failure.add_note(
                        "runtime close also failed during cleanup: "
                        f"{type(exc).__name__}: {exc}"
                    )
                except BaseException:
                    pass

        for signum in reversed(installed_handlers):
            try:
                signal.signal(signum, previous_handlers[signum])
            except BaseException as exc:
                if primary_failure is None and cleanup_failure is None:
                    cleanup_failure = exc
                elif primary_failure is not None:
                    try:
                        primary_failure.add_note(
                            "signal handler restoration also failed during cleanup: "
                            f"{type(exc).__name__}: {exc}"
                        )
                    except BaseException:
                        pass

        if cleanup_failure is not None:
            if started and isinstance(cleanup_failure, Exception):
                raise ProductRuntimeError(
                    type(cleanup_failure).__name__
                ) from cleanup_failure
            raise cleanup_failure


def run_product_command(
    *,
    workspace: Path,
    source_factory: str,
    initial_bankroll: str,
    max_cycles: int | None,
    poll_seconds: float,
    output_format: str = "json",
) -> int:
    try:
        return run_product(
            workspace=workspace,
            source_factory=source_factory,
            initial_bankroll=initial_bankroll,
            max_cycles=max_cycles,
            poll_seconds=poll_seconds,
            output_format=output_format,
        )
    except ProductRuntimeError as exc:
        _print_failure(
            kind="product_runtime_failure",
            error_code="product_runtime_failed",
            error_type=exc.error_type,
            output_format=output_format,
        )
        return 4
    except Exception as exc:
        try:
            output_format = _validated_output_format(output_format)
        except ValueError:
            output_format = "json"
        _print_failure(
            kind="product_start_failure",
            error_code="product_start_failed",
            error_type=type(exc).__name__,
            output_format=output_format,
        )
        return 3


def _parser() -> argparse.ArgumentParser:
    parser = _SecretSafeArgumentParser(
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
    parser.add_argument(
        "--format",
        dest="output_format",
        choices=sorted(_OUTPUT_FORMATS),
        default="json",
        help=(
            "stdout format: json preserves the machine-readable contract; text emits "
            "stable labelled records for keyboard/screen-reader operator use"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return run_product_command(
        workspace=args.workspace,
        source_factory=args.source_factory,
        initial_bankroll=args.bankroll,
        max_cycles=args.max_cycles,
        poll_seconds=args.poll_seconds,
        output_format=args.output_format,
    )


if __name__ == "__main__":
    raise SystemExit(main())
