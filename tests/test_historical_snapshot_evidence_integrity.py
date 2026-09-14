from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from autosport.historical_snapshot import capture_historical_snapshot
from autosport.parlayapi_provider import (
    HttpJsonResponse,
    ParlayApiTableTennisProvider,
    ProviderPayloadError,
)


class _Transport:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.urls: list[str] = []

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> HttpJsonResponse:
        self.urls.append(url)
        return HttpJsonResponse(self.payload, 200, {"x-api-version": "test"})


def _payload() -> dict[str, object]:
    return {
        "timestamp": "2026-09-12T10:00:00Z",
        "previous_timestamp": "2026-09-12T09:55:00Z",
        "next_timestamp": "2026-09-12T10:05:00Z",
        "data": [
            {
                "id": "tt-1",
                "sport_key": "table_tennis",
                "commence_time": "2026-09-12T11:00:00Z",
                "home_team": "Player A",
                "away_team": "Player B",
                "bookmakers": [
                    {
                        "key": "bovada",
                        "title": "Bovada",
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Player A", "price": 1.8},
                                    {"name": "Player B", "price": 2.1},
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }


class HistoricalSnapshotEvidenceIntegrityTests(unittest.TestCase):
    @staticmethod
    def _provider(transport: _Transport) -> ParlayApiTableTennisProvider:
        return ParlayApiTableTennisProvider(
            "secret-key-must-not-leak",
            transport=transport,
            clock=lambda: "2026-09-13T02:00:00+00:00",
            sleeper=lambda _: None,
        )

    def test_same_output_and_evidence_path_fails_before_provider_request(self) -> None:
        transport = _Transport(_payload())
        provider = self._provider(transport)
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "snapshot.jsonl"
            with self.assertRaisesRegex(
                ValueError,
                "output_path and evidence_path must refer to different files",
            ):
                capture_historical_snapshot(
                    provider,
                    requested_at="2026-09-12T10:03:00Z",
                    output_path=destination,
                    evidence_path=destination,
                )
            self.assertFalse(destination.exists())
        self.assertEqual(transport.urls, [])

    def test_existing_hardlink_alias_fails_without_rewriting_either_name(self) -> None:
        transport = _Transport(_payload())
        provider = self._provider(transport)
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "snapshot.jsonl"
            evidence = Path(temp) / "evidence.json"
            output.write_text("sentinel\n", encoding="utf-8")
            try:
                os.link(output, evidence)
            except OSError as exc:
                self.skipTest(f"hard-link creation is unavailable: {exc}")

            with self.assertRaisesRegex(
                ValueError,
                "output_path and evidence_path must refer to different files",
            ):
                capture_historical_snapshot(
                    provider,
                    requested_at="2026-09-12T10:03:00Z",
                    output_path=output,
                    evidence_path=evidence,
                )

            self.assertEqual(output.read_text(encoding="utf-8"), "sentinel\n")
            self.assertEqual(evidence.read_text(encoding="utf-8"), "sentinel\n")
        self.assertEqual(transport.urls, [])

    def test_nonfinite_provider_response_fails_before_market_or_evidence_publication(self) -> None:
        payload = _payload()
        payload["provider_diagnostic"] = float("nan")
        transport = _Transport(payload)
        provider = self._provider(transport)
        with tempfile.TemporaryDirectory() as temp:
            market = Path(temp) / "snapshot.jsonl"
            evidence = Path(temp) / "evidence.json"
            with self.assertRaisesRegex(
                ProviderPayloadError,
                "strict UTF-8 JSON values",
            ):
                capture_historical_snapshot(
                    provider,
                    requested_at="2026-09-12T10:03:00Z",
                    output_path=market,
                    evidence_path=evidence,
                )
            self.assertFalse(market.exists())
            self.assertFalse(evidence.exists())
        self.assertEqual(len(transport.urls), 1)

    def test_non_utf8_provider_string_fails_before_market_or_evidence_publication(self) -> None:
        payload = _payload()
        payload["provider_diagnostic"] = "\ud800"
        transport = _Transport(payload)
        provider = self._provider(transport)
        with tempfile.TemporaryDirectory() as temp:
            market = Path(temp) / "snapshot.jsonl"
            evidence = Path(temp) / "evidence.json"
            with self.assertRaisesRegex(
                ProviderPayloadError,
                "strict UTF-8 JSON values",
            ):
                capture_historical_snapshot(
                    provider,
                    requested_at="2026-09-12T10:03:00Z",
                    output_path=market,
                    evidence_path=evidence,
                )
            self.assertFalse(market.exists())
            self.assertFalse(evidence.exists())
        self.assertEqual(len(transport.urls), 1)

    def test_capture_does_not_clobber_legacy_fixed_temp_name(self) -> None:
        transport = _Transport(_payload())
        provider = self._provider(transport)
        with tempfile.TemporaryDirectory() as temp:
            market = Path(temp) / "snapshot.jsonl"
            evidence = Path(temp) / "evidence.json"
            legacy_temp = market.with_name(market.name + ".tmp")
            legacy_temp.write_text("unrelated-sentinel\n", encoding="utf-8")

            report = capture_historical_snapshot(
                provider,
                requested_at="2026-09-12T10:03:00Z",
                output_path=market,
                evidence_path=evidence,
            )

            self.assertTrue(report.has_data)
            self.assertTrue(market.is_file())
            self.assertTrue(evidence.is_file())
            self.assertEqual(legacy_temp.read_text(encoding="utf-8"), "unrelated-sentinel\n")
            self.assertEqual(
                [path for path in Path(temp).iterdir() if path.name.startswith(f".{market.name}.")],
                [],
            )


if __name__ == "__main__":
    unittest.main()
