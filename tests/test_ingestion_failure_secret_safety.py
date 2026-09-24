from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.continuous_observation import _redacted_error
from autosport.ingestion_health import SourceHealthStore
from autosport.providers import ProviderUnavailableError


_NOW = "2026-09-21T14:00:00+00:00"


class _HostileError(Exception):
    def __str__(self) -> str:
        raise RuntimeError("password=must-never-render")


class IngestionFailureSecretSafetyTests(unittest.TestCase):
    def test_source_health_failure_persists_redacted_diagnostic_and_truth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source_health.json"
            store = SourceHealthStore(path)
            environment_secret = "synthetic-environment-secret-7f3d"
            error = ProviderUnavailableError(
                "password=synthetic-password-91 "
                "token=synthetic-token-22 "
                "url=https://synthetic-user:synthetic-pass@example.test/feed"
                "?api_key=synthetic-query-secret-31 "
                f"environment={environment_secret}"
            )

            with patch.dict(
                "os.environ",
                {"AUTOSPORT_PARLAYAPI_KEY": environment_secret},
                clear=False,
            ):
                state = store.record_failure("fixture-source", now=_NOW, error=error)

            self.assertEqual(state.status, "failed")
            self.assertEqual(state.poll_count, 1)
            self.assertEqual(state.total_failures, 1)
            self.assertEqual(state.consecutive_failures, 1)
            self.assertEqual(state.last_error_at, _NOW)
            self.assertIsNotNone(state.last_error)
            self.assertTrue(state.last_error.startswith("ProviderUnavailableError: "))

            raw = path.read_text(encoding="utf-8")
            for secret in (
                "synthetic-password-91",
                "synthetic-token-22",
                "synthetic-user:synthetic-pass",
                "synthetic-query-secret-31",
                environment_secret,
            ):
                self.assertNotIn(secret, raw)
            self.assertIn("[REDACTED]", raw)

            reopened = SourceHealthStore(path).get("fixture-source")
            self.assertEqual(reopened.status, "failed")
            self.assertEqual(reopened.poll_count, 1)
            self.assertEqual(reopened.total_failures, 1)
            self.assertEqual(reopened.last_error_at, _NOW)
            self.assertEqual(reopened.last_error, state.last_error)

            payload = json.loads(raw)
            persisted = payload["sources"]["fixture-source"]
            self.assertEqual(persisted["last_error"], state.last_error)

    def test_source_health_failure_survives_hostile_exception_stringification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SourceHealthStore(Path(tmp) / "source_health.json")
            state = store.record_failure(
                "fixture-source",
                now=_NOW,
                error=_HostileError(),
            )

            self.assertEqual(state.status, "failed")
            self.assertEqual(state.poll_count, 1)
            self.assertEqual(state.total_failures, 1)
            self.assertEqual(
                state.last_error,
                "_HostileError: exception details unavailable",
            )

    def test_continuous_observation_uses_shared_pattern_and_explicit_redaction(self) -> None:
        environment_secret = "synthetic-environment-secret-4c8a"
        explicit_secret = "synthetic-explicit-secret-62"
        error = ValueError(
            "password=synthetic-password-13 "
            "authorization=Bearer synthetic-bearer-55 "
            f"environment={environment_secret} "
            f"opaque={explicit_secret}"
        )

        with patch.dict(
            "os.environ",
            {"AUTOSPORT_PARLAYAPI_KEY": environment_secret},
            clear=False,
        ):
            rendered = _redacted_error(error, (explicit_secret,))

        self.assertTrue(rendered.startswith("ValueError: "))
        for secret in (
            "synthetic-password-13",
            "synthetic-bearer-55",
            environment_secret,
            explicit_secret,
        ):
            self.assertNotIn(secret, rendered)
        self.assertIn("[REDACTED]", rendered)

    def test_ordinary_failure_detail_remains_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SourceHealthStore(Path(tmp) / "source_health.json")
            state = store.record_failure(
                "fixture-source",
                now=_NOW,
                error=ProviderUnavailableError("temporary upstream outage"),
            )
            self.assertEqual(
                state.last_error,
                "ProviderUnavailableError: temporary upstream outage",
            )


if __name__ == "__main__":
    unittest.main()
