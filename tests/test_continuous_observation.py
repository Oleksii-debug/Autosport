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

    def test_invalid_previous_status_fails_before_any_provider_io(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "continuous_observation_status.json").write_text("{broken", encoding="utf-8")
            provider = SequenceProvider([_batch(_quote(), cursor="must-not-run")])

            with self.assertRaisesRegex(ValueError, "status is unreadable"):
                self._run(provider, self._config(workspace, max_cycles=1))
            self.assertEqual(provider.calls, 0)

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
