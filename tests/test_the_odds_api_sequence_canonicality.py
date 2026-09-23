import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from autosport.providers import CanonicalNormalizer
from autosport.storage import SQLiteMarketStore
from autosport.the_odds_api_provider import HttpJsonResponse, TheOddsApiProvider


def _event(
    *,
    price: Decimal = Decimal("2.10"),
    market_last_update: str = "2026-09-23T13:00:00Z",
) -> dict[str, object]:
    return {
        "id": "0123456789abcdef0123456789abcdef",
        "sport_key": "soccer_epl",
        "commence_time": "2026-09-23T15:00:00Z",
        "bookmakers": [
            {
                "key": "book-a",
                "sid": "event-77",
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": market_last_update,
                        "sid": "market-88",
                        "outcomes": [
                            {
                                "name": "Alpha",
                                "price": price,
                                "sid": "outcome-99",
                            }
                        ],
                    }
                ],
            }
        ],
    }


def _utc_microseconds(value: str) -> int:
    instant = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = instant - epoch
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


class TheOddsApiSequenceCanonicalityTests(unittest.TestCase):
    def test_later_provider_source_time_is_later_sequence_and_current_projection(self) -> None:
        responses = iter(
            [
                HttpJsonResponse(
                    [_event(price=Decimal("2.10"), market_last_update="2026-09-23T13:00:00Z")],
                    200,
                    {"x-requests-remaining": "499"},
                    body_sha256="1" * 64,
                ),
                HttpJsonResponse(
                    [_event(price=Decimal("2.20"), market_last_update="2026-09-23T13:01:00Z")],
                    200,
                    {"x-requests-remaining": "498"},
                    body_sha256="2" * 64,
                ),
            ]
        )
        clocks = iter(("2026-09-23T14:00:00Z", "2026-09-23T14:01:00Z"))
        provider = TheOddsApiProvider(
            "synthetic-secret",
            sport="soccer_epl",
            transport=lambda *_: next(responses),
            clock=lambda: next(clocks),
        )

        first = provider.read_batch().quotes[0]
        second = provider.read_batch().quotes[0]

        self.assertEqual(first.sequence, _utc_microseconds("2026-09-23T13:00:00Z"))
        self.assertEqual(second.sequence, _utc_microseconds("2026-09-23T13:01:00Z"))
        self.assertGreater(second.sequence, first.sequence)

        normalizer = CanonicalNormalizer()
        first_event = normalizer.normalize(provider.source_id, first)
        second_event = normalizer.normalize(provider.source_id, second)
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                self.assertTrue(store.append(first_event))
                self.assertTrue(store.append(second_event))
                current = store.current_by_source()[
                    (provider.source_id, second_event.quote_key)
                ]
                self.assertEqual(current.decimal_odds, Decimal("2.20"))
                self.assertEqual(current.sequence, second.sequence)
            finally:
                store.close()

    def test_unchanged_quote_with_new_acquisition_evidence_dedupes_cleanly(self) -> None:
        responses = iter(
            [
                HttpJsonResponse(
                    [_event()],
                    200,
                    {
                        "x-requests-remaining": "499",
                        "x-requests-used": "1",
                        "x-requests-last": "1",
                    },
                    body_sha256="3" * 64,
                ),
                HttpJsonResponse(
                    [_event()],
                    200,
                    {
                        "x-requests-remaining": "498",
                        "x-requests-used": "2",
                        "x-requests-last": "1",
                    },
                    body_sha256="4" * 64,
                ),
            ]
        )
        clocks = iter(("2026-09-23T14:00:00Z", "2026-09-23T14:01:00Z"))
        provider = TheOddsApiProvider(
            "synthetic-secret",
            sport="soccer_epl",
            transport=lambda *_: next(responses),
            clock=lambda: next(clocks),
        )

        first_batch = provider.read_batch()
        first_evidence = provider.last_request_evidence
        second_batch = provider.read_batch()
        second_evidence = provider.last_request_evidence
        first = first_batch.quotes[0]
        second = second_batch.quotes[0]

        self.assertEqual(first.sequence, second.sequence)
        self.assertEqual(first.metadata["request"], second.metadata["request"])
        for acquisition_field in ("observed_at", "response_sha256", "quota"):
            self.assertNotIn(acquisition_field, first.metadata["request"])
            self.assertNotIn(acquisition_field, second.metadata["request"])

        self.assertIsNotNone(first_evidence)
        self.assertIsNotNone(second_evidence)
        self.assertEqual(first_evidence.quota_remaining, 499)
        self.assertEqual(second_evidence.quota_remaining, 498)
        self.assertEqual(first_evidence.response_sha256, "3" * 64)
        self.assertEqual(second_evidence.response_sha256, "4" * 64)
        self.assertEqual(first_batch.cursor, "3" * 64)
        self.assertEqual(second_batch.cursor, "4" * 64)

        normalizer = CanonicalNormalizer()
        first_event = normalizer.normalize(provider.source_id, first)
        second_event = normalizer.normalize(provider.source_id, second)
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                self.assertTrue(store.append(first_event))
                self.assertFalse(store.append(second_event))
                self.assertEqual(len(store.events()), 1)
            finally:
                store.close()

    def test_changed_quote_at_same_source_timestamp_is_conflicting_source_truth(self) -> None:
        responses = iter(
            [
                HttpJsonResponse([_event(price=Decimal("2.10"))], 200, {}),
                HttpJsonResponse([_event(price=Decimal("2.11"))], 200, {}),
            ]
        )
        clocks = iter(("2026-09-23T14:00:00Z", "2026-09-23T14:01:00Z"))
        provider = TheOddsApiProvider(
            "synthetic-secret",
            sport="soccer_epl",
            transport=lambda *_: next(responses),
            clock=lambda: next(clocks),
        )

        first = provider.read_batch().quotes[0]
        second = provider.read_batch().quotes[0]
        self.assertEqual(first.sequence, second.sequence)

        normalizer = CanonicalNormalizer()
        first_event = normalizer.normalize(provider.source_id, first)
        second_event = normalizer.normalize(provider.source_id, second)
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                self.assertTrue(store.append(first_event))
                with self.assertRaisesRegex(
                    ValueError,
                    "conflicting duplicate market event identity",
                ):
                    store.append(second_event)
            finally:
                store.close()

    def test_historical_sequence_is_actual_snapshot_time_not_payload_hash(self) -> None:
        payload = {
            "timestamp": "2026-09-23T12:40:00Z",
            "previous_timestamp": "2026-09-23T12:35:00Z",
            "next_timestamp": "2026-09-23T12:45:00Z",
            "data": [
                _event(
                    price=Decimal("2.10"),
                    market_last_update="2026-09-23T12:39:00Z",
                )
            ],
        }
        provider = TheOddsApiProvider(
            "synthetic-secret",
            sport="soccer_epl",
            transport=lambda *_: HttpJsonResponse(payload, 200, {}),
            clock=lambda: "2026-09-23T14:00:00Z",
        )

        snapshot = provider.read_historical_snapshot("2026-09-23T12:42:00Z")
        self.assertEqual(
            snapshot.batch.quotes[0].sequence,
            _utc_microseconds("2026-09-23T12:40:00Z"),
        )


if __name__ == "__main__":
    unittest.main()
