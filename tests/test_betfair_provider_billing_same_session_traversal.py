from __future__ import annotations

import copy
from datetime import datetime, timezone
import json

import pytest

import autosport.betfair_provider_billing_inputs as inputs
import autosport.betfair_provider_billing_inputs_authority as authority
from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)


class _Clock:
    def __init__(self) -> None:
        self._tick = 0

    def __call__(self) -> datetime:
        self._tick += 1
        return datetime(2026, 9, 22, 3, 0, self._tick, tzinfo=timezone.utc)


class _Transport:
    def post(
        self,
        _url: str,
        *,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        assert headers["X-Application"] == "template-app"
        assert headers["X-Authentication"] == "template-session"
        assert timeout_seconds > 0
        request = json.loads(body)
        method = request["method"]
        if method == "AccountAPING/v1.0/getAccountDetails":
            result: object = {"currencyCode": "GBP"}
        elif method == "AccountAPING/v1.0/getDeveloperAppKeys":
            result = [
                {
                    "appId": 41,
                    "appName": "autosport",
                    "appVersions": [
                        {
                            "owner": "owner",
                            "versionId": 7,
                            "version": "1.0",
                            "applicationKey": "template-app",
                            "delayData": False,
                            "subscriptionRequired": False,
                            "ownerManaged": False,
                            "active": True,
                            "vendorId": "vendor-3",
                        }
                    ],
                }
            ]
        elif method == "AccountAPING/v1.0/getAccountStatement":
            offset = request["params"]["fromRecord"]
            result = {
                "accountStatement": [
                    {
                        "refId": f"row-{offset}",
                        "itemDate": f"2026-09-21T00:00:0{offset + 1}Z",
                        "amount": -1,
                        "balance": 100 - offset,
                        "itemClass": "UNKNOWN",
                        "itemClassData": {"offset": offset},
                    }
                ],
                "moreAvailable": offset == 0,
            }
        else:  # pragma: no cover
            raise AssertionError(method)
        return json.dumps(
            {"jsonrpc": "2.0", "id": request["id"], "result": result},
            separators=(",", ":"),
        ).encode("utf-8")


def _template(offset: int):
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("template-app", "template-session"),
        transport=_Transport(),
        clock=_Clock(),
        venue_id="betfair",
        account_id="template-only",
    )
    return inputs.read_betfair_provider_billing_inputs(
        client,
        from_record=offset,
        record_count=1,
    )


def test_traversal_requires_one_authenticated_session_without_retaining_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    templates = {0: _template(0), 1: _template(1)}

    def fake_read(
        _client: BetfairReadOnlyClient,
        *,
        from_record: int = 0,
        **_kwargs: object,
    ):
        return copy.deepcopy(templates[from_record])

    previous_opener = authority._urllib_request.__dict__.get("_opener")
    monkeypatch.setattr(inputs, "read_betfair_provider_billing_inputs", fake_read)
    try:
        read, _validate, validate_traversal = authority._build_observation_authority()

        session_a = BetfairSessionCredentials("product-app", "session-a")
        first = read(session_a, from_record=0, record_count=1)
        second_same_session = read(session_a, from_record=1, record_count=1)
        same_session_pages = (first, second_same_session)

        assert validate_traversal(same_session_pages) is same_session_pages

        reconstructed_session_a = BetfairSessionCredentials(
            "product-app", "session-a"
        )
        second_reconstructed_session = read(
            reconstructed_session_a,
            from_record=1,
            record_count=1,
        )
        reconstructed_pages = (first, second_reconstructed_session)
        assert validate_traversal(reconstructed_pages) is reconstructed_pages

        closure = dict(
            zip(
                validate_traversal.__code__.co_freevars,
                (cell.cell_contents for cell in validate_traversal.__closure__ or ()),
            )
        )
        registry = closure["issued"]
        assert registry
        assert all(
            all(type(value) is not BetfairSessionCredentials for value in record)
            for record in registry.values()
        )
        assert all(type(record[2]) is bytes for record in registry.values())

        session_b = BetfairSessionCredentials("product-app", "session-b")
        second_other_session = read(session_b, from_record=1, record_count=1)

        with pytest.raises(
            authority.BetfairProviderBillingInputsAuthorityError,
            match="one authenticated session capability",
        ):
            validate_traversal((first, second_other_session))
    finally:
        authority._urllib_request.__dict__["_opener"] = previous_opener
