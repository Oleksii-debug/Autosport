from __future__ import annotations

import signal
from pathlib import Path

import pytest

import autosport.product_entrypoint as entrypoint


class TickFailure(RuntimeError):
    pass


class CloseFailure(RuntimeError):
    pass


class CloseInterrupt(KeyboardInterrupt):
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
    ) -> None:
        self.tick_failure = tick_failure
        self.close_failure = close_failure
        self.close_calls = 0
        self.start_calls = 0
        self.stop_calls = 0

    def start(self) -> object:
        self.start_calls += 1
        return object()

    def tick(self) -> object:
        if self.tick_failure is not None:
            raise self.tick_failure
        return object()

    def stop(self, _reason: str) -> object:
        self.stop_calls += 1
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


def test_tick_failure_remains_primary_when_close_also_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = _Runtime(
        tick_failure=TickFailure("tick"),
        close_failure=CloseFailure("close"),
    )
    _install_runtime(monkeypatch, runtime)

    with pytest.raises(entrypoint.ProductRuntimeError) as caught:
        entrypoint.run_product(
            workspace=tmp_path,
            source_factory="ignored:factory",
            max_cycles=1,
            install_signal_handlers=False,
        )

    assert caught.value.error_type == "TickFailure"
    assert isinstance(caught.value.__cause__, TickFailure)
    assert runtime.close_calls == 1


def test_tick_failure_remains_primary_when_close_raises_baseexception(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = _Runtime(
        tick_failure=TickFailure("tick"),
        close_failure=CloseInterrupt("close interrupt"),
    )
    _install_runtime(monkeypatch, runtime)

    with pytest.raises(entrypoint.ProductRuntimeError) as caught:
        entrypoint.run_product(
            workspace=tmp_path,
            source_factory="ignored:factory",
            max_cycles=1,
            install_signal_handlers=False,
        )

    assert caught.value.error_type == "TickFailure"
    assert isinstance(caught.value.__cause__, TickFailure)
    assert runtime.close_calls == 1


def test_partial_signal_install_failure_closes_and_restores_installed_handler(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = _Runtime()
    _install_runtime(monkeypatch, runtime)
    previous = {
        signal.SIGINT: object(),
        signal.SIGTERM: object(),
    }
    calls: list[tuple[signal.Signals, object]] = []

    monkeypatch.setattr(entrypoint.signal, "getsignal", lambda signum: previous[signum])

    def fake_signal(signum: signal.Signals, handler: object) -> None:
        calls.append((signum, handler))
        if signum is signal.SIGTERM and handler is not previous[signal.SIGTERM]:
            raise InstallFailure("install")

    monkeypatch.setattr(entrypoint.signal, "signal", fake_signal)

    with pytest.raises(InstallFailure):
        entrypoint.run_product(
            workspace=tmp_path,
            source_factory="ignored:factory",
            max_cycles=1,
        )

    assert runtime.start_calls == 0
    assert runtime.close_calls == 1
    assert calls[-1] == (signal.SIGINT, previous[signal.SIGINT])


def test_successful_product_body_reports_close_failure_as_runtime_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = _Runtime(close_failure=CloseFailure("close"))
    _install_runtime(monkeypatch, runtime)

    with pytest.raises(entrypoint.ProductRuntimeError) as caught:
        entrypoint.run_product(
            workspace=tmp_path,
            source_factory="ignored:factory",
            max_cycles=1,
            install_signal_handlers=False,
        )

    assert caught.value.error_type == "CloseFailure"
    assert isinstance(caught.value.__cause__, CloseFailure)
    assert runtime.start_calls == 1
    assert runtime.stop_calls == 1
    assert runtime.close_calls == 1


def test_restore_failure_after_success_attempts_all_restores_and_is_runtime_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = _Runtime()
    _install_runtime(monkeypatch, runtime)
    previous = {
        signal.SIGINT: object(),
        signal.SIGTERM: object(),
    }
    restored: list[signal.Signals] = []

    monkeypatch.setattr(entrypoint.signal, "getsignal", lambda signum: previous[signum])

    def fake_signal(signum: signal.Signals, handler: object) -> None:
        if handler is previous[signum]:
            restored.append(signum)
            if signum is signal.SIGTERM:
                raise RestoreFailure("restore")

    monkeypatch.setattr(entrypoint.signal, "signal", fake_signal)

    with pytest.raises(entrypoint.ProductRuntimeError) as caught:
        entrypoint.run_product(
            workspace=tmp_path,
            source_factory="ignored:factory",
            max_cycles=1,
        )

    assert caught.value.error_type == "RestoreFailure"
    assert isinstance(caught.value.__cause__, RestoreFailure)
    assert restored == [signal.SIGTERM, signal.SIGINT]
    assert runtime.close_calls == 1
