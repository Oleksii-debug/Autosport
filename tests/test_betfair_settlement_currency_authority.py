from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
import urllib.request as _urllib_request

import pytest

from autosport.betfair_account_identity import build_betfair_authenticated_client
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


class _Response:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, limit: int) -> bytes:
        assert limit >= len(self._payload)
        return self._payload


class _ProviderResponses:
    def __init__(self) -> None:
        self.currency = "USD"
        self.customer_order_ref: str | None = None
        self.account_details_reads = 0
        self.mutate_client_on_details_read: int | None = None
        self.client: BetfairReadOnlyClient | None = None

    def response(self, request) -> _Response:
        assert request.data is not None
        rpc = json.loads(request.data.decode("utf-8"))
        method = rpc["method"]
        request_id = rpc["id"]
        if method.endswith("getAccountDetails"):
            self.account_details_reads += 1
            result = {
                "currencyCode": self.currency,
                "localeCode": "en",
                "region": "GBR",
                "timezone": "UTC",
            }
            if self.mutate_client_on_details_read == self.account_details_reads:
                assert self.client is not None
                self.client._credentials = BetfairSessionCredentials(
                    "app-key-rotated",
                    "session-token-rotated",
                )
        elif method.endswith("listMarketCatalogue"):
            result = [{"marketId": "1.234", "event": {"id": "event-1"}}]
        elif method.endswith("listCurrentOrders"):
            result = {"currentOrders": [], "moreAvailable": False}
        elif method.endswith("listClearedOrders"):
            rows = []
            if rpc["params"]["betStatus"] == "SETTLED":
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
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "result": result},
            separators=(",", ":"),
        ).encode("utf-8")
        return _Response(payload)


class _Opener:
    def __init__(self, provider: _ProviderResponses) -> None:
        self._provider = provider

    def open(self, request, data=None, timeout: float = 0):
        assert data is None
        assert timeout > 0
        return self._provider.response(request)


def _context(tmp_path, monkeypatch):
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
    provider = _ProviderResponses()
    provider.customer_order_ref = provider_ref
    monkeypatch.setattr(_urllib_request, "_opener", _Opener(provider))
    client = build_betfair_authenticated_client(
        BetfairSessionCredentials("app-key", "session-token"),
        account_label="acct-1",
    )
    provider.client = client
    return ledger, plan, action, provider_ref, provider, client


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
    monkeypatch,
) -> None:
    ledger, plan, action, provider_ref, _provider, client = _context(
        tmp_path, monkeypatch
    )
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
    monkeypatch,
) -> None:
    ledger, plan, action, provider_ref, _provider, client = _context(
        tmp_path, monkeypatch
    )
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
    monkeypatch,
) -> None:
    ledger, plan, action, provider_ref, _provider, client = _context(
        tmp_path, monkeypatch
    )
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


def test_authenticated_currency_change_is_new_semantic_revision(
    tmp_path,
    monkeypatch,
) -> None:
    ledger, plan, action, provider_ref, provider, client = _context(
        tmp_path, monkeypatch
    )
    store = BetfairSettlementRevisionStore(tmp_path / "settlement.jsonl")

    first = _ingest(
        store,
        ledger,
        plan,
        action,
        _qualified_capture(client, provider_ref),
    ).revision
    assert first.provider_profit_currency == "USD"

    provider.currency = "EUR"
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


def test_direct_client_cannot_mint_currency_qualified_profit(tmp_path, monkeypatch) -> None:
    _ledger, _plan, _action, provider_ref, provider, _client = _context(
        tmp_path, monkeypatch
    )
    direct = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-key", "session-token"),
        venue_id="betfair",
        account_id="acct-1",
    )
    provider.client = direct

    with pytest.raises(
        BetfairSettlementRevisionError,
        match="lacks K07 authenticated client/session authority",
    ):
        _qualified_capture(direct, provider_ref)


def test_credential_swap_between_currency_and_execution_invalidates_qualification(
    tmp_path,
    monkeypatch,
) -> None:
    _ledger, _plan, _action, provider_ref, provider, client = _context(
        tmp_path, monkeypatch
    )
    # The guard's first K07 identity read is details read #1.  The legacy currency
    # bridge then performs details read #2 immediately before execution readback.
    provider.mutate_client_on_details_read = 2

    with pytest.raises(
        BetfairSettlementRevisionError,
        match="authenticated session changed during execution readback",
    ):
        _qualified_capture(client, provider_ref)


def test_credential_rotation_after_capture_blocks_currency_persistence(
    tmp_path,
    monkeypatch,
) -> None:
    ledger, plan, action, provider_ref, _provider, client = _context(
        tmp_path, monkeypatch
    )
    capture = _qualified_capture(client, provider_ref)
    client._credentials = BetfairSessionCredentials(
        "app-key-rotated",
        "session-token-rotated",
    )

    with pytest.raises(
        BetfairSettlementRevisionError,
        match="authenticated session changed before persistence",
    ):
        _ingest(
            BetfairSettlementRevisionStore(tmp_path / "settlement.jsonl"),
            ledger,
            plan,
            action,
            capture,
        )
