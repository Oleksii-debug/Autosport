from __future__ import annotations

import io
import signal
from contextlib import redirect_stderr
from types import SimpleNamespace

import pytest

import autosport.product_entrypoint as entrypoint


class TickFailure(RuntimeError):
    pass


class CloseFailure(RuntimeError):
    pass


class InstallFailure(RuntimeError):
    pass


class RestoreFailure(RuntimeError):
    pass


class _Runtime:
    def __init__(
        self,
        *,
        tick_failure: Exception | None = None,
        close_failure: BaseException | None = None,
        signal_during_tick: object | None = None,
        signal_after_max_stop: object | None = None,
    ) -> None:
        self.tick_failure = tick_failure
        self.close_failure = close_failure
        self.signal_during_tick = signal_during_tick
        self.signal_after_max_stop = signal_after_max_stop
        self.start_calls = 0
        self.tick_calls = 0
        self.stop_calls: list[str] = []
        self.close_calls = 0

    def start(self) -> object:
        self.start_calls += 1
        return object()

    def tick(self) -> object:
        self.tick_calls += 1
        if self.signal_during_tick is not None:
            self.signal_during_tick.requested = True
        if self.tick_failure is not None:
            raise self.tick_failure
        return object()

    def stop(self, reason: str) -> object:
        self.stop_calls.append(reason)
        if reason == "max_cycles_reached" and self.signal_after_max_stop is not None:
            self.signal_after_max_stop.requested = True
        return object()

    def close(self) -> None:
        self.close_calls += 1
        if self.close_failure is not None:
            raise self.close_failure


def _install_runtime(monkeypatch: pytest.MonkeyPatch, runtime: _Runtime) -> None:
    monkeypatch.setattr(entrypoint, "_validated_source", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        entrypoint,
        "build_autonomous_product_runtime",
        lambda **_kwargs: runtime,
    )
    monkeypatch.setattr(entrypoint, "_print_record", lambda *_args, **_kwargs: None)


def _stop_request(*, requested: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        requested=requested,
        reason="signal:SIGTERM",
        exit_code=128 + int(signal.SIGTERM),
    )


def test_secret_safe_parser_never_echoes_rejected_cli_value() -> None:
    sentinel = "S3CR3T-CANARY-DO-NOT-ECHO"
    stderr = io.StringIO()

    with redirect_stderr(stderr), pytest.raises(SystemExit) as raised:
        entrypoint._parser().parse_args(
            [
                "--source-factory",
                "example:factory",
                f"--api-key={sentinel}",
            ]
        )

    assert raised.value.code == 2
    output = stderr.getvalue()
    assert "invalid command-line arguments" in output
    assert "use --help" in output
    assert sentinel not in output
    assert "--api-key=" not in output


def test_product_stop_signals_add_sigbreak_without_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(entrypoint.signal, "SIGBREAK", 21, raising=False)
    signals = entrypoint._product_stop_signals()

    assert signals[:2] == (int(signal.SIGINT), int(signal.SIGTERM))
    assert signals[-1] == 21
    assert len(signals) == len(set(signals))


def test_signal_during_final_tick_wins_over_max_cycles_and_freezes_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop_request = _stop_request()
    runtime = _Runtime(signal_during_tick=stop_request)
    _install_runtime(monkeypatch, runtime)
    monkeypatch.setattr(entrypoint, "_SignalStopRequest", lambda: stop_request)

    code = entrypoint.run_product(
        workspace="unused",
        source_factory="unused:factory",
        max_cycles=1,
        poll_seconds=0,
        sleep=lambda _seconds: pytest.fail("final bounded cycle must not sleep"),
        install_signal_handlers=False,
    )

    assert code == 128 + int(signal.SIGTERM)
    assert runtime.stop_calls == ["signal:SIGTERM"]
    assert runtime.close_calls == 1


def test_late_signal_cannot_rewrite_selected_max_cycles_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop_request = _stop_request()
    runtime = _Runtime(signal_after_max_stop=stop_request)
    _install_runtime(monkeypatch, runtime)
    monkeypatch.setattr(entrypoint, "_SignalStopRequest", lambda: stop_request)

    code = entrypoint.run_product(
        workspace="unused",
        source_factory="unused:factory",
        max_cycles=1,
        poll_seconds=0,
        sleep=lambda _seconds: pytest.fail("final bounded cycle must not sleep"),
        install_signal_handlers=False,
    )

    assert stop_request.requested is True
    assert code == 0
    assert runtime.stop_calls == ["max_cycles_reached"]
    assert runtime.close_calls == 1


