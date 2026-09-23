from __future__ import annotations

from io import BytesIO
import json
import unittest
from urllib.error import HTTPError

import autosport.the_odds_api_provider as odds_api_module
from autosport.the_odds_api_provider import (
    HttpJsonResponse,
    TheOddsApiProvider,
    TheOddsApiTransportError,
)


class TheOddsApiFailureTaxonomyTests(unittest.TestCase):
    def test_injected_failure_preserves_provider_code_and_quota_without_raw_message(self):
        cases = (
            (
                429,
                "EXCEEDED_FREQ_LIMIT",
                {"x-requests-remaining": "91", "x-requests-used": "9", "x-requests-last": "1"},
            ),
            (
                401,
                "OUT_OF_USAGE_CREDITS",
                {"x-requests-remaining": "0", "x-requests-used": "500", "x-requests-last": "1"},
            ),
        )
        for status, error_code, headers in cases:
            with self.subTest(error_code=error_code):
                response = HttpJsonResponse(
                    {
                        "message": "SECRET_PROVIDER_MESSAGE_MUST_NOT_BE_RETAINED",
                        "error_code": error_code,
                        "details_url": "https://the-odds-api.com/liveapi/guides/v4/api-error-codes.html",
                    },
                    status,
                    headers,
                )
                provider = TheOddsApiProvider(
                    "key",
                    sport="soccer_epl",
                    transport=lambda *_args, response=response: response,
                )

                with self.assertRaises(TheOddsApiTransportError) as context:
                    provider.read_batch()

                failure = context.exception
                self.assertEqual(failure.status_code, status)
                self.assertEqual(failure.provider_error_code, error_code)
                self.assertEqual(
                    (
                        failure.quota_remaining,
                        failure.quota_used,
                        failure.quota_last,
                    ),
                    tuple(int(headers[name]) for name in (
                        "x-requests-remaining",
                        "x-requests-used",
                        "x-requests-last",
                    )),
                )
                self.assertNotIn("SECRET_PROVIDER_MESSAGE", str(failure))

    def test_product_http_error_preserves_taxonomy_and_detaches_secret_context(self):
        secret = "SECRET_SENTINEL_DO_NOT_RETAIN"
        url = (
            "https://api.the-odds-api.com/v4/sports/soccer_epl/odds"
            f"?apiKey={secret}&regions=eu&markets=h2h"
        )
        payload = {
            "message": "Too many requests",
            "error_code": "EXCEEDED_FREQ_LIMIT",
            "details_url": "https://the-odds-api.com/liveapi/guides/v4/api-error-codes.html",
        }
        headers = {
            "x-requests-remaining": "17",
            "x-requests-used": "83",
            "x-requests-last": "1",
        }

        class FailingOpener:
            def open(self, request, timeout):
                raise HTTPError(
                    request.full_url,
                    429,
                    "Too Many Requests",
                    headers,
                    BytesIO(json.dumps(payload).encode("utf-8")),
                )

        with self.assertRaises(TheOddsApiTransportError) as context:
            odds_api_module._perform_http_json_response(
                url,
                1.0,
                opener_factory=lambda *_: FailingOpener(),
                proxy_handler_factory=odds_api_module.ProxyHandler,
                request_factory=odds_api_module.Request,
            )

        failure = context.exception
        self.assertEqual(failure.status_code, 429)
        self.assertEqual(failure.provider_error_code, "EXCEEDED_FREQ_LIMIT")
        self.assertEqual(failure.quota_remaining, 17)
        self.assertEqual(failure.quota_used, 83)
        self.assertEqual(failure.quota_last, 1)
        self.assertIsNone(failure.__cause__)
        self.assertIsNone(failure.__context__)
        self.assertNotIn(secret, str(failure))

    def test_malformed_failure_body_stays_sanitized_failure_with_quota_evidence(self):
        url = (
            "https://api.the-odds-api.com/v4/sports/soccer_epl/odds"
            "?apiKey=secret&regions=eu&markets=h2h"
        )
        headers = {
            "x-requests-remaining": "0",
            "x-requests-used": "100",
            "x-requests-last": "0",
        }

        class FailingOpener:
            def open(self, request, timeout):
                raise HTTPError(
                    request.full_url,
                    401,
                    "Unauthorized",
                    headers,
                    BytesIO(b"{not-json"),
                )

        with self.assertRaises(TheOddsApiTransportError) as context:
            odds_api_module._perform_http_json_response(
                url,
                1.0,
                opener_factory=lambda *_: FailingOpener(),
                proxy_handler_factory=odds_api_module.ProxyHandler,
                request_factory=odds_api_module.Request,
            )

        failure = context.exception
        self.assertEqual(failure.status_code, 401)
        self.assertIsNone(failure.provider_error_code)
        self.assertEqual(failure.quota_remaining, 0)
        self.assertEqual(failure.quota_used, 100)
        self.assertEqual(failure.quota_last, 0)

    def test_oversized_failure_body_cannot_become_success_or_unbounded_error_evidence(self):
        url = (
            "https://api.the-odds-api.com/v4/sports/soccer_epl/odds"
            "?apiKey=secret&regions=eu&markets=h2h"
        )
        oversized = b"x" * (odds_api_module.THE_ODDS_API_MAX_RESPONSE_BYTES + 1)

        class FailingOpener:
            def open(self, request, timeout):
                raise HTTPError(
                    request.full_url,
                    500,
                    "Server Error",
                    {},
                    BytesIO(oversized),
                )

        with self.assertRaises(TheOddsApiTransportError) as context:
            odds_api_module._perform_http_json_response(
                url,
                1.0,
                opener_factory=lambda *_: FailingOpener(),
                proxy_handler_factory=odds_api_module.ProxyHandler,
                request_factory=odds_api_module.Request,
            )

        failure = context.exception
        self.assertEqual(failure.status_code, 500)
        self.assertIsNone(failure.provider_error_code)
        self.assertIsNone(failure.quota_remaining)
        self.assertIsNone(failure.quota_used)
        self.assertIsNone(failure.quota_last)

    def test_product_http_helper_captures_failure_evidence_parser(self):
        defaults = odds_api_module._perform_http_json_response.__kwdefaults__ or {}
        self.assertIs(
            defaults["failure_evidence_builder"],
            odds_api_module._failure_evidence,
        )


if __name__ == "__main__":
    unittest.main()
