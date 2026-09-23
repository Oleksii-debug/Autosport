import io
import os
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from autosport import data_tools_entry


class DataToolsEntrySecretRedactionTests(unittest.TestCase):
    def _run_expected_failure(self, message: str) -> str:
        stderr = io.StringIO()
        with patch.object(
            data_tools_entry,
            "_dispatch",
            side_effect=ValueError(message),
        ):
            with redirect_stderr(stderr):
                result = data_tools_entry.main(["verify-dataset", "dataset"])
        self.assertEqual(result, 3)
        self.assertNotIn("Traceback", stderr.getvalue())
        return stderr.getvalue().strip()

    def test_expected_failure_redacts_url_query_bearer_and_key_value_secrets(self):
        rendered = self._run_expected_failure(
            "provider failed "
            "https://alice:super-password@example.test/path?api_key=query-secret "
            "Authorization: Bearer bearer-secret password=plain-secret"
        )

        self.assertIn("provider failed", rendered)
        self.assertIn("[REDACTED]", rendered)
        self.assertNotIn("alice:super-password", rendered)
        self.assertNotIn("query-secret", rendered)
        self.assertNotIn("bearer-secret", rendered)
        self.assertNotIn("plain-secret", rendered)

    def test_expected_failure_redacts_bare_configured_secret_value(self):
        secret = "configured-secret-48c2"
        with patch.dict(
            os.environ,
            {"AUTOSPORT_TEST_API_KEY": secret},
            clear=False,
        ):
            rendered = self._run_expected_failure(
                f"provider rejected credential value {secret}"
            )

        self.assertIn("provider rejected credential value", rendered)
        self.assertIn("[REDACTED]", rendered)
        self.assertNotIn(secret, rendered)

    def test_expected_failure_keeps_one_line_shape_after_redaction(self):
        rendered = self._run_expected_failure(
            "first line api_key=secret-one\nsecond line token=secret-two"
        )

        self.assertNotIn("\n", rendered)
        self.assertIn("first line api_key=[REDACTED]", rendered)
        self.assertIn("second line token=[REDACTED]", rendered)
        self.assertNotIn("secret-one", rendered)
        self.assertNotIn("secret-two", rendered)

    def test_expected_failure_preserves_non_secret_context(self):
        rendered = self._run_expected_failure(
            "dataset manifest checksum mismatch at row 7"
        )

        self.assertEqual(
            rendered,
            "Autosport-Data: verify-dataset=FAIL_CLOSED error=ValueError: "
            "dataset manifest checksum mismatch at row 7",
        )


if __name__ == "__main__":
    unittest.main()
