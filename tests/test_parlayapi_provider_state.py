import json
import unittest

from autosport.parlayapi_provider import (
    HttpJsonResponse,
    ParlayApiTableTennisProvider,
    ProviderPayloadError,
    ProviderTransportError,
)


EVENT = {
    "id": "tt-provider-state-1",
    "sport_key": "table_tennis",
    "sport_title": "Table Tennis",
    "commence_time": "2026-09-21T20:00:00Z",
    "home_team": "Player A",
    "away_team": "Player B",
    "bookmakers": [
        {
            "key": "book-a",
            "title": "Book A",
            "last_update": "2026-09-21T19:59:58Z",
            "markets": [
                {
                    "key": "h2h",
                    "last_update": "2026-09-21T19:59:59Z",
                    "outcomes": [
                        {"name": "Player A", "price": 1.8},
                        {"name": "Player B", "price": 2.05},
                    ],
                }
            ],
        }
    ],
}


def provider_state_header(
    *,
    source: str = "book-a",
    role: str = "primary",
    age_seconds: float | None = 1.25,
    truncated: bool = False,
) -> str:
    payload = {
        "ts": 1790020800,
        "src": {source: {"age_s": age_seconds, "role": role}},
    }
    if truncated:
        payload["truncated"] = True
    return json.dumps(payload, separators=(",", ":"))


