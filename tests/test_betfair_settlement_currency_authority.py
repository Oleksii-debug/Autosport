from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest

from autosport.betfair_account_readonly import (
    BetfairAccountDetailsObservation,
    BetfairEvidence,
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)
from autosport.betfair_settlement_revisions import (
    BetfairSettlementRevisionError,
    BetfairSettlementRevisionStore,
    read_currency_qualified_execution_readback,
)
from autosport.real_execution_ledger import (
    AcknowledgementStatus,
    ExecutionAction,
    ExecutionPlan,
    ExternalAcknowledgement,
    RealExecutionLedger,
)


@pytest.fixture(autouse=True)
def _isolated_monotonic_authority(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str((tmp_path.parent / f"{tmp_path.name}-monotonic-authority").resolve()),
    )


class _Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 29, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        value = self.value
        self.value += timedelta(seconds=1)
        return value


class _Transport:
    def __init__(self) -> None:
        self.currency = "USD"
        self.customer_order_ref: str | None = None

    def post(self, url: str, *, headers, body: bytes, timeout_seconds: float) -> bytes:
        request = json.loads(body.decode("utf-8"))
        method = request["method"]
        request_id = request["id"]
        if method.endswith("getAccountDetails"):
            result = {
                "currencyCode": self.currency,
                "localeCode": "en",
                "region": "GBR",
                "timezone": "UTC",
            }
        elif method.endswith("listMarketCatalogue"):
            result = [{"marketId": "1.234", "event": {"id": "event-1"}}]
        elif method.endswith("listCurrentOrders"):
            result = {"currentOrders": [], "moreAvailable": False}
        elif method.endswith("listClearedOrders"):
            rows = []
            if request["params"]["betStatus"] == "SETTLED":
                rows.append(
                    {
                        "betId": "bet-777",
                        "marketId": "1.234",
                        "eventId": "event-1",
                        "selectionId": 10,
                        "side": "BACK",
                        "placedDate": "2026-09-28T21:00:00+00:00",
                        "settledDate": "2026-09-28T22:00:00+00:00",
                        "priceRequested": 2,
                        "priceMatched": 2,
                        "sizeSettled": 5,
                        "profit": 4,
                        "customerOrderRef": self.customer_order_ref,
                        "betOutcome": "WON",
                    }
                )
            result = {"clearedOrders": rows, "moreAvailable": False}
        else:  # pragma: no cover
            raise AssertionError(method)
        return json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "result": result},
            separators=(",", ":"),
        ).encode("utf-8")


def _context(tmp_path):
    action = ExecutionAction(
        action_id="action-1",
        bookmaker_id="betfair",
        account_id="acct-1",
        event_id="event-1",
        market_id="1.234",
        selection_id="10",
        side="BACK",
        requested_odds=Decimal("2"),
        requested_stake=Decimal("5"),
        quote_id="quote-1",
        quote_observed_at="2026-09-28T20:59:00+00:00",
        expires_at="2026-09-28T21:05:00+00:00",
    )
    ledger = RealExecutionLedger(tmp_path / "execution.jsonl")
    plan = ExecutionPlan(
        plan_id="plan-1",
        bookmaker_profile_version="profile-1",
        decision_id="decision-1",
        approval_id="approval-1",
        created_at="2026-09-28T20:59:30+00:00",
        actions=(action,),
    )
    ledger.reserve_plan(plan)
    ledger.begin_attempt(
        plan_id=plan.plan_id,
        action_id=action.action_id,
        attempt_id="attempt-1",
        reserved_at="2026-09-28T21:00:00+00:00",
    )
    provider_ref = ledger.bind_provider_order_reference(
        attempt_id="attempt-1",
        provider_id="betfair",
    )
    ledger.mark_submitted(
        "attempt-1",
        submitted_at="2026-09-28T21:01:00+00:00",
    )
    ledger.acknowledge(
        ExternalAcknowledgement(
            attempt_id="attempt-1",
            external_receipt_id="bet-777",
            status=AcknowledgementStatus.ACCEPTED,
            acknowledged_at="2026-09-28T21:02:00+00:00",
            accepted_odds=Decimal("2"),
            accepted_stake=Decimal("5"),
        )
    )
    transport = _Transport()
    transport.customer_order_ref = provider_ref
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-key", "session-token"),
        transport=transport,
        clock=_Clock(),
        venue_id="betfair",
        account_id="acct-1",
    )
    return ledger, plan, action, provider_ref, transport, client


