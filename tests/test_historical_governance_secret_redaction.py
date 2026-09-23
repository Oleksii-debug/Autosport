import io
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from autosport import historical_governance


class HistoricalGovernanceSecretRedactionTests(unittest.TestCase):
    def _run_failure(self, entrypoint, message: str) -> str:
        stdout = io.StringIO()
        with patch.object(
            historical_governance,
            "_require_bound_governance",
            side_effect=ValueError(message),
        ):
            with redirect_stdout(stdout):
                result = entrypoint(["--governance-proof", "unused.json"])
        self.assertEqual(result, 3)
        self.assertNotIn("Traceback", stdout.getvalue())
        return stdout.getvalue().strip()

    def test_corpus_failure_redacts_url_query_bearer_and_key_value_secrets(self):
        rendered = self._run_failure(
            historical_governance.corpus_main,
            "authority fetch failed "
            "https://alice:super-password@example.test/proof?api_key=query-secret "
            "Authorization: Bearer bearer-secret password=plain-secret",
        )

        self.assertTrue(rendered.startswith("historical_corpus=FAIL_CLOSED error="))
        self.assertIn("authority fetch failed", rendered)
        self.assertIn("[REDACTED]", rendered)
        self.assertNotIn("alice:super-password", rendered)
        self.assertNotIn("query-secret", rendered)
        self.assertNotIn("bearer-secret", rendered)
        self.assertNotIn("plain-secret", rendered)

    def test_bundle_failure_redacts_configured_bare_secret_value(self):
        secret = "configured-governance-secret-48c2"
        with patch.dict(
            os.environ,
            {"AUTOSPORT_TEST_API_KEY": secret},
            clear=False,
        ):
            rendered = self._run_failure(
                historical_governance.bundle_corpus_main,
                f"authority provider rejected credential {secret}",
            )

        self.assertTrue(
            rendered.startswith("historical_bundle_corpus=FAIL_CLOSED error=")
        )
        self.assertIn("authority provider rejected credential", rendered)
        self.assertIn("[REDACTED]", rendered)
        self.assertNotIn(secret, rendered)

    def test_failure_remains_one_line_after_redaction(self):
        rendered = self._run_failure(
            historical_governance.corpus_main,
            "first line api_key=secret-one\nsecond line token=secret-two",
        )

        self.assertNotIn("\n", rendered)
        self.assertIn("first line api_key=[REDACTED]", rendered)
        self.assertIn("second line token=[REDACTED]", rendered)
        self.assertNotIn("secret-one", rendered)
        self.assertNotIn("secret-two", rendered)

    def test_non_secret_failure_context_is_preserved(self):
        rendered = self._run_failure(
            historical_governance.corpus_main,
            "governance proof schema_version must be 1",
        )

        self.assertEqual(
            rendered,
            "historical_corpus=FAIL_CLOSED error="
            "governance proof schema_version must be 1",
        )

    def test_broken_exception_stringifier_fails_closed_without_traceback(self):
        class BrokenStringValueError(ValueError):
            def __str__(self):
                raise RuntimeError("diagnostic formatter failed")

        stdout = io.StringIO()
        with patch.object(
            historical_governance,
            "_require_bound_governance",
            side_effect=BrokenStringValueError(),
        ):
            with redirect_stdout(stdout):
                result = historical_governance.corpus_main(
                    ["--governance-proof", "unused.json"]
                )

        self.assertEqual(result, 3)
        self.assertEqual(
            stdout.getvalue().strip(),
            "historical_corpus=FAIL_CLOSED error=exception details unavailable",
        )
        self.assertNotIn("Traceback", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
