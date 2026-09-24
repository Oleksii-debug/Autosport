from __future__ import annotations

import unittest

from autosport.secret_redaction import (
    REDACTED,
    redact_operator_text,
    redact_operator_value,
    safe_exception_text,
)


class ProviderSecretRedactionFalsifiers(unittest.TestCase):
    """Production-shaped falsifiers for provider credentials at operator boundaries."""

    def assert_redacted(self, rendered: str, secret: str) -> None:
        self.assertNotIn(secret, rendered)
        self.assertIn(REDACTED, rendered)

    def test_betfair_x_authentication_header_is_redacted_by_key(self) -> None:
        secret = "session-secret-804-falsifier"
        rendered = redact_operator_text("X-Authentication: " + secret)

        self.assert_redacted(rendered, secret)
        self.assertEqual(rendered, "X-Authentication: " + REDACTED)

    def test_betfair_x_application_header_is_redacted_by_key(self) -> None:
        secret = "application-secret-804-falsifier"
        rendered = redact_operator_text("X-Application: " + secret)

        self.assert_redacted(rendered, secret)
        self.assertEqual(rendered, "X-Application: " + REDACTED)

    def test_betfair_application_key_structured_value_is_redacted(self) -> None:
        secret = "application-key-804-falsifier"
        rendered = redact_operator_value({"application_key": secret, "market": "winner"})

        self.assertEqual(rendered["application_key"], REDACTED)
        self.assertEqual(rendered["market"], "winner")

    def test_quoted_json_credential_key_is_redacted_in_exception_text(self) -> None:
        secret = "json-secret-804-falsifier"
        rendered = safe_exception_text(
            RuntimeError('{"api_key":"' + secret + '","market":"winner"}')
        )

        self.assert_redacted(rendered, secret)
        self.assertIn('"api_key":"' + REDACTED + '"', rendered)
        self.assertIn('"market":"winner"', rendered)

    def test_quoted_python_repr_session_token_is_redacted_in_exception_text(self) -> None:
        secret = "repr-secret-804-falsifier"
        rendered = safe_exception_text(
            RuntimeError("{'session_token': '" + secret + "', 'market': 'winner'}")
        )

        self.assert_redacted(rendered, secret)
        self.assertIn("'session_token': '" + REDACTED + "'", rendered)
        self.assertIn("'market': 'winner'", rendered)


if __name__ == "__main__":
    unittest.main()
