from __future__ import annotations

import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from autosport.historical_acquisition import capture_historical_acquisition_bundle
from autosport.parlayapi_provider import (
    HttpJsonResponse,
    ParlayApiTableTennisProvider,
    ProviderPayloadError,
)


class _ScopeMutatingTransport:
    def __init__(self) -> None:
        self.provider: ParlayApiTableTennisProvider | None = None
        self.urls: list[str] = []
        self.odds_calls = 0

    def __call__(
        self,
        url: str,
        headers: dict[str, str],
        timeout: float,
    ) -> HttpJsonResponse:
        self.urls.append(url)
        parsed = urlparse(url)
        if parsed.path.endswith("/coverage"):
            query = parse_qs(parsed.query)
            date_from = query["dateFrom"][0]
            date_to = query["dateTo"][0]
            return HttpJsonResponse(
                {
                    "sport_key": "table_tennis",
                    "window": {"date_from": date_from, "date_to": date_to},
                    "by_source": {
                        "test-source": {
                            "rows": 1,
                            "first_date": date_from,
                            "last_date": date_to,
                            "priced_rows": 1,
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
        if parsed.path.endswith("/odds"):
            self.odds_calls += 1
            if self.odds_calls == 1:
                assert self.provider is not None
                self.provider.regions = ("eu",)
                self.provider.markets = ("totals",)
            return HttpJsonResponse(
                {
                    "timestamp": "2026-09-12T10:00:00Z",
                    "previous_timestamp": "2026-09-12T09:55:00Z",
                    "next_timestamp": "2026-09-12T10:05:00Z",
                    "data": [],
                },
                200,
                {"x-api-version": "test"},
            )
        raise AssertionError(f"unexpected URL after scope mutation: {url}")


def test_bundle_fails_before_second_snapshot_after_inflight_scope_mutation() -> None:
    transport = _ScopeMutatingTransport()
    provider = ParlayApiTableTennisProvider(
        "unit-test-key",
        transport=transport,
        clock=lambda: "2026-09-13T03:00:00+00:00",
        sleeper=lambda _: None,
    )
    transport.provider = provider

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / "acquisition"
        with pytest.raises(
            ProviderPayloadError,
            match="provider logical scope changed during acquisition",
        ):
            capture_historical_acquisition_bundle(
                provider,
                requested_at=(
                    "2026-09-12T10:03:00Z",
                    "2026-09-12T10:08:00Z",
                ),
                results_date="2026-09-10",
                output_dir=root,
            )
        assert not root.exists()

    odds_urls = [url for url in transport.urls if urlparse(url).path.endswith("/odds")]
    assert len(odds_urls) == 1
    first_scope = parse_qs(urlparse(odds_urls[0]).query)
    assert first_scope["regions"] == ["us"]
    assert first_scope["markets"] == ["h2h,spreads,totals"]
