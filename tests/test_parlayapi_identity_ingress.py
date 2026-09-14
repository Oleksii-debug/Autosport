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
    def _read(event: dict) -> tuple:
        provider = ParlayApiTableTennisProvider(
            "key",
            transport=lambda *_: HttpJsonResponse([event], 200, {}),
            clock=lambda: "2026-09-12T20:00:10+00:00",
        )
        return provider.read_batch().quotes

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
        with self.assertRaisesRegex(ProviderPayloadError, "bookmaker identity must be a string"):
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

    def test_missing_primary_string_fields_use_existing_fallbacks(self):
        event = copy.deepcopy(_BASE_EVENT)
        del event["id"]
        event["canonical_event_id"] = "canonical-100"
        del event["bookmakers"][0]["key"]
        quotes = self._read(event)
        self.assertEqual(len(quotes), 1)
        self.assertEqual(quotes[0].provider_event_id, "canonical-100")
        self.assertEqual(quotes[0].provider_market_id, "Book A:h2h")
        self.assertEqual(quotes[0].provider_selection_id, "Player A")

    def test_missing_bookmaker_identity_fails_closed_instead_of_colliding_on_synthetic_default(self):
        event = copy.deepcopy(_BASE_EVENT)
        del event["bookmakers"][0]["key"]
        del event["bookmakers"][0]["title"]
        with self.assertRaisesRegex(ProviderPayloadError, "missing key/title identity"):
            self._read(event)


if __name__ == "__main__":
    unittest.main()
