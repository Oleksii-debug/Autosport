from __future__ import annotations

from dataclasses import replace
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
from autosport.provider_billing_row_attribution import (
    resolve_provider_billing_row_attribution,
)


class _Clock:
    def __init__(self) -> None:
        self._tick = 0

    def __call__(self) -> datetime:
        self._tick += 1
        return datetime(2026, 9, 22, 6, 0, self._tick, tzinfo=timezone.utc)


def _transaction_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "eventId": 0,
        "eventTypeId": 0,
        "fullMarketName": (
            "Bet Txn Charge for over 5000 per hour on 08/03/2021 - TEST"
        ),
        "marketName": "DEBIT",
        "marketType": "NOT_APPLICABLE",
        "selectionId": 0,
        "transactionType": "ACCOUNT_DEBIT",
        "transactionId": 200000488391954,
        "winLose": "RESULT_NOT_APPLICABLE",
    }
    payload.update(overrides)
    return payload


def _statement_row(
    *,
    nested: dict[str, object] | None = None,
    nested_raw: str | None = None,
    item_class: str = "UNKNOWN",
    amount: int = -1,
) -> dict[str, object]:
    if nested_raw is None:
        nested_raw = json.dumps(
            _transaction_payload() if nested is None else nested,
            separators=(",", ":"),
        )
    return {
        "refId": "0",
        "itemDate": "2026-09-21T09:27:13Z",
        "amount": amount,
        "balance": 100,
        "itemClass": item_class,
        "itemClassData": {"unknownStatementItem": nested_raw},
    }


class _Transport:
    def __init__(self, row: dict[str, object]) -> None:
        self.row = row

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
            result = [
                {
                    "appId": 41,
                    "appName": "autosport",
                    "appVersions": [
                        {
                            "owner": "provider-owner",
                            "versionId": 7,
                            "version": "1.0",
                            "applicationKey": "live-key",
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
            result = {
                "accountStatement": [self.row],
                "moreAvailable": False,
            }
        else:  # pragma: no cover
            raise AssertionError(method)
        return json.dumps(
            {"jsonrpc": "2.0", "id": request["id"], "result": result},
            separators=(",", ":"),
        ).encode("utf-8")


def _read(row: dict[str, object]):
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("live-key", "session-token"),
        transport=_Transport(row),
        clock=_Clock(),
        venue_id="betfair",
        account_id="caller-label-is-not-authority",
    )
    return read_betfair_provider_billing_inputs(client)


def test_documented_transaction_charge_emits_only_privacy_minimized_signal() -> None:
    observation = _read(_statement_row())
    item = observation.statement.items[0]

    assert item.provider_charge_class == "BETFAIR_TRANSACTION_CHARGE"
    assert item.provider_transaction_id == 200000488391954
    assert "Bet Txn Charge for over 5000 per hour" not in repr(item)
    assert "Bet Txn Charge for over 5000 per hour" not in repr(observation)
    assert not hasattr(item, "full_market_name")
    assert not hasattr(observation, "allocated_amount")
    assert not hasattr(observation, "cost_class")


@pytest.mark.parametrize(
    ("nested_overrides", "item_class", "amount"),
    [
        ({"eventId": 1}, "UNKNOWN", -1),
        ({"eventId": False}, "UNKNOWN", -1),
        ({"eventTypeId": 1}, "UNKNOWN", -1),
        ({"selectionId": 1}, "UNKNOWN", -1),
        ({"marketName": "CREDIT"}, "UNKNOWN", -1),
        ({"marketType": "MATCH_ODDS"}, "UNKNOWN", -1),
        ({"transactionType": "ACCOUNT_CREDIT"}, "UNKNOWN", -1),
        ({"winLose": "WIN"}, "UNKNOWN", -1),
        ({"transactionId": 0}, "UNKNOWN", -1),
        ({"transactionId": True}, "UNKNOWN", -1),
        ({"fullMarketName": "Other account debit"}, "UNKNOWN", -1),
        ({}, "COMMISSION", -1),
        ({}, "UNKNOWN", 1),
    ],
)
def test_near_miss_statement_rows_never_mint_transaction_charge_signal(
    nested_overrides: dict[str, object],
    item_class: str,
    amount: int,
) -> None:
    nested = _transaction_payload(**nested_overrides)
    item = _read(
        _statement_row(
            nested=nested,
            item_class=item_class,
            amount=amount,
        )
    ).statement.items[0]

    assert item.provider_charge_class is None
    assert item.provider_transaction_id is None


def test_malformed_or_unrecognized_description_is_unclassified_not_zero() -> None:
    item = _read(
        _statement_row(nested_raw="not-json")
    ).statement.items[0]

    assert item.provider_charge_class is None
    assert item.provider_transaction_id is None
    assert not hasattr(item, "known_zero")


def test_transaction_charge_signal_is_bound_into_billing_evidence_identity() -> None:
    observation = _read(_statement_row())
    item = observation.statement.items[0]
    assert item.provider_transaction_id is not None

    tampered_item = replace(
        item,
        provider_transaction_id=item.provider_transaction_id + 1,
    )
    tampered_statement = replace(
        observation.statement,
        items=(tampered_item,),
    )

    with pytest.raises(
        BetfairReadOnlyError,
        match="combined evidence digest mismatch",
    ):
        replace(
            observation,
            statement=tampered_statement,
        )


def test_transaction_charge_signal_does_not_mint_intent_allocation() -> None:
    observation = _read(_statement_row())
    evidence = resolve_provider_billing_row_attribution(observation, "0")

    assert evidence.attribution_state == "UNPROVEN"
    assert evidence.missing_authorities == (
        "AUTOSPORT_ACTIVITY_NUMERATOR",
        "PROVIDER_WINDOW_TOTAL_DENOMINATOR",
        "COST_APPLICABILITY",
        "ALLOCATION_RULE",
    )
    assert evidence.source_evidence_sha256 == observation.evidence_sha256
    assert not hasattr(evidence, "allocated_amount")
    assert not hasattr(evidence, "allocation_fraction")
