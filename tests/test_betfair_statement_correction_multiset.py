from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json

from autosport.betfair_account_readonly import BetfairReadOnlyClient, BetfairSessionCredentials
from autosport.betfair_provider_billing_inputs import read_betfair_provider_billing_inputs


class _Clock:
    def __init__(self) -> None:
        self._tick = 0

    def __call__(self) -> datetime:
        self._tick += 1
        return datetime(2026, 9, 21, 11, 30, self._tick, tzinfo=timezone.utc)


class _StatementTransport:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        del url, headers, timeout_seconds
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
                            "applicationKey": "k",
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
            result = {"accountStatement": self.rows, "moreAvailable": False}
        else:  # pragma: no cover
            raise AssertionError(method)
        return json.dumps(
            {"jsonrpc": "2.0", "id": request["id"], "result": result},
            separators=(",", ":"),
        ).encode("utf-8")


def _rows() -> list[dict[str, object]]:
    # Multiple provider money-movement rows may share a reference and timestamp.
    # Those fields are therefore not a unique row key: preserve the full multiset.
    return [
        {
            "refId": "shared-ref",
            "itemDate": "2026-09-20T09:00:00Z",
            "amount": -10,
            "balance": 90,
            "itemClass": "UNKNOWN",
            "itemClassData": {
                "transactionType": "ACCOUNT_DEBIT",
                "winLose": "RESULT_FIX",
                "movement": "first",
            },
        },
        {
            "refId": "shared-ref",
            "itemDate": "2026-09-20T09:00:00Z",
            "amount": 10,
            "balance": 100,
            "itemClass": "UNKNOWN",
            "itemClassData": {
                "transactionType": "ACCOUNT_DEBIT",
                "winLose": "RESULT_FIX",
                "movement": "second",
            },
        },
    ]


def _read(rows: list[dict[str, object]]):
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("k", "s"),
        transport=_StatementTransport(rows),
        clock=_Clock(),
        venue_id="betfair",
        account_id="local-label",
    )
    return read_betfair_provider_billing_inputs(client)


def test_same_ref_and_item_date_rows_preserve_multiplicity_and_order() -> None:
    observation = _read(_rows())
    items = observation.statement.items

    assert len(items) == 2
    assert [item.ref_id for item in items] == ["shared-ref", "shared-ref"]
    assert [item.item_date for item in items] == [
        "2026-09-20T09:00:00Z",
        "2026-09-20T09:00:00Z",
    ]
    assert [item.amount for item in items] == [Decimal("-10"), Decimal("10")]
    assert [item.balance for item in items] == [Decimal("90"), Decimal("100")]
    assert items[0].item_class_data_sha256 != items[1].item_class_data_sha256


def test_same_ref_and_item_date_sibling_payload_change_changes_evidence_identity() -> None:
    original_rows = _rows()
    changed_rows = _rows()
    second_data = dict(changed_rows[1]["itemClassData"])  # type: ignore[arg-type]
    second_data["movement"] = "second-revised"
    changed_rows[1] = {
        **changed_rows[1],
        "amount": 11,
        "balance": 101,
        "itemClassData": second_data,
    }

    original = _read(original_rows)
    changed = _read(changed_rows)

    assert original.statement.items[0] == changed.statement.items[0]
    assert original.statement.items[1].ref_id == changed.statement.items[1].ref_id
    assert original.statement.items[1].item_date == changed.statement.items[1].item_date
    assert original.statement.items[1] != changed.statement.items[1]
    assert original.evidence_sha256 != changed.evidence_sha256


def test_reordering_same_ref_and_item_date_siblings_changes_capture_identity() -> None:
    rows = _rows()
    forward = _read(rows)
    reverse = _read(list(reversed(rows)))

    assert len(forward.statement.items) == 2
    assert len(reverse.statement.items) == 2
    assert forward.statement.items == tuple(reversed(reverse.statement.items))
    assert forward.evidence_sha256 != reverse.evidence_sha256
