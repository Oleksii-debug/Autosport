from datetime import datetime, timezone
from hashlib import sha256
import json

import pytest

import autosport.betfair_account_readonly as betfair_account_readonly
import autosport.betfair_marketbook_freshness as betfair_marketbook_freshness
from autosport.betfair_account_readonly import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    BETTING_JSON_RPC_ENDPOINT,
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)
from autosport.betfair_marketbook_freshness import (
    BetfairMarketBookDelayObservation,
    BetfairMarketBookFreshnessError,
    _canonical_network_transport,
    read_market_book_delay,
)


NOW = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)


class FakeTransport:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls: list[dict[str, object]] = []

    def post(self, url, *, headers, body, timeout_seconds):
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout_seconds": timeout_seconds,
            }
        )
        return self.payload


class FakeNetworkResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, max_bytes: int) -> bytes:
        return self.payload[:max_bytes]


def _payload(
    *,
    request_id: int = 1,
    market_id: str = "1.234",
    delayed: object = False,
    include_delay: bool = True,
) -> bytes:
    row = {"marketId": market_id}
    if include_delay:
        row["isMarketDataDelayed"] = delayed
    return json.dumps(
        {"jsonrpc": "2.0", "result": [row], "id": request_id},
        separators=(",", ":"),
    ).encode("utf-8")


def _client(payload: bytes) -> tuple[BetfairReadOnlyClient, FakeTransport]:
    transport = FakeTransport(payload)
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=transport,
        clock=lambda: NOW,
        venue_id="betfair-global",
        account_id="acct-1",
    )
    return client, transport


def test_injected_transport_parses_non_delayed_payload_but_cannot_mint_positive_authority():
    payload = _payload(delayed=False)
    client, transport = _client(payload)

    observation = read_market_book_delay(client, "1.234")

    assert observation.venue_id == "betfair-global"
    assert observation.configured_account_ref == "acct-1"
    assert observation.adapter_id == ADAPTER_ID
    assert observation.adapter_version == ADAPTER_VERSION
    assert observation.market_id == "1.234"
    assert observation.is_market_data_delayed is False
    assert observation.observed_at == NOW.isoformat()
    assert observation.source_payload_sha256 == sha256(payload).hexdigest()
    assert observation.proves_provider_account_identity is False
    fields = set(BetfairMarketBookDelayObservation.__dataclass_fields__)
    assert "configured_account_ref" in fields
    assert "account_id" not in fields
    with pytest.raises(
        BetfairMarketBookFreshnessError,
        match="lacks canonical production network origin",
    ):
        observation.assert_authoritative()

    call = transport.calls[0]
    assert call["url"] == BETTING_JSON_RPC_ENDPOINT
    request = json.loads(call["body"])
    assert request["method"] == "SportsAPING/v1.0/listMarketBook"
    assert request["params"] == {"marketIds": ["1.234"]}
    assert b"app-secret" not in call["body"]
    assert b"session-secret" not in call["body"]


def test_provider_delayed_true_from_injected_transport_is_only_negative_authority():
    client, _ = _client(_payload(delayed=True))

    observation = read_market_book_delay(client, "1.234")

    observation.assert_authoritative()
    assert observation.is_market_data_delayed is True
    with pytest.raises(
        BetfairMarketBookFreshnessError,
        match="lacks canonical production network origin",
    ):
        observation.assert_positive_authoritative()


def test_unmodified_default_http_transport_is_the_only_positive_network_origin_shape():
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret")
    )

    assert _canonical_network_transport(client) is True

    client._transport.post = lambda *args, **kwargs: b"{}"
    assert _canonical_network_transport(client) is False


def test_structurally_compatible_custom_transport_is_not_positive_network_origin():
    client, _ = _client(_payload(delayed=False))

    assert _canonical_network_transport(client) is False


@pytest.mark.parametrize("delayed", [0, 1, "false", None, [], {}])
def test_delay_flag_must_be_exact_bool(delayed):
    client, _ = _client(_payload(delayed=delayed))

    with pytest.raises(BetfairMarketBookFreshnessError, match="must be bool"):
        read_market_book_delay(client, "1.234")


def test_missing_delay_flag_fails_closed():
    client, _ = _client(_payload(include_delay=False))

    with pytest.raises(
        BetfairMarketBookFreshnessError,
        match="isMarketDataDelayed is missing",
    ):
        read_market_book_delay(client, "1.234")


def test_market_rebinding_fails_closed():
    client, _ = _client(_payload(market_id="9.999"))

    with pytest.raises(
        BetfairMarketBookFreshnessError,
        match="different market",
    ):
        read_market_book_delay(client, "1.234")


def test_directly_constructed_observation_cannot_mint_provider_authority():
    forged = BetfairMarketBookDelayObservation(
        venue_id="betfair-global",
        configured_account_ref="acct-1",
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        market_id="1.234",
        is_market_data_delayed=False,
        observed_at=NOW.isoformat(),
        source_payload_sha256="a" * 64,
    )

    with pytest.raises(
        BetfairMarketBookFreshnessError,
        match="not issued by canonical Betfair adapter",
    ):
        forged.assert_authoritative()


def test_response_id_must_match_request():
    client, _ = _client(_payload(request_id=7))

    with pytest.raises(
        BetfairMarketBookFreshnessError,
        match="response id does not match",
    ):
        read_market_book_delay(client, "1.234")


