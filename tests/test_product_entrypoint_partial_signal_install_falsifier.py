from __future__ import annotations

import signal
import unittest
from unittest.mock import Mock, patch

from autosport.product_entrypoint import run_product


class PartialSignalInstallRollbackTests(unittest.TestCase):
    def test_partial_signal_install_failure_restores_handlers_and_closes_runtime(self) -> None:
        runtime = Mock()
        runtime.start.side_effect = AssertionError(
            "runtime must not start after handler setup fails"
        )

        stop_signals = (int(signal.SIGINT), int(signal.SIGTERM), 21)
        previous_handlers = {signum: object() for signum in stop_signals}
        signal_calls: list[tuple[int, object]] = []

        def set_signal(signum: int, handler: object) -> object:
            signal_calls.append((signum, handler))
            if (
                signum == stop_signals[-1]
                and handler is not previous_handlers[signum]
            ):
                raise OSError("simulated SIGBREAK handler installation failure")
            return previous_handlers[signum]

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
                side_effect=lambda signum: previous_handlers[signum],
            ),
            patch(
                "autosport.product_entrypoint.signal.signal",
                side_effect=set_signal,
            ),
        ):
            with self.assertRaisesRegex(
                OSError,
                "simulated SIGBREAK handler installation failure",
            ):
                run_product(
                    workspace="unused-workspace",
                    source_factory="unused:factory",
                    max_cycles=1,
                    poll_seconds=0,
                )

        runtime.start.assert_not_called()
        runtime.close.assert_called_once_with()

        restored = signal_calls[-2:]
        self.assertEqual(
            restored,
            [
                (stop_signals[0], previous_handlers[stop_signals[0]]),
                (stop_signals[1], previous_handlers[stop_signals[1]]),
            ],
        )


if __name__ == "__main__":
    unittest.main()
