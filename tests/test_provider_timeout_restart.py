import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.continuous_observation import (
    ContinuousObservationConfig,
    run_continuous_observation,
)
from autosport.providers import ProviderBatch, ProviderQuote, ProviderUnavailableError
from autosport.storage import SQLiteMarketStore


_TIMEOUT_AT = "2026-09-21T08:00:00+00:00"
_RECOVERED_AT = "2026-09-21T08:00:02+00:00"


def _quote() -> ProviderQuote:
    return ProviderQuote(
        provider_event_id="match-1",
        provider_market_id="winner",
        provider_selection_id="player-a",
        decimal_odds=Decimal("1.80"),
        observed_ts=_RECOVERED_AT,
        sequence=1,
        source_ts="2026-09-21T08:00:01+00:00",
    )


class _SequenceProvider:
    source_id = "timeout-restart-fixture"

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = 0

    def read_batch(self, max_items: int = 1000) -> ProviderBatch:
        self.calls += 1
        if not self.steps:
            raise AssertionError("test provider exhausted unexpectedly")
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        if len(step.quotes) > max_items:
            raise AssertionError("test provider batch exceeds requested bound")
        return step


class _CrashAtBackoff:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> bool:
        self.calls.append(seconds)
        raise SystemExit("simulated abrupt termination at provider backoff boundary")


class ProviderTimeoutRestartTests(unittest.TestCase):
    @staticmethod
    def _config(workspace: Path, *, max_cycles: int) -> ContinuousObservationConfig:
        return ContinuousObservationConfig(
            workspace=workspace,
            max_cycles=max_cycles,
            max_runtime_seconds=120,
            interval_seconds=1,
            max_backoff_seconds=4,
            max_items=10,
        )

    def test_provider_timeout_crash_then_restart_preserves_unclean_boundary_and_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            failing_provider = _SequenceProvider(
                [ProviderUnavailableError("temporary provider outage")]
            )
            crash = _CrashAtBackoff()

            with self.assertRaisesRegex(
                SystemExit,
                "simulated abrupt termination at provider backoff boundary",
            ):
                run_continuous_observation(
                    failing_provider,
                    self._config(workspace, max_cycles=2),
                    ingestion_clock=lambda: _TIMEOUT_AT,
                    monotonic=lambda: 0.0,
                    wall_clock=lambda: _TIMEOUT_AT,
                    waiter=crash,
                    reporter=None,
                    run_id="run-timeout",
                )

            self.assertEqual(failing_provider.calls, 1)
            self.assertEqual(crash.calls, [1.0])

            status_path = workspace / "continuous_observation_status.json"
            crashed_status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(crashed_status["run_id"], "run-timeout")
            self.assertEqual(crashed_status["state"], "provider_unavailable")
            self.assertEqual(crashed_status["attempted_cycles"], 1)
            self.assertEqual(crashed_status["successful_cycles"], 0)
            self.assertEqual(crashed_status["provider_unavailable_streak"], 1)
            self.assertEqual(crashed_status["last_error_kind"], "provider_unavailable")
            self.assertFalse(crashed_status["previous_unclean_shutdown"])

            recovered_provider = _SequenceProvider(
                [
                    ProviderBatch(
                        _SequenceProvider.source_id,
                        (_quote(),),
                        cursor="recovered",
                    )
                ]
            )
            recovered = run_continuous_observation(
                recovered_provider,
                self._config(workspace, max_cycles=1),
                ingestion_clock=lambda: _RECOVERED_AT,
                monotonic=lambda: 0.0,
                wall_clock=lambda: _RECOVERED_AT,
                waiter=lambda _seconds: False,
                reporter=None,
                run_id="run-recovered",
            )

            self.assertEqual(recovered.exit_code, 0)
            self.assertEqual(recovered.stop_reason, "max_cycles")
            self.assertEqual(recovered.attempted_cycles, 1)
            self.assertEqual(recovered.successful_cycles, 1)
            self.assertEqual(recovered.total_received, 1)
            self.assertEqual(recovered.total_accepted, 1)
            self.assertEqual(recovered_provider.calls, 1)

            restarted_status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(restarted_status["run_id"], "run-recovered")
            self.assertEqual(restarted_status["state"], "stopped")
            self.assertEqual(restarted_status["previous_run_id"], "run-timeout")
            self.assertEqual(restarted_status["previous_state"], "provider_unavailable")
            self.assertTrue(restarted_status["previous_unclean_shutdown"])
            self.assertEqual(restarted_status["provider_unavailable_streak"], 0)
            self.assertEqual(restarted_status["health_status"], "healthy")
            self.assertIsNone(restarted_status["last_error_kind"])
            self.assertIsNone(restarted_status["last_error"])

            store = SQLiteMarketStore(workspace / "market.db")
            try:
                events = store.events()
            finally:
                store.close()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].sequence, 1)


if __name__ == "__main__":
    unittest.main()
