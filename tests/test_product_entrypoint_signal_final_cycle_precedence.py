import signal
from pathlib import Path
from unittest.mock import patch

import autosport.product_entrypoint as product_entrypoint


class _FakeRuntime:
    def __init__(self, *, signal_during_tick: bool) -> None:
        self.signal_during_tick = signal_during_tick
        self.stop_reasons: list[str] = []
        self.closed = False
        self._installed_handlers: dict[int, object] = {}

    def start(self) -> object:
        return object()

    def tick(self) -> object:
        if self.signal_during_tick:
            handler = self._installed_handlers[int(signal.SIGTERM)]
            assert callable(handler)
            handler(int(signal.SIGTERM), None)
        return object()

    def stop(self, reason: str) -> object:
        self.stop_reasons.append(reason)
        return object()

    def close(self) -> None:
        self.closed = True


def _run_one_cycle(*, signal_during_tick: bool) -> tuple[int, _FakeRuntime]:
    runtime = _FakeRuntime(signal_during_tick=signal_during_tick)
    previous_handler = object()

    def install_handler(signum: int, handler: object) -> object:
        if callable(handler):
            runtime._installed_handlers[int(signum)] = handler
        return previous_handler

    with (
        patch.object(product_entrypoint, "_validated_source", return_value=object()),
        patch.object(
            product_entrypoint,
            "build_autonomous_product_runtime",
            return_value=runtime,
        ),
        patch.object(
            product_entrypoint,
            "_product_stop_signals",
            return_value=(int(signal.SIGTERM),),
        ),
        patch.object(
            product_entrypoint.signal,
            "getsignal",
            return_value=previous_handler,
        ),
        patch.object(
            product_entrypoint.signal,
            "signal",
            side_effect=install_handler,
        ),
        patch.object(product_entrypoint, "_print_record"),
    ):
        code = product_entrypoint.run_product(
            workspace=Path("unused"),
            source_factory="ignored:factory",
            max_cycles=1,
            poll_seconds=0,
            sleep=lambda _seconds: (_ for _ in ()).throw(
                AssertionError("bounded final cycle must not sleep")
            ),
        )

    return code, runtime


def test_signal_observed_during_final_allowed_tick_wins_terminal_stop_reason() -> None:
    code, runtime = _run_one_cycle(signal_during_tick=True)

    assert code == 128 + int(signal.SIGTERM)
    assert runtime.stop_reasons == ["signal:SIGTERM"]
    assert runtime.closed is True


def test_no_signal_final_allowed_tick_still_records_max_cycles_reached() -> None:
    code, runtime = _run_one_cycle(signal_during_tick=False)

    assert code == 0
    assert runtime.stop_reasons == ["max_cycles_reached"]
    assert runtime.closed is True