def test_partial_signal_install_failure_closes_and_restores_only_installed_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _Runtime()
    _install_runtime(monkeypatch, runtime)
    stop_signals = (int(signal.SIGINT), int(signal.SIGTERM), 21)
    previous = {signum: object() for signum in stop_signals}
    calls: list[tuple[int, object]] = []

    monkeypatch.setattr(entrypoint, "_product_stop_signals", lambda: stop_signals)
    monkeypatch.setattr(entrypoint.signal, "getsignal", lambda signum: previous[signum])

    def fake_signal(signum: int, handler: object) -> None:
        calls.append((signum, handler))
        if signum == stop_signals[-1] and handler is not previous[signum]:
            raise InstallFailure("install")

    monkeypatch.setattr(entrypoint.signal, "signal", fake_signal)

    with pytest.raises(InstallFailure):
        entrypoint.run_product(
            workspace="unused",
            source_factory="unused:factory",
            max_cycles=1,
            poll_seconds=0,
        )

    assert runtime.start_calls == 0
    assert runtime.close_calls == 1
    restorations = [
        call for call in calls if any(call[1] is handler for handler in previous.values())
    ]
    assert restorations == [
        (stop_signals[1], previous[stop_signals[1]]),
        (stop_signals[0], previous[stop_signals[0]]),
    ]


def test_primary_tick_failure_survives_close_and_restore_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _Runtime(
        tick_failure=TickFailure("tick"),
        close_failure=CloseFailure("close"),
    )
    _install_runtime(monkeypatch, runtime)
    stop_signals = (int(signal.SIGINT), int(signal.SIGTERM))
    previous = {signum: object() for signum in stop_signals}

    monkeypatch.setattr(entrypoint, "_product_stop_signals", lambda: stop_signals)
    monkeypatch.setattr(entrypoint.signal, "getsignal", lambda signum: previous[signum])

    def fake_signal(signum: int, handler: object) -> None:
        if handler is previous[signal.SIGTERM]:
            raise RestoreFailure("restore")

    monkeypatch.setattr(entrypoint.signal, "signal", fake_signal)

    with pytest.raises(entrypoint.ProductRuntimeError) as raised:
        entrypoint.run_product(
            workspace="unused",
            source_factory="unused:factory",
            max_cycles=1,
            poll_seconds=0,
        )

    assert raised.value.error_type == "TickFailure"
    assert isinstance(raised.value.__cause__, TickFailure)
    assert runtime.stop_calls == ["runtime_error"]
    assert runtime.close_calls == 1


def test_default_idle_wait_is_preempted_into_signal_stop_without_second_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _WaitStop:
        requested = False
        reason = "signal:SIGTERM"
        exit_code = 128 + int(signal.SIGTERM)

        def handle(self, _signum: int, _frame: object) -> None:
            self.requested = True

        def wait(self, _timeout: float) -> bool:
            self.requested = True
            return True

    stop_request = _WaitStop()
    runtime = _Runtime()
    _install_runtime(monkeypatch, runtime)
    monkeypatch.setattr(entrypoint, "_SignalStopRequest", lambda: stop_request)
    monkeypatch.setattr(entrypoint, "_product_stop_signals", lambda: (int(signal.SIGTERM),))
    monkeypatch.setattr(entrypoint.signal, "getsignal", lambda _signum: object())
    monkeypatch.setattr(entrypoint.signal, "signal", lambda *_args: None)

    code = entrypoint.run_product(
        workspace="unused",
        source_factory="unused:factory",
        max_cycles=2,
        poll_seconds=30,
    )

    assert code == 128 + int(signal.SIGTERM)
    assert runtime.tick_calls == 1
    assert runtime.stop_calls == ["signal:SIGTERM"]
    assert runtime.close_calls == 1


def test_start_status_output_failure_terminalizes_runtime_before_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _Runtime()
    _install_runtime(monkeypatch, runtime)

    def fail_output(*_args: object, **_kwargs: object) -> None:
        raise BrokenPipeError("stdout closed")

    monkeypatch.setattr(entrypoint, "_print_record", fail_output)

    with pytest.raises(entrypoint.ProductRuntimeError) as raised:
        entrypoint.run_product(
            workspace="unused",
            source_factory="unused:factory",
            max_cycles=1,
            poll_seconds=0,
            install_signal_handlers=False,
        )

    assert raised.value.error_type == "BrokenPipeError"
    assert isinstance(raised.value.__cause__, BrokenPipeError)
    assert runtime.start_calls == 1
    assert runtime.tick_calls == 0
    assert runtime.stop_calls == ["runtime_error"]
    assert runtime.close_calls == 1


def test_output_failure_after_successful_stop_does_not_issue_second_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _Runtime()
    _install_runtime(monkeypatch, runtime)
    print_calls = 0

    def fail_terminal_output(*_args: object, **_kwargs: object) -> None:
        nonlocal print_calls
        print_calls += 1
        if print_calls == 3:
            raise BrokenPipeError("stdout closed after stop")

    monkeypatch.setattr(entrypoint, "_print_record", fail_terminal_output)

    with pytest.raises(entrypoint.ProductRuntimeError) as raised:
        entrypoint.run_product(
            workspace="unused",
            source_factory="unused:factory",
            max_cycles=1,
            poll_seconds=0,
            install_signal_handlers=False,
        )

    assert raised.value.error_type == "BrokenPipeError"
    assert runtime.start_calls == 1
    assert runtime.tick_calls == 1
    assert runtime.stop_calls == ["max_cycles_reached"]
    assert runtime.close_calls == 1
