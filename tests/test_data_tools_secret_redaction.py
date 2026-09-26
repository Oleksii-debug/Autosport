from __future__ import annotations

import builtins
import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from autosport import data_tools_entry


class DataToolsSecretRedactionFalsifiers(unittest.TestCase):
    def _run_expected_failure(self, detail: str) -> str:
        stderr = io.StringIO()
        with patch(
            "autosport.data_tools_entry._dispatch",
            side_effect=ValueError(detail),
        ):
            with redirect_stderr(stderr):
                result = data_tools_entry.main(["verify-dataset", "dataset-dir"])

        self.assertEqual(result, 3)
        output = stderr.getvalue()
        self.assertIn(
            "Autosport-Data: verify-dataset=FAIL_CLOSED error=ValueError",
            output,
        )
        self.assertNotIn("Traceback", output)
        return output

    def test_authorization_bearer_secret_is_not_emitted(self) -> None:
        secret = "AS-DATATOOLS-BEARER-SENTINEL-47f5"
        output = self._run_expected_failure(
            f"provider rejected request Authorization: Bearer {secret}"
        )

        self.assertNotIn(secret, output)
        self.assertNotIn(f"Bearer {secret}", output)

    def test_url_userinfo_and_sensitive_query_values_are_not_emitted(self) -> None:
        username = "operator-secret-user"
        password = "AS-DATATOOLS-PASSWORD-SENTINEL-3ad7"
        api_key = "AS-DATATOOLS-APIKEY-SENTINEL-9251"
        token = "AS-DATATOOLS-TOKEN-SENTINEL-6c2e"
        output = self._run_expected_failure(
            "provider URL failed: "
            f"https://{username}:{password}@example.invalid/feed"
            f"?api_key={api_key}&token={token}&market=tt"
        )

        self.assertNotIn(username, output)
        self.assertNotIn(password, output)
        self.assertNotIn(api_key, output)
        self.assertNotIn(token, output)

    def test_structured_provider_credential_values_are_not_emitted(self) -> None:
        api_key = "AS-DATATOOLS-STRUCTURED-APIKEY-2f81"
        session_token = "AS-DATATOOLS-SESSION-TOKEN-a340"
        output = self._run_expected_failure(
            f"provider failure api_key='{api_key}' session_token='{session_token}'"
        )

        self.assertNotIn(api_key, output)
        self.assertNotIn(session_token, output)

    def test_configured_bare_secret_value_is_not_emitted(self) -> None:
        secret = "AS-DATATOOLS-CONFIGURED-SECRET-48c2"
        with patch.dict(
            os.environ,
            {"AUTOSPORT_TEST_API_KEY": secret},
            clear=False,
        ):
            output = self._run_expected_failure(
                f"provider rejected credential value {secret}"
            )

        self.assertIn("provider rejected credential value", output)
        self.assertIn("[REDACTED]", output)
        self.assertNotIn(secret, output)

    def test_multiline_secret_diagnostic_is_flattened_after_redaction(self) -> None:
        first = "AS-DATATOOLS-MULTILINE-KEY-1"
        second = "AS-DATATOOLS-MULTILINE-TOKEN-2"
        output = self._run_expected_failure(
            f"first line api_key={first}\nsecond line token={second}"
        )

        self.assertNotIn(first, output)
        self.assertNotIn(second, output)
        self.assertNotIn("\n", output.rstrip("\n"))
        self.assertIn("first line api_key=[REDACTED]", output)
        self.assertIn("second line token=[REDACTED]", output)

    def test_exception_type_metadata_cannot_become_operator_output(self) -> None:
        secret_type_name = "AS_DATATOOLS_SECRET_TYPE_SENTINEL_81f2"
        hostile_type = type(secret_type_name, (ValueError,), {})
        exc = hostile_type("ordinary failure")

        with patch.dict(vars(builtins), {secret_type_name: hostile_type}):
            output = data_tools_entry._expected_failure_message(
                "verify-dataset",
                exc,
            )

        self.assertIn("error=ValueError", output)
        self.assertIn("ordinary failure", output)
        self.assertNotIn(secret_type_name, output)

    def test_rebound_redactor_cannot_publish_raw_secret(self) -> None:
        secret = "AS-DATATOOLS-REDISPATCH-SENTINEL-5e8a"
        exc = ValueError(f"Authorization: Bearer {secret}")

        with patch.object(
            data_tools_entry,
            "safe_exception_detail",
            lambda _exc, **_kwargs: f"Authorization: Bearer {secret}",
        ):
            output = data_tools_entry._expected_failure_message(
                "verify-dataset",
                exc,
            )

        self.assertIn("error=ExpectedFailure", output)
        self.assertIn("exception details unavailable", output)
        self.assertNotIn(secret, output)

    def test_in_place_redactor_code_swap_cannot_publish_raw_secret(self) -> None:
        secret = "AS-DATATOOLS-CODE-SWAP-SENTINEL-91ce"
        exc = ValueError(f"Authorization: Bearer {secret}")
        canonical = data_tools_entry.safe_exception_detail
        original_code = canonical.__code__

        def forged(_exc, *, unavailable_detail):
            del _exc, unavailable_detail
            return "Authorization: Bearer AS-DATATOOLS-CODE-SWAP-SENTINEL-91ce"

        try:
            canonical.__code__ = forged.__code__
            output = data_tools_entry._expected_failure_message(
                "verify-dataset",
                exc,
            )
        finally:
            canonical.__code__ = original_code

        self.assertIn("error=ExpectedFailure", output)
        self.assertIn("exception details unavailable", output)
        self.assertNotIn(secret, output)

    def test_unknown_command_never_echoes_caller_controlled_text(self) -> None:
        secret = "AS-DATATOOLS-UNKNOWN-COMMAND-SECRET-58a1"
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            result = data_tools_entry.main([secret])

        self.assertEqual(result, 2)
        output = stdout.getvalue()
        self.assertIn("Autosport-Data: unknown command", output)
        self.assertIn("Usage:", output)
        self.assertNotIn(secret, output)


if __name__ == "__main__":
    unittest.main()