from __future__ import annotations

from dataclasses import replace
import calendar
from datetime import datetime, timezone
from hashlib import sha256
import json
from urllib.error import HTTPError
from urllib.request import Request

import pytest

import autosport.betfair_historical_entitlement as historical_module
from autosport.betfair_account_identity import (
    build_betfair_authenticated_client,
    resolve_betfair_authenticated_account_identity,
)
from autosport.betfair_account_readonly import (
    ACCOUNT_JSON_RPC_ENDPOINT,
    BetfairSessionCredentials,
    UrllibBetfairHttpTransport,
)
from autosport.betfair_historical_entitlement import (
    BetfairHistoricalBackpressure,
    BetfairHistoricalEntitlementClient,
    BetfairHistoricalEntitlementError,
    HistoricalDownloadFilter,
    HistoricalEntitlementSnapshot,
    HistoricalProviderOriginWitness,
    UrllibBetfairHistoricalTransport,
    _SameOriginRedirectHandler,
)


NOW = datetime(2026, 9, 22, 0, 0, tzinfo=timezone.utc)
PATH_A = "/data/xds/historic/BASIC/28139610/1.130129050.bz2"
PATH_B = "/data/xds/historic/BASIC/28139610/1.130129060.bz2"
RAW_A = b"BZh9-test-historical-market-a"


