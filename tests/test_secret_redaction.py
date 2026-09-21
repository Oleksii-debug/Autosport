from __future__ import annotations

import os
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest import mock

from autosport.dataset_worker import _safe_worker_error
from autosport import historical_acquisition
from autosport.diagnostic import _render_exception
from autosport.live_observation import OneShotObservationWorker
from autosport.replay_worker import _terminal_error
from autosport.secret_redaction import (
    REDACTED,
    is_sensitive_key,
    redact_operator_text,
    redact_operator_value,
    safe_exception_text,
)


class SecretRedactionTests(unittest.TestCase):
    def test_sensitive_key_detection_is_case_and_separator_insensitive(self) -> None:
        for key in (
            "api_key",
            "Api-Key",
            "X-API-Key",
            "AUTOSPORT_PARLAYAPI_KEY",
            "accessToken",
            "REFRESH_TOKEN",
            "password",
            "Authorization",
            "client_secret",
            "AWS_SECRET_ACCESS_KEY",
            "private_key",
            "credentials",
        ):
            with self.subTest(key=key):
                self.assertTrue(is_sensitive_key(key))

        for key in ("market_key", "token_count", "secretary", "authorization_mode"):
            with self.subTest(key=key):
                self.assertFalse(is_sensitive_key(key))

    def test_text_redacts_key_values_bearer_url_userinfo_and_sensitive_query(self) -> None:
        source = (
            "api_key=alpha123 password:'bravo456' Authorization: Bearer charlie789 "
            "url=https://user:delta999@example.test/path?token=echo123&market=match"
        )
        redacted = redact_operator_text(source)

        for secret in ("alpha123", "bravo456", "charlie789", "user", "delta999", "echo123"):
            self.assertNotIn(secret, redacted)
        self.assertIn("api_key=" + REDACTED, redacted)
        self.assertIn("password:'" + REDACTED + "'", redacted)
        self.assertIn("Authorization: " + REDACTED, redacted)
        self.assertIn("https://" + REDACTED + "@example.test/path", redacted)
        self.assertIn("market=match", redacted)

    def test_text_redaction_is_idempotent(self) -> None:
        source = (
            "Authorization: Bearer alpha123 "
            "api_key=bravo456 "
            "postgresql://user:charlie789@example.test/db?token=delta123"
        )
        once = redact_operator_text(source)
        with mock.patch.dict(
            os.environ,
            {"AUTOSPORT_PARLAYAPI_KEY": "REDA"},
            clear=False,
        ):
            twice = redact_operator_text(once)
        self.assertEqual(twice, once)

    def test_quoted_authorization_value_is_fully_redacted(self) -> None:
        source = 'Authorization: "Bearer quoted-secret-123" region=eu'
        redacted = redact_operator_text(source)
        self.assertNotIn("quoted-secret-123", redacted)
        self.assertIn('Authorization: "' + REDACTED + '"', redacted)
        self.assertIn("region=eu", redacted)

    def test_nested_sensitive_keys_are_redacted_without_hiding_normal_config(self) -> None:
        source = {
            "provider": "demo",
            "nested": {
                "Api_Key": "alpha",
                "PASSWORD": "bravo",
                "region": "eu",
            },
            "items": [
                {"authorization": "Bearer charlie", "market": "winner"},
                "plain",
            ],
        }

        redacted = redact_operator_value(source)

        self.assertEqual(redacted["provider"], "demo")
        self.assertEqual(redacted["nested"]["region"], "eu")
        self.assertEqual(redacted["nested"]["Api_Key"], REDACTED)
        self.assertEqual(redacted["nested"]["PASSWORD"], REDACTED)
        self.assertEqual(redacted["items"][0]["authorization"], REDACTED)
        self.assertEqual(redacted["items"][0]["market"], "winner")
        self.assertEqual(redacted["items"][1], "plain")

    def test_current_parlay_environment_secret_is_redacted_even_when_unlabelled(self) -> None:
        secret = "parlay-positive-control-secret"
        with mock.patch.dict(
            os.environ,
            {"AUTOSPORT_PARLAYAPI_KEY": secret},
            clear=False,
        ):
            rendered = redact_operator_text("provider failure echoed " + secret)
        self.assertNotIn(secret, rendered)
        self.assertIn(REDACTED, rendered)

    def test_safe_exception_and_existing_worker_renderers_redact_secrets(self) -> None:
        secret = "worker-secret-987"
        error = RuntimeError("api_key=" + secret + " market=winner")

        with mock.patch.dict(
            os.environ,
            {"AUTOSPORT_PARLAYAPI_KEY": secret},
            clear=False,
        ):
            outputs = (
                safe_exception_text(error),
                _safe_worker_error(error),
                _terminal_error(error),
                _render_exception(error),
            )

        for rendered in outputs:
            with self.subTest(rendered=rendered):
                self.assertNotIn(secret, rendered)
                self.assertIn("RuntimeError:", rendered)
                self.assertIn("market=winner", rendered)

    def test_live_worker_terminal_error_uses_redaction_boundary(self) -> None:
        secret = "live-secret-654"
        worker = OneShotObservationWorker()

        with mock.patch.dict(
            os.environ,
            {"AUTOSPORT_PARLAYAPI_KEY": secret},
            clear=False,
        ):
            self.assertTrue(
                worker.start(
                    lambda: (_ for _ in ()).throw(
                        RuntimeError("Authorization: Bearer " + secret)
                    )
                )
            )
            thread = worker._thread
            self.assertIsNotNone(thread)
            thread.join(timeout=2.0)
            self.assertFalse(thread.is_alive())
            message = worker.poll()

        self.assertIsNotNone(message)
        self.assertIsNone(message.result)
        self.assertNotIn(secret, message.error)
        self.assertIn("RuntimeError:", message.error)
        self.assertIn("Authorization: " + REDACTED, message.error)

    def test_historical_cli_redacts_failure_detail_without_changing_exit_contract(self) -> None:
        secret = "historical-secret-321"
        output = StringIO()
        argv = [
            "--at",
            "2026-09-12T10:03:00Z",
            "--results-date",
            "2026-09-10",
        ]

        with (
            mock.patch.dict(
                os.environ,
                {"AUTOSPORT_PARLAYAPI_KEY": secret},
                clear=False,
            ),
            mock.patch.object(
                historical_acquisition,
                "ParlayApiTableTennisProvider",
                return_value=object(),
            ),
            mock.patch.object(
                historical_acquisition,
                "capture_historical_acquisition_bundle",
                side_effect=ValueError("password=" + secret + " region=us"),
            ),
            redirect_stdout(output),
        ):
            result = historical_acquisition.main(argv)

        rendered = output.getvalue()
        self.assertEqual(result, 3)
        self.assertNotIn(secret, rendered)
        self.assertIn("historical_acquisition=FAIL_CLOSED", rendered)
        self.assertIn("password=" + REDACTED, rendered)
        self.assertIn("region=us", rendered)

    def test_hostile_exception_type_name_cannot_reach_operator_text(self) -> None:
        secret = "TYPE-NAME-SECRET-7f31"
        hostile_type = type(
            "ProviderError_" + secret + "\nAuthorization",
            (RuntimeError,),
            {},
        )

        rendered = safe_exception_text(
            hostile_type("message detail is intentionally irrelevant")
        )

        self.assertNotIn(secret, rendered)
        self.assertNotIn("Authorization", rendered)
        self.assertNotIn("\n", rendered)
        self.assertEqual(
            rendered,
            "RuntimeError: message detail is intentionally irrelevant",
        )

    def test_secret_shaped_identifier_type_falls_back_to_safe_parent(self) -> None:
        hostile_type = type(
            "ProviderSecretToken7f31",
            (ValueError,),
            {},
        )

        rendered = safe_exception_text(hostile_type("ordinary detail"))

        self.assertEqual(rendered, "ValueError: ordinary detail")

    def test_hostile_exception_string_still_terminalizes_without_secret(self) -> None:
        class HostileRenderedString(str):
            def __format__(self, spec: str) -> str:
                raise RuntimeError("format must not run")

        class HostileError(BaseException):
            def __str__(self) -> str:
                return HostileRenderedString("token=hidden-token-123")

        rendered = safe_exception_text(HostileError())
        self.assertEqual(rendered, "HostileError: token=" + REDACTED)


if __name__ == "__main__":
    unittest.main()
