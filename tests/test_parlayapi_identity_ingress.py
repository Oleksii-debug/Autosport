import copy
import unittest

from autosport.parlayapi_provider import (
    HttpJsonResponse,
    ParlayApiTableTennisProvider,
    ProviderPayloadError,
)


_BASE_EVENT = {
    "id": "tt-100",
    "sport_key": "table_tennis",
    "bookmakers": [
        {
            "key": "book-a",
            "title": "Book A",
            "last_update": "2026-09-12T20:00:00Z",
            "markets": [
                {
                    "key": "h2h",
                    "last_update": "2026-09-12T20:00:01Z",
                    "outcomes": [{"name": "Player A", "price": 1.80}],
                }
            ],
        }
    ],
}


class ParlayApiIdentityIngressTests(unittest.TestCase):
    @staticmethod
    def _read_events(events: list[dict], *, max_items: int = 1000) -> tuple:
        provider = ParlayApiTableTennisProvider(
            "key",
            transport=lambda *_: HttpJsonResponse(events, 200, {}),
            clock=lambda: "2026-09-12T20:00:10+00:00",
        )
        return provider.read_batch(max_items=max_items).quotes

    @classmethod
    def _read(cls, event: dict) -> tuple:
        return cls._read_events([event])

    def test_non_string_event_id_does_not_fall_back_or_coerce(self):
        event = copy.deepcopy(_BASE_EVENT)
        event["id"] = 0
        event["canonical_event_id"] = "fallback-id"
        with self.assertRaisesRegex(ProviderPayloadError, "event id must be a string"):
            self._read(event)

    def test_empty_event_id_does_not_fall_back_to_canonical_id(self):
        event = copy.deepcopy(_BASE_EVENT)
        event["id"] = ""
        event["canonical_event_id"] = "fallback-id"
        with self.assertRaisesRegex(ProviderPayloadError, "non-empty trimmed string"):
            self._read(event)

    def test_colon_bearing_event_id_fails_before_source_scope_can_alias(self):
        event = copy.deepcopy(_BASE_EVENT)
        event["id"] = "scope:event"
        with self.assertRaisesRegex(ProviderPayloadError, "source-scope delimiter"):
            self._read(event)

    def test_non_string_bookmaker_key_does_not_fall_back_to_title(self):
        event = copy.deepcopy(_BASE_EVENT)
        event["bookmakers"][0]["key"] = 0
        with self.assertRaisesRegex(ProviderPayloadError, "bookmaker key must be a string"):
            self._read(event)

    def test_empty_bookmaker_key_does_not_fall_back_to_title(self):
        event = copy.deepcopy(_BASE_EVENT)
        event["bookmakers"][0]["key"] = ""
        with self.assertRaisesRegex(ProviderPayloadError, "non-empty trimmed string"):
            self._read(event)

    def test_non_string_market_key_is_not_stringified_into_identity(self):
        event = copy.deepcopy(_BASE_EVENT)
        event["bookmakers"][0]["markets"][0]["key"] = 7
        with self.assertRaisesRegex(ProviderPayloadError, "market key must be a string"):
            self._read(event)

    def test_non_string_selection_name_is_not_stringified_into_identity(self):
        event = copy.deepcopy(_BASE_EVENT)
        event["bookmakers"][0]["markets"][0]["outcomes"][0]["name"] = 7
        with self.assertRaisesRegex(ProviderPayloadError, "outcome name must be a string"):
            self._read(event)

    def test_whitespace_identity_is_rejected_not_trimmed(self):
        event = copy.deepcopy(_BASE_EVENT)
        event["id"] = " tt-100 "
        with self.assertRaisesRegex(ProviderPayloadError, "non-empty trimmed string"):
            self._read(event)

    def test_missing_event_id_uses_existing_canonical_id_fallback(self):
        event = copy.deepcopy(_BASE_EVENT)
        del event["id"]
        event["canonical_event_id"] = "canonical-100"
        quotes = self._read(event)
        self.assertEqual(len(quotes), 1)
        self.assertEqual(quotes[0].provider_event_id, "canonical-100")
        self.assertEqual(quotes[0].provider_market_id, "book-a:h2h")
        self.assertEqual(quotes[0].provider_selection_id, "Player A")

    def test_bookmaker_title_cannot_mint_canonical_market_identity(self):
        event = copy.deepcopy(_BASE_EVENT)
        del event["bookmakers"][0]["key"]
        with self.assertRaisesRegex(ProviderPayloadError, "stable provider key identity"):
            self._read(event)

    def test_missing_bookmaker_identity_fails_closed_instead_of_colliding_on_synthetic_default(self):
        event = copy.deepcopy(_BASE_EVENT)
        del event["bookmakers"][0]["key"]
        del event["bookmakers"][0]["title"]
        with self.assertRaisesRegex(ProviderPayloadError, "stable provider key identity"):
            self._read(event)

    def test_same_bookmaker_title_with_distinct_provider_keys_does_not_alias(self):
        event = copy.deepcopy(_BASE_EVENT)
        second_bookmaker = copy.deepcopy(event["bookmakers"][0])
        second_bookmaker["key"] = "book-b"
        event["bookmakers"].append(second_bookmaker)
        quotes = self._read(event)
        self.assertEqual(len(quotes), 2)
        self.assertEqual(
            {quote.provider_market_id for quote in quotes},
            {"book-a:h2h", "book-b:h2h"},
        )

    def test_provider_sport_scope_mismatch_fails_closed(self):
        event = copy.deepcopy(_BASE_EVENT)
        event["sport_key"] = "soccer"
        with self.assertRaisesRegex(ProviderPayloadError, "sport_key conflicts"):
            self._read(event)

    def test_partial_participant_binding_fails_closed(self):
        event = copy.deepcopy(_BASE_EVENT)
        event["home_team"] = "Player A"
        with self.assertRaisesRegex(ProviderPayloadError, "declare both"):
            self._read(event)

    def test_timezone_naive_declared_start_cannot_be_canonical_identity(self):
        event = copy.deepcopy(_BASE_EVENT)
        event.update(
            commence_time="2026-09-12T20:30:00",
            home_team="Player A",
            away_team="Player B",
        )
        with self.assertRaisesRegex(ProviderPayloadError, "timezone offset"):
            self._read(event)

    def test_equivalent_declared_start_offsets_share_one_canonical_instant(self):
        first = copy.deepcopy(_BASE_EVENT)
        first.update(
            commence_time="2026-09-12T20:30:00Z",
            home_team="Player A",
            away_team="Player B",
        )
        second = copy.deepcopy(first)
        second["commence_time"] = "2026-09-12T22:30:00+02:00"
        second["bookmakers"][0]["key"] = "book-b"

        quotes = self._read_events([first, second])

        self.assertEqual(len(quotes), 2)
        self.assertEqual(
            {quote.provider_market_id for quote in quotes},
            {"book-a:h2h", "book-b:h2h"},
        )

    def test_repeated_event_id_cannot_change_declared_participants(self):
        first = copy.deepcopy(_BASE_EVENT)
        first.update(
            commence_time="2026-09-12T20:30:00Z",
            home_team="Player A",
            away_team="Player B",
        )
        second = copy.deepcopy(first)
        second["home_team"] = "Player C"
        with self.assertRaisesRegex(ProviderPayloadError, "conflicting canonical identity witnesses"):
            self._read_events([first, second])

    def test_repeated_event_id_cannot_change_declared_start(self):
        first = copy.deepcopy(_BASE_EVENT)
        first.update(
            commence_time="2026-09-12T20:30:00Z",
            home_team="Player A",
            away_team="Player B",
        )
        second = copy.deepcopy(first)
        second["commence_time"] = "2026-09-12T20:31:00Z"
        with self.assertRaisesRegex(ProviderPayloadError, "conflicting canonical identity witnesses"):
            self._read_events([first, second])

    def test_duplicate_selection_token_in_same_canonical_market_is_ambiguous(self):
        event = copy.deepcopy(_BASE_EVENT)
        outcomes = event["bookmakers"][0]["markets"][0]["outcomes"]
        outcomes.append({"name": "Player A", "price": 1.90})
        with self.assertRaisesRegex(ProviderPayloadError, "ambiguous duplicate"):
            self._read(event)

    def test_duplicate_beyond_requested_batch_limit_fails_before_partial_batch_escapes(self):
        event = copy.deepcopy(_BASE_EVENT)
        outcomes = event["bookmakers"][0]["markets"][0]["outcomes"]
        outcomes.append({"name": "Player A", "price": 1.90})
        with self.assertRaisesRegex(ProviderPayloadError, "ambiguous duplicate"):
            self._read_events([event], max_items=1)

    def test_same_selection_token_at_distinct_handicap_lines_remains_distinct(self):
        event = copy.deepcopy(_BASE_EVENT)
        market = event["bookmakers"][0]["markets"][0]
        market["key"] = "spreads"
        market["outcomes"] = [
            {"name": "Player A", "price": 1.80, "point": -1.5},
            {"name": "Player A", "price": 1.90, "point": -2.5},
        ]
        quotes = self._read(event)
        self.assertEqual(len(quotes), 2)
        self.assertNotEqual(quotes[0].provider_market_id, quotes[1].provider_market_id)

    def test_same_participant_labels_under_distinct_provider_event_ids_do_not_alias(self):
        first = copy.deepcopy(_BASE_EVENT)
        first.update(
            commence_time="2026-09-12T20:30:00Z",
            home_team="Player A",
            away_team="Player B",
        )
        second = copy.deepcopy(first)
        second["id"] = "tt-101"
        second["bookmakers"][0]["markets"][0]["outcomes"][0]["price"] = 1.95
        quotes = self._read_events([first, second])
        self.assertEqual({quote.provider_event_id for quote in quotes}, {"tt-100", "tt-101"})


if __name__ == "__main__":
    unittest.main()
