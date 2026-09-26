import io
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from autosport import strategy_comparison


class StrategyComparisonSecretRedactionTests(unittest.TestCase):
    @staticmethod
    def _args() -> SimpleNamespace:
        return SimpleNamespace(
            run_summaries=[Path("baseline.json"), Path("challenger.json")],
            baseline_strategy=None,
            output=Path("comparison.json"),
        )

    def _run_failure(self, exc: OSError | ValueError) -> str:
        parser = MagicMock()
        parser.parse_args.return_value = self._args()
        stdout = io.StringIO()
        with patch.object(strategy_comparison, "build_parser", return_value=parser):
            with patch.object(
                strategy_comparison,
                "load_strategy_run_summary",
                side_effect=exc,
            ):
                with redirect_stdout(stdout):
                    result = strategy_comparison.main([])
        self.assertEqual(result, 2)
        self.assertNotIn("Traceback", stdout.getvalue())
        return stdout.getvalue().strip()

    def test_failure_redacts_url_query_bearer_and_key_value_secrets(self):
        rendered = self._run_failure(
            ValueError(
                "comparison input failed "
                "https://alice:super-password@example.test/run?api_key=query-secret "
                "Authorization: Bearer bearer-secret password=plain-secret"
            )
        )

        self.assertTrue(rendered.startswith("strategy_comparison=FAIL_CLOSED error="))
        self.assertIn("comparison input failed", rendered)
        self.assertIn("[REDACTED]", rendered)
        self.assertNotIn("alice:super-password", rendered)
        self.assertNotIn("query-secret", rendered)
        self.assertNotIn("bearer-secret", rendered)
        self.assertNotIn("plain-secret", rendered)

    def test_failure_redacts_configured_bare_secret_value(self):
        secret = "configured-comparison-secret-48c2"
        with patch.dict(
            os.environ,
            {"AUTOSPORT_TEST_API_KEY": secret},
            clear=False,
        ):
            rendered = self._run_failure(
                OSError(f"comparison source rejected credential {secret}")
            )

        self.assertIn("comparison source rejected credential", rendered)
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
            ValueError("strategy comparison requires at least two run summaries")
        )

        self.assertEqual(
            rendered,
            "strategy_comparison=FAIL_CLOSED error="
            "strategy comparison requires at least two run summaries",
        )

    def test_broken_exception_stringifier_fails_closed_without_traceback(self):
        class BrokenStringValueError(ValueError):
            def __str__(self):
                raise RuntimeError("diagnostic formatter failed")

        rendered = self._run_failure(BrokenStringValueError())

        self.assertEqual(
            rendered,
            "strategy_comparison=FAIL_CLOSED error=exception details unavailable",
        )


if __name__ == "__main__":
    unittest.main()
