from __future__ import annotations

import json

import pytest

import autosport.betfair_account_readonly as betfair_account_readonly
from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)
from autosport.betfair_marketbook_freshness import (
    BetfairMarketBookFreshnessError,
    read_market_book_delay,
)


class _FakeNetworkResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, max_bytes: int) -> bytes:
        return self._payload[:max_bytes]


class _SwitchingCredentials(BetfairSessionCredentials):
    """One credential object whose observed secret values change by read."""

    def __init__(self) -> None:
        object.__setattr__(self, "_armed", False)
        super().__init__("seed-app", "seed-session")
        object.__setattr__(self, "_app_reads", 0)
        object.__setattr__(self, "_session_reads", 0)
        object.__setattr__(self, "_armed", True)

    def __getattribute__(self, name: str):
        if name in {"application_key", "session_token"}:
            data = object.__getattribute__(self, "__dict__")
            if data.get("_armed", False):
                if name == "application_key":
                    index = data["_app_reads"]
                    data["_app_reads"] = index + 1
                    return "app-A" if index == 0 else "app-B"
                index = data["_session_reads"]
                data["_session_reads"] = index + 1
                return "session-A" if index == 0 else "session-B"
        return super().__getattribute__(name)


def test_same_identity_credentials_cannot_stitch_two_authenticated_contexts(
    monkeypatch,
) -> None:
    calls: list[tuple[str, dict[str, str], dict[str, object]]] = []

    def fake_urlopen(request, timeout):
        headers = {key.lower(): value for key, value in request.header_items()}
        body = json.loads(request.data.decode("utf-8"))
        calls.append((request.full_url, headers, body))
        request_id = body["id"]

        if body["method"] == "SportsAPING/v1.0/listMarketBook":
            assert headers["x-application"] == "app-A"
            assert headers["x-authentication"] == "session-A"
            payload = {
                "jsonrpc": "2.0",
                "result": [
                    {
                        "marketId": "1.234",
                        "isMarketDataDelayed": False,
                    }
                ],
                "id": request_id,
            }
        elif body["method"] == "AccountAPING/v1.0/getDeveloperAppKeys":
            assert "x-application" not in headers
            assert headers["x-authentication"] == "session-B"
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
                                "applicationKey": "app-B",
                                "delayData": False,
                                "subscriptionRequired": False,
                                "ownerManaged": False,
                                "active": True,
                            }
                        ],
                    }
                ],
                "id": request_id,
            }
        else:
            raise AssertionError(f"unexpected Betfair method: {body['method']}")

        return _FakeNetworkResponse(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        )

    monkeypatch.setattr(betfair_account_readonly, "urlopen", fake_urlopen)
    credentials = _SwitchingCredentials()

    # Safe implementations may reject non-exact credentials at client construction,
    # reject the auth transition during capture, or leave the observation unable to
    # assert positive authority. They must not accept A->B solely because the same
    # Python credential object remained installed on the client.
    try:
        client = BetfairReadOnlyClient(
            credentials,
            venue_id="betfair-global",
            account_id="configured-account",
        )
        observation = read_market_book_delay(client, "1.234")
    except (TypeError, BetfairMarketBookFreshnessError):
        return

    assert len(calls) == 2
    assert calls[0][2]["method"] == "SportsAPING/v1.0/listMarketBook"
    assert calls[1][2]["method"] == "AccountAPING/v1.0/getDeveloperAppKeys"

    with pytest.raises(BetfairMarketBookFreshnessError):
        observation.assert_positive_authoritative()
