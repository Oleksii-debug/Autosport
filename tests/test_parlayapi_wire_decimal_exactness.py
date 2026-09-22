from __future__ import annotations

from decimal import Decimal
import json
import unittest
from unittest.mock import patch

from autosport.parlayapi_provider import ParlayApiTableTennisProvider


class _FakeResponse:
    def __init__(self, raw: bytes, *, headers: dict[str, str] | None = None) -> None:
        self._raw = raw
        self.status = 200
        self.headers = headers or {}

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class _FakeOpener:
    def __init__(self, responder) -> None:
        self._responder = responder

    def open(self, request, timeout):
        return self._responder(request.full_url, timeout)


class ParlayApiWireDecimalExactnessTests(unittest.TestCase):
    def test_production_odds_transport_preserves_fractional_wire_tokens_exactly(self) -> None:
        live_raw = b"""[
          {
            "id":"tt-wire-1",
            "sport_key":"table_tennis",
            "bookmakers":[
              {
                "key":"book-a",
                "last_update":"2026-09-22T10:00:00Z",
                "markets":[
                  {
                    "key":"h2h",
                    "last_update":"2026-09-22T10:00:01Z",
                    "outcomes":[
                      {"name":"A","price":1.234567890123456789}
                    ]
                  },
                  {
                    "key":"spreads",
                    "last_update":"2026-09-22T10:00:02Z",
                    "outcomes":[
                      {
                        "name":"A",
                        "price":2.000000000000000001,
                        "point":-1.500000000000000001
                      }
                    ]
                  }
                ]
              }
            ]
          }
        ]"""

        opener = _FakeOpener(lambda _url, _timeout: _FakeResponse(live_raw))
        with patch("autosport.parlayapi_provider.build_opener", return_value=opener):
            provider = ParlayApiTableTennisProvider(
                "dummy-key",
                clock=lambda: "2026-09-22T10:00:03+00:00",
            )
            batch = provider.read_batch()

        self.assertEqual(len(batch.quotes), 2)
        self.assertEqual(
            batch.quotes[0].decimal_odds,
            Decimal("1.234567890123456789"),
        )
        self.assertEqual(
            batch.quotes[1].decimal_odds,
            Decimal("2.000000000000000001"),
        )
        self.assertEqual(
            batch.quotes[1].provider_market_id,
            "book-a:spreads:1.500000000000000001",
        )

    def test_historical_transport_keeps_existing_canonical_json_hash_path(self) -> None:
        coverage_raw = b"""{
          "sport_key":"table_tennis",
          "window":{"date_from":"2026-09-01","date_to":"2026-09-12"},
          "by_source":{
            "book-a":{
              "rows":1,
              "first_date":"2026-09-01",
              "last_date":"2026-09-12",
              "priced_rows":1
            }
          },
          "_diagnostic_ratio":1.234567890123456789
        }"""
        headers = {
            "x-historical-window-hours": "720",
            "x-historical-window-from": "2026-08-14T00:00:00Z",
        }

        opener = _FakeOpener(
            lambda _url, _timeout: _FakeResponse(coverage_raw, headers=headers)
        )
        with patch("autosport.parlayapi_provider.build_opener", return_value=opener):
            provider = ParlayApiTableTennisProvider(
                "dummy-key",
                clock=lambda: "2026-09-22T10:00:03+00:00",
            )
            report = provider.historical_coverage("2026-09-01", "2026-09-12")

        self.assertEqual(report.total_rows, 1)
        self.assertEqual(report.total_priced_rows, 1)
        self.assertEqual(len(report.response_sha256), 64)

        # The historical canonicalization contract still receives ordinary JSON
        # float values and therefore remains serializable by stdlib json.dumps.
        parsed = json.loads(coverage_raw.decode("utf-8"))
        canonical = json.dumps(
            parsed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self.assertEqual(
            report.response_sha256,
            __import__("hashlib").sha256(canonical.encode("utf-8")).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()
