import copy
import json
import unittest
from decimal import Decimal

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
            "stale_seconds": 1.0,
            "last_update_ms": 1790020799000,
            "topped_up": False,
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


class _ThreeArgumentParlaySubclass(ParlayApiTableTennisProvider):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.event_quote_calls = 0

    def _event_quotes(self, event, observed_ts, http_status):
        self.event_quote_calls += 1
        return super()._event_quotes(event, observed_ts, http_status)


class ParlayApiProviderStateTests(unittest.TestCase):
    def _provider(self, response: HttpJsonResponse, **kwargs) -> ParlayApiTableTennisProvider:
        return ParlayApiTableTennisProvider(
            "key",
            transport=lambda *_: response,
            clock=lambda: "2026-09-21T20:00:01+00:00",
            **kwargs,
        )

    def test_three_argument_subclass_hook_preserves_provider_state_projection(self):
        response = HttpJsonResponse(
            [EVENT],
            200,
            {"X-Provider-State": provider_state_header()},
        )
        provider = _ThreeArgumentParlaySubclass(
            "key",
            transport=lambda *_: response,
            clock=lambda: "2026-09-21T20:00:01+00:00",
        )
        batch = provider.read_batch()

        self.assertEqual(provider.event_quote_calls, 1)
        self.assertEqual(len(batch.quotes), 2)
        self.assertEqual(batch.quality_flags, ())
        self.assertEqual(batch.quotes[0].metadata["provider_state_role"], "primary")
        self.assertEqual(batch.quotes[0].metadata["provider_block_stale_seconds"], 1.0)

    def test_three_argument_subclass_cannot_reintroduce_offline_rows(self):
        response = HttpJsonResponse(
            [EVENT],
            200,
            {"X-Provider-State": provider_state_header(role="offline", age_seconds=None)},
        )
        provider = _ThreeArgumentParlaySubclass(
            "key",
            transport=lambda *_: response,
            clock=lambda: "2026-09-21T20:00:01+00:00",
        )
        batch = provider.read_batch()

        self.assertEqual(provider.event_quote_calls, 1)
        self.assertEqual(batch.quotes, ())
        self.assertEqual(batch.quality_flags, ("UPSTREAM_SOURCE_OFFLINE",))

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
        self.assertEqual(metadata["provider_block_stale_seconds"], 1.0)
        self.assertEqual(metadata["provider_block_last_update_ms"], 1790020799000)
        self.assertFalse(metadata["provider_block_topped_up"])
        self.assertTrue(metadata["provider_block_freshness_complete"])

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


    def test_primary_source_does_not_erase_topped_up_block_truth(self):
        event = copy.deepcopy(EVENT)
        event["bookmakers"][0]["topped_up"] = True
        event["bookmakers"][0]["stale_seconds"] = 45.5
        batch = self._provider(
            HttpJsonResponse(
                [event],
                200,
                {"X-Provider-State": provider_state_header(role="primary", age_seconds=0.1)},
            )
        ).read_batch()
        self.assertEqual(batch.quality_flags, ("UPSTREAM_BOOKMAKER_TOPPED_UP",))
        self.assertEqual(len(batch.quotes), 2)
        for quote in batch.quotes:
            self.assertTrue(quote.metadata["provider_block_topped_up"])
            self.assertEqual(quote.metadata["provider_block_stale_seconds"], 45.5)
            self.assertTrue(quote.metadata["provider_block_freshness_complete"])

    def test_missing_block_freshness_remains_explicit_unknown(self):
        event = copy.deepcopy(EVENT)
        del event["bookmakers"][0]["last_update_ms"]
        batch = self._provider(
            HttpJsonResponse([event], 200, {"X-Provider-State": provider_state_header()})
        ).read_batch()
        self.assertEqual(batch.quality_flags, ("BOOKMAKER_BLOCK_FRESHNESS_MISSING",))
        metadata = batch.quotes[0].metadata
        self.assertEqual(metadata["provider_block_stale_seconds"], 1.0)
        self.assertIsNone(metadata["provider_block_last_update_ms"])
        self.assertFalse(metadata["provider_block_topped_up"])
        self.assertFalse(metadata["provider_block_freshness_complete"])

    def test_topped_up_survives_even_when_other_block_freshness_is_missing(self):
        event = copy.deepcopy(EVENT)
        event["bookmakers"][0]["topped_up"] = True
        del event["bookmakers"][0]["last_update_ms"]
        batch = self._provider(
            HttpJsonResponse([event], 200, {"X-Provider-State": provider_state_header()})
        ).read_batch()
        self.assertEqual(
            batch.quality_flags,
            ("BOOKMAKER_BLOCK_FRESHNESS_MISSING", "UPSTREAM_BOOKMAKER_TOPPED_UP"),
        )
        self.assertTrue(batch.quotes[0].metadata["provider_block_topped_up"])
        self.assertFalse(batch.quotes[0].metadata["provider_block_freshness_complete"])

    def test_decimal_decoded_block_age_remains_json_safe_and_observable(self):
        event = copy.deepcopy(EVENT)
        event["bookmakers"][0]["stale_seconds"] = Decimal("1.25")
        batch = self._provider(
            HttpJsonResponse([event], 200, {"X-Provider-State": provider_state_header()})
        ).read_batch()
        self.assertEqual(batch.quality_flags, ())
        self.assertEqual(batch.quotes[0].metadata["provider_block_stale_seconds"], 1.25)
        self.assertIsInstance(
            batch.quotes[0].metadata["provider_block_stale_seconds"], float
        )

    def test_malformed_block_freshness_fails_closed(self):
        invalid = (
            ("stale_seconds", True),
            ("stale_seconds", -0.1),
            ("stale_seconds", float("nan")),
            ("last_update_ms", True),
            ("last_update_ms", -1),
            ("last_update_ms", 1.5),
            ("topped_up", 1),
        )
        for field, value in invalid:
            with self.subTest(field=field, value=value):
                event = copy.deepcopy(EVENT)
                event["bookmakers"][0][field] = value
                with self.assertRaises(ProviderPayloadError):
                    self._provider(
                        HttpJsonResponse([event], 200, {"X-Provider-State": provider_state_header()})
                    ).read_batch()
        alias_event = copy.deepcopy(EVENT)
        alias_event["bookmakers"][0]["staleSeconds"] = 1.0
        with self.assertRaises(ProviderPayloadError):
            self._provider(
                HttpJsonResponse([alias_event], 200, {"X-Provider-State": provider_state_header()})
            ).read_batch()

    def test_distinct_bookmaker_blocks_preserve_distinct_freshness(self):
        event = copy.deepcopy(EVENT)
        second = copy.deepcopy(event["bookmakers"][0])
        second["key"] = "book-b"
        second["title"] = "Book B"
        second["stale_seconds"] = 88.25
        second["last_update_ms"] = 1790020700000
        event["bookmakers"].append(second)
        header = json.dumps(
            {"ts": 1790020800, "src": {
                "book-a": {"age_s": 0.1, "role": "primary"},
                "book-b": {"age_s": 0.2, "role": "primary"},
            }}, separators=(",", ":")
        )
        batch = self._provider(HttpJsonResponse([event], 200, {"X-Provider-State": header})).read_batch()
        self.assertEqual(batch.quality_flags, ())
        by_book = {}
        for quote in batch.quotes:
            by_book.setdefault(quote.metadata["bookmaker_key"], quote.metadata)
        self.assertEqual(by_book["book-a"]["provider_block_stale_seconds"], 1.0)
        self.assertEqual(by_book["book-b"]["provider_block_stale_seconds"], 88.25)
        self.assertEqual(by_book["book-a"]["provider_block_last_update_ms"], 1790020799000)
        self.assertEqual(by_book["book-b"]["provider_block_last_update_ms"], 1790020700000)

    def test_topped_up_evidence_survives_chunked_delivery(self):
        event = copy.deepcopy(EVENT)
        event["bookmakers"][0]["topped_up"] = True
        provider = self._provider(
            HttpJsonResponse([event], 200, {"X-Provider-State": provider_state_header()})
        )
        first = provider.read_batch(max_items=1)
        second = provider.read_batch(max_items=1)
        self.assertEqual(first.quality_flags, ("TRUNCATED_BATCH", "UPSTREAM_BOOKMAKER_TOPPED_UP"))
        self.assertEqual(second.quality_flags, ("UPSTREAM_BOOKMAKER_TOPPED_UP",))
        self.assertTrue(first.quotes[0].metadata["provider_block_topped_up"])
        self.assertTrue(second.quotes[0].metadata["provider_block_topped_up"])


if __name__ == "__main__":
    unittest.main()
