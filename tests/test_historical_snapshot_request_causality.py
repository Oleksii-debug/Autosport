from __future__ import annotations

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
    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> HttpJsonResponse:
        return HttpJsonResponse(
            {
                "timestamp": "2026-09-13T01:00:00Z",
                "data": [],
            },
            200,
            {"x-api-version": "test"},
        )


class HistoricalSnapshotRequestCausalityTests(unittest.TestCase):
    def test_requested_historical_time_after_capture_clock_fails_closed(self) -> None:
        provider = ParlayApiTableTennisProvider(
            "test-key",
            transport=_Transport(),
            clock=lambda: "2026-09-13T02:00:00+00:00",
            sleeper=lambda _: None,
        )
        with tempfile.TemporaryDirectory() as tmp:
            market = Path(tmp) / "market.jsonl"
            evidence = Path(tmp) / "evidence.json"
            with self.assertRaisesRegex(ProviderPayloadError, "capture clock is before requested_at"):
                capture_historical_snapshot(
                    provider,
                    requested_at="2026-09-14T00:00:00+00:00",
                    output_path=market,
                    evidence_path=evidence,
                )
            self.assertFalse(market.exists())
            self.assertFalse(evidence.exists())


if __name__ == "__main__":
    unittest.main()