class ParlayApiProviderStateTests(unittest.TestCase):
    def _provider(self, response: HttpJsonResponse, **kwargs) -> ParlayApiTableTennisProvider:
        return ParlayApiTableTennisProvider(
            "key",
            transport=lambda *_: response,
            clock=lambda: "2026-09-21T20:00:01+00:00",
            **kwargs,
        )

    def test_primary_same_response_state_is_bound_to_quote_metadata(self):
        response = HttpJsonResponse(
            [EVENT],
            200,
            {"X-Provider-State": provider_state_header()},
        )
        batch = self._provider(response).read_batch()

        self.assertEqual(batch.quality_flags, ())
        self.assertEqual(len(batch.quotes), 2)
        metadata = batch.quotes[0].metadata
        self.assertEqual(metadata["provider_state_role"], "primary")
        self.assertEqual(metadata["provider_state_age_seconds"], 1.25)
        self.assertEqual(metadata["provider_state_server_timestamp"], 1790020800)
        self.assertFalse(metadata["provider_state_truncated"])

    def test_degraded_rows_are_preserved_but_batch_is_marked_degraded(self):
        response = HttpJsonResponse(
            [EVENT],
            200,
            {
                "X-Provider-State": provider_state_header(
                    role="degraded",
                    age_seconds=75.0,
                )
            },
        )
        batch = self._provider(response).read_batch()

        self.assertEqual(len(batch.quotes), 2)
        self.assertEqual(batch.quality_flags, ("UPSTREAM_SOURCE_DEGRADED",))
        self.assertEqual(batch.quotes[0].metadata["provider_state_role"], "degraded")
        self.assertEqual(batch.quotes[0].metadata["provider_state_age_seconds"], 75.0)

    def test_offline_rows_are_not_emitted_as_time_sensitive_observations(self):
        response = HttpJsonResponse(
            [EVENT],
            200,
            {
                "X-Provider-State": provider_state_header(
                    role="offline",
                    age_seconds=None,
                )
            },
        )
        batch = self._provider(response).read_batch()

        self.assertEqual(batch.quotes, ())
        self.assertEqual(batch.quality_flags, ("UPSTREAM_SOURCE_OFFLINE",))

    def test_state_uncovered_bookmaker_is_not_treated_as_fresh(self):
        response = HttpJsonResponse(
            [EVENT],
            200,
            {
                "X-Provider-State": provider_state_header(
                    source="some-other-book",
                    role="primary",
                    age_seconds=0.5,
                )
            },
        )
        batch = self._provider(response).read_batch()

        self.assertEqual(batch.quotes, ())
        self.assertEqual(batch.quality_flags, ("PROVIDER_STATE_UNCOVERED_SOURCE",))

    def test_truncated_state_keeps_omitted_source_fail_closed(self):
        header = json.dumps(
            {"ts": 1790020800, "src": {}, "truncated": True},
            separators=(",", ":"),
        )
        batch = self._provider(
            HttpJsonResponse([EVENT], 200, {"X-Provider-State": header})
        ).read_batch()

        self.assertEqual(batch.quotes, ())
        self.assertEqual(
            batch.quality_flags,
            ("PROVIDER_STATE_TRUNCATED", "PROVIDER_STATE_UNCOVERED_SOURCE"),
        )

    def test_missing_state_preserves_compatibility_but_marks_completeness_unknown(self):
        batch = self._provider(HttpJsonResponse([EVENT], 200, {})).read_batch()

        self.assertEqual(len(batch.quotes), 2)
        self.assertEqual(batch.quality_flags, ("PROVIDER_STATE_MISSING",))
        self.assertNotIn("provider_state_role", batch.quotes[0].metadata)

    def test_malformed_provider_state_fails_closed(self):
        malformed_headers = (
            json.dumps({"ts": True, "src": {}}),
            json.dumps(
                {
                    "ts": 1790020800,
                    "src": {"book-a": {"age_s": None, "role": "primary"}},
                }
            ),
            json.dumps(
                {
                    "ts": 1790020800,
                    "src": {"book-a": {"age_s": -1, "role": "degraded"}},
                }
            ),
            json.dumps(
                {
                    "ts": 1790020800,
                    "src": {"book-a": {"age_s": 1, "role": "mystery"}},
                }
            ),
        )
        for raw in malformed_headers:
            with self.subTest(raw=raw):
                provider = self._provider(
                    HttpJsonResponse([EVENT], 200, {"X-Provider-State": raw})
                )
                with self.assertRaises(ProviderPayloadError):
                    provider.read_batch()

    def test_custom_transport_429_is_retried_and_never_becomes_empty_success(self):
        attempts = []
        sleeps = []

        def transport(url, headers, timeout):
            attempts.append(url)
            if len(attempts) == 1:
                return HttpJsonResponse([], 429, {"Retry-After": "9"})
            return HttpJsonResponse(
                [EVENT],
                200,
                {"X-Provider-State": provider_state_header()},
            )

        provider = ParlayApiTableTennisProvider(
            "key",
            transport=transport,
            sleeper=sleeps.append,
            max_attempts=2,
            max_backoff_seconds=0.25,
            clock=lambda: "2026-09-21T20:00:01+00:00",
        )
        batch = provider.read_batch()

        self.assertEqual(len(attempts), 2)
        self.assertEqual(sleeps, [0.25])
        self.assertEqual(len(batch.quotes), 2)

    def test_custom_transport_auth_error_is_not_retried_or_normalized_to_empty(self):
        attempts = []

        def transport(url, headers, timeout):
            attempts.append(url)
            return HttpJsonResponse([], 401, {})

        provider = ParlayApiTableTennisProvider(
            "key",
            transport=transport,
            max_attempts=5,
            sleeper=lambda _: self.fail("401 must not sleep/retry"),
        )
        with self.assertRaises(ProviderTransportError) as raised:
            provider.read_batch()

        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(len(attempts), 1)

    def test_provider_state_quality_survives_chunked_batch_delivery(self):
        response = HttpJsonResponse(
            [EVENT],
            200,
            {
                "X-Provider-State": provider_state_header(
                    role="degraded",
                    age_seconds=60.0,
                )
            },
        )
        provider = self._provider(response)

        first = provider.read_batch(max_items=1)
        second = provider.read_batch(max_items=1)

        self.assertEqual(len(first.quotes), 1)
        self.assertEqual(len(second.quotes), 1)
        self.assertEqual(
            first.quality_flags,
            ("TRUNCATED_BATCH", "UPSTREAM_SOURCE_DEGRADED"),
        )
        self.assertEqual(second.quality_flags, ("UPSTREAM_SOURCE_DEGRADED",))


if __name__ == "__main__":
    unittest.main()
