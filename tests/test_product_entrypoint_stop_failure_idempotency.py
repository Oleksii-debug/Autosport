from __future__ import annotations

import signal
from types import SimpleNamespace

import pytest

import autosport.product_entrypoint as entrypoint


class StopFailure(RuntimeError):
    pass


class _FailingStopRuntime:
    def __init__(self) -> None:
        self.stop_calls: list[str] = []
        self.durable_stop_effects: list[str] = []
        self.tick_calls = 0
        self.close_calls = 0

    def start(self) -> object:
        return object()

    def tick(self) -> object:
        self.tick_calls += 1
        return object()

    def stop(self, reason: str) -> object:
        self.stop_calls.append(reason)
        # Simulate a STOP implementation that durably records the chosen cause and
        # only then fails while completing its remaining work. Retrying with a
        # different cleanup reason would contradict that durable terminal choice.
        self.durable_stop_effects.append(reason)
        raise StopFailure(reason)

    def close(self) -> None:
        self.close_calls += 1


def _install_runtime(monkeypatch: pytest.MonkeyPatch, runtime: _FailingStopRuntime) -> None:
    monkeypatch.setattr(entrypoint, "_validated_source", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        entrypoint,
        "build_autonomous_product_runtime",
        lambda **_kwargs: runtime,
    )
    monkeypatch.setattr(entrypoint, "_print_record", lambda *_args, **_kwargs: None)


def test_failed_max_cycles_stop_is_not_reissued_as_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _FailingStopRuntime()
    _install_runtime(monkeypatch, runtime)

    with pytest.raises(entrypoint.ProductRuntimeError) as raised:
        entrypoint.run_product(
            workspace="unused",
            source_factory="unused:factory",
            max_cycles=1,
            poll_seconds=0,
            install_signal_handlers=False,
        )

    assert raised.value.error_type == "StopFailure"
    assert isinstance(raised.value.__cause__, StopFailure)
    assert runtime.tick_calls == 1
    assert runtime.stop_calls == ["max_cycles_reached"]
    assert runtime.durable_stop_effects == ["max_cycles_reached"]
    assert runtime.close_calls == 1


def test_failed_signal_stop_is_not_reissued_as_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _FailingStopRuntime()
    _install_runtime(monkeypatch, runtime)
    stop_request = SimpleNamespace(
        requested=True,
        reason="signal:SIGTERM",
        exit_code=128 + int(signal.SIGTERM),
    )
    monkeypatch.setattr(entrypoint, "_SignalStopRequest", lambda: stop_request)

    with pytest.raises(entrypoint.ProductRuntimeError) as raised:
        entrypoint.run_product(
            workspace="unused",
            source_factory="unused:factory",
            max_cycles=2,
            poll_seconds=0,
            install_signal_handlers=False,
        )

    assert raised.value.error_type == "StopFailure"
    assert isinstance(raised.value.__cause__, StopFailure)
    assert runtime.tick_calls == 0
    assert runtime.stop_calls == ["signal:SIGTERM"]
    assert runtime.durable_stop_effects == ["signal:SIGTERM"]
    assert runtime.close_calls == 1
