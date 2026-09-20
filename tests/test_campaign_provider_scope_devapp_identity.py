from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest

import autosport._campaign_provider_scope_devapp_identity as devapp
from autosport.betfair_account_readonly import (
    ACCOUNT_JSON_RPC_ENDPOINT,
    ADAPTER_ID,
    ADAPTER_VERSION,
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
    UrllibBetfairHttpTransport,
)
from autosport.bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from autosport.campaign_provider_scope_authority import (
    CampaignProviderScopeError,
    assert_provider_scope_capture_authoritative,
    capture_betfair_provider_scope,
)
from autosport.real_execution_ledger import ExecutionAction


APP_KEY = "fake-live-application-key-secret"
SESSION_TOKEN = "fake-session-token-secret"


def _response(result: object, request_id: int) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "result": result, "id": request_id},
        separators=(",", ":"),
    ).encode("utf-8")


def _developer_app(
    *,
    app_id: int = 1001,
    version_ids: tuple[int, ...] = (2002, 2001),
    owner_managed: bool = False,
) -> list[dict[str, object]]:
    return [
        {
            "appName": "private-name-not-identity",
            "appId": app_id,
            "appVersions": [
                {
                    "owner": "private-owner-not-identity",
                    "versionId": version_id,
                    "version": "1.0" if index == 0 else "1.1",
                    "applicationKey": f"secret-app-key-{version_id}",
                    "delayData": index == 0,
                    "subscriptionRequired": False,
                    "ownerManaged": owner_managed,
                    "active": True,
                }
                for index, version_id in enumerate(version_ids)
            ],
        }
    ]


def _canonical_client() -> BetfairReadOnlyClient:
    return BetfairReadOnlyClient(
        BetfairSessionCredentials(APP_KEY, SESSION_TOKEN)
    )


def test_developer_app_identity_is_order_independent_and_secret_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _canonical_client()
    calls: list[dict[str, object]] = []
    responses = [
        _response(_developer_app(version_ids=(2002, 2001)), 1),
        _response(_developer_app(version_ids=(2001, 2002)), 2),
    ]

    def scripted_post(self, url, *, headers, body, timeout_seconds):
        calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout_seconds": timeout_seconds,
            }
        )
        return responses.pop(0)

    monkeypatch.setattr(UrllibBetfairHttpTransport, "post", scripted_post)

    first = devapp._read_developer_account_identity(client)
    second = devapp._read_developer_account_identity(client)

    assert first.account_identity_sha256 == second.account_identity_sha256
    assert len(first.account_identity_sha256) == 64
    assert APP_KEY not in repr(first)
    assert SESSION_TOKEN not in repr(first)
    assert "secret-app-key-2001" not in repr(first)
    assert "secret-app-key-2002" not in repr(first)
    assert len(calls) == 2
    for call in calls:
        assert call["url"] == ACCOUNT_JSON_RPC_ENDPOINT
        assert call["headers"]["X-Authentication"] == SESSION_TOKEN
        assert "X-Application" not in call["headers"]
        assert APP_KEY.encode() not in call["body"]
        assert SESSION_TOKEN.encode() not in call["body"]
        request = json.loads(call["body"])
        assert request["method"] == "AccountAPING/v1.0/getDeveloperAppKeys"
        assert request["params"] == {}


def test_developer_app_identity_changes_with_provider_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _canonical_client()
    responses = [
        _response(_developer_app(app_id=1001), 1),
        _response(_developer_app(app_id=1002), 2),
        _response(_developer_app(app_id=1001, version_ids=(2001, 3001)), 3),
    ]
    monkeypatch.setattr(
        UrllibBetfairHttpTransport,
        "post",
        lambda self, url, *, headers, body, timeout_seconds: responses.pop(0),
    )

    base = devapp._read_developer_account_identity(client)
    other_app = devapp._read_developer_account_identity(client)
    other_version = devapp._read_developer_account_identity(client)

    assert base.account_identity_sha256 != other_app.account_identity_sha256
    assert base.account_identity_sha256 != other_version.account_identity_sha256


