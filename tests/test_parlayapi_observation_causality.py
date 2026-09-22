from __future__ import annotations

import unittest

from autosport.parlayapi_provider import (
    HttpJsonResponse,
    ParlayApiTableTennisProvider,
    ProviderTransportError,
)


OBSERVED = "2026-09-22T09:50:01+00:00"

LIVE_EVENT = {
    "id": "tt-causal-1",
    "bookmakers": [
        {
            "key": "book-a",
            "markets": [
                {
                    "key": "h2h",
                    "outcomes": [
                        {"name": "Player A", "price": 2.0},
                    ],
                }
            ],
        }
    ],
}

COVERAGE = {
    "sport_key": "table_tennis",
    "window": {"date_from": "2026-09-01", "date_to": "2026-09-12"},
    "by_source": {
        "book-a": {
            "rows": 1,
            "first_date": "2026-09-01",
            "last_date": "2026-09-12",
            "priced_rows": 1,
        }
    },
}

COVERAGE_HEADERS = {
    "x-historical-window-hours": "720",
    "x-historical-window-from": "2026-08-14T00:00:00Z",
}


class ParlayApiObservationCausalityTests(unittest.TestCase):
    def test_default_transport_rejects_caller_clock_override(self) -> None:
        with self.assertRaisesRegex(ValueError, "clock override requires a custom transport"):
            ParlayApiTableTennisProvider(
                "dummy-key",
                clock=lambda: "2000-01-01T00:00:00+00:00",
            )

    def test_live_observation_clock_is_sampled_after_successful_transport(self) -> None:
        response_returned = False
        clock_calls = 0

        def transport(*_args):
            nonlocal response_returned
            response_returned = True
            return HttpJsonResponse([LIVE_EVENT], 200, {})

        def clock() -> str:
            nonlocal clock_calls
            clock_calls += 1
            self.assertTrue(response_returned)
            return OBSERVED

        provider = ParlayApiTableTennisProvider(
            "dummy-key",
            transport=transport,
            clock=clock,
        )

        batch = provider.read_batch()

        self.assertEqual(clock_calls, 1)
        self.assertEqual(batch.cursor, OBSERVED)
        self.assertEqual(len(batch.quotes), 1)
        self.assertEqual(batch.quotes[0].observed_ts, OBSERVED)

    def test_retry_cannot_backdate_live_observation_to_first_attempt(self) -> None:
        attempts = 0
        sleeps: list[float] = []

        def transport(*_args):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ProviderTransportError("transient", 503)
            return HttpJsonResponse([LIVE_EVENT], 200, {})

        def clock() -> str:
            self.assertEqual(attempts, 2)
            return OBSERVED

        provider = ParlayApiTableTennisProvider(
            "dummy-key",
            transport=transport,
            clock=clock,
            sleeper=sleeps.append,
            max_attempts=2,
        )

        batch = provider.read_batch()

        self.assertEqual(attempts, 2)
        self.assertEqual(sleeps, [0.25])
        self.assertEqual(batch.quotes[0].observed_ts, OBSERVED)

    def test_terminal_transport_failure_never_mints_live_observation_time(self) -> None:
        def transport(*_args):
            raise ProviderTransportError("unavailable", 503)

        def clock() -> str:
            self.fail("failed transport must not mint product observation time")

        provider = ParlayApiTableTennisProvider(
            "dummy-key",
            transport=transport,
            clock=clock,
            max_attempts=1,
        )

        with self.assertRaises(ProviderTransportError):
            provider.read_batch()

    def test_historical_observed_at_is_sampled_after_successful_transport(self) -> None:
        response_returned = False
        clock_calls = 0

        def transport(*_args):
            nonlocal response_returned
            response_returned = True
            return HttpJsonResponse(COVERAGE, 200, COVERAGE_HEADERS)

        def clock() -> str:
            nonlocal clock_calls
            clock_calls += 1
            self.assertTrue(response_returned)
            return OBSERVED

        provider = ParlayApiTableTennisProvider(
            "dummy-key",
            transport=transport,
            clock=clock,
        )

        report = provider.historical_coverage("2026-09-01", "2026-09-12")

        self.assertEqual(clock_calls, 1)
        self.assertEqual(report.observed_at, OBSERVED)

    def test_terminal_transport_failure_never_mints_historical_observation_time(self) -> None:
        def transport(*_args):
            raise ProviderTransportError("unavailable", 503)

        def clock() -> str:
            self.fail("failed historical transport must not mint observation time")

        provider = ParlayApiTableTennisProvider(
            "dummy-key",
            transport=transport,
            clock=clock,
            max_attempts=1,
        )

        with self.assertRaises(ProviderTransportError):
            provider.historical_coverage("2026-09-01", "2026-09-12")


if __name__ == "__main__":
    unittest.main()
