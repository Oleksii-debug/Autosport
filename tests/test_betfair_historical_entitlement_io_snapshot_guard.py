from __future__ import annotations

import json
import urllib.request as _urllib_request

import pytest

import autosport.betfair_historical_entitlement as historical_module
from autosport.betfair_account_identity import (
    build_betfair_authenticated_client,
    resolve_betfair_authenticated_account_identity,
)
from autosport.betfair_account_readonly import (
    ACCOUNT_JSON_RPC_ENDPOINT,
    BetfairSessionCredentials,
)
from autosport.betfair_historical_entitlement import (
    BetfairHistoricalEntitlementClient,
    BetfairHistoricalEntitlementError,
    HistoricalDownloadFilter,
    UrllibBetfairHistoricalTransport,
)


PATH = "/data/xds/historic/BASIC/28139610/1.130129050.bz2"
RAW = b"BZh9-guarded-historical-market"


def _install_k07_opener(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, limit: int) -> bytes:
            assert limit >= len(self._payload)
            return self._payload

    class Opener:
        def open(self, fullurl, data=None, timeout: float = 0):
            assert data is None
            request = fullurl
            assert request.full_url == ACCOUNT_JSON_RPC_ENDPOINT
            decoded = json.loads(request.data.decode("utf-8"))
            payload = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": decoded["id"],
                    "result": {
                        "currencyCode": "EUR",
                        "localeCode": "en",
                        "region": "GBR",
                        "timezone": "Europe/London",
                    },
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            return Response(payload)

    monkeypatch.setattr(_urllib_request, "_opener", Opener())


def _context(monkeypatch: pytest.MonkeyPatch):
    _install_k07_opener(monkeypatch)
    client = build_betfair_authenticated_client(
        BetfairSessionCredentials("app-key", "session-token")
    )
    identity = resolve_betfair_authenticated_account_identity(client)
    return client, identity


def _filter() -> HistoricalDownloadFilter:
    return HistoricalDownloadFilter(
        sport="Horse Racing",
        plan="Basic Plan",
        from_day=1,
        from_month=3,
        from_year=2017,
        to_day=31,
        to_month=3,
        to_year=2017,
        market_types=("WIN",),
        countries=("GB",),
        file_types=("M",),
    )


def _install_structural_historical_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    packages = [
        {
            "sport": "Horse Racing",
            "plan": "Basic Plan",
            "forDate": "2017-03-01T00:00:00",
            "purchaseItemId": 206,
        }
    ]

    def post_json(self, url, *, ssoid, body, timeout_seconds):
        assert ssoid == "session-token"
        if url.endswith("/GetMyData"):
            return json.dumps(packages, separators=(",", ":")).encode("utf-8")
        if url.endswith("/DownloadListOfFiles"):
            return json.dumps([PATH], separators=(",", ":")).encode("utf-8")
        raise AssertionError(url)

    def get_file(self, url, *, ssoid, timeout_seconds):
        assert ssoid == "session-token"
        assert "DownloadFile?filePath=" in url
        return RAW

    monkeypatch.setattr(UrllibBetfairHistoricalTransport, "post_json", post_json)
    monkeypatch.setattr(UrllibBetfairHistoricalTransport, "get_file", get_file)


def test_historical_positive_entrypoints_report_io_snapshot_seal() -> None:
    for function in (
        BetfairHistoricalEntitlementClient.get_entitlement_snapshot,
        BetfairHistoricalEntitlementClient.list_files,
        BetfairHistoricalEntitlementClient.download_file,
        BetfairHistoricalEntitlementClient.issue_provider_origin_witness,
    ):
        assert getattr(function, "_autosport_historical_io_snapshot_sealed", False)


def test_rebound_product_clock_fails_closed_even_for_structural_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, identity = _context(monkeypatch)
    _install_structural_historical_transport(monkeypatch)
    historical = BetfairHistoricalEntitlementClient(client, identity)

    monkeypatch.setattr(historical_module, "datetime", object())
    with pytest.raises(BetfairHistoricalEntitlementError, match="clock authority"):
        historical.get_entitlement_snapshot()


def test_restored_public_transport_cannot_retroactively_bless_synthetic_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, identity = _context(monkeypatch)
    original_post = UrllibBetfairHistoricalTransport.post_json
    original_get = UrllibBetfairHistoricalTransport.get_file
    _install_structural_historical_transport(monkeypatch)

    historical = BetfairHistoricalEntitlementClient(client, identity)
    snapshot = historical.get_entitlement_snapshot()
    listing = historical.list_files(snapshot, _filter())
    evidence, raw = historical.download_file(snapshot, listing, PATH)

    # Restore an apparently canonical public transport before witness issuance.
    # Hidden acquisition provenance must still remember that these bytes came from
    # the structural test seam rather than the sealed provider-I/O snapshot.
    monkeypatch.setattr(UrllibBetfairHistoricalTransport, "post_json", original_post)
    monkeypatch.setattr(UrllibBetfairHistoricalTransport, "get_file", original_get)
    assert historical_module._canonical_historical_network_transport(
        historical._transport
    )

    with pytest.raises(
        BetfairHistoricalEntitlementError,
        match="did not traverse the canonical Historical Data provider-origin transport chain",
    ):
        historical.issue_provider_origin_witness(evidence, raw)