@pytest.mark.parametrize(
    "result",
    (
        [],
        _developer_app() + _developer_app(app_id=1002),
        _developer_app(owner_managed=True),
    ),
)
def test_ambiguous_or_vendor_managed_developer_apps_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    result: object,
) -> None:
    client = _canonical_client()
    monkeypatch.setattr(
        UrllibBetfairHttpTransport,
        "post",
        lambda self, url, *, headers, body, timeout_seconds: _response(result, 1),
    )

    with pytest.raises(CampaignProviderScopeError):
        devapp._read_developer_account_identity(client)


def test_injected_transport_is_rejected_before_any_provider_call() -> None:
    class InjectedTransport:
        def __init__(self) -> None:
            self.called = False

        def post(self, url, *, headers, body, timeout_seconds):
            self.called = True
            raise AssertionError("must not be called")

    transport = InjectedTransport()
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials(APP_KEY, SESSION_TOKEN),
        transport=transport,
    )

    with pytest.raises(
        CampaignProviderScopeError,
        match="stable authenticated Betfair account identity is unavailable",
    ):
        devapp._read_developer_account_identity(client)

    assert transport.called is False


def test_positive_capture_uses_stable_non_secret_account_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    placed = (now - timedelta(minutes=2)).isoformat()
    settled = (now - timedelta(minutes=1)).isoformat()
    client = _canonical_client()
    responses = [
        _response(_developer_app(), 1),
        _response([{"marketId": "1.234", "event": {"id": "event-1"}}], 2),
        _response({"currentOrders": [], "moreAvailable": False}, 3),
        _response(
            {
                "clearedOrders": [
                    {
                        "betId": "bet-1",
                        "eventId": "event-1",
                        "marketId": "1.234",
                        "selectionId": 42,
                        "side": "BACK",
                        "placedDate": placed,
                        "settledDate": settled,
                        "priceRequested": 2,
                        "priceMatched": 2,
                        "sizeSettled": 1,
                        "profit": 1,
                        "customerOrderRef": "action-1",
                    }
                ],
                "moreAvailable": False,
            },
            4,
        ),
        _response({"clearedOrders": [], "moreAvailable": False}, 5),
        _response({"clearedOrders": [], "moreAvailable": False}, 6),
        _response({"clearedOrders": [], "moreAvailable": False}, 7),
    ]
    monkeypatch.setattr(
        UrllibBetfairHttpTransport,
        "post",
        lambda self, url, *, headers, body, timeout_seconds: responses.pop(0),
    )

    profile = BookmakerCapabilityProfile(
        venue_id="betfair",
        account_id="default-account",
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        profile_version=1,
        facts=(
            BookmakerCapabilityFact(
                BookmakerCapability.BET_READBACK,
                BookmakerCapabilityState.SUPPORTED,
            ),
        ),
        observed_at=(now - timedelta(minutes=3)).isoformat(),
        source_ref="betfair://profile/devapp-identity-test",
        source_payload_sha256="a" * 64,
    )
    action = ExecutionAction(
        action_id="action-1",
        bookmaker_id="betfair",
        account_id="default-account",
        event_id="event-1",
        market_id="1.234",
        selection_id="42",
        side="BACK",
        requested_odds=Decimal("2"),
        requested_stake=Decimal("1"),
        quote_id="quote-1",
        quote_observed_at=(now - timedelta(seconds=30)).isoformat(),
        expires_at=(now + timedelta(minutes=2)).isoformat(),
    )

    capture = capture_betfair_provider_scope(
        client,
        action,
        profile,
        expected_profile_sha256=profile.profile_id,
    )
    assert_provider_scope_capture_authoritative(capture)

    assert capture.authenticated_account_id.startswith("betfair-account-evidence:")
    assert capture.account_details_sha256 in capture.authenticated_account_id
    serialized = json.dumps(capture.payload(), sort_keys=True)
    assert APP_KEY not in serialized
    assert SESSION_TOKEN not in serialized
    assert "secret-app-key-2001" not in serialized
    assert "secret-app-key-2002" not in serialized
    assert capture.event_id == "event-1"
    assert capture.market_id == "1.234"
    assert responses == []
