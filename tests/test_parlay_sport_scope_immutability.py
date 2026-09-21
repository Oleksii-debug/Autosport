from __future__ import annotations

import unittest
from urllib.parse import urlparse

from autosport.parlay_sport_provider import ParlayApiSportProvider
from autosport.parlayapi_provider import HttpJsonResponse


def _compact_event() -> dict[str, object]:
    return {
        "id": "event-1",
        "commence_time": "2026-09-21T18:00:00Z",
        "home_team": "Alpha",
        "away_team": "Beta",
        "bookmakers": [
            {
                "key": "book-a",
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": "2026-09-21T12:00:00Z",
                        "outcomes": [
                            {"name": "Alpha", "price": "1.80"},
                            {"name": "Beta", "price": "2.10"},
                        ],
                    }
                ],
            }
        ],
    }


def _attempt_rebind(provider: ParlayApiSportProvider, name: str, value: str) -> None:
    """Exercise public attribute rebinding without prescribing the repair mechanism."""

    try:
        setattr(provider, name, value)
    except (AttributeError, TypeError):
        # A repair may make the configured identity immutable at assignment time.
        pass


class ParlaySportScopeImmutabilityTests(unittest.TestCase):
    def _provider(self, calls: list[str]) -> ParlayApiSportProvider:
        def transport(url, headers, timeout):
            calls.append(url)
            return HttpJsonResponse([_compact_event()], 200, {})

        return ParlayApiSportProvider(
            "basketball_nba",
            "secret",
            transport=transport,
            clock=lambda: "2026-09-21T12:00:01+00:00",
            sleeper=lambda _: None,
        )

    def _assert_original_scope(
        self,
        provider: ParlayApiSportProvider,
        calls: list[str],
    ) -> None:
        batch = provider.read_batch()

        self.assertEqual(urlparse(calls[0]).path, "/v1/sports/basketball_nba/odds")
        self.assertEqual(batch.source_id, "parlayapi:basketball_nba")
        self.assertTrue(batch.quotes)
        self.assertTrue(all(quote.sport == "basketball_nba" for quote in batch.quotes))
        self.assertTrue(
            all(
                quote.metadata["sport_key"] == "basketball_nba"
                for quote in batch.quotes
            )
        )

    def test_sport_key_rebinding_cannot_retarget_configured_provider_scope(self) -> None:
        calls: list[str] = []
        provider = self._provider(calls)

        _attempt_rebind(provider, "sport_key", "tennis_atp")

        self._assert_original_scope(provider, calls)

    def test_source_id_rebinding_cannot_relabel_configured_provider_scope(self) -> None:
        calls: list[str] = []
        provider = self._provider(calls)

        _attempt_rebind(provider, "source_id", "parlayapi:tennis_atp")

        self._assert_original_scope(provider, calls)

    def test_coordinated_rebinding_cannot_turn_one_configured_adapter_into_another(self) -> None:
        calls: list[str] = []
        provider = self._provider(calls)

        _attempt_rebind(provider, "sport_key", "tennis_atp")
        _attempt_rebind(provider, "source_id", "parlayapi:tennis_atp")

        self._assert_original_scope(provider, calls)

    def test_sport_key_rebinding_cannot_retarget_historical_coverage_scope(self) -> None:
        calls: list[str] = []
        payload = {
            "sport_key": "basketball_nba",
            "window": {"date_from": "2026-09-20", "date_to": "2026-09-21"},
            "by_source": {
                "book-a": {
                    "rows": 2,
                    "priced_rows": 2,
                    "first_date": "2026-09-20",
                    "last_date": "2026-09-21",
                }
            },
        }
        response_headers = {
            "x-historical-window-hours": "168",
            "x-historical-window-from": "2026-09-14T00:00:00Z",
            "x-api-version": "test",
        }

        def transport(url, headers, timeout):
            calls.append(url)
            return HttpJsonResponse(payload, 200, response_headers)

        provider = ParlayApiSportProvider(
            "basketball_nba",
            "secret",
            transport=transport,
            clock=lambda: "2026-09-21T12:00:01+00:00",
            sleeper=lambda _: None,
        )
        _attempt_rebind(provider, "sport_key", "tennis_atp")

        report = provider.historical_coverage("2026-09-20", "2026-09-21")

        self.assertEqual(
            urlparse(calls[0]).path,
            "/v1/historical/sports/basketball_nba/coverage",
        )
        self.assertEqual(report.sport_key, "basketball_nba")


if __name__ == "__main__":
    unittest.main()
