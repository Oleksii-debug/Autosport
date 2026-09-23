from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from autosport.historical_acquisition import capture_historical_acquisition_bundle
from autosport.parlayapi_provider import (
    HttpJsonResponse,
    ParlayApiTableTennisProvider,
    ProviderPayloadError,
)


class _CoverageOnlyTransport:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> HttpJsonResponse:
        self.urls.append(url)
        parsed = urlparse(url)
        if not parsed.path.endswith("/coverage"):
            raise AssertionError(f"scope contradiction was not rejected before child acquisition: {url}")
        query = parse_qs(parsed.query)
        date_from = query["dateFrom"][0]
        date_to = query["dateTo"][0]
        return HttpJsonResponse(
            {
                "sport_key": "table_tennis",
                "window": {"date_from": date_from, "date_to": date_to},
                "by_source": {
                    "test-source": {
                        "rows": 3,
                        "first_date": date_from,
                        "last_date": date_to,
                        "priced_rows": 2,
                    }
                },
            },
            200,
            {
                "x-api-version": "test",
                "x-historical-window-hours": "168",
                "x-historical-window-from": "2026-09-06T00:00:00Z",
            },
        )


class HistoricalAcquisitionCoverageScopeBindingTests(unittest.TestCase):
    @staticmethod
    def _provider(transport: _CoverageOnlyTransport) -> ParlayApiTableTennisProvider:
        return ParlayApiTableTennisProvider(
            "unit-test-key",
            transport=transport,
            clock=lambda: "2026-09-13T03:00:00+00:00",
            sleeper=lambda _: None,
        )

    def _assert_forged_report_rejected(self, **changes: object) -> None:
        transport = _CoverageOnlyTransport()
        provider = self._provider(transport)
        real_historical_coverage = provider.historical_coverage

        def forged_report(date_from: str, date_to: str):
            report = real_historical_coverage(date_from, date_to)
            return replace(report, **changes)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "acquisition"
            with patch.object(provider, "historical_coverage", side_effect=forged_report):
                with self.assertRaisesRegex(
                    ProviderPayloadError,
                    "historical coverage preflight .* mismatch",
                ):
                    capture_historical_acquisition_bundle(
                        provider,
                        requested_at=("2026-09-12T10:03:00Z",),
                        results_date="2026-09-10",
                        output_dir=root,
                    )
            self.assertFalse(root.exists())

        self.assertEqual(len(transport.urls), 1)
        self.assertTrue(urlparse(transport.urls[0]).path.endswith("/coverage"))

    def test_returned_coverage_report_cannot_change_requested_scope(self) -> None:
        for changes in (
            {"sport_key": "football"},
            {"date_from": "2026-09-09"},
            {"date_to": "2026-09-13"},
        ):
            with self.subTest(changes=changes):
                self._assert_forged_report_rejected(**changes)

    def test_provider_sport_scope_cannot_change_during_coverage_preflight(self) -> None:
        transport = _CoverageOnlyTransport()
        provider = self._provider(transport)
        real_historical_coverage = provider.historical_coverage

        def mutate_provider_scope(date_from: str, date_to: str):
            report = real_historical_coverage(date_from, date_to)
            provider.sport_key = "football"
            return replace(report, sport_key="football")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "acquisition"
            with patch.object(provider, "historical_coverage", side_effect=mutate_provider_scope):
                with self.assertRaisesRegex(
                    ProviderPayloadError,
                    "historical coverage preflight sport_key mismatch",
                ):
                    capture_historical_acquisition_bundle(
                        provider,
                        requested_at=("2026-09-12T10:03:00Z",),
                        results_date="2026-09-10",
                        output_dir=root,
                    )
            self.assertFalse(root.exists())

        self.assertEqual(len(transport.urls), 1)
        self.assertTrue(urlparse(transport.urls[0]).path.endswith("/coverage"))


if __name__ == "__main__":
    unittest.main()
