import json
import tempfile
import unittest
from pathlib import Path

from autosport.continuous_observation import (
    ContinuousObservationConfig,
    run_continuous_observation,
)
from autosport.ingestion_health import SourceHealthStore
from autosport.providers import ProviderBatch, ProviderUnavailableError


_NOW = "2026-09-23T20:00:00+00:00"


class _SequenceProvider:
    source_id = "restart-backoff-fixture"

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = 0

    def read_batch(self, max_items: int = 1000) -> ProviderBatch:
        self.calls += 1
        if not self.steps:
            return ProviderBatch(self.source_id, (), cursor=f"empty-{self.calls}")
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def _empty_batch(cursor: str) -> ProviderBatch:
    return ProviderBatch(_SequenceProvider.source_id, (), cursor=cursor)


class ContinuousObservationRestartBackoffTests(unittest.TestCase):
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

    def test_unclean_restart_continues_provider_unavailable_backoff_streak(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            first_provider = _SequenceProvider(
                [ProviderUnavailableError("provider outage before crash")]
            )

            def crash_during_backoff(seconds: float) -> bool:
                self.assertEqual(seconds, 1.0)
                raise SystemExit("simulated process death during provider backoff")

            with self.assertRaisesRegex(
                SystemExit,
                "simulated process death during provider backoff",
            ):
                run_continuous_observation(
                    first_provider,
                    self._config(workspace),
                    ingestion_clock=lambda: _NOW,
                    monotonic=lambda: 0.0,
                    wall_clock=lambda: _NOW,
                    waiter=crash_during_backoff,
                    reporter=None,
                    run_id="run-before-crash",
                )

            self.assertEqual(first_provider.calls, 1)
            persisted_failure = SourceHealthStore(
                workspace / "source_health.json"
            ).get(_SequenceProvider.source_id)
            self.assertEqual(persisted_failure.status, "failed")
            self.assertEqual(persisted_failure.consecutive_failures, 1)
            self.assertEqual(
                persisted_failure.last_failure_kind,
                "provider_unavailable",
            )
            self.assertEqual(
                persisted_failure.consecutive_failure_kind_count,
                1,
            )

            crash_status = json.loads(
                (workspace / "continuous_observation_status.json").read_text("utf-8")
            )
            self.assertEqual(crash_status["state"], "provider_unavailable")
            self.assertEqual(crash_status["provider_unavailable_streak"], 1)
            self.assertEqual(crash_status["last_error_kind"], "provider_unavailable")

            restart_waits: list[float] = []
            second_provider = _SequenceProvider(
                [
                    ProviderUnavailableError("same provider still unavailable"),
                    _empty_batch("recovered"),
                ]
            )
            result = run_continuous_observation(
                second_provider,
                self._config(workspace),
                ingestion_clock=lambda: _NOW,
                monotonic=lambda: 0.0,
                wall_clock=lambda: _NOW,
                waiter=lambda seconds: restart_waits.append(seconds) or False,
                reporter=None,
                run_id="run-after-crash",
            )

            # One already-durable provider-unavailable failure exists before restart.
            # The next failure is therefore streak 2 and must use the 2-second
            # exponential backoff, not restart at the initial 1-second delay.
            # Restart first honors the still-active backoff from the already
            # durable first outage, then streak 2 earns the normal 2-second wait.
            self.assertEqual(restart_waits, [1.0, 2.0])
            self.assertEqual(second_provider.calls, 2)
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.successful_cycles, 1)

            recovered = SourceHealthStore(
                workspace / "source_health.json"
            ).get(_SequenceProvider.source_id)
            self.assertEqual(recovered.status, "healthy")
            self.assertEqual(recovered.consecutive_failures, 0)
            self.assertIsNone(recovered.last_failure_kind)
            self.assertEqual(recovered.consecutive_failure_kind_count, 0)

            recovered_status = json.loads(
                (workspace / "continuous_observation_status.json").read_text("utf-8")
            )
            self.assertTrue(recovered_status["previous_unclean_shutdown"])
            self.assertEqual(recovered_status["provider_unavailable_streak"], 0)
            self.assertIsNone(recovered_status["last_error_kind"])


    def test_restart_does_not_inherit_backoff_from_other_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)

            class _OtherSourceProvider(_SequenceProvider):
                source_id = "other-restart-backoff-fixture"

            first_provider = _SequenceProvider(
                [ProviderUnavailableError("source A outage")]
            )
            with self.assertRaises(SystemExit):
                run_continuous_observation(
                    first_provider,
                    self._config(workspace),
                    ingestion_clock=lambda: _NOW,
                    monotonic=lambda: 0.0,
                    wall_clock=lambda: _NOW,
                    waiter=lambda seconds: (_ for _ in ()).throw(SystemExit("crash")),
                    reporter=None,
                    run_id="source-a-before-crash",
                )

            waits: list[float] = []
            other_provider = _OtherSourceProvider(
                [
                    ProviderUnavailableError("source B outage"),
                    ProviderBatch(_OtherSourceProvider.source_id, (), cursor="recovered"),
                ]
            )
            result = run_continuous_observation(
                other_provider,
                self._config(workspace),
                ingestion_clock=lambda: _NOW,
                monotonic=lambda: 0.0,
                wall_clock=lambda: _NOW,
                waiter=lambda seconds: waits.append(seconds) or False,
                reporter=None,
                run_id="source-b-after-crash",
            )

            self.assertEqual(waits, [1.0])
            self.assertEqual(result.exit_code, 0)

    def test_restart_does_not_inherit_backoff_from_non_provider_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            first_provider = _SequenceProvider([ValueError("invalid provider payload")])
            first_result = run_continuous_observation(
                first_provider,
                self._config(workspace),
                ingestion_clock=lambda: _NOW,
                monotonic=lambda: 0.0,
                wall_clock=lambda: _NOW,
                waiter=lambda seconds: False,
                reporter=None,
                run_id="validation-failure-before-restart",
            )
            self.assertEqual(first_result.exit_code, 3)

            waits: list[float] = []
            second_provider = _SequenceProvider(
                [
                    ProviderUnavailableError("provider now unavailable"),
                    _empty_batch("recovered-after-validation-failure"),
                ]
            )
            second_result = run_continuous_observation(
                second_provider,
                self._config(workspace),
                ingestion_clock=lambda: _NOW,
                monotonic=lambda: 0.0,
                wall_clock=lambda: _NOW,
                waiter=lambda seconds: waits.append(seconds) or False,
                reporter=None,
                run_id="provider-outage-after-validation-failure",
            )

            self.assertEqual(waits, [1.0])
            self.assertEqual(second_result.exit_code, 0)


    def test_mixed_failure_history_does_not_inflate_provider_backoff_on_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)

            validation_provider = _SequenceProvider(
                [ValueError("invalid provider payload before outage")]
            )
            validation_result = run_continuous_observation(
                validation_provider,
                self._config(workspace),
                ingestion_clock=lambda: _NOW,
                monotonic=lambda: 0.0,
                wall_clock=lambda: _NOW,
                waiter=lambda seconds: False,
                reporter=None,
                run_id="validation-before-outage",
            )
            self.assertEqual(validation_result.exit_code, 3)

            before_outage = SourceHealthStore(
                workspace / "source_health.json"
            ).get(_SequenceProvider.source_id)
            self.assertEqual(before_outage.consecutive_failures, 1)
            self.assertEqual(
                before_outage.last_failure_kind,
                "provider_or_validation",
            )
            self.assertEqual(
                before_outage.consecutive_failure_kind_count,
                1,
            )

            outage_provider = _SequenceProvider(
                [ProviderUnavailableError("provider outage before second crash")]
            )

            def crash_during_outage_backoff(seconds: float) -> bool:
                self.assertEqual(seconds, 1.0)
                raise SystemExit("simulated crash after mixed failure history")

            with self.assertRaisesRegex(
                SystemExit,
                "simulated crash after mixed failure history",
            ):
                run_continuous_observation(
                    outage_provider,
                    self._config(workspace),
                    ingestion_clock=lambda: _NOW,
                    monotonic=lambda: 0.0,
                    wall_clock=lambda: _NOW,
                    waiter=crash_during_outage_backoff,
                    reporter=None,
                    run_id="outage-before-second-crash",
                )

            mixed_health = SourceHealthStore(
                workspace / "source_health.json"
            ).get(_SequenceProvider.source_id)
            self.assertEqual(mixed_health.consecutive_failures, 2)
            self.assertEqual(mixed_health.last_failure_kind, "provider_unavailable")
            self.assertEqual(mixed_health.consecutive_failure_kind_count, 1)

            mixed_status = json.loads(
                (workspace / "continuous_observation_status.json").read_text("utf-8")
            )
            self.assertEqual(mixed_status["last_error_kind"], "provider_unavailable")
            self.assertEqual(mixed_status["provider_unavailable_streak"], 1)

            restart_waits: list[float] = []
            restarted_provider = _SequenceProvider(
                [
                    ProviderUnavailableError("same outage continues after restart"),
                    _empty_batch("recovered-after-mixed-history"),
                ]
            )
            result = run_continuous_observation(
                restarted_provider,
                self._config(workspace),
                ingestion_clock=lambda: _NOW,
                monotonic=lambda: 0.0,
                wall_clock=lambda: _NOW,
                waiter=lambda seconds: restart_waits.append(seconds) or False,
                reporter=None,
                run_id="restart-after-mixed-history",
            )

            # Only the immediately preceding provider-unavailable failure belongs to
            # this backoff streak. The earlier validation failure remains durable
            # health history but must not double exponential provider backoff.
            self.assertEqual(restart_waits, [1.0, 2.0])
            self.assertEqual(result.exit_code, 0)

    def test_restart_after_backoff_deadline_calls_provider_without_startup_wait(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            first_provider = _SequenceProvider(
                [ProviderUnavailableError("provider outage before delayed restart")]
            )

            with self.assertRaises(SystemExit):
                run_continuous_observation(
                    first_provider,
                    self._config(workspace),
                    ingestion_clock=lambda: _NOW,
                    monotonic=lambda: 0.0,
                    wall_clock=lambda: _NOW,
                    waiter=lambda _seconds: (_ for _ in ()).throw(
                        SystemExit("crash during provider backoff")
                    ),
                    reporter=None,
                    run_id="before-deadline-test",
                )

            waits: list[float] = []
            restarted = _SequenceProvider(
                [
                    ProviderUnavailableError("outage after deadline"),
                    _empty_batch("recovered-after-deadline"),
                ]
            )
            result = run_continuous_observation(
                restarted,
                self._config(workspace),
                ingestion_clock=lambda: _NOW,
                monotonic=lambda: 0.0,
                wall_clock=lambda: "2026-09-23T20:00:02+00:00",
                waiter=lambda seconds: waits.append(seconds) or False,
                reporter=None,
                run_id="after-deadline-test",
            )

            # The prior streak-1 deadline was T0+1s and has already elapsed.
            # Only the newly recorded streak-2 outage waits for 2 seconds.
            self.assertEqual(waits, [2.0])
            self.assertEqual(restarted.calls, 2)
            self.assertEqual(result.exit_code, 0)

    def test_stop_during_restart_backoff_prevents_first_provider_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            first_provider = _SequenceProvider(
                [ProviderUnavailableError("provider outage before stopped restart")]
            )

            with self.assertRaises(SystemExit):
                run_continuous_observation(
                    first_provider,
                    self._config(workspace),
                    ingestion_clock=lambda: _NOW,
                    monotonic=lambda: 0.0,
                    wall_clock=lambda: _NOW,
                    waiter=lambda _seconds: (_ for _ in ()).throw(
                        SystemExit("crash during provider backoff")
                    ),
                    reporter=None,
                    run_id="before-stop-test",
                )

            waits: list[float] = []
            restarted = _SequenceProvider(
                [ProviderUnavailableError("must never be called")]
            )
            result = run_continuous_observation(
                restarted,
                self._config(workspace),
                ingestion_clock=lambda: _NOW,
                monotonic=lambda: 0.0,
                wall_clock=lambda: _NOW,
                waiter=lambda seconds: waits.append(seconds) or True,
                reporter=None,
                run_id="stopped-during-startup-backoff",
            )

            self.assertEqual(waits, [1.0])
            self.assertEqual(restarted.calls, 0)
            self.assertEqual(result.stop_reason, "operator_stop")
            self.assertEqual(result.exit_code, 0)


    def test_wall_clock_rollback_does_not_shorten_restart_backoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            first_provider = _SequenceProvider(
                [ProviderUnavailableError("provider outage before clock rollback")]
            )

            with self.assertRaises(SystemExit):
                run_continuous_observation(
                    first_provider,
                    self._config(workspace),
                    ingestion_clock=lambda: _NOW,
                    monotonic=lambda: 0.0,
                    wall_clock=lambda: _NOW,
                    waiter=lambda _seconds: (_ for _ in ()).throw(
                        SystemExit("crash during provider backoff")
                    ),
                    reporter=None,
                    run_id="before-clock-rollback",
                )

            waits: list[float] = []
            restarted = _SequenceProvider(
                [ProviderUnavailableError("must not run during rollback wait")]
            )
            result = run_continuous_observation(
                restarted,
                self._config(workspace),
                ingestion_clock=lambda: _NOW,
                monotonic=lambda: 0.0,
                wall_clock=lambda: "2026-09-23T19:59:59+00:00",
                waiter=lambda seconds: waits.append(seconds) or True,
                reporter=None,
                run_id="after-clock-rollback",
            )

            self.assertEqual(waits, [1.0])
            self.assertEqual(restarted.calls, 0)
            self.assertEqual(result.stop_reason, "operator_stop")

    def test_max_runtime_expiring_inside_restart_backoff_prevents_provider_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            first_provider = _SequenceProvider(
                [ProviderUnavailableError("provider outage before bounded restart")]
            )

            with self.assertRaises(SystemExit):
                run_continuous_observation(
                    first_provider,
                    self._config(workspace),
                    ingestion_clock=lambda: _NOW,
                    monotonic=lambda: 0.0,
                    wall_clock=lambda: _NOW,
                    waiter=lambda _seconds: (_ for _ in ()).throw(
                        SystemExit("crash during provider backoff")
                    ),
                    reporter=None,
                    run_id="before-bounded-runtime-restart",
                )

            bounded = ContinuousObservationConfig(
                workspace=workspace,
                max_cycles=2,
                max_runtime_seconds=0.5,
                interval_seconds=1,
                max_backoff_seconds=8,
                max_items=10,
            )
            waits: list[float] = []
            restarted = _SequenceProvider(
                [ProviderUnavailableError("must not run after runtime deadline")]
            )
            result = run_continuous_observation(
                restarted,
                bounded,
                ingestion_clock=lambda: _NOW,
                monotonic=lambda: 0.0,
                wall_clock=lambda: _NOW,
                waiter=lambda seconds: waits.append(seconds) or False,
                reporter=None,
                run_id="bounded-runtime-restart",
            )

            self.assertEqual(waits, [0.5])
            self.assertEqual(restarted.calls, 0)
            self.assertEqual(result.stop_reason, "max_runtime")
            self.assertEqual(result.exit_code, 0)


if __name__ == "__main__":
    unittest.main()
