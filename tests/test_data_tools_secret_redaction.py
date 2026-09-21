from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
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


if __name__ == "__main__":
    unittest.main()
