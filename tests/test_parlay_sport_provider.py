from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse

from autosport.historical_matches import capture_historical_matches
from autosport.parlay_sport_provider import ParlayApiSportProvider
from autosport.parlayapi_provider import HttpJsonResponse, ProviderPayloadError


def _event(*, sport_key: object = "basketball_nba") -> dict[str, object]:
    event: dict[str, object] = {
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
    if sport_key is not None:
        event["sport_key"] = sport_key
    return event


class ParlayApiSportProviderTests(unittest.TestCase):
    def test_authenticated_sport_scope_binds_url_source_and_quotes(self) -> None:
        calls: list[str] = []

        def transport(url, headers, timeout):
            calls.append(url)
            return HttpJsonResponse([_event()], 200, {})

        provider = ParlayApiSportProvider(
            "basketball_nba",
            "secret",
            transport=transport,
            clock=lambda: "2026-09-21T12:00:01+00:00",
            sleeper=lambda _: None,
        )
        batch = provider.read_batch()

        self.assertEqual(batch.source_id, "parlayapi:basketball_nba")
        self.assertEqual(len(batch.quotes), 2)
        self.assertTrue(all(quote.sport == "basketball_nba" for quote in batch.quotes))
        self.assertTrue(
            all(quote.metadata["sport_key"] == "basketball_nba" for quote in batch.quotes)
        )
        self.assertEqual(urlparse(calls[0]).path, "/v1/sports/basketball_nba/odds")

    def test_identical_provider_ids_in_different_sports_keep_distinct_source_scope(self) -> None:
        def basketball_transport(url, headers, timeout):
            return HttpJsonResponse([_event(sport_key="basketball_nba")], 200, {})

        def tennis_transport(url, headers, timeout):
            return HttpJsonResponse([_event(sport_key="tennis_atp")], 200, {})

        basketball = ParlayApiSportProvider(
            "basketball_nba",
            "secret",
            transport=basketball_transport,
            clock=lambda: "2026-09-21T12:00:01+00:00",
            sleeper=lambda _: None,
        ).read_batch()
        tennis = ParlayApiSportProvider(
            "tennis_atp",
            "secret",
            transport=tennis_transport,
            clock=lambda: "2026-09-21T12:00:01+00:00",
            sleeper=lambda _: None,
        ).read_batch()

        self.assertNotEqual(basketball.source_id, tennis.source_id)
        self.assertEqual(basketball.quotes[0].sport, "basketball_nba")
        self.assertEqual(tennis.quotes[0].sport, "tennis_atp")
        self.assertEqual(
            basketball.quotes[0].provider_event_id,
            tennis.quotes[0].provider_event_id,
        )
        self.assertEqual(
            basketball.quotes[0].provider_market_id,
            tennis.quotes[0].provider_market_id,
        )

    def test_explicit_cross_sport_payload_fails_closed(self) -> None:
        provider = ParlayApiSportProvider(
            "basketball_nba",
            "secret",
            transport=lambda *_: HttpJsonResponse(
                [_event(sport_key="table_tennis")], 200, {}
            ),
            clock=lambda: "2026-09-21T12:00:01+00:00",
            sleeper=lambda _: None,
        )

        with self.assertRaisesRegex(ProviderPayloadError, "sport_key"):
            provider.read_batch()

    def test_compact_payload_without_sport_key_uses_request_scope_not_table_tennis_default(self) -> None:
        provider = ParlayApiSportProvider(
            "basketball_nba",
            "secret",
            transport=lambda *_: HttpJsonResponse([_event(sport_key=None)], 200, {}),
            clock=lambda: "2026-09-21T12:00:01+00:00",
            sleeper=lambda _: None,
        )

        quote = provider.read_batch().quotes[0]
        self.assertEqual(quote.sport, "basketball_nba")
        self.assertEqual(quote.metadata["sport_key"], "basketball_nba")

    def test_explicit_null_sport_key_fails_closed_instead_of_inheriting_scope(self) -> None:
        event = _event()
        event["sport_key"] = None
        provider = ParlayApiSportProvider(
            "basketball_nba",
            "secret",
            transport=lambda *_: HttpJsonResponse([event], 200, {}),
            clock=lambda: "2026-09-21T12:00:01+00:00",
            sleeper=lambda _: None,
        )

        with self.assertRaisesRegex(ProviderPayloadError, "sport_key"):
            provider.read_batch()

    def test_sport_key_is_exact_url_safe_identity_not_a_normalized_label(self) -> None:
        invalid = (
            "",
            " basketball_nba",
            "basketball_nba ",
            "Basketball_NBA",
            "basketball-nba",
            "basketball__nba",
            "../table_tennis",
            "баскетбол",
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "sport_key"):
                    ParlayApiSportProvider(value, "secret", transport=lambda *_: None)

    def test_non_table_tennis_preview_is_not_overclaimed(self) -> None:
        with self.assertRaisesRegex(ValueError, "public_preview"):
            ParlayApiSportProvider(
                "basketball_nba",
                public_preview=True,
                transport=lambda *_: None,
            )

        calls: list[str] = []

        def transport(url, headers, timeout):
            calls.append(url)
            return HttpJsonResponse([_event(sport_key="table_tennis")], 200, {})

        provider = ParlayApiSportProvider(
            "table_tennis",
            public_preview=True,
            transport=transport,
            clock=lambda: "2026-09-21T12:00:01+00:00",
            sleeper=lambda _: None,
        )
        provider.read_batch(max_items=1)
        self.assertEqual(urlparse(calls[0]).path, "/v1/try/table_tennis/odds")

    def test_historical_coverage_reuses_configured_sport_scope(self) -> None:
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
        headers = {
            "x-historical-window-hours": "168",
            "x-historical-window-from": "2026-09-14T00:00:00Z",
            "x-api-version": "test",
        }

        def transport(url, request_headers, timeout):
            calls.append(url)
            return HttpJsonResponse(payload, 200, headers)

        provider = ParlayApiSportProvider(
            "basketball_nba",
            "secret",
            transport=transport,
            clock=lambda: "2026-09-21T12:00:01+00:00",
            sleeper=lambda _: None,
        )
        report = provider.historical_coverage("2026-09-20", "2026-09-21")

        self.assertEqual(report.sport_key, "basketball_nba")
        self.assertEqual(
            urlparse(calls[0]).path,
            "/v1/historical/sports/basketball_nba/coverage",
        )

    def test_historical_match_capture_is_sport_scoped_without_deriving_settlement_truth(self) -> None:
        calls: list[str] = []
        headers = {
            "x-historical-window-hours": "168",
            "x-historical-window-from": "2026-09-14T00:00:00Z",
            "x-api-version": "test",
        }

        def transport(url, request_headers, timeout):
            calls.append(url)
            return HttpJsonResponse(
                [{"provider_defined_id": "match-1", "opaque": {"score": "101-99"}}],
                200,
                headers,
            )

        provider = ParlayApiSportProvider(
            "basketball_nba",
            "secret",
            transport=transport,
            clock=lambda: "2026-09-21T12:00:01+00:00",
            sleeper=lambda _: None,
        )
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "matches.json"
            evidence = Path(temp) / "matches.evidence.json"
            capture_historical_matches(
                provider,
                requested_date="2026-09-20",
                output_path=output,
                evidence_path=evidence,
            )
            captured = json.loads(output.read_text(encoding="utf-8"))
            proof = json.loads(evidence.read_text(encoding="utf-8"))

        self.assertEqual(
            urlparse(calls[0]).path,
            "/v1/historical/sports/basketball_nba/matches",
        )
        self.assertEqual(captured["sport_key"], "basketball_nba")
        self.assertEqual(proof["sport_key"], "basketball_nba")
        self.assertFalse(proof["sealed_quote_outcomes_derived"])
        self.assertFalse(proof["replay_corpus_ready"])
        self.assertFalse(proof["real_money_execution"])


if __name__ == "__main__":
    unittest.main()
