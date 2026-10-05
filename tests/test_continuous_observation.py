import json
import sqlite3
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from autosport.continuous_observation import (
    ContinuousObservationConfig,
    main,
    run_continuous_observation,
)
from autosport.ingestion_health import SourceHealthStore
from autosport.providers import ProviderBatch, ProviderQuote, ProviderUnavailableError
from autosport.storage import SQLiteMarketStore


_NOW = "2026-09-19T17:15:00+00:00"


def _quote(*, sequence: int = 1, odds: str = "1.80") -> ProviderQuote:
    return ProviderQuote(
        provider_event_id="match-1",
        provider_market_id="winner",
        provider_selection_id="player-a",
        decimal_odds=Decimal(odds),
        observed_ts=_NOW,
        sequence=sequence,
        source_ts="2026-09-19T17:14:59+00:00",
    )


class SequenceProvider:
    source_id = "continuous-fixture"

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
        if len(step.quotes) > max_items:
            raise AssertionError("test provider batch exceeds requested bound")
        return step


def _batch(*quotes: ProviderQuote, cursor: str, flags=()) -> ProviderBatch:
    return ProviderBatch(
        SequenceProvider.source_id,
        tuple(quotes),
        cursor=cursor,
        quality_flags=tuple(flags),
    )


class ContinuousObservationTests(unittest.TestCase):
    @staticmethod
    def _config(workspace: Path, *, max_cycles: int = 2, max_items: int = 10):
        return ContinuousObservationConfig(
            workspace=workspace,
            max_cycles=max_cycles,
            max_runtime_seconds=120,
            interval_seconds=1,
            max_backoff_seconds=4,
            max_items=max_items,
        )

    @staticmethod
    def _run(provider, config, **kwargs):
        return run_continuous_observation(
            provider,
            config,
            ingestion_clock=lambda: _NOW,
            monotonic=lambda: 0.0,
            waiter=lambda _seconds: False,
            reporter=None,
            **kwargs,
        )

    def test_noncallable_provider_read_fails_before_workspace_creation(self):
        class Provider:
            source_id = "continuous-fixture"
            read_batch = object()

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "not-created"
            with self.assertRaisesRegex(TypeError, "read_batch must be callable"):
                self._run(Provider(), self._config(workspace, max_cycles=1))

            self.assertFalse(workspace.exists())

    def test_invalid_run_id_fails_before_workspace_creation(self):
        provider = SequenceProvider([_batch(_quote(), cursor="must-not-run")])

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "not-created"
            with self.assertRaisesRegex(ValueError, "run_id"):
                self._run(
                    provider,
                    self._config(workspace, max_cycles=1),
                    run_id=" invalid ",
                )

            self.assertFalse(workspace.exists())
            self.assertEqual(provider.calls, 0)

    def test_noncallable_runtime_hook_fails_before_workspace_creation(self):
        provider = SequenceProvider([_batch(_quote(), cursor="must-not-run")])

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "not-created"
            with self.assertRaisesRegex(TypeError, "waiter must be callable"):
                run_continuous_observation(
                    provider,
                    self._config(workspace, max_cycles=1),
                    ingestion_clock=lambda: _NOW,
                    monotonic=lambda: 0.0,
                    waiter=object(),
                    reporter=None,
                )

            self.assertFalse(workspace.exists())
            self.assertEqual(provider.calls, 0)

    def test_invalid_wall_clock_output_fails_before_workspace_creation(self):
        provider = SequenceProvider([_batch(_quote(), cursor="must-not-run")])

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "not-created"
            with self.assertRaisesRegex(TypeError, "wall_clock must return"):
                run_continuous_observation(
                    provider,
                    self._config(workspace, max_cycles=1),
                    ingestion_clock=lambda: _NOW,
                    monotonic=lambda: 0.0,
                    wall_clock=lambda: object(),
                    reporter=None,
                )

            self.assertFalse(workspace.exists())
            self.assertEqual(provider.calls, 0)

    def test_nonfinite_monotonic_fails_before_workspace_creation(self):
        provider = SequenceProvider([_batch(_quote(), cursor="must-not-run")])

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "not-created"
            with self.assertRaisesRegex(ValueError, "monotonic must return a finite number"):
                run_continuous_observation(
                    provider,
                    self._config(workspace, max_cycles=1),
                    ingestion_clock=lambda: _NOW,
                    monotonic=lambda: float("nan"),
                    reporter=None,
                )

            self.assertFalse(workspace.exists())
            self.assertEqual(provider.calls, 0)

    def test_late_invalid_wall_clock_never_enters_status_payload(self):
        samples = iter([_NOW, object()])
        provider = SequenceProvider([_batch(_quote(), cursor="must-not-run")])

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with self.assertRaisesRegex(TypeError, "wall_clock must return"):
                run_continuous_observation(
                    provider,
                    self._config(workspace, max_cycles=1),
                    ingestion_clock=lambda: _NOW,
                    monotonic=lambda: 0.0,
                    wall_clock=lambda: next(samples),
                    reporter=None,
                )

            self.assertEqual(provider.calls, 0)
            self.assertFalse(
                (workspace / "continuous_observation_status.json").exists()
            )

    def test_monotonic_regression_fails_closed_after_committed_cycle(self):
        samples = iter([0.0, 1.0, 0.5])
        provider = SequenceProvider([_batch(_quote(), cursor="first")])

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with self.assertRaisesRegex(ValueError, "monotonic clock must not regress"):
                run_continuous_observation(
                    provider,
                    self._config(workspace, max_cycles=2),
                    ingestion_clock=lambda: _NOW,
                    monotonic=lambda: next(samples),
                    waiter=lambda _seconds: False,
                    reporter=None,
                )

            self.assertEqual(provider.calls, 1)
            store = SQLiteMarketStore(workspace / "market.db")
            try:
                self.assertEqual(len(store.events()), 1)
            finally:
                store.close()
            status = json.loads(
                (workspace / "continuous_observation_status.json").read_text("utf-8")
            )
            self.assertEqual(status["state"], "failed")
            self.assertEqual(
                status["last_error_kind"],
                "local_startup_or_status_failure",
            )

    def test_invalid_stop_event_contract_fails_before_workspace_creation(self):
        class InvalidStopEvent:
            is_set = object()
            wait = object()

        provider = SequenceProvider([_batch(_quote(), cursor="must-not-run")])

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "not-created"
            with self.assertRaisesRegex(TypeError, "stop_event is_set must be callable"):
                run_continuous_observation(
                    provider,
                    self._config(workspace, max_cycles=1),
                    stop_event=InvalidStopEvent(),
                    ingestion_clock=lambda: _NOW,
                    monotonic=lambda: 0.0,
                    reporter=None,
                )

            self.assertFalse(workspace.exists())
            self.assertEqual(provider.calls, 0)

    def test_falsy_stop_event_is_not_replaced_by_default_event(self):
        class FalsyStopEvent:
            def __bool__(self):
                return False

            def is_set(self):
                return True

            def wait(self, _seconds):
                raise AssertionError("wait must not run when stop is already requested")

        provider = SequenceProvider([_batch(_quote(), cursor="must-not-run")])

        with tempfile.TemporaryDirectory() as tmp:
            result = run_continuous_observation(
                provider,
                self._config(Path(tmp), max_cycles=1),
                stop_event=FalsyStopEvent(),
                ingestion_clock=lambda: _NOW,
                monotonic=lambda: 0.0,
                reporter=None,
            )

            self.assertEqual(result.stop_reason, "operator_stop")
            self.assertEqual(provider.calls, 0)

    def test_falsy_waiter_is_not_replaced_by_stop_event_wait(self):
        calls = []

        class FalsyWaiter:
            def __bool__(self):
                return False

            def __call__(self, seconds):
                calls.append(seconds)
                return True

        provider = SequenceProvider(
            [
                _batch(_quote(), cursor="first"),
                _batch(_quote(sequence=2), cursor="must-not-run"),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            result = run_continuous_observation(
                provider,
                self._config(Path(tmp), max_cycles=2),
                ingestion_clock=lambda: _NOW,
                monotonic=lambda: 0.0,
                waiter=FalsyWaiter(),
                reporter=None,
            )

            self.assertEqual(result.stop_reason, "operator_stop")
            self.assertEqual(provider.calls, 1)
            self.assertEqual(calls, [1.0])

    def test_nonboolean_stop_state_fails_closed_before_provider_io(self):
        class InvalidStopEvent:
            def is_set(self):
                return object()

            def wait(self, _seconds):
                return False

        provider = SequenceProvider([_batch(_quote(), cursor="must-not-run")])

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with self.assertRaisesRegex(TypeError, "is_set must return bool"):
                run_continuous_observation(
                    provider,
                    self._config(workspace, max_cycles=1),
                    stop_event=InvalidStopEvent(),
                    ingestion_clock=lambda: _NOW,
                    monotonic=lambda: 0.0,
                    reporter=None,
                )

            self.assertEqual(provider.calls, 0)
            status = json.loads(
                (workspace / "continuous_observation_status.json").read_text("utf-8")
            )
            self.assertEqual(status["state"], "failed")

    def test_nonboolean_waiter_result_fails_closed_after_committed_cycle(self):
        provider = SequenceProvider(
            [
                _batch(_quote(), cursor="first"),
                _batch(_quote(sequence=2), cursor="must-not-run"),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with self.assertRaisesRegex(TypeError, "waiter must return bool"):
                run_continuous_observation(
                    provider,
                    self._config(workspace, max_cycles=2),
                    ingestion_clock=lambda: _NOW,
                    monotonic=lambda: 0.0,
                    waiter=lambda _seconds: object(),
                    reporter=None,
                )

            self.assertEqual(provider.calls, 1)
            store = SQLiteMarketStore(workspace / "market.db")
            try:
                self.assertEqual(len(store.events()), 1)
            finally:
                store.close()

    def test_provider_identity_substitution_fails_before_workspace_creation(self):
        class SourceId(str):
            pass

        class Provider:
            source_id = SourceId("continuous-fixture")

            def read_batch(self, max_items: int = 1000):
                raise AssertionError("provider I/O must not run")

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "not-created"
            with self.assertRaisesRegex(TypeError, "source_id must be an exact string"):
                self._run(Provider(), self._config(workspace, max_cycles=1))

            self.assertFalse(workspace.exists())

    def test_provider_reserved_source_identity_fails_before_workspace_creation(self):
        class Provider:
            source_id = "continuous|fixture"

            def read_batch(self, max_items: int = 1000):
                raise AssertionError("provider I/O must not run")

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "not-created"
            with self.assertRaisesRegex(ValueError, "source_id must be canonical"):
                self._run(Provider(), self._config(workspace, max_cycles=1))

            self.assertFalse(workspace.exists())

    def test_repeated_snapshot_is_deduplicated_without_duplicate_market_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            quote = _quote()
            provider = SequenceProvider(
                [
                    _batch(quote, cursor="snapshot-1"),
                    _batch(quote, cursor="snapshot-2"),
                ]
            )
            result = self._run(provider, self._config(workspace))

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.successful_cycles, 2)
            self.assertEqual(result.total_received, 2)
            self.assertEqual(result.total_accepted, 1)
            store = SQLiteMarketStore(workspace / "market.db")
            try:
                self.assertEqual(len(store.events()), 1)
            finally:
                store.close()

    def test_one_cycle_drains_truncated_snapshot_before_advancing_cycle_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = SequenceProvider(
                [
                    _batch(_quote(sequence=1), cursor="snapshot-a", flags=("TRUNCATED_BATCH",)),
                    _batch(_quote(sequence=2, odds="1.81"), cursor="snapshot-a"),
                ]
            )
            result = self._run(provider, self._config(Path(tmp), max_cycles=1, max_items=1))

            self.assertEqual(result.attempted_cycles, 1)
            self.assertEqual(result.successful_cycles, 1)
            self.assertEqual(result.total_received, 2)
            self.assertEqual(result.total_accepted, 2)
            self.assertEqual(provider.calls, 2)

    def test_restart_backoff_rejects_regressing_monotonic_before_provider_io(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            first_provider = SequenceProvider(
                [ProviderUnavailableError("temporary outage")]
            )
            first = self._run(
                first_provider,
                self._config(workspace, max_cycles=1),
                run_id="first-run",
            )
            self.assertEqual(first.exit_code, 4)

            second_provider = SequenceProvider(
                [_batch(_quote(), cursor="must-not-run")]
            )
            samples = iter([0.0, -1.0])
            with self.assertRaisesRegex(ValueError, "monotonic clock must not regress"):
                run_continuous_observation(
                    second_provider,
                    self._config(workspace, max_cycles=1),
                    ingestion_clock=lambda: _NOW,
                    monotonic=lambda: next(samples),
                    wall_clock=lambda: _NOW,
                    waiter=lambda _seconds: False,
                    reporter=None,
                    run_id="second-run",
                )

            self.assertEqual(second_provider.calls, 0)

    def test_provider_unavailable_uses_bounded_retry_and_can_recover(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = SequenceProvider(
                [
                    ProviderUnavailableError("temporary outage"),
                    _batch(_quote(), cursor="recovered"),
                ]
            )
            waits = []
            result = run_continuous_observation(
                provider,
                self._config(Path(tmp)),
                ingestion_clock=lambda: _NOW,
                monotonic=lambda: 0.0,
                waiter=lambda seconds: waits.append(seconds) or False,
                reporter=None,
            )

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.attempted_cycles, 2)
            self.assertEqual(result.successful_cycles, 1)
            self.assertEqual(waits, [1.0])
            status = json.loads((Path(tmp) / "continuous_observation_status.json").read_text("utf-8"))
            self.assertEqual(status["state"], "stopped")
            self.assertEqual(status["health_status"], "healthy")
            self.assertIsNone(status["last_error"])

    def test_provider_unavailable_respects_attempt_budget_and_backoff_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = SequenceProvider(
                [ProviderUnavailableError(f"outage-{index}") for index in range(4)]
            )
            waits = []
            config = ContinuousObservationConfig(
                workspace=Path(tmp),
                max_cycles=4,
                max_runtime_seconds=120,
                interval_seconds=1,
                max_backoff_seconds=2,
                max_items=10,
            )
            result = run_continuous_observation(
                provider,
                config,
                ingestion_clock=lambda: _NOW,
                monotonic=lambda: 0.0,
                waiter=lambda seconds: waits.append(seconds) or False,
                reporter=None,
            )

            self.assertEqual(result.exit_code, 4)
            self.assertEqual(result.stop_reason, "max_cycles_after_provider_unavailable")
            self.assertEqual(result.attempted_cycles, 4)
            self.assertEqual(result.successful_cycles, 0)
            self.assertEqual(provider.calls, 4)
            self.assertEqual(waits, [1.0, 2.0, 2.0])

    def test_local_durable_failure_is_terminal_and_never_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = SequenceProvider([_batch(_quote(), cursor="unused")])
            with patch(
                "autosport.continuous_observation.poll_open_market_store_once",
                side_effect=sqlite3.OperationalError("disk write failed"),
            ) as poll:
                result = self._run(provider, self._config(Path(tmp), max_cycles=5))

            self.assertEqual(result.exit_code, 5)
            self.assertEqual(result.stop_reason, "local_durable_failure")
            self.assertEqual(result.attempted_cycles, 1)
            self.assertEqual(result.successful_cycles, 0)
            poll.assert_called_once()

    def test_operator_stop_during_wait_prevents_second_provider_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = SequenceProvider(
                [
                    _batch(_quote(), cursor="first"),
                    _batch(_quote(sequence=2), cursor="must-not-run"),
                ]
            )
            result = run_continuous_observation(
                provider,
                self._config(Path(tmp), max_cycles=5),
                ingestion_clock=lambda: _NOW,
                monotonic=lambda: 0.0,
                waiter=lambda _seconds: True,
                reporter=None,
            )

            self.assertEqual(result.stop_reason, "operator_stop")
            self.assertEqual(result.successful_cycles, 1)
            self.assertEqual(provider.calls, 1)

    def test_stop_requested_inside_provider_cycle_commits_that_cycle_then_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            stop_event = threading.Event()

            class StopDuringReadProvider(SequenceProvider):
                def read_batch(self, max_items: int = 1000) -> ProviderBatch:
                    batch = super().read_batch(max_items=max_items)
                    stop_event.set()
                    return batch

            provider = StopDuringReadProvider(
                [
                    _batch(_quote(), cursor="durable-before-stop"),
                    _batch(_quote(sequence=2), cursor="must-not-run"),
                ]
            )
            result = run_continuous_observation(
                provider,
                self._config(Path(tmp), max_cycles=5),
                stop_event=stop_event,
                ingestion_clock=lambda: _NOW,
                monotonic=lambda: 0.0,
                reporter=None,
            )

            self.assertEqual(result.stop_reason, "operator_stop")
            self.assertEqual(result.successful_cycles, 1)
            self.assertEqual(result.total_accepted, 1)
            self.assertEqual(provider.calls, 1)
            store = SQLiteMarketStore(Path(tmp) / "market.db")
            try:
                self.assertEqual(len(store.events()), 1)
            finally:
                store.close()

    def test_restart_reopens_canonical_workspace_and_preserves_dedupe(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            first = self._run(
                SequenceProvider([_batch(_quote(), cursor="first")]),
                self._config(workspace, max_cycles=1),
                run_id="run-one",
            )
            second = self._run(
                SequenceProvider([_batch(_quote(), cursor="second")]),
                self._config(workspace, max_cycles=1),
                run_id="run-two",
            )

            self.assertEqual(first.total_accepted, 1)
            self.assertEqual(second.total_accepted, 0)
            store = SQLiteMarketStore(workspace / "market.db")
            try:
                self.assertEqual(len(store.events()), 1)
            finally:
                store.close()
            status = json.loads((workspace / "continuous_observation_status.json").read_text("utf-8"))
            self.assertEqual(status["run_id"], "run-two")
            self.assertEqual(status["previous_run_id"], "run-one")
            self.assertEqual(status["previous_state"], "stopped")
            self.assertFalse(status["previous_unclean_shutdown"])
            health = SourceHealthStore(workspace / "source_health.json").get(provider_source_id := SequenceProvider.source_id)
            self.assertEqual(health.source_id, provider_source_id)
            self.assertEqual(health.poll_count, 2)
            self.assertEqual(health.total_received, 2)
            self.assertEqual(health.total_accepted, 1)

    def test_status_path_cannot_overwrite_market_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            market_path = workspace / "market.db"
            sentinel = b"canonical-market-sentinel"
            market_path.write_bytes(sentinel)
            provider = SequenceProvider([_batch(_quote(), cursor="must-not-run")])
            config = ContinuousObservationConfig(
                workspace=workspace,
                max_cycles=1,
                max_runtime_seconds=120,
                interval_seconds=1,
                max_backoff_seconds=4,
                max_items=10,
                status_path=market_path,
            )

            with self.assertRaisesRegex(ValueError, "status_path must not collide"):
                self._run(provider, config)

            self.assertEqual(provider.calls, 0)
            self.assertEqual(market_path.read_bytes(), sentinel)

    def test_status_path_symlink_alias_cannot_overwrite_health_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            health_path = workspace / "source_health.json"
            health_path.write_text('{"sentinel": true}', encoding="utf-8")
            alias = workspace / "status-alias.json"
            try:
                alias.symlink_to(health_path.name)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable on this platform")

            provider = SequenceProvider([_batch(_quote(), cursor="must-not-run")])
            config = ContinuousObservationConfig(
                workspace=workspace,
                max_cycles=1,
                max_runtime_seconds=120,
                interval_seconds=1,
                max_backoff_seconds=4,
                max_items=10,
                status_path=alias,
            )

            with self.assertRaisesRegex(ValueError, "status_path must not collide"):
                self._run(provider, config)

            self.assertEqual(provider.calls, 0)
            self.assertEqual(
                health_path.read_text(encoding="utf-8"),
                '{"sentinel": true}',
            )

    def test_status_path_cannot_use_source_health_lock_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            lock_path = workspace / "source_health.json.lock"
            provider = SequenceProvider([_batch(_quote(), cursor="must-not-run")])
            config = ContinuousObservationConfig(
                workspace=workspace,
                max_cycles=1,
                max_runtime_seconds=120,
                interval_seconds=1,
                max_backoff_seconds=4,
                max_items=10,
                status_path=lock_path,
            )

            with self.assertRaisesRegex(ValueError, "status_path must not collide"):
                self._run(provider, config)

            self.assertEqual(provider.calls, 0)
            self.assertFalse(lock_path.exists())

    def test_status_path_cannot_use_sqlite_wal_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            wal_path = workspace / "market.db-wal"
            provider = SequenceProvider([_batch(_quote(), cursor="must-not-run")])
            config = ContinuousObservationConfig(
                workspace=workspace,
                max_cycles=1,
                max_runtime_seconds=120,
                interval_seconds=1,
                max_backoff_seconds=4,
                max_items=10,
                status_path=wal_path,
            )

            with self.assertRaisesRegex(ValueError, "status_path must not collide"):
                self._run(provider, config)

            self.assertEqual(provider.calls, 0)
            self.assertFalse(wal_path.exists())

    def test_status_path_cannot_enter_monotonic_authority_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            workspace = base / "workspace"
            authority_root = base / "machine-authority"
            status_path = authority_root / "operator-status.json"
            provider = SequenceProvider([_batch(_quote(), cursor="must-not-run")])
            config = ContinuousObservationConfig(
                workspace=workspace,
                max_cycles=1,
                max_runtime_seconds=120,
                interval_seconds=1,
                max_backoff_seconds=4,
                max_items=10,
                status_path=status_path,
            )

            with patch.dict(
                "os.environ",
                {"AUTOSPORT_MONOTONIC_AUTHORITY_ROOT": str(authority_root)},
                clear=False,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "status_path must not enter monotonic authority root",
                ):
                    self._run(provider, config)

            self.assertEqual(provider.calls, 0)
            self.assertFalse(status_path.exists())
            self.assertFalse(workspace.exists())

    def test_invalid_previous_status_fails_before_any_provider_io(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "continuous_observation_status.json").write_text("{broken", encoding="utf-8")
            provider = SequenceProvider([_batch(_quote(), cursor="must-not-run")])

            with self.assertRaisesRegex(ValueError, "status is unreadable"):
                self._run(provider, self._config(workspace, max_cycles=1))
            self.assertEqual(provider.calls, 0)

    def test_unprintable_cycle_error_preserves_terminal_status(self):
        class UnprintableError(Exception):
            def __str__(self):
                raise RuntimeError("stringification must not mask primary failure")

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            provider = SequenceProvider([_batch(_quote(), cursor="must-not-run")])
            with patch(
                "autosport.continuous_observation.poll_open_market_store_once",
                side_effect=UnprintableError(),
            ):
                result = self._run(provider, self._config(workspace, max_cycles=1))

            self.assertEqual(result.exit_code, 3)
            self.assertEqual(result.stop_reason, "fail_closed_provider_or_validation_error")
            status = json.loads(
                (workspace / "continuous_observation_status.json").read_text("utf-8")
            )
            self.assertEqual(status["state"], "failed")
            self.assertEqual(
                status["last_error"],
                "UnprintableError: exception details unavailable",
            )

    def test_provider_error_status_redacts_configured_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            secret = "super-secret-key"
            provider = SequenceProvider([ProviderUnavailableError(f"request failed token={secret}")])
            result = self._run(
                provider,
                self._config(workspace, max_cycles=1),
                redact_values=(secret,),
            )

            self.assertEqual(result.exit_code, 4)
            raw = (workspace / "continuous_observation_status.json").read_text("utf-8")
            self.assertNotIn(secret, raw)
            self.assertIn("[REDACTED]", raw)

    def test_reporter_runtime_failure_cannot_abort_durable_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            provider = SequenceProvider([_batch(_quote(), cursor="first")])
            calls = []

            def broken_reporter(message):
                calls.append(message)
                raise RuntimeError("display backend unavailable")

            result = run_continuous_observation(
                provider,
                self._config(workspace, max_cycles=1),
                ingestion_clock=lambda: _NOW,
                monotonic=lambda: 0.0,
                waiter=lambda _seconds: False,
                reporter=broken_reporter,
            )

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.successful_cycles, 1)
            self.assertEqual(provider.calls, 1)
            self.assertGreaterEqual(len(calls), 3)
            store = SQLiteMarketStore(workspace / "market.db")
            try:
                self.assertEqual(len(store.events()), 1)
            finally:
                store.close()
            status = json.loads(
                (workspace / "continuous_observation_status.json").read_text("utf-8")
            )
            self.assertEqual(status["state"], "stopped")

    def test_status_publication_failure_happens_before_provider_network_io(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = SequenceProvider([_batch(_quote(), cursor="must-not-run")])
            with patch(
                "autosport.continuous_observation.atomic_write_json",
                side_effect=OSError("status disk unavailable"),
            ):
                with self.assertRaisesRegex(OSError, "status disk unavailable"):
                    self._run(provider, self._config(Path(tmp), max_cycles=1))
            self.assertEqual(provider.calls, 0)

    def test_cli_requires_explicit_network_opt_in_before_provider_factory(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []

            def provider_factory(*args, **kwargs):
                calls.append((args, kwargs))
                raise AssertionError("provider factory must not run")

            code = main([tmp, "--public-preview"], provider_factory=provider_factory)
            self.assertEqual(code, 2)
            self.assertEqual(calls, [])

    def test_cli_contains_malformed_provider_contract_without_workspace_side_effect(self):
        class MalformedProvider:
            source_id = "continuous-fixture"
            read_batch = object()

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "not-created"

            def provider_factory(*_args, **_kwargs):
                return MalformedProvider()

            with patch("builtins.print") as print_mock:
                code = main(
                    [str(workspace), "--enable-network-observation", "--public-preview"],
                    provider_factory=provider_factory,
                )

            self.assertEqual(code, 5)
            self.assertFalse(workspace.exists())
            rendered = "\n".join(
                " ".join(str(arg) for arg in call.args)
                for call in print_mock.call_args_list
            )
            self.assertIn("continuous_observation=FAIL_CLOSED", rendered)
            self.assertIn("read_batch must be callable", rendered)

    def test_cli_contains_provider_factory_type_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            def provider_factory(*_args, **_kwargs):
                raise TypeError("provider construction type failure")

            with patch("builtins.print") as print_mock:
                code = main(
                    [tmp, "--enable-network-observation", "--public-preview"],
                    provider_factory=provider_factory,
                )

            self.assertEqual(code, 2)
            rendered = "\n".join(
                " ".join(str(arg) for arg in call.args)
                for call in print_mock.call_args_list
            )
            self.assertIn("continuous_observation=CONFIG_ERROR", rendered)
            self.assertIn("provider construction type failure", rendered)

    def test_cli_provider_factory_error_redacts_configured_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            secret = "super-secret-key"

            def provider_factory(*_args, **_kwargs):
                raise ValueError(f"invalid credential token={secret}")

            with patch.dict("os.environ", {"AUTOSPORT_PARLAYAPI_KEY": secret}, clear=False):
                with patch("builtins.print") as print_mock:
                    code = main(
                        [tmp, "--enable-network-observation"],
                        provider_factory=provider_factory,
                    )

            self.assertEqual(code, 2)
            rendered = "\n".join(
                " ".join(str(arg) for arg in call.args)
                for call in print_mock.call_args_list
            )
            self.assertNotIn(secret, rendered)
            self.assertIn("[REDACTED]", rendered)

    def test_config_rejects_zero_interval_to_prevent_tight_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "interval_seconds"):
                ContinuousObservationConfig(workspace=Path(tmp), interval_seconds=0)


if __name__ == "__main__":
    unittest.main()
