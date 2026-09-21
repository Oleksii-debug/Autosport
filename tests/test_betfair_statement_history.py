from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)
from autosport.betfair_provider_billing_inputs import read_betfair_provider_billing_inputs
from autosport.betfair_statement_history import (
    BetfairStatementHistory,
    BetfairStatementHistoryError,
)


class _Clock:
    def __init__(self) -> None:
        self._current = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        self._current += timedelta(seconds=1)
        return self._current


class _StatementTransport:
    def __init__(self, rows: list[dict[str, object]], *, currency: str = "GBP") -> None:
        self.rows = rows
        self.currency = currency

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
            result: object = {"currencyCode": self.currency}
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
            params = request["params"]
            start = params["fromRecord"]
            count = params["recordCount"]
            page = self.rows[start : start + count]
            result = {
                "accountStatement": page,
                "moreAvailable": start + count < len(self.rows),
            }
        else:  # pragma: no cover
            raise AssertionError(method)
        return json.dumps(
            {"jsonrpc": "2.0", "id": request["id"], "result": result},
            separators=(",", ":"),
        ).encode("utf-8")


def _rows() -> list[dict[str, object]]:
    return [
        {
            "refId": "shared-ref",
            "itemDate": "2026-09-20T09:00:00Z",
            "amount": 10,
            "balance": 110,
            "itemClass": "UNKNOWN",
            "itemClassData": {
                "transactionType": "ACCOUNT_CREDIT",
                "winLose": "RESULT_WON",
                "sibling": "one",
            },
        },
        {
            "refId": "shared-ref",
            "itemDate": "2026-09-20T09:00:00Z",
            "amount": -1,
            "balance": 109,
            "itemClass": "UNKNOWN",
            "itemClassData": {
                "transactionType": "ACCOUNT_DEBIT",
                "winLose": "COMMISSION",
                "sibling": "two",
            },
        },
        {
            "refId": "other-ref",
            "itemDate": "2026-09-20T10:00:00Z",
            "amount": 2,
            "balance": 111,
            "itemClass": "UNKNOWN",
            "itemClassData": {
                "transactionType": "ACCOUNT_CREDIT",
                "winLose": "RESULT_WON",
            },
        },
    ]


def _client(transport: _StatementTransport) -> BetfairReadOnlyClient:
    return BetfairReadOnlyClient(
        BetfairSessionCredentials("k", "s"),
        transport=transport,
        clock=_Clock(),
        venue_id="betfair",
        account_id="local-label-not-provider-authority",
    )


def _capture(
    client: BetfairReadOnlyClient,
    *,
    record_count: int = 2,
    statement_from: str = "2026-09-01T00:00:00Z",
    statement_to: str = "2026-09-30T00:00:00Z",
):
    pages = []
    offset = 0
    while True:
        observation = read_betfair_provider_billing_inputs(
            client,
            from_record=offset,
            record_count=record_count,
            statement_from=statement_from,
            statement_to=statement_to,
        )
        pages.append(observation)
        if not observation.statement.more_available:
            return tuple(pages)
        offset += record_count


def test_complete_multi_page_capture_preserves_order_multiplicity_and_evidence() -> None:
    transport = _StatementTransport(_rows())
    history = BetfairStatementHistory.empty().append_pages(_capture(_client(transport)))
    snapshot = history.current_restated_view()

    assert snapshot.pagination_complete is True
    assert snapshot.cross_page_atomicity_proven is False
    assert snapshot.economic_classification_complete is False
    assert snapshot.economic_total_authoritative is False
    assert len(snapshot.pages) == 2
    assert len(snapshot.rows) == 3
    assert [row.ref_id for row in snapshot.rows[:2]] == ["shared-ref", "shared-ref"]
    assert snapshot.rows[0].item_class_data_sha256 != snapshot.rows[1].item_class_data_sha256
    assert [row.ordinal for row in snapshot.rows] == [0, 1, 2]
    assert not hasattr(snapshot, "net_amount")
    assert not hasattr(snapshot, "pnl")


def test_partial_pagination_cannot_publish_complete_snapshot() -> None:
    transport = _StatementTransport(_rows())
    client = _client(transport)
    first_page = read_betfair_provider_billing_inputs(
        client,
        from_record=0,
        record_count=2,
        statement_from="2026-09-01T00:00:00Z",
        statement_to="2026-09-30T00:00:00Z",
    )
    assert first_page.statement.more_available is True

    with pytest.raises(BetfairStatementHistoryError, match="partial pagination"):
        BetfairStatementHistory.empty().append_pages((first_page,))