def test_exact_canonical_client_type_is_required():
    class ForgedClient(BetfairReadOnlyClient):
        pass

    transport = FakeTransport(_payload())
    client = ForgedClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=transport,
        clock=lambda: NOW,
    )

    with pytest.raises(TypeError, match="exact BetfairReadOnlyClient"):
        read_market_book_delay(client, "1.234")


def _canonical_network_capture(
    monkeypatch,
    *,
    metadata_application_key: str = "app-secret",
    delay_data: object = False,
    active: object = True,
    market_delayed: object = False,
):
    calls = []

    def fake_urlopen(request, timeout):
        headers = {key.lower(): value for key, value in request.header_items()}
        body = json.loads(request.data.decode("utf-8"))
        calls.append((request.full_url, headers, body))
        request_id = body["id"]
        if body["method"] == "SportsAPING/v1.0/listMarketBook":
            payload = {
                "jsonrpc": "2.0",
                "result": [
                    {
                        "marketId": "1.234",
                        "isMarketDataDelayed": market_delayed,
                    }
                ],
                "id": request_id,
            }
        elif body["method"] == "AccountAPING/v1.0/getDeveloperAppKeys":
            payload = {
                "jsonrpc": "2.0",
                "result": [
                    {
                        "appName": "autosport-test",
                        "appId": 101,
                        "appVersions": [
                            {
                                "owner": "synthetic-owner",
                                "versionId": 202,
                                "version": "synthetic-1",
                                "applicationKey": metadata_application_key,
                                "delayData": delay_data,
                                "subscriptionRequired": False,
                                "ownerManaged": False,
                                "active": active,
                            }
                        ],
                    }
                ],
                "id": request_id,
            }
        else:
            raise AssertionError(f"unexpected Betfair method: {body['method']}")
        return FakeNetworkResponse(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        )

    monkeypatch.setattr(betfair_account_readonly, "urlopen", fake_urlopen)
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        venue_id="betfair-global",
        account_id="configured-account",
    )
    observation = read_market_book_delay(client, "1.234")
    return observation, calls


def test_canonical_network_observation_binds_authenticated_live_app_context(monkeypatch):
    observation, calls = _canonical_network_capture(monkeypatch)

    observation.assert_positive_authoritative()
    assert observation.application_key_class == "live"
    assert observation.authenticated_context_sha256 is not None
    assert observation.developer_app_id == 101
    assert observation.application_version_id == 202
    assert observation.application_key_delay_data is False
    assert observation.application_key_active is True
    assert observation.application_key_owner_managed is False
    assert "app-secret" not in repr(observation)
    assert "session-secret" not in repr(observation)

    developer_call = next(
        call
        for call in calls
        if call[2]["method"] == "AccountAPING/v1.0/getDeveloperAppKeys"
    )
    assert "x-authentication" in developer_call[1]
    assert "x-application" not in developer_call[1]
    assert b"app-secret" not in json.dumps(developer_call[2]).encode("utf-8")
    assert b"session-secret" not in json.dumps(developer_call[2]).encode("utf-8")


def test_authenticated_metadata_must_match_exact_application_key(monkeypatch):
    with pytest.raises(
        BetfairMarketBookFreshnessError,
        match="must match exactly one provider app version",
    ):
        _canonical_network_capture(
            monkeypatch,
            metadata_application_key="different-app-key",
        )


@pytest.mark.parametrize("delay_data", [0, 1, "false", None, [], {}])
def test_authenticated_delay_data_must_be_exact_bool(monkeypatch, delay_data):
    with pytest.raises(BetfairMarketBookFreshnessError, match="delayData must be bool"):
        _canonical_network_capture(monkeypatch, delay_data=delay_data)


@pytest.mark.parametrize("active", [0, 1, "true", None, [], {}])
def test_authenticated_app_active_must_be_exact_bool(monkeypatch, active):
    with pytest.raises(BetfairMarketBookFreshnessError, match="active must be bool"):
        _canonical_network_capture(monkeypatch, active=active)


def test_credential_rotation_between_capture_and_authority_registration_fails(monkeypatch):
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        venue_id="betfair-global",
        account_id="configured-account",
    )
    synthetic = BetfairMarketBookDelayObservation(
        venue_id="betfair-global",
        configured_account_ref="configured-account",
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        market_id="1.234",
        is_market_data_delayed=False,
        observed_at=NOW.isoformat(),
        source_payload_sha256="a" * 64,
        authenticated_context_sha256="b" * 64,
        developer_app_id=101,
        application_version_id=202,
        application_key_delay_data=False,
        application_key_active=True,
        application_key_owner_managed=False,
    )

    def rotate_after_capture(client_arg, market_id):
        assert client_arg is client
        assert market_id == "1.234"
        client_arg._credentials = BetfairSessionCredentials(
            "rotated-app-secret",
            "rotated-session-secret",
        )
        return synthetic

    monkeypatch.setattr(
        betfair_marketbook_freshness,
        "_read_market_book_delay",
        rotate_after_capture,
    )

    with pytest.raises(
        BetfairMarketBookFreshnessError,
        match="changed before authority registration",
    ):
        read_market_book_delay(client, "1.234")
