from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest

from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)
from autosport.betfair_settlement_revisions import (
    BetfairSettlementRevisionError,
    BetfairSettlementRevisionStore,
)
from autosport.real_execution_ledger import ExecutionAction


class _Clock:
    def __init__(self) -> None:
        self._value = datetime(2026, 9, 21, 20, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        result = self._value
        self._value += timedelta(seconds=1)
        return result


class _BetfairReadbackTransport:
    def __init__(self) -> None:
        self.provider_status = "SETTLED"
        self.profit = 4
        self.settled_date = "2026-09-21T19:00:00+00:00"

    def post(self, url: str, *, headers, body: bytes, timeout_seconds: float) -> bytes:
        request = json.loads(body.decode("utf-8"))
        method = request["method"]
        request_id = request["id"]
        if method.endswith("listMarketCatalogue"):
            result = [{"marketId": "1.234", "event": {"id": "event-1"}}]
        elif method.endswith("listCurrentOrders"):
            result = {"currentOrders": [], "moreAvailable": False}
        elif method.endswith("listClearedOrders"):
            status = request["params"]["betStatus"]
            rows = []
            if status == self.provider_status:
                rows.append(
                    {
                        "betId": "bet-777",
                        "marketId": "1.234",
                        "eventId": "event-1",
                        "selectionId": 10,
                        "side": "BACK",
                        "placedDate": "2026-09-21T18:00:00+00:00",
                        "settledDate": self.settled_date,
                        "priceRequested": 2,
                        "priceMatched": 2,
                        "sizeSettled": 5,
                        "profit": self.profit,
                        "customerOrderRef": "action-1",
                    }
                )
            result = {"clearedOrders": rows, "moreAvailable": False}
        else:  # pragma: no cover - the readback surface is intentionally closed
            raise AssertionError(method)
        return json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "result": result},
            separators=(",", ":"),
        ).encode("utf-8")


def _action(**changes) -> ExecutionAction:
    values = {
        "action_id": "action-1",
        "bookmaker_id": "betfair",
        "account_id": "acct-1",
        "event_id": "event-1",
        "market_id": "1.234",
        "selection_id": "10",
        "side": "BACK",
        "requested_odds": Decimal("2"),
        "requested_stake": Decimal("5"),
        "quote_id": "quote-1",
        "quote_observed_at": "2026-09-21T17:59:00+00:00",
        "expires_at": "2026-09-21T18:05:00+00:00",
    }
    values.update(changes)
    return ExecutionAction(**values)


def _client(transport: _BetfairReadbackTransport) -> BetfairReadOnlyClient:
    return BetfairReadOnlyClient(
        BetfairSessionCredentials("app-key", "session-token"),
        transport=transport,
        clock=_Clock(),
        venue_id="betfair",
        account_id="acct-1",
    )


def _capture(client: BetfairReadOnlyClient):
    return client.read_execution_readback(action_id="action-1", market_id="1.234")


def test_identical_reread_is_idempotent_and_restart_safe(tmp_path) -> None:
    transport = _BetfairReadbackTransport()
    client = _client(transport)
    path = tmp_path / "settlement-revisions.jsonl"
    store = BetfairSettlementRevisionStore(path)

    first = store.ingest(_action(), _capture(client))
    second = store.ingest(_action(), _capture(client))

    assert first.created is True
    assert second.created is False
    assert second.revision.revision_id == first.revision.revision_id
    assert len(store.revisions) == 1
    assert first.revision.permanent_final is False
    assert first.revision.terminal_space_exact is False

    restarted = BetfairSettlementRevisionStore(path)
    current = restarted.current("betfair", "acct-1", "bet-777")
    assert current is not None
    assert current.revision_id == first.revision.revision_id
    assert len(restarted.revisions) == 1


def test_later_void_correction_appends_and_does_not_leak_backward(tmp_path) -> None:
    transport = _BetfairReadbackTransport()
    client = _client(transport)
    store = BetfairSettlementRevisionStore(tmp_path / "settlement-revisions.jsonl")

    first = store.ingest(_action(), _capture(client)).revision
    transport.provider_status = "VOIDED"
    transport.profit = 0
    transport.settled_date = "2026-09-21T19:30:00+00:00"
    second = store.ingest(_action(), _capture(client)).revision

    assert second.revision_number == 2
    assert second.previous_revision_id == first.revision_id
    assert second.provider_status == "VOIDED"
    assert second.provider_profit == Decimal("0")
    assert second.permanent_final is False
    assert second.terminal_space_exact is False
    assert store.as_of("betfair", "acct-1", "bet-777", first.available_at) == first
    assert store.as_of("betfair", "acct-1", "bet-777", second.available_at) == second


def test_identity_mismatch_and_forged_capture_fail_closed(tmp_path) -> None:
    transport = _BetfairReadbackTransport()
    client = _client(transport)
    store = BetfairSettlementRevisionStore(tmp_path / "settlement-revisions.jsonl")
    capture = _capture(client)

    with pytest.raises(BetfairSettlementRevisionError, match="event mismatch"):
        store.ingest(_action(event_id="different-event"), capture)

    forged = replace(capture, evidence_sha256="0" * 64)
    with pytest.raises(BetfairSettlementRevisionError, match="not canonical adapter-issued"):
        store.ingest(_action(), forged)


def test_external_bet_cannot_be_rebound_to_another_action(tmp_path) -> None:
    transport = _BetfairReadbackTransport()
    client = _client(transport)
    store = BetfairSettlementRevisionStore(tmp_path / "settlement-revisions.jsonl")
    store.ingest(_action(), _capture(client))

    # A different execution cannot launder the same external receipt into a new
    # lineage even if every other provider identity matches.
    capture = client.read_execution_readback(
        action_id="other-action",
        market_id="1.234",
        provider_order_ref="action-1",
    )
    with pytest.raises(BetfairSettlementRevisionError, match="different execution action"):
        store.ingest(_action(action_id="other-action"), capture)


def test_tampered_durable_record_is_rejected_on_restart(tmp_path) -> None:
    transport = _BetfairReadbackTransport()
    client = _client(transport)
    path = tmp_path / "settlement-revisions.jsonl"
    store = BetfairSettlementRevisionStore(path)
    store.ingest(_action(), _capture(client))

    record = json.loads(path.read_text(encoding="utf-8"))
    record["revision"]["provider_profit"] = "999"
    path.write_text(json.dumps(record, separators=(",", ":")) + "\n", encoding="utf-8")

    with pytest.raises(BetfairSettlementRevisionError, match="digest mismatch"):
        BetfairSettlementRevisionStore(path)
