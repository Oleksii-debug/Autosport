from __future__ import annotations

import signal
import unittest
from unittest.mock import Mock, patch

from autosport.product_entrypoint import run_product


class SignalLookupFailureCleanupTests(unittest.TestCase):
    def test_getsignal_failure_closes_runtime_before_any_handler_mutation(self) -> None:
        runtime = Mock()
        runtime.start.side_effect = AssertionError(
            "runtime must not start after signal handler lookup fails"
        )

        stop_signals = (int(signal.SIGINT), int(signal.SIGTERM), 21)
        lookup_calls: list[int] = []

        def get_signal(signum: int) -> object:
            lookup_calls.append(signum)
            if signum == stop_signals[1]:
                raise OSError("simulated signal handler lookup failure")
            return object()

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
                "autosport.product_entrypoint._product_stop_signals",
                return_value=stop_signals,
            ),
            patch(
                "autosport.product_entrypoint.signal.getsignal",
                side_effect=get_signal,
            ),
            patch("autosport.product_entrypoint.signal.signal") as set_signal,
        ):
            with self.assertRaisesRegex(
                OSError,
                "simulated signal handler lookup failure",
            ):
                run_product(
                    workspace="unused-workspace",
                    source_factory="unused:factory",
                    max_cycles=1,
                    poll_seconds=0,
                )

        self.assertEqual(lookup_calls, list(stop_signals[:2]))
        set_signal.assert_not_called()
        runtime.start.assert_not_called()
        runtime.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
