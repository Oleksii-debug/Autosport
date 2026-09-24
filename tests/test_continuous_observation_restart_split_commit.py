import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import autosport.continuous_observation as subject
from autosport.continuous_observation import (
    ContinuousObservationConfig,
    run_continuous_observation,
)
from autosport.ingestion_health import SourceHealthStore
from autosport.providers import ProviderBatch, ProviderUnavailableError


_NOW = "2026-09-23T21:30:00+00:00"


class _SplitCommitProvider:
    source_id = "split-commit-provider"

    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = 0

    def read_batch(self, max_items: int = 1000) -> ProviderBatch:
        self.calls += 1
        if not self.steps:
            raise AssertionError("unexpected provider call")
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def _empty_batch() -> ProviderBatch:
    return ProviderBatch(
        _SplitCommitProvider.source_id,
        (),
        cursor="recovered",
    )


class ContinuousObservationSplitCommitRestartTests(unittest.TestCase):
    @staticmethod
    def _config(workspace: Path) -> ContinuousObservationConfig:
        return ContinuousObservationConfig(
            workspace=workspace,
            max_cycles=2,
            max_runtime_seconds=120,
            interval_seconds=1,
            max_backoff_seconds=8,
            max_items=10,
        )

    def test_crash_after_health_failure_before_typed_status_keeps_backoff_streak(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            original_publish = subject._publish_status

            def crash_before_typed_failure_status(path, payload, *, reporter):
                if payload["state"] == "provider_unavailable":
                    raise SystemExit(
                        "simulated process death after health commit before typed status"
                    )
                return original_publish(path, payload, reporter=reporter)

            first = _SplitCommitProvider(
                ProviderUnavailableError("provider outage before split-commit crash")
            )
            with patch.object(
                subject,
                "_publish_status",
                crash_before_typed_failure_status,
            ):
                with self.assertRaisesRegex(
                    SystemExit,
                    "after health commit before typed status",
                ):
                    run_continuous_observation(
                        first,
                        self._config(workspace),
                        ingestion_clock=lambda: _NOW,
                        monotonic=lambda: 0.0,
                        wall_clock=lambda: _NOW,
                        waiter=lambda _seconds: False,
                        reporter=None,
                        run_id="before-split-commit-crash",
                    )

            self.assertEqual(first.calls, 1)
            durable_health = SourceHealthStore(
                workspace / "source_health.json"
            ).get(_SplitCommitProvider.source_id)
            self.assertEqual(durable_health.status, "failed")
            self.assertEqual(durable_health.consecutive_failures, 1)
            self.assertEqual(
                durable_health.last_failure_kind,
                "provider_unavailable",
            )
            self.assertEqual(
                durable_health.consecutive_failure_kind_count,
                1,
            )

            stale_status = json.loads(
                (workspace / "continuous_observation_status.json").read_text("utf-8")
            )
            self.assertEqual(stale_status["state"], "attempting")
            self.assertEqual(stale_status["provider_unavailable_streak"], 0)
            self.assertIsNone(stale_status["last_error_kind"])

            waits: list[float] = []
            restarted = _SplitCommitProvider(
                ProviderUnavailableError("same provider outage after restart"),
                _empty_batch(),
            )
            result = run_continuous_observation(
                restarted,
                self._config(workspace),
                ingestion_clock=lambda: _NOW,
                monotonic=lambda: 0.0,
                wall_clock=lambda: _NOW,
                waiter=lambda seconds: waits.append(seconds) or False,
                reporter=None,
                run_id="after-split-commit-crash",
            )

            # The first provider-unavailable transition is already durable in
            # SourceHealthStore. Restart must first honor the remaining streak-1
            # deadline; the next outage is streak 2 and earns the 2-second wait.
            self.assertEqual(waits, [1.0, 2.0])
            self.assertEqual(restarted.calls, 2)
            self.assertEqual(result.exit_code, 0)


if __name__ == "__main__":
    unittest.main()
