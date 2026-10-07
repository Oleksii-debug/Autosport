from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
    BetfairSessionCredentials,
)
from autosport.betfair_provider_billing_inputs import (
    read_betfair_provider_billing_inputs,
)


_MISSING = object()


class _Clock:
    def __call__(self) -> datetime:
        return datetime(2026, 9, 21, 3, 40, tzinfo=timezone.utc)


class _Transport:
    def __init__(
        self,
        *,
        selected_owner: object = "provider-account-A",
        other_owner: str = "provider-account-B",
    ) -> None:
        self.selected_owner = selected_owner
        self.other_owner = other_owner

    def post(
        self,
        _url: str,
        *,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        assert headers["X-Application"] == "live-key"
        assert headers["X-Authentication"] == "session-token"
        assert timeout_seconds > 0
        request = json.loads(body)
        method = request["method"]
        if method == "AccountAPING/v1.0/getAccountDetails":
            result: object = {"currencyCode": "GBP"}
        elif method == "AccountAPING/v1.0/getDeveloperAppKeys":
            selected = {
                "versionId": 7,
                "version": "1.0",
                "applicationKey": "live-key",
                "delayData": False,
                "subscriptionRequired": False,
                "ownerManaged": False,
                "active": True,
                "vendorId": "vendor-3",
            }
            if self.selected_owner is not _MISSING:
                selected["owner"] = self.selected_owner
            result = [
                {
                    "appId": 41,
                    "appName": "autosport",
                    "appVersions": [
                        {
                            "owner": self.other_owner,
                            "versionId": 8,
                            "version": "1.1-delayed",
                            "applicationKey": "other-key",
                            "delayData": True,
                            "subscriptionRequired": False,
                            "ownerManaged": False,
                            "active": True,
                            "vendorId": "vendor-3",
                        },
                        selected,
                    ],
                }
            ]
        elif method == "AccountAPING/v1.0/getAccountStatement":
            result = {"accountStatement": [], "moreAvailable": False}
        else:  # pragma: no cover
            raise AssertionError(method)
        return json.dumps(
            {"jsonrpc": "2.0", "id": request["id"], "result": result},
            separators=(",", ":"),
        ).encode("utf-8")


def _read(
    *,
    account_id: str = "caller-label-A",
    selected_owner: object = "provider-account-A",
    other_owner: str = "provider-account-B",
):
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("live-key", "session-token"),
        transport=_Transport(
            selected_owner=selected_owner,
            other_owner=other_owner,
        ),
        clock=_Clock(),
        venue_id="betfair",
        account_id=account_id,
    )
    return read_betfair_provider_billing_inputs(client)


def test_exact_matched_version_owner_is_provider_observation_not_caller_label() -> None:
    first = _read(account_id="caller-label-A", other_owner="unselected-A")
    second = _read(account_id="caller-label-B", other_owner="unselected-B")

    assert first.entitlement.provider_owner == "provider-account-A"
    assert second.entitlement.provider_owner == "provider-account-A"
    assert first.entitlement.source_projection_sha256 == (
        second.entitlement.source_projection_sha256
    )
    assert first.evidence_sha256 == second.evidence_sha256
    assert not hasattr(first.entitlement, "account_id")


def test_selected_provider_owner_is_bound_into_projection_and_aggregate_identity() -> None:
    first = _read(selected_owner="provider-account-A")
    second = _read(selected_owner="provider-account-C")

    assert first.entitlement.provider_owner != second.entitlement.provider_owner
    assert first.entitlement.source_projection_sha256 != (
        second.entitlement.source_projection_sha256
    )
    assert first.evidence_sha256 != second.evidence_sha256


@pytest.mark.parametrize(
    "selected_owner, expected_error",
    [
        (_MISSING, "provider_owner is missing from provider response"),
        ("", "provider_owner must be a non-empty canonical string"),
        (" owner ", "provider_owner must be a non-empty canonical string"),
    ],
    ids=["missing", "blank", "noncanonical-whitespace"],
)
def test_missing_or_noncanonical_selected_provider_owner_fails_closed(
    selected_owner: object,
    expected_error: str,
) -> None:
    with pytest.raises(BetfairReadOnlyError, match=expected_error):
        _read(selected_owner=selected_owner)
