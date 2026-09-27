from __future__ import annotations

import json

import pytest

import autosport.betfair_account_readonly as betfair_readonly
from autosport.account_snapshot_acquisition import (
    AccountSnapshotAcquisitionError,
    BetfairAccountSnapshotAcquirer,
)
from autosport.betfair_account_readonly import BetfairSessionCredentials
from autosport.bookmaker_capability import BookmakerCapability


def _response(result: object, request_id: int) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "result": result, "id": request_id},
        separators=(",", ":"),
    ).encode("utf-8")


_DEVELOPER_APPS = _response(
    [
        {
            "appId": 12345,
            "appVersions": [
                {
                    "versionId": 67890,
                    "version": "1.0",
                    "applicationKey": "DEVAPP-SECRET-SENTINEL",
                    "ownerManaged": False,
                }
            ],
        }
    ],
    1,
)
_DETAILS = _response(
    {
        "currencyCode": "GBP",
        "localeCode": "en",
        "region": "GBR",
        "timezone": "Europe/London",
    },
    2,
)
_FUNDS = _response(
    {
        "availableToBetBalance": 100.10,
        "exposure": -12.34,
        "retainedCommission": 0.05,
        "exposureLimit": -5000.00,
    },
    3,
)


def _install_transport(monkeypatch, responses: list[bytes]):
    queue = list(responses)
    calls: list[dict[str, object]] = []

    def post(self, url, *, headers, body, timeout_seconds):
        calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout_seconds": timeout_seconds,
            }
        )
        if not queue:
            raise AssertionError("unexpected provider call")
        return queue.pop(0)

    monkeypatch.setattr(
        betfair_readonly.UrllibBetfairHttpTransport,
        "post",
        post,
    )
    return calls


def _credentials(label: str) -> BetfairSessionCredentials:
    return BetfairSessionCredentials(
        f"APP-SECRET-{label}",
        f"SESSION-SECRET-{label}",
    )


def _balance_capabilities() -> frozenset[BookmakerCapability]:
    return frozenset({BookmakerCapability.BALANCE_READ})


def test_live_retry_cannot_cross_authenticated_credential_origin(
    tmp_path,
    monkeypatch,
) -> None:
    database = tmp_path / "account.sqlite3"
    first_calls = _install_transport(
        monkeypatch,
        [_DEVELOPER_APPS, _DETAILS, _FUNDS],
    )
    first = BetfairAccountSnapshotAcquirer(
        database,
        _credentials("A"),
        account_id="default-account",
    ).acquire(
        _balance_capabilities(),
        acquisition_id="shared-live-request",
    )
    assert len(first_calls) == 3
    assert first.source_authority_proven is True

    # The original live object remains strongly referenced above. A second canonical
    # acquirer with different credentials but the same caller-local account label must
    # not inherit that remote-provider capability from the idempotency fast path.
    different_origin_calls = _install_transport(monkeypatch, [])
    second = BetfairAccountSnapshotAcquirer(
        database,
        _credentials("B"),
        account_id="default-account",
    )
    with pytest.raises(
        AccountSnapshotAcquisitionError,
        match="bound to a different authenticated credential origin",
    ):
        second.acquire(
            _balance_capabilities(),
            acquisition_id="shared-live-request",
        )
    assert different_origin_calls == []

    # A reconstructed canonical acquirer carrying the exact same in-memory credential
    # values remains an idempotent retry of the same authenticated origin and performs
    # no provider I/O while the exact issued object is still live.
    same_origin_calls = _install_transport(monkeypatch, [])
    retry = BetfairAccountSnapshotAcquirer(
        database,
        _credentials("A"),
        account_id="default-account",
    ).acquire(
        _balance_capabilities(),
        acquisition_id="shared-live-request",
    )
    assert retry is first
    assert same_origin_calls == []
