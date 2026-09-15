import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autosport import ingestion as ingestion_module
from autosport.cli import run_observe_table_tennis
from autosport.parlayapi_provider import ProviderTransportError
from autosport.providers import InMemoryProvider, ProviderQuote


class _FailingProvider:
    source_id = "fixture:transport-failure"

    def read_batch(self, max_items: int = 1000):
        raise ProviderTransportError("provider HTTP 503", status_code=503)


class _FutureCommittedIngestionHealthError(RuntimeError):
    def __init__(self, outcome):
        super().__init__("market events were committed but source health persistence failed")
        self.outcome = outcome


class CliObservationIsolationTests(unittest.TestCase):
    @staticmethod
    def _provider() -> InMemoryProvider:
        return InMemoryProvider(
            "fixture:cli-isolation",
            [
                ProviderQuote(
                    provider_event_id="match-1",
                    provider_market_id="winner",
                    provider_selection_id="player-a",
                    decimal_odds=Decimal("1.80"),
                    observed_ts="2026-09-14T00:00:00+00:00",
                    sequence=1,
                )
            ],
        )

    def test_observation_does_not_open_or_rewrite_corrupt_economic_state(self):
        def factory(api_key, *, public_preview):
            self.assertIsNone(api_key)
            self.assertTrue(public_preview)
            return self._provider()

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            paper_book = workspace / "paper_book.json"
            run_registry = workspace / "run_registry.json"
            paper_book.write_bytes(b"{not-valid-paper-book-json")
            run_registry.write_bytes(b"{not-valid-run-registry-json")
            expected_book = paper_book.read_bytes()
            expected_registry = run_registry.read_bytes()

            output = io.StringIO()
            with redirect_stdout(output):
                code = run_observe_table_tennis(
                    workspace,
                    public_preview=True,
                    max_items=10,
                    show=10,
                    provider_factory=factory,
                )

            self.assertEqual(code, 0)
            self.assertIn("source=fixture:cli-isolation health=healthy", output.getvalue())
            self.assertEqual(paper_book.read_bytes(), expected_book)
            self.assertEqual(run_registry.read_bytes(), expected_registry)
            self.assertFalse((workspace / "decisions.jsonl").exists())

    def test_invalid_limits_fail_closed_before_provider_or_workspace_side_effects(self):
        provider_calls = 0

        def factory(api_key, *, public_preview):
            nonlocal provider_calls
            provider_calls += 1
            return self._provider()

        cases = (
            ("show", {"max_items": 10, "show": -1}, "show must be non-negative"),
            ("max-items", {"max_items": 0, "show": 10}, "max_items must be positive"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, limits, error_message in cases:
                with self.subTest(name=name):
                    workspace = root / name
                    output = io.StringIO()
                    with redirect_stdout(output):
                        code = run_observe_table_tennis(
                            workspace,
                            public_preview=True,
                            provider_factory=factory,
                            **limits,
                        )

                    self.assertEqual(code, 3)
                    self.assertEqual(
                        output.getvalue().strip(),
                        f"observation=FAIL_CLOSED error={error_message}",
                    )
                    self.assertFalse(workspace.exists())

        self.assertEqual(provider_calls, 0)

    def test_sqlite_failure_is_reported_fail_closed_without_traceback(self):
        def factory(api_key, *, public_preview):
            self.assertIsNone(api_key)
            self.assertTrue(public_preview)
            return self._provider()

        with tempfile.TemporaryDirectory() as tmp:
            output = io.StringIO()
            with patch(
                "autosport.cli.observe_workspace_once",
                side_effect=sqlite3.DatabaseError("file is not a database"),
            ):
                with redirect_stdout(output):
                    code = run_observe_table_tennis(
                        Path(tmp),
                        public_preview=True,
                        max_items=10,
                        show=10,
                        provider_factory=factory,
                    )

            self.assertEqual(code, 3)
            self.assertEqual(
                output.getvalue().strip(),
                "observation=FAIL_CLOSED error=file is not a database",
            )

    def test_provider_transport_failure_is_reported_fail_closed_without_traceback(self):
        def factory(api_key, *, public_preview):
            self.assertIsNone(api_key)
            self.assertTrue(public_preview)
            return _FailingProvider()

        with tempfile.TemporaryDirectory() as tmp:
            output = io.StringIO()
            with redirect_stdout(output):
                code = run_observe_table_tennis(
                    Path(tmp),
                    public_preview=True,
                    max_items=10,
                    show=10,
                    provider_factory=factory,
                )

            self.assertEqual(code, 3)
            self.assertEqual(
                output.getvalue().strip(),
                "observation=FAIL_CLOSED error=provider HTTP 503",
            )

    def test_committed_health_failure_preserves_market_commit_truth_without_traceback(self):
        def factory(api_key, *, public_preview):
            self.assertIsNone(api_key)
            self.assertTrue(public_preview)
            return self._provider()

        outcome = SimpleNamespace(
            source_id="fixture:cli-isolation",
            received=10,
            accepted=7,
            rejected=3,
            cursor="cursor-9",
            quality_flags=("INVALID_QUOTE", "STALE_SOURCE"),
        )
        committed_error = _FutureCommittedIngestionHealthError(outcome)

        with tempfile.TemporaryDirectory() as tmp:
            output = io.StringIO()
            with (
                patch(
                    "autosport.cli.ingestion_module.CommittedIngestionHealthError",
                    _FutureCommittedIngestionHealthError,
                    create=True,
                ),
                patch("autosport.cli.observe_workspace_once", side_effect=committed_error),
                redirect_stdout(output),
            ):
                code = run_observe_table_tennis(
                    Path(tmp),
                    public_preview=True,
                    max_items=10,
                    show=10,
                    provider_factory=factory,
                )

        self.assertEqual(code, 4)
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "accepted": 7,
                "cursor": "cursor-9",
                "health_repair_required": True,
                "market_committed": True,
                "observation": "COMMITTED_HEALTH_FAILURE",
                "quality_flags": ["INVALID_QUOTE", "STALE_SOURCE"],
                "received": 10,
                "rejected": 3,
                "source_health_persisted": False,
                "source_id": "fixture:cli-isolation",
                "whole_poll_retry_safe": False,
            },
        )

    @unittest.skipUnless(
        hasattr(ingestion_module, "CommittedIngestionHealthError")
        and hasattr(ingestion_module, "CommittedIngestionOutcome"),
        "requires integrated committed-ingestion health contract",
    )
    def test_integrated_committed_health_error_uses_same_cli_truth_contract(self):
        def factory(api_key, *, public_preview):
            self.assertIsNone(api_key)
            self.assertTrue(public_preview)
            return self._provider()

        outcome_type = ingestion_module.CommittedIngestionOutcome
        error_type = ingestion_module.CommittedIngestionHealthError
        outcome = outcome_type(
            source_id="fixture:cli-isolation",
            now="2026-09-15T12:00:00+00:00",
            received=10,
            accepted=7,
            rejected=3,
            elapsed_seconds=0.25,
            cursor="cursor-9",
            latest_source_ts="2026-09-15T11:59:59+00:00",
            quality_flags=("INVALID_QUOTE", "STALE_SOURCE"),
            health_before=None,
        )
        committed_error = error_type(outcome)

        with tempfile.TemporaryDirectory() as tmp:
            output = io.StringIO()
            with (
                patch("autosport.cli.observe_workspace_once", side_effect=committed_error),
                redirect_stdout(output),
            ):
                code = run_observe_table_tennis(
                    Path(tmp),
                    public_preview=True,
                    max_items=10,
                    show=10,
                    provider_factory=factory,
                )

        self.assertEqual(code, 4)
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "accepted": 7,
                "cursor": "cursor-9",
                "health_repair_required": True,
                "market_committed": True,
                "observation": "COMMITTED_HEALTH_FAILURE",
                "quality_flags": ["INVALID_QUOTE", "STALE_SOURCE"],
                "received": 10,
                "rejected": 3,
                "source_health_persisted": False,
                "source_id": "fixture:cli-isolation",
                "whole_poll_retry_safe": False,
            },
        )

    def test_unrelated_runtime_error_is_not_reclassified_as_committed_health_failure(self):
        def factory(api_key, *, public_preview):
            self.assertIsNone(api_key)
            self.assertTrue(public_preview)
            return self._provider()

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch(
                    "autosport.cli.ingestion_module.CommittedIngestionHealthError",
                    _FutureCommittedIngestionHealthError,
                    create=True,
                ),
                patch(
                    "autosport.cli.observe_workspace_once",
                    side_effect=RuntimeError("unrelated local runtime failure"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "unrelated local runtime failure"):
                    run_observe_table_tennis(
                        Path(tmp),
                        public_preview=True,
                        max_items=10,
                        show=10,
                        provider_factory=factory,
                    )


if __name__ == "__main__":
    unittest.main()
