import unittest
from decimal import Decimal

from autosport.the_odds_api_provider import (
    HttpJsonResponse,
    TheOddsApiPayloadError,
    TheOddsApiProvider,
)


def _event():
    return {
        "id": "0123456789abcdef0123456789abcdef",
        "sport_key": "soccer_epl",
        "sport_title": "soccer_epl",
        "commence_time": "2026-09-22T15:00:00Z",
        "home_team": "Alpha",
        "away_team": "Beta",
        "bookmakers": [
            {
                "key": "book-a",
                "title": "Book A",
                "last_update": "2026-09-22T12:39:00Z",
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": "2026-09-22T12:39:00Z",
                        "outcomes": [
                            {"name": "Alpha", "price": Decimal("2.10")}
                        ],
                    }
                ],
            }
        ],
    }


def _provider(payload):
    return TheOddsApiProvider(
        "k",
        sport="soccer_epl",
        transport=lambda *_: HttpJsonResponse(payload, 200, {}),
        clock=lambda: "2026-09-22T14:00:00+00:00",
    )


class TheOddsApiHistoricalNeighborConsistencyTests(unittest.TestCase):
    def test_next_snapshot_at_or_before_requested_time_fails_closed(self):
        payload = {
            "timestamp": "2026-09-22T12:40:00Z",
            "previous_timestamp": "2026-09-22T12:35:00Z",
            "next_timestamp": "2026-09-22T12:41:00Z",
            "data": [_event()],
        }

        with self.assertRaisesRegex(
            TheOddsApiPayloadError,
            "next_timestamp must be later than requested_at",
        ):
            _provider(payload).read_historical_snapshot("2026-09-22T12:42:00Z")

    def test_next_snapshot_after_requested_time_preserves_closest_snapshot_evidence(self):
        payload = {
            "timestamp": "2026-09-22T12:40:00Z",
            "previous_timestamp": "2026-09-22T12:35:00Z",
            "next_timestamp": "2026-09-22T12:45:00Z",
            "data": [_event()],
        }

        snapshot = _provider(payload).read_historical_snapshot(
            "2026-09-22T12:42:00Z"
        )

        self.assertEqual(snapshot.snapshot_at, "2026-09-22T12:40:00Z")
        self.assertEqual(snapshot.next_snapshot_at, "2026-09-22T12:45:00Z")
        self.assertEqual(snapshot.requested_at, "2026-09-22T12:42:00Z")


if __name__ == "__main__":
    unittest.main()
