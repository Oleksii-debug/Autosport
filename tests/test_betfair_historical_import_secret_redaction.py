import io
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from autosport import betfair_historical_read_once


class BetfairHistoricalImportSecretRedactionTests(unittest.TestCase):
    @staticmethod
    def _args() -> SimpleNamespace:
        return SimpleNamespace(
            inputs=[Path("input.bz2")],
            output_dir=Path("dataset"),
            acquired_at="2026-01-01T00:00:00Z",
            terms_reference="https://example.test/terms",
            retention_basis="user-supplied local archive",
            redistribution_policy="prohibited",
            dataset_name="betfair-table-tennis-history",
            market_types=None,
        )

    def _run_failure(self, exc: OSError | ValueError) -> str:
        parser = MagicMock()
        parser.parse_args.return_value = self._args()
        stdout = io.StringIO()
        with patch.object(
            betfair_historical_read_once,
            "build_parser",
            return_value=parser,
        ):
            with patch.object(
                betfair_historical_read_once,
                "import_betfair_historical_read_once",
                side_effect=exc,
            ):
                with redirect_stdout(stdout):
                    result = betfair_historical_read_once.main([])
        self.assertEqual(result, 3)
        self.assertNotIn("Traceback", stdout.getvalue())
        return stdout.getvalue().strip()

    def test_failure_redacts_url_query_bearer_and_key_value_secrets(self):
        rendered = self._run_failure(
            ValueError(
                "Betfair import failed "
                "https://alice:super-password@example.test/archive?api_key=query-secret "
                "Authorization: Bearer bearer-secret password=plain-secret"
            )
        )

        self.assertTrue(
            rendered.startswith("betfair_historical_import=FAIL_CLOSED error=")
        )
        self.assertIn("Betfair import failed", rendered)
        self.assertIn("[REDACTED]", rendered)
        self.assertNotIn("alice:super-password", rendered)
        self.assertNotIn("query-secret", rendered)
        self.assertNotIn("bearer-secret", rendered)
        self.assertNotIn("plain-secret", rendered)

    def test_failure_redacts_configured_bare_secret_value(self):
        secret = "configured-betfair-secret-48c2"
        with patch.dict(
            os.environ,
            {"AUTOSPORT_TEST_API_KEY": secret},
            clear=False,
        ):
            rendered = self._run_failure(
                OSError(f"archive provider rejected credential {secret}")
            )

        self.assertIn("archive provider rejected credential", rendered)
        self.assertIn("[REDACTED]", rendered)
        self.assertNotIn(secret, rendered)

    def test_failure_remains_one_line_after_redaction(self):
        rendered = self._run_failure(
            ValueError("first line api_key=secret-one\nsecond line token=secret-two")
        )

        self.assertNotIn("\n", rendered)
        self.assertIn("first line api_key=[REDACTED]", rendered)
        self.assertIn("second line token=[REDACTED]", rendered)
        self.assertNotIn("secret-one", rendered)
        self.assertNotIn("secret-two", rendered)

    def test_non_secret_failure_context_is_preserved(self):
        rendered = self._run_failure(
            ValueError("Betfair historical compressed input is truncated")
        )

        self.assertEqual(
            rendered,
            "betfair_historical_import=FAIL_CLOSED error="
            "Betfair historical compressed input is truncated",
        )

    def test_broken_exception_stringifier_fails_closed_without_traceback(self):
        class BrokenStringValueError(ValueError):
            def __str__(self):
                raise RuntimeError("diagnostic formatter failed")

        rendered = self._run_failure(BrokenStringValueError())

        self.assertEqual(
            rendered,
            "betfair_historical_import=FAIL_CLOSED error=exception details unavailable",
        )


if __name__ == "__main__":
    unittest.main()
