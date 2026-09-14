from __future__ import annotations

import copy
import unittest

from autosport.parlayapi_provider import (
    HttpJsonResponse,
    ParlayApiTableTennisProvider,
    ProviderPayloadError,
)


_BASE_EVENT = {
    "id": "event-1",
    "bookmakers": [
        {
            "key": "book-a",
            "last_update": "2026-09-14T08:00:00Z",
            "markets": [
                {
                    "key": "h2h",
                    "last_update": "2026-09-14T08:00:00Z",
                    "outcomes": [
                        {"name": "A", "price": 2.0},
                        {"name": "B", "price": 2.1},
                    ],
                }
            ],
        }
    ],
}


def _provider(event: object) -> ParlayApiTableTennisProvider:
    return ParlayApiTableTennisProvider(
        "key",
        transport=lambda *_: HttpJsonResponse([event], 200, {}),
        clock=lambda: "2026-09-14T08:00:10+00:00",
    )


class ParlayApiMalformedQuoteStructureTests(unittest.TestCase):
    def test_nested_malformed_quote_structures_fail_closed(self) -> None:
        cases: list[tuple[str, object, str]] = []

        event = copy.deepcopy(_BASE_EVENT)
        event["bookmakers"] = ["not-an-object"]
        cases.append(("bookmaker", event, "bookmaker entries must be objects"))

        event = copy.deepcopy(_BASE_EVENT)
        event["bookmakers"][0]["markets"] = "not-a-list"
        cases.append(("markets-type", event, "bookmaker markets must be a list"))

        event = copy.deepcopy(_BASE_EVENT)
        event["bookmakers"][0]["markets"] = ["not-an-object"]
        cases.append(("market", event, "market entries must be objects"))

        event = copy.deepcopy(_BASE_EVENT)
        del event["bookmakers"][0]["markets"][0]["key"]
        cases.append(("market-key", event, "market is missing key"))

        event = copy.deepcopy(_BASE_EVENT)
        event["bookmakers"][0]["markets"][0]["outcomes"] = "not-a-list"
        cases.append(("outcomes-type", event, "market outcomes must be a list"))

        event = copy.deepcopy(_BASE_EVENT)
        event["bookmakers"][0]["markets"][0]["outcomes"] = ["not-an-object"]
        cases.append(("outcome", event, "outcome entries must be objects"))

        event = copy.deepcopy(_BASE_EVENT)
        del event["bookmakers"][0]["markets"][0]["outcomes"][0]["name"]
        cases.append(("outcome-name", event, "outcome is missing name"))

        event = copy.deepcopy(_BASE_EVENT)
        event["bookmakers"][0]["markets"][0]["outcomes"][0]["price"] = "not-a-price"
        cases.append(("price", event, "outcome price must be finite decimal odds greater than 1"))

        event = copy.deepcopy(_BASE_EVENT)
        event["bookmakers"][0]["markets"][0]["outcomes"][0]["point"] = "not-a-point"
        cases.append(("point", event, "outcome point must be a finite decimal"))

        for label, malformed, message in cases:
            with self.subTest(label=label):
                with self.assertRaisesRegex(ProviderPayloadError, message):
                    _provider(malformed).read_batch(max_items=100)

    def test_valid_missing_price_remains_explicitly_unpriced_not_malformed(self) -> None:
        event = copy.deepcopy(_BASE_EVENT)
        event["bookmakers"][0]["markets"][0]["outcomes"][0].pop("price")

        batch = _provider(event).read_batch(max_items=100)

        self.assertEqual([quote.provider_selection_id for quote in batch.quotes], ["B"])


if __name__ == "__main__":
    unittest.main()
