import unittest

from autosport.live_observation import OneShotObservationWorker


class _BrokenStringError(RuntimeError):
    def __str__(self) -> str:
        raise RuntimeError("stringification failed")


class LiveObservationSecretRedactionTests(unittest.TestCase):
    def _run_failure(self, exc: BaseException) -> str:
        worker = OneShotObservationWorker()

        def fail():
            raise exc

        self.assertTrue(worker.start(fail))
        self.assertIsNotNone(worker._thread)
        worker._thread.join(timeout=1)
        self.assertFalse(worker._thread.is_alive())
        message = worker.poll()
        self.assertIsNotNone(message)
        self.assertIsNone(message.result)
        self.assertIsNotNone(message.error)
        self.assertFalse(worker.busy)
        return message.error

    def test_redacts_environment_style_api_key_without_hiding_diagnostic_context(self):
        error = self._run_failure(
            RuntimeError(
                'provider rejected AUTOSPORT_PARLAYAPI_KEY="super-secret-key" market=tt-123'
            )
        )

        self.assertEqual(
            error,
            "RuntimeError: provider rejected AUTOSPORT_PARLAYAPI_KEY=[REDACTED] market=tt-123",
        )
        self.assertNotIn("super-secret-key", error)

    def test_redacts_authorization_header_and_url_userinfo(self):
        error = self._run_failure(
            RuntimeError(
                "request failed Authorization: Bearer bearer-secret "
                "url=https://alice:password123@example.test/markets"
            )
        )

        self.assertIn("Authorization: Bearer [REDACTED]", error)
        self.assertIn("https://[REDACTED]@example.test/markets", error)
        self.assertNotIn("bearer-secret", error)
        self.assertNotIn("password123", error)

    def test_redacts_query_parameter_secret_but_preserves_neighboring_fields(self):
        error = self._run_failure(
            RuntimeError("GET /markets?api_key=query-secret&market=1.234")
        )

        self.assertIn("api_key=[REDACTED]&market=1.234", error)
        self.assertNotIn("query-secret", error)

    def test_ordinary_failure_text_is_preserved(self):
        self.assertEqual(
            self._run_failure(RuntimeError("network unavailable")),
            "RuntimeError: network unavailable",
        )

    def test_broken_exception_stringification_still_publishes_terminal_message(self):
        self.assertEqual(self._run_failure(_BrokenStringError()), "_BrokenStringError")


if __name__ == "__main__":
    unittest.main()