def _install_details_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    def post(
        self: UrllibBetfairHttpTransport,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        assert url == ACCOUNT_JSON_RPC_ENDPOINT
        request = json.loads(body.decode("utf-8"))
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request["id"],
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

    monkeypatch.setattr(UrllibBetfairHttpTransport, "post", post)


def _context(monkeypatch: pytest.MonkeyPatch):
    _install_details_transport(monkeypatch)
    client = build_betfair_authenticated_client(
        BetfairSessionCredentials("secret-app-key", "secret-session-token")
    )
    identity = resolve_betfair_authenticated_account_identity(client)
    return client, identity


def _filter(
    *,
    from_month: int = 3,
    to_month: int = 3,
    file_types: tuple[str, ...] = ("M",),
) -> HistoricalDownloadFilter:
    return HistoricalDownloadFilter(
        sport="Horse Racing",
        plan="Basic Plan",
        from_day=1,
        from_month=from_month,
        from_year=2017,
        to_day=calendar.monthrange(2017, to_month)[1],
        to_month=to_month,
        to_year=2017,
        market_types=("WIN",),
        countries=("GB",),
        file_types=file_types,
    )


def _install_historical_transport(
    monkeypatch: pytest.MonkeyPatch,
    *,
    my_data: object | None = None,
    paths: object | None = None,
    file_bytes: bytes = RAW_A,
) -> None:
    package_payload = (
        my_data
        if my_data is not None
        else [
            {
                "sport": "Horse Racing",
                "plan": "Basic Plan",
                "forDate": "2017-03-01T00:00:00",
                "purchaseItemId": 206,
            }
        ]
    )
    path_payload = paths if paths is not None else [PATH_A, PATH_B]

    def post_json(
        self: UrllibBetfairHistoricalTransport,
        url: str,
        *,
        ssoid: str,
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        assert ssoid == "secret-session-token"
        assert timeout_seconds > 0
        if url.endswith("/GetMyData"):
            assert body == b"{}"
            return json.dumps(package_payload, separators=(",", ":")).encode("utf-8")
        if url.endswith("/DownloadListOfFiles"):
            request = json.loads(body.decode("utf-8"))
            assert request["sport"] == "Horse Racing"
            assert request["plan"] == "Basic Plan"
            assert request["fileTypeCollection"] == ["M"]
            return json.dumps(path_payload, separators=(",", ":")).encode("utf-8")
        raise AssertionError(url)

    def get_file(
        self: UrllibBetfairHistoricalTransport,
        url: str,
        *,
        ssoid: str,
        timeout_seconds: float,
    ) -> bytes:
        assert ssoid == "secret-session-token"
        assert "DownloadFile?filePath=%2Fdata%2F" in url
        assert timeout_seconds > 0
        return file_bytes

    monkeypatch.setattr(UrllibBetfairHistoricalTransport, "post_json", post_json)
    monkeypatch.setattr(UrllibBetfairHistoricalTransport, "get_file", get_file)


def _acquire(monkeypatch: pytest.MonkeyPatch):
    client, identity = _context(monkeypatch)
    _install_historical_transport(monkeypatch)
    historical = BetfairHistoricalEntitlementClient(client, identity)
    snapshot = historical.get_entitlement_snapshot()
    listing = historical.list_files(snapshot, _filter())
    evidence, raw = historical.download_file(snapshot, listing, PATH_A)
    return client, historical, snapshot, listing, evidence, raw


def test_structural_purchase_listing_download_binding_stays_non_authoritative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, historical, snapshot, listing, evidence, raw = _acquire(monkeypatch)

    assert snapshot.usage_scope == "PERSONAL_NONCOMMERCIAL"
    assert snapshot.redistributable is False
    assert listing.entitlement_snapshot_sha256 == snapshot.snapshot_sha256
    assert listing.provider_paths == (PATH_A, PATH_B)
    assert evidence.provider_path == PATH_A
    assert evidence.listing_sha256 == listing.listing_sha256
    assert evidence.raw_sha256 == sha256(RAW_A).hexdigest()
    assert evidence.byte_length == len(RAW_A)
    assert snapshot.provider_acquisition_verified is False
    assert listing.provider_acquisition_verified is False
    assert evidence.provider_acquisition_verified is False
    assert snapshot.usage_rights_verified is False
    assert listing.usage_rights_verified is False
    assert evidence.usage_rights_verified is False
    assert snapshot.rights_revalidation_required is True
    assert listing.rights_revalidation_required is True
    assert evidence.rights_revalidation_required is True
    with pytest.raises(
        BetfairHistoricalEntitlementError,
        match="provider acquisition provenance is not mechanically proven",
    ):
        historical.require_authoritative_download(evidence, raw)


def test_structural_synthetic_transport_cannot_issue_provider_origin_witness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, historical, _, _, evidence, raw = _acquire(monkeypatch)

    with pytest.raises(
        BetfairHistoricalEntitlementError,
        match="did not traverse the canonical Historical Data provider-origin transport chain",
    ):
        historical.issue_provider_origin_witness(evidence, raw)


def test_caller_constructed_provider_origin_witness_is_not_authoritative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, _, _, evidence, _ = _acquire(monkeypatch)
    forged = HistoricalProviderOriginWitness(
        session_context_id=evidence.session_context_id,
        entitlement_snapshot_sha256=evidence.entitlement_snapshot_sha256,
        listing_sha256=evidence.listing_sha256,
        download_file_identity_sha256=evidence.file_identity_sha256,
        provider_path=evidence.provider_path,
        retrieved_at=evidence.retrieved_at,
        raw_sha256=evidence.raw_sha256,
        byte_length=evidence.byte_length,
        transport_contract_sha256="0" * 64,
    )

    assert forged.provider_origin_verified is False
    assert forged.usage_rights_verified is False
    assert forged.rights_revalidation_required is True
    with pytest.raises(
        BetfairHistoricalEntitlementError,
        match="was not issued by the canonical client",
    ):
        forged.assert_authoritative()


def test_fresh_builtin_transport_has_canonical_executable_surface() -> None:
    transport = UrllibBetfairHistoricalTransport()

    assert historical_module._canonical_historical_network_transport(transport) is True


def test_instance_method_shadow_revokes_canonical_transport_origin() -> None:
    transport = UrllibBetfairHistoricalTransport()
    transport.post_json = lambda *args, **kwargs: b"[]"  # type: ignore[method-assign]

    assert historical_module._canonical_historical_network_transport(transport) is False


def test_class_method_rebind_revokes_canonical_transport_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = UrllibBetfairHistoricalTransport()
    monkeypatch.setattr(
        UrllibBetfairHistoricalTransport,
        "get_file",
        lambda self, url, *, ssoid, timeout_seconds: RAW_A,
    )

    assert historical_module._canonical_historical_network_transport(transport) is False


def test_opener_replacement_revokes_canonical_transport_origin() -> None:
    transport = UrllibBetfairHistoricalTransport()
    transport._opener = object()

    assert historical_module._canonical_historical_network_transport(transport) is False


def test_opener_handler_mutation_revokes_canonical_transport_origin() -> None:
    transport = UrllibBetfairHistoricalTransport()
    transport._opener.handlers.pop()

    assert historical_module._canonical_historical_network_transport(transport) is False


def test_caller_constructed_snapshot_cannot_authorize_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, identity = _context(monkeypatch)
    _install_historical_transport(monkeypatch)
    historical = BetfairHistoricalEntitlementClient(client, identity)
    issued = historical.get_entitlement_snapshot()
    forged = HistoricalEntitlementSnapshot(
        session_context_id=issued.session_context_id,
        observed_at=issued.observed_at,
        response_sha256=issued.response_sha256,
        packages=issued.packages,
        usage_scope=issued.usage_scope,
        redistributable=False,
        terms_reference=issued.terms_reference,
        terms_as_of=issued.terms_as_of,
    )
    assert forged.snapshot_sha256 == issued.snapshot_sha256

    with pytest.raises(BetfairHistoricalEntitlementError, match="not issued unchanged"):
        historical.list_files(forged, _filter())


def test_copy_of_listing_cannot_authorize_download(monkeypatch: pytest.MonkeyPatch) -> None:
    client, identity = _context(monkeypatch)
    _install_historical_transport(monkeypatch)
    historical = BetfairHistoricalEntitlementClient(client, identity)
    snapshot = historical.get_entitlement_snapshot()
    listing = historical.list_files(snapshot, _filter())
    copied = replace(listing)
    assert copied.listing_sha256 == listing.listing_sha256

    with pytest.raises(BetfairHistoricalEntitlementError, match="not issued unchanged"):
        historical.download_file(snapshot, copied, PATH_A)


def test_unlisted_path_cannot_be_downloaded(monkeypatch: pytest.MonkeyPatch) -> None:
    client, identity = _context(monkeypatch)
    _install_historical_transport(monkeypatch, paths=[PATH_A])
    historical = BetfairHistoricalEntitlementClient(client, identity)
    snapshot = historical.get_entitlement_snapshot()
    listing = historical.list_files(snapshot, _filter())

    with pytest.raises(BetfairHistoricalEntitlementError, match="not returned"):
        historical.download_file(snapshot, listing, PATH_B)


def test_http_200_non_bzip2_download_cannot_mint_file_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, identity = _context(monkeypatch)
    _install_historical_transport(monkeypatch, file_bytes=b"<html>error</html>")
    historical = BetfairHistoricalEntitlementClient(client, identity)
    snapshot = historical.get_entitlement_snapshot()
    listing = historical.list_files(snapshot, _filter())

    with pytest.raises(BetfairHistoricalEntitlementError, match="canonical bzip2"):
        historical.download_file(snapshot, listing, PATH_A)


def test_mutated_download_bytes_fail_integrity_revalidation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, historical, _, _, evidence, raw = _acquire(monkeypatch)
    mutated = raw + b"tamper"

    with pytest.raises(BetfairHistoricalEntitlementError, match="do not match"):
        historical.require_authoritative_download(evidence, mutated)


def test_same_object_download_evidence_tamper_is_detected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, historical, _, _, evidence, raw = _acquire(monkeypatch)
    object.__setattr__(evidence, "raw_sha256", "0" * 64)

    with pytest.raises(BetfairHistoricalEntitlementError, match="not issued unchanged"):
        historical.require_authoritative_download(evidence, raw)


def test_commercial_approved_cannot_be_minted_by_this_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, snapshot, _, _, _ = _acquire(monkeypatch)

    with pytest.raises(BetfairHistoricalEntitlementError, match="commercial-use"):
        HistoricalEntitlementSnapshot(
            session_context_id=snapshot.session_context_id,
            observed_at=snapshot.observed_at,
            response_sha256=snapshot.response_sha256,
            packages=snapshot.packages,
            usage_scope="COMMERCIAL_APPROVED",
            redistributable=False,
            terms_reference=snapshot.terms_reference,
            terms_as_of=snapshot.terms_as_of,
        )


def test_dated_terms_reference_cannot_mint_current_usage_rights(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, historical, snapshot, listing, evidence, raw = _acquire(monkeypatch)

    assert snapshot.usage_scope == "PERSONAL_NONCOMMERCIAL"
    assert snapshot.terms_as_of == "2026-09-22"
    assert snapshot.usage_rights_verified is False
    assert snapshot.rights_revalidation_required is True
    assert listing.usage_rights_verified is False
    assert evidence.usage_rights_verified is False

    with pytest.raises(
        BetfairHistoricalEntitlementError,
        match="provider acquisition provenance is not mechanically proven",
    ):
        historical.require_authoritative_download(evidence, raw)


def test_filter_must_be_covered_for_every_requested_month(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, identity = _context(monkeypatch)
    _install_historical_transport(monkeypatch)
    historical = BetfairHistoricalEntitlementClient(client, identity)
    snapshot = historical.get_entitlement_snapshot()

    with pytest.raises(BetfairHistoricalEntitlementError, match="month range"):
        historical.list_files(snapshot, _filter(from_month=3, to_month=4))


def test_m_and_e_cannot_be_requested_together() -> None:
    with pytest.raises(BetfairHistoricalEntitlementError, match="choose one"):
        _filter(file_types=("M", "E"))


def test_duplicate_purchase_item_id_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    client, identity = _context(monkeypatch)
    duplicate = [
        {
            "sport": "Horse Racing",
            "plan": "Basic Plan",
            "forDate": "2017-03-01T00:00:00",
            "purchaseItemId": 206,
        },
        {
            "sport": "Horse Racing",
            "plan": "Basic Plan",
            "forDate": "2017-03-01T00:00:00",
            "purchaseItemId": 206,
        },
    ]
    _install_historical_transport(monkeypatch, my_data=duplicate)
    historical = BetfairHistoricalEntitlementClient(client, identity)

    with pytest.raises(BetfairHistoricalEntitlementError, match="duplicate/conflicting"):
        historical.get_entitlement_snapshot()


def test_conflicting_purchase_item_id_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    client, identity = _context(monkeypatch)
    conflict = [
        {
            "sport": "Horse Racing",
            "plan": "Basic Plan",
            "forDate": "2017-03-01T00:00:00",
            "purchaseItemId": 206,
        },
        {
            "sport": "Cricket",
            "plan": "Advanced Plan",
            "forDate": "2017-04-01T00:00:00",
            "purchaseItemId": 206,
        },
    ]
    _install_historical_transport(monkeypatch, my_data=conflict)
    historical = BetfairHistoricalEntitlementClient(client, identity)

    with pytest.raises(BetfairHistoricalEntitlementError, match="duplicate/conflicting"):
        historical.get_entitlement_snapshot()


def test_caller_cannot_inject_acquisition_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, identity = _context(monkeypatch)

    with pytest.raises(TypeError, match="clock"):
        BetfairHistoricalEntitlementClient(
            client, identity, clock=lambda: NOW  # type: ignore[call-arg]
        )


def test_transport_replacement_after_capture_revokes_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, identity = _context(monkeypatch)
    _install_historical_transport(monkeypatch)
    historical = BetfairHistoricalEntitlementClient(client, identity)
    snapshot = historical.get_entitlement_snapshot()
    historical._transport = UrllibBetfairHistoricalTransport()

    with pytest.raises(BetfairHistoricalEntitlementError, match="transport origin"):
        historical.list_files(snapshot, _filter())


def test_context_rotation_after_capture_revokes_snapshot_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, identity = _context(monkeypatch)
    _install_historical_transport(monkeypatch)
    historical = BetfairHistoricalEntitlementClient(client, identity)
    snapshot = historical.get_entitlement_snapshot()
    object.__setattr__(client._credentials, "session_token", "rotated-session")

    with pytest.raises(BetfairHistoricalEntitlementError, match="no longer authoritative"):
        historical.list_files(snapshot, _filter())


def test_duplicate_or_traversal_provider_paths_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, identity = _context(monkeypatch)
    _install_historical_transport(monkeypatch, paths=[PATH_A, "/data/../secret"])
    historical = BetfairHistoricalEntitlementClient(client, identity)
    snapshot = historical.get_entitlement_snapshot()

    with pytest.raises(BetfairHistoricalEntitlementError, match="traversal"):
        historical.list_files(snapshot, _filter())


def test_strict_provider_json_rejects_duplicate_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    client, identity = _context(monkeypatch)

    def post_json(self, url, *, ssoid, body, timeout_seconds):
        assert url.endswith("/GetMyData")
        return (
            b'[{"sport":"Horse Racing","sport":"Cricket","plan":"Basic Plan",'
            b'"forDate":"2017-03-01T00:00:00","purchaseItemId":206}]'
        )

    monkeypatch.setattr(UrllibBetfairHistoricalTransport, "post_json", post_json)
    historical = BetfairHistoricalEntitlementClient(client, identity)

    with pytest.raises(BetfairHistoricalEntitlementError, match="duplicate object key"):
        historical.get_entitlement_snapshot()


def test_429_is_retryable_backpressure_not_empty_entitlement() -> None:
    transport = UrllibBetfairHistoricalTransport()

    class FakeOpener:
        def open(self, request, timeout):
            raise HTTPError(request.full_url, 429, "Too Many Requests", {}, None)

    transport._opener = FakeOpener()
    with pytest.raises(BetfairHistoricalBackpressure, match="backpressure"):
        transport.post_json(
            "https://historicdata.betfair.com/api/GetMyData",
            ssoid="never-echo-this-token",
            body=b"{}",
            timeout_seconds=1,
        )


def test_cross_origin_redirect_is_blocked_before_credential_forwarding() -> None:
    handler = _SameOriginRedirectHandler()
    request = Request(
        "https://historicdata.betfair.com/api/DownloadFile",
        headers={"ssoid": "secret-token"},
    )

    with pytest.raises(BetfairHistoricalEntitlementError, match="cross-origin"):
        handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://evil.example/download",
        )


def test_downgrade_redirect_is_blocked() -> None:
    handler = _SameOriginRedirectHandler()
    request = Request(
        "https://historicdata.betfair.com/api/DownloadFile",
        headers={"ssoid": "secret-token"},
    )

    with pytest.raises(BetfairHistoricalEntitlementError, match="cross-origin"):
        handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "http://historicdata.betfair.com/api/DownloadFile",
        )


def test_secrets_do_not_enter_evidence_repr_or_digests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, snapshot, listing, evidence, _ = _acquire(monkeypatch)
    rendered = repr((snapshot, listing, evidence))
    joined = "|".join(
        [snapshot.snapshot_sha256, listing.listing_sha256, evidence.file_identity_sha256]
    )

    for secret in ("secret-app-key", "secret-session-token"):
        assert secret not in rendered
        assert secret not in joined


def test_filter_date_range_must_be_ordered() -> None:
    with pytest.raises(BetfairHistoricalEntitlementError, match="end precedes"):
        HistoricalDownloadFilter(
            sport="Horse Racing",
            plan="Basic Plan",
            from_day=31,
            from_month=3,
            from_year=2017,
            to_day=1,
            to_month=3,
            to_year=2017,
        )
