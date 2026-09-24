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

    def test_authorization_auth_scheme_credentials_are_fully_redacted(self) -> None:
        cases = (
            ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
            ("Authorization: Token opaque-token-804", "opaque-token-804"),
            ("Authorization: Negotiate YIIB-wrapped-token", "YIIB-wrapped-token"),
        )

        for source, secret in cases:
            with self.subTest(source=source):
                redacted = redact_operator_text(source)
                self.assertEqual(redacted, "Authorization: " + REDACTED)
                self.assertNotIn(secret, redacted)


    def test_multi_parameter_authorization_headers_redact_entire_current_line(self) -> None:
        cases = (
            (
                'Authorization: Digest username="user", realm="exchange", '
                'nonce="nonce-1", response="digest-secret-804"',
                "digest-secret-804",
            ),
            (
                "Authorization: AWS4-HMAC-SHA256 "
                "Credential=AKIAEXAMPLE/20260922/eu/service/aws4_request, "
                "SignedHeaders=host;x-amz-date, Signature=aws-signature-secret-804",
                "aws-signature-secret-804",
            ),
        )

        for source, secret in cases:
            with self.subTest(source=source):
                redacted = redact_operator_text(source)
                self.assertEqual(redacted, "Authorization: " + REDACTED)
                self.assertNotIn(secret, redacted)

    def test_multi_parameter_authorization_redaction_stops_at_line_boundary(self) -> None:
        source = (
            'Authorization: Digest username="user", response="digest-secret-804"\n'
            "market=winner region=eu"
        )

        redacted = redact_operator_text(source)

        self.assertEqual(
            redacted,
            "Authorization: " + REDACTED + "\nmarket=winner region=eu",
        )
        self.assertNotIn("digest-secret-804", redacted)
        self.assertIn("market=winner region=eu", redacted)

    def test_safe_exception_text_redacts_multi_parameter_authorization_tail(self) -> None:
        secret = "digest-exception-secret-804"
        rendered = safe_exception_text(
            RuntimeError(
                'provider failed Authorization: Digest username="user", '
                'realm="exchange", response="' + secret + '"'
            )
        )

        self.assertNotIn(secret, rendered)
        self.assertEqual(
            rendered,
            "RuntimeError: provider failed Authorization: " + REDACTED,
        )

    def test_text_redacts_percent_encoded_sensitive_query_keys(self) -> None:
        source = (
            "https://example.test/path?"
            "api%5Fkey=alpha123&access%5Ftoken=bravo456&market=match"
        )

        redacted = redact_operator_text(source)

        self.assertNotIn("alpha123", redacted)
        self.assertNotIn("bravo456", redacted)
        self.assertIn("api%5Fkey=" + REDACTED, redacted)
        self.assertIn("access%5Ftoken=" + REDACTED, redacted)
        self.assertIn("market=match", redacted)

    def test_human_readable_spaced_credential_labels_are_redacted(self) -> None:
        cases = (
            ("api key=api-secret-804", "api-secret-804"),
            ('client secret: "client-secret-804"', "client-secret-804"),
            ("session token = 'session-secret-804'", "session-secret-804"),
            ("X API Key=x-api-secret-804", "x-api-secret-804"),
            ("secret access key=access-secret-804", "access-secret-804"),
            ("private\tkey=private-secret-804", "private-secret-804"),
        )

        for source, secret in cases:
            with self.subTest(source=source):
                redacted = redact_operator_text(source)
                self.assertNotIn(secret, redacted)
                self.assertIn(REDACTED, redacted)

    def test_safe_exception_text_redacts_spaced_credential_label(self) -> None:
        secret = "spaced-label-exception-secret-804"
        rendered = safe_exception_text(
            RuntimeError("provider rejected api key=" + secret + " region=eu")
        )

        self.assertNotIn(secret, rendered)
        self.assertEqual(
            rendered,
            "RuntimeError: provider rejected api key=" + REDACTED + " region=eu",
        )

    def test_non_sensitive_spaced_label_is_preserved(self) -> None:
        source = "market name=winner source status=healthy"

        self.assertEqual(redact_operator_text(source), source)

    def test_quoted_sensitive_keys_with_literal_separators_are_redacted(self) -> None:
        cases = (
            ('{"api key":"space-secret-804"}', "space-secret-804", '"api key"'),
            (
                '{"client/secret":"slash-secret-804"}',
                "slash-secret-804",
                '"client/secret"',
            ),
            (
                "{'session token':'token-secret-804'}",
                "token-secret-804",
                "'session token'",
            ),
        )

        for source, secret, key_spelling in cases:
            with self.subTest(source=source):
                redacted = redact_operator_text(source)
                self.assertNotIn(secret, redacted)
                self.assertIn(key_spelling, redacted)
                self.assertIn(REDACTED, redacted)

    def test_safe_exception_text_redacts_quoted_separator_sensitive_key(self) -> None:
        secret = "quoted-separator-exception-secret-804"
        rendered = safe_exception_text(
            RuntimeError('{"api key":"' + secret + '","market name":"winner"}')
        )

        self.assertNotIn(secret, rendered)
        self.assertIn('"api key":"' + REDACTED + '"', rendered)
        self.assertIn('"market name":"winner"', rendered)

    def test_non_sensitive_quoted_key_with_literal_separator_is_preserved(self) -> None:
        source = '{"market name":"winner","league/name":"open"}'

        self.assertEqual(redact_operator_text(source), source)

    def test_text_redacts_escaped_sensitive_serialized_keys(self) -> None:
        cases = (
            (r'{"api\u005fkey":"alpha123"}', "alpha123", r"api\u005fkey"),
            (
                r"{'session\x5ftoken':'bravo456'}",
                "bravo456",
                r"session\x5ftoken",
            ),
            (r'{"\u0061pi_key":"charlie789"}', "charlie789", r"\u0061pi_key"),
        )

        for source, secret, escaped_key in cases:
            with self.subTest(source=source):
                redacted = redact_operator_text(source)
                self.assertNotIn(secret, redacted)
                self.assertIn(escaped_key, redacted)
                self.assertIn(REDACTED, redacted)

    def test_text_redacts_short_escaped_separator_sensitive_serialized_keys(self) -> None:
        cases = (
            (r'{"api\\tkey":"short-tab-secret-804"}', "short-tab-secret-804", r"api\\tkey"),
            (
                r'{"session\\ntoken":"short-newline-secret-804"}',
                "short-newline-secret-804",
                r"session\\ntoken",
            ),
            (
                r"{'client\\rsecret':'short-cr-secret-804'}",
                "short-cr-secret-804",
                r"client\\rsecret",
            ),
        )

        for source, secret, escaped_key in cases:
            with self.subTest(source=source):
                redacted = redact_operator_text(source)
                self.assertNotIn(secret, redacted)
                self.assertIn(escaped_key, redacted)
                self.assertIn(REDACTED, redacted)

    def test_safe_exception_text_redacts_short_escaped_credential_key(self) -> None:
        secret = "short-escape-exception-secret-804"
        source = r'{"api\\tkey":"' + secret + '"}'

        rendered = safe_exception_text(RuntimeError(source))

        self.assertNotIn(secret, rendered)
        self.assertIn(r"api\\tkey", rendered)
        self.assertIn(REDACTED, rendered)

    def test_non_sensitive_short_escaped_serialized_key_is_preserved(self) -> None:
        source = r'{"market\\tname":"winner"}'

        self.assertEqual(redact_operator_text(source), source)

    def test_safe_exception_text_redacts_escaped_serialized_credential_key(self) -> None:
        secret = "serialized-secret-804"
        source = r'{"session\u005ftoken":"' + secret + '"}'

        rendered = safe_exception_text(RuntimeError(source))

        self.assertNotIn(secret, rendered)
        self.assertIn(r"session\u005ftoken", rendered)
        self.assertIn(REDACTED, rendered)

    def test_non_sensitive_escaped_serialized_key_is_preserved(self) -> None:
        source = r'{"market\u005fname":"winner"}'

        self.assertEqual(redact_operator_text(source), source)

    def test_escaped_quotes_inside_sensitive_values_do_not_leak_suffix(self) -> None:
        cases = (
            (
                r'{"api_key":"alpha\"BRAVO-SECRET"}',
                '{"api_key":"' + REDACTED + '"}',
            ),
            (
                r"{'session_token':'alpha\'BRAVO-SECRET'}",
                "{'session_token':'" + REDACTED + "'}",
            ),
            (
                r'Authorization: "Bearer alpha\"BRAVO-SECRET" region=eu',
                'Authorization: "' + REDACTED + '" region=eu',
            ),
        )

        for source, expected in cases:
            with self.subTest(source=source):
                redacted = redact_operator_text(source)
                self.assertEqual(redacted, expected)
                self.assertNotIn("BRAVO-SECRET", redacted)

    def test_safe_exception_text_redacts_escaped_quoted_secret_tail(self) -> None:
        error = RuntimeError(r'{"api_key":"alpha\"BRAVO-SECRET"}')

        rendered = safe_exception_text(error)

        self.assertEqual(
            rendered,
            'RuntimeError: {"api_key":"' + REDACTED + '"}',
        )
        self.assertNotIn("BRAVO-SECRET", rendered)

    def test_safe_exception_text_reuses_one_shot_secret_iterable(self) -> None:
        secret = "Alpha42"
        dynamic_type = type(
            "ProviderRuntimeFailure",
            (RuntimeError,),
            {},
        )
        one_shot_secrets = (value for value in (secret,))

        rendered = safe_exception_text(
            dynamic_type("ordinary detail " + secret),
            extra_secret_values=one_shot_secrets,
        )

        self.assertNotIn(secret, rendered)
        self.assertEqual(
            rendered,
            "RuntimeError: ordinary detail " + REDACTED,
        )

    def test_known_secret_containing_redacted_marker_is_fully_redacted(self) -> None:
        secret = "alpha" + REDACTED + "omega"

        rendered = redact_operator_text(
            "provider echoed " + secret + " end",
            extra_secret_values=(secret,),
        )

        self.assertEqual(rendered, "provider echoed " + REDACTED + " end")
        self.assertNotIn("alpha", rendered)
        self.assertNotIn("omega", rendered)

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

    def test_marker_free_custom_type_name_falls_back_to_builtin_parent(self) -> None:
        custom_type = type(
            "OpaqueFailureA1B2C3D4",
            (RuntimeError,),
            {},
        )

        rendered = safe_exception_text(custom_type("ordinary detail"))

        self.assertEqual(rendered, "RuntimeError: ordinary detail")
        self.assertNotIn("OpaqueFailureA1B2C3D4", rendered)

    def test_custom_direct_baseexception_type_falls_back_to_baseexception(self) -> None:
        custom_type = type(
            "OpaqueRootFailure",
            (BaseException,),
            {},
        )

        rendered = safe_exception_text(custom_type("ordinary detail"))

        self.assertEqual(rendered, "BaseException: ordinary detail")
        self.assertNotIn("OpaqueRootFailure", rendered)

    def test_builtin_exception_type_name_is_preserved(self) -> None:
        rendered = safe_exception_text(FileNotFoundError("missing"))

        self.assertEqual(rendered, "FileNotFoundError: missing")

    def test_hostile_exception_string_still_terminalizes_without_secret(self) -> None:
        class HostileRenderedString(str):
            def __format__(self, spec: str) -> str:
                raise RuntimeError("format must not run")

        class HostileError(BaseException):
            def __str__(self) -> str:
                return HostileRenderedString("token=hidden-token-123")

        rendered = safe_exception_text(HostileError())
        self.assertEqual(rendered, "BaseException: token=" + REDACTED)


    def test_nested_wrapper_cannot_swallow_sensitive_pair(self) -> None:
        secret = "wrapped-secret-804"
        cases = (
            ("detail=api_key=" + secret, "detail=api_key=" + REDACTED),
            (
                "message=session_token=" + secret,
                "message=session_token=" + REDACTED,
            ),
            (
                'detail="api_key=' + secret + '"',
                'detail="api_key=' + REDACTED + '"',
            ),
            (
                '{"detail":"api_key=' + secret + '"}',
                '{"detail":"api_key=' + REDACTED + '"}',
            ),
            ("payload=password=" + secret, "payload=password=" + REDACTED),
            (
                "meta=client_secret=" + secret,
                "meta=client_secret=" + REDACTED,
            ),
            (
                "detail=access_token=" + secret,
                "detail=access_token=" + REDACTED,
            ),
            (
                "outer=AUTOSPORT_PARLAYAPI_KEY=" + secret,
                "outer=AUTOSPORT_PARLAYAPI_KEY=" + REDACTED,
            ),
        )

        for source, expected in cases:
            with self.subTest(source=source):
                redacted = redact_operator_text(source)
                self.assertEqual(redacted, expected)
                self.assertNotIn(secret, redacted)

    def test_cookie_headers_redact_entire_line_and_preserve_next_line(self) -> None:
        secret = "cookie-secret-804"
        cases = (
            (
                "Cookie: session_token=" + secret,
                "Cookie: " + REDACTED,
            ),
            (
                "Set-Cookie: sid=" + secret + "; HttpOnly; Secure",
                "Set-Cookie: " + REDACTED,
            ),
        )

        for header, expected_header in cases:
            with self.subTest(header=header):
                source = header + "\nmarket=winner region=eu"
                redacted = redact_operator_text(source)
                self.assertEqual(
                    redacted,
                    expected_header + "\nmarket=winner region=eu",
                )
                self.assertNotIn(secret, redacted)
                self.assertIn("market=winner region=eu", redacted)

    def test_cookie_keys_are_sensitive_only_at_exact_structured_key(self) -> None:
        self.assertTrue(is_sensitive_key("Cookie"))
        self.assertTrue(is_sensitive_key("Set-Cookie"))
        self.assertFalse(is_sensitive_key("my_cookie"))

        redacted = redact_operator_value(
            {
                "Cookie": "sid=cookie-structured-secret",
                "Set-Cookie": "sid=set-cookie-structured-secret",
                "my_cookie": "ordinary-setting",
            }
        )

        self.assertEqual(redacted["Cookie"], REDACTED)
        self.assertEqual(redacted["Set-Cookie"], REDACTED)
        self.assertEqual(redacted["my_cookie"], "ordinary-setting")

    def test_safe_exception_text_redacts_nested_pair_and_cookie_header(self) -> None:
        nested_secret = "nested-exception-secret-804"
        cookie_secret = "cookie-exception-secret-804"
        rendered = safe_exception_text(
            RuntimeError(
                "detail=api_key="
                + nested_secret
                + "\nCookie: sid="
                + cookie_secret
                + "\nmarket=winner"
            )
        )

        self.assertNotIn(nested_secret, rendered)
        self.assertNotIn(cookie_secret, rendered)
        self.assertIn("detail=api_key=" + REDACTED, rendered)
        self.assertIn("Cookie: " + REDACTED, rendered)
        self.assertIn("market=winner", rendered)


if __name__ == "__main__":
    unittest.main()
