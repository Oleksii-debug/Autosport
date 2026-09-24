from __future__ import annotations

import signal
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autosport.product_entrypoint import run_product


class _Runtime:
    def __init__(self) -> None:
        self.workspace = Path("ignored-workspace")
        self.manifest = SimpleNamespace(source_id="provider-a")
        self.tick_completed = threading.Event()
        self.stop_reasons: list[str] = []
        self.close_calls = 0

    def start(self) -> object:
        return object()

    def tick(self) -> object:
        self.tick_completed.set()
        return object()

    def stop(self, reason: str) -> object:
        self.stop_reasons.append(reason)
        return object()

    def close(self) -> None:
        self.close_calls += 1


class ProductEntrypointSignalIdleWaitPreemptionTests(unittest.TestCase):
    def test_signal_stop_preempts_idle_poll_wait(self) -> None:
        runtime = _Runtime()
        signal_sent = threading.Event()

        def send_signal_during_idle_wait() -> None:
            self.assertTrue(runtime.tick_completed.wait(timeout=1.0))
            # Give the main thread enough time to enter the poll wait after publishing
            # the completed tick. The poll interval is intentionally much larger than
            # this delay so the test distinguishes prompt STOP from a full resumed sleep.
            time.sleep(0.05)
            signal.raise_signal(signal.SIGINT)
            signal_sent.set()

        sender = threading.Thread(target=send_signal_during_idle_wait, daemon=True)
        sender.start()
        started_at = time.monotonic()
        try:
            with (
                patch(
                    "autosport.product_entrypoint._validated_source",
                    return_value=object(),
                ),
                patch(
                    "autosport.product_entrypoint.build_autonomous_product_runtime",
                    return_value=runtime,
                ),
                patch("autosport.product_entrypoint._print_record"),
            ):
                exit_code = run_product(
                    workspace=Path("ignored-workspace"),
                    source_factory="ignored:factory",
                    initial_bankroll="100",
                    max_cycles=None,
                    poll_seconds=2.0,
                    install_signal_handlers=True,
                )
        finally:
            sender.join(timeout=1.0)

        elapsed = time.monotonic() - started_at
        self.assertTrue(signal_sent.is_set())
        self.assertFalse(sender.is_alive())
        self.assertEqual(exit_code, 128 + int(signal.SIGINT))
        self.assertEqual(runtime.stop_reasons, ["signal:SIGINT"])
        self.assertEqual(runtime.close_calls, 1)
        self.assertLess(
            elapsed,
            1.0,
            "operator signal STOP remained blocked by the full idle poll interval",
        )


if __name__ == "__main__":
    unittest.main()