def test_exact_capture_replay_is_idempotent() -> None:
    transport = _StatementTransport(_rows())
    pages = _capture(_client(transport))
    first = BetfairStatementHistory.empty().append_pages(pages)
    replay = first.append_pages(pages)

    assert replay is first
    assert replay.history_id == first.history_id
    assert len(replay.snapshots) == 1


def test_later_restatement_never_rewrites_prior_as_known_view() -> None:
    transport = _StatementTransport(_rows())
    client = _client(transport)
    first_pages = _capture(client)
    history = BetfairStatementHistory.empty().append_pages(first_pages)
    original = history.current_restated_view()

    changed = _rows()
    changed[0] = {
        **changed[0],
        "amount": -10,
        "balance": 99,
        "itemClassData": {
            "transactionType": "ACCOUNT_DEBIT",
            "winLose": "RESULT_ERR",
            "sibling": "one-restated",
        },
    }
    changed.append(
        {
            "refId": "fix-ref",
            "itemDate": "2026-09-20T11:00:00Z",
            "amount": 10,
            "balance": 109,
            "itemClass": "UNKNOWN",
            "itemClassData": {
                "transactionType": "ACCOUNT_CREDIT",
                "winLose": "RESULT_FIX",
            },
        }
    )
    transport.rows = changed
    history = history.append_pages(_capture(client))
    latest = history.current_restated_view()

    assert latest.snapshot_id != original.snapshot_id
    assert latest.predecessor_snapshot_id == original.snapshot_id
    assert latest.ordered_rows_sha256 != original.ordered_rows_sha256
    assert history.latest_capture_is_restatement() is True
    assert history.as_known_at(original.available_at).snapshot_id == original.snapshot_id
    assert history.as_known_at(latest.available_at).snapshot_id == latest.snapshot_id
    assert len(original.rows) == 3
    assert len(latest.rows) == 4


def test_later_omission_is_current_view_evidence_not_erasure_of_history() -> None:
    transport = _StatementTransport(_rows())
    client = _client(transport)
    history = BetfairStatementHistory.empty().append_pages(_capture(client))
    before = history.current_restated_view()

    transport.rows = _rows()[:1]
    history = history.append_pages(_capture(client))
    current = history.current_restated_view()

    assert len(before.rows) == 3
    assert len(current.rows) == 1
    assert history.as_known_at(before.available_at).rows == before.rows
    assert history.latest_capture_is_restatement() is True


def test_restart_round_trip_preserves_chain_cutoffs_and_identities() -> None:
    transport = _StatementTransport(_rows())
    client = _client(transport)
    history = BetfairStatementHistory.empty().append_pages(_capture(client))
    first = history.current_restated_view()

    transport.rows = list(reversed(_rows()))
    history = history.append_pages(_capture(client))
    latest = history.current_restated_view()

    restored = BetfairStatementHistory.from_json(history.to_json())

    assert restored.history_id == history.history_id
    assert [item.snapshot_id for item in restored.snapshots] == [
        item.snapshot_id for item in history.snapshots
    ]
    assert restored.as_known_at(first.available_at).snapshot_id == first.snapshot_id
    assert restored.as_known_at(latest.available_at).snapshot_id == latest.snapshot_id


def test_persisted_history_rejects_broken_predecessor_and_duplicate_json_keys() -> None:
    transport = _StatementTransport(_rows())
    history = BetfairStatementHistory.empty().append_pages(_capture(_client(transport)))

    data = json.loads(history.to_json())
    data["snapshots"][0]["predecessor_snapshot_id"] = "a" * 64
    with pytest.raises(BetfairStatementHistoryError, match="snapshot_id mismatch|predecessor"):
        BetfairStatementHistory.from_json(
            json.dumps(data, sort_keys=True, separators=(",", ":"))
        )

    with pytest.raises(BetfairStatementHistoryError, match="duplicate key"):
        BetfairStatementHistory.from_json(
            '{"schema":"autosport.betfair_statement_history",'
            '"schema":"autosport.betfair_statement_history",'
            '"schema_version":1,"snapshots":[]}'
        )


def test_cutoff_before_first_complete_capture_fails_closed() -> None:
    transport = _StatementTransport(_rows())
    history = BetfairStatementHistory.empty().append_pages(_capture(_client(transport)))

    with pytest.raises(BetfairStatementHistoryError, match="causally available"):
        history.as_known_at("2026-09-21T11:00:00+00:00")


def test_scope_change_cannot_be_spliced_into_existing_history() -> None:
    first_transport = _StatementTransport(_rows(), currency="GBP")
    first = BetfairStatementHistory.empty().append_pages(
        _capture(_client(first_transport))
    )

    second_transport = _StatementTransport(_rows(), currency="EUR")
    with pytest.raises(BetfairStatementHistoryError, match="scope"):
        first.append_pages(_capture(_client(second_transport)))
