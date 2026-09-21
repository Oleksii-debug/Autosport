from __future__ import annotations

import io
import signal
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autosport.product_entrypoint import run_product


@dataclass(frozen=True)
class _Record:
    state: str
    reason: str | None = None
    cycle_index: int = 0


class _Runtime:
    def __init__(self, *, on_tick=None, on_stop=None) -> None:
        self.workspace = Path("signal-stop-test")
        self.manifest = SimpleNamespace(source_id="provider-a")
        self.on_tick = on_tick
        self.on_stop = on_stop
        self.stop_reasons: list[str] = []
        self.closed = False

    def start(self) -> _Record:
        return _Record(state="RUNNING")

    def tick(self) -> _Record:
        if self.on_tick is not None:
            self.on_tick()
        return _Record(state="RUNNING", cycle_index=1)

    def stop(self, reason: str) -> _Record:
        self.stop_reasons.append(reason)
        if self.on_stop is not None:
            self.on_stop(reason)
        return _Record(state="STOPPED", reason=reason, cycle_index=1)

    def close(self) -> None:
        self.closed = True


def _run(runtime: _Runtime, handlers: dict[signal.Signals, object]) -> int:
    def fake_signal(signum, handler):
        handlers[signum] = handler
        return None

    with (
        patch(
            "autosport.product_entrypoint._validated_source",
            return_value=object(),
        ),
        patch(
            "autosport.product_entrypoint.build_autonomous_product_runtime",
            return_value=runtime,
        ),
        patch(
            "autosport.product_entrypoint.signal.getsignal",
            return_value=signal.SIG_DFL,
        ),
        patch(
            "autosport.product_entrypoint.signal.signal",
            side_effect=fake_signal,
        ),
        patch("sys.stdout", new=io.StringIO()),
    ):
        return run_product(
            workspace="signal-stop-test",
            source_factory="unused:factory",
            max_cycles=1,
            poll_seconds=0,
            sleep=lambda _: (_ for _ in ()).throw(AssertionError("must not sleep")),
        )


def test_signal_during_final_tick_wins_over_max_cycles() -> None:
    handlers: dict[signal.Signals, object] = {}
    runtime = _Runtime(
        on_tick=lambda: handlers[signal.SIGTERM](signal.SIGTERM, None)
    )

    code = _run(runtime, handlers)

    assert code == 128 + signal.SIGTERM
    assert runtime.stop_reasons == ["signal:SIGTERM"]
    assert runtime.closed is True


def test_signal_inside_max_cycles_stop_does_not_rewrite_exit_code() -> None:
    handlers: dict[signal.Signals, object] = {}

    def on_stop(reason: str) -> None:
        assert reason == "max_cycles_reached"
        handlers[signal.SIGTERM](signal.SIGTERM, None)

    runtime = _Runtime(on_stop=on_stop)

    code = _run(runtime, handlers)

    assert code == 0
    assert runtime.stop_reasons == ["max_cycles_reached"]


def test_later_signal_cannot_split_chosen_signal_reason_and_exit_code() -> None:
    handlers: dict[signal.Signals, object] = {}

    def on_tick() -> None:
        handlers[signal.SIGINT](signal.SIGINT, None)

    def on_stop(reason: str) -> None:
        assert reason == "signal:SIGINT"
        handlers[signal.SIGTERM](signal.SIGTERM, None)

    runtime = _Runtime(on_tick=on_tick, on_stop=on_stop)

    code = _run(runtime, handlers)

    assert code == 128 + signal.SIGINT
    assert runtime.stop_reasons == ["signal:SIGINT"]


def test_plain_max_cycles_completion_keeps_zero_exit_code() -> None:
    handlers: dict[signal.Signals, object] = {}
    runtime = _Runtime()

    code = _run(runtime, handlers)

    assert code == 0
    assert runtime.stop_reasons == ["max_cycles_reached"]