def _ingest(store, ledger, plan, action, capture):
    return store.ingest(
        ledger,
        plan_id=plan.plan_id,
        attempt_id="attempt-1",
        action=action,
        capture=capture,
    )


def _qualified_capture(client, provider_ref):
    return read_currency_qualified_execution_readback(
        client,
        action_id="action-1",
        market_id="1.234",
        provider_order_ref=provider_ref,
    )


def test_ordinary_readback_preserves_profit_only_as_unqualified_provider_number(
    tmp_path,
) -> None:
    ledger, plan, action, provider_ref, _transport, client = _context(tmp_path)
    capture = client.read_execution_readback(
        action_id="action-1",
        market_id="1.234",
        provider_order_ref=provider_ref,
    )
    revision = _ingest(
        BetfairSettlementRevisionStore(tmp_path / "settlement.jsonl"),
        ledger,
        plan,
        action,
        capture,
    ).revision

    assert revision.provider_profit == Decimal("4")
    assert revision.provider_profit_currency is None
    assert revision.provider_profit_currency_qualified is False
    with pytest.raises(
        BetfairSettlementRevisionError,
        match="no authenticated currency authority",
    ):
        _ = revision.provider_profit_money


def test_same_client_authenticated_account_details_qualify_provider_profit_money(
    tmp_path,
) -> None:
    ledger, plan, action, provider_ref, _transport, client = _context(tmp_path)
    path = tmp_path / "settlement.jsonl"
    capture = _qualified_capture(client, provider_ref)
    revision = _ingest(
        BetfairSettlementRevisionStore(path),
        ledger,
        plan,
        action,
        capture,
    ).revision

    assert revision.provider_profit_currency == "USD"
    assert revision.provider_profit_currency_qualified is True
    assert revision.provider_profit_money == (Decimal("4"), "USD")

    restarted = BetfairSettlementRevisionStore(path)
    persisted = restarted.current("betfair", "acct-1", "bet-777")
    assert persisted is not None
    assert persisted.provider_profit_money == (Decimal("4"), "USD")


def test_caller_constructed_account_details_cannot_upgrade_ordinary_capture(
    tmp_path,
) -> None:
    ledger, plan, action, provider_ref, _transport, client = _context(tmp_path)
    forged = BetfairAccountDetailsObservation(
        currency_code="EUR",
        locale_code=None,
        region=None,
        timezone_name=None,
        evidence=BetfairEvidence(
            observed_at="2026-09-28T20:59:59+00:00",
            source_payload_sha256="f" * 64,
        ),
    )
    assert forged.currency_code == "EUR"

    capture = client.read_execution_readback(
        action_id="action-1",
        market_id="1.234",
        provider_order_ref=provider_ref,
    )
    revision = _ingest(
        BetfairSettlementRevisionStore(tmp_path / "settlement.jsonl"),
        ledger,
        plan,
        action,
        capture,
    ).revision

    assert revision.provider_profit_currency is None
    with pytest.raises(BetfairSettlementRevisionError):
        _ = revision.provider_profit_money


def test_authenticated_currency_change_is_new_semantic_revision(tmp_path) -> None:
    ledger, plan, action, provider_ref, transport, client = _context(tmp_path)
    store = BetfairSettlementRevisionStore(tmp_path / "settlement.jsonl")

    first = _ingest(
        store,
        ledger,
        plan,
        action,
        _qualified_capture(client, provider_ref),
    ).revision
    assert first.provider_profit_currency == "USD"

    transport.currency = "EUR"
    second = _ingest(
        store,
        ledger,
        plan,
        action,
        _qualified_capture(client, provider_ref),
    ).revision

    assert second.revision_number == 2
    assert second.previous_revision_id == first.revision_id
    assert second.provider_profit_currency == "EUR"
    assert second.provider_profit == first.provider_profit
