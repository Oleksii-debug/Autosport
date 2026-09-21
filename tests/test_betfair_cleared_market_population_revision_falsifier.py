from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from autosport import betfair_cleared_market_population as population
from autosport.betfair_account_readonly import (
    BetfairAccountDetailsObservation,
    BetfairClearedOrderObservation,
    BetfairClearedOrderPage,
    BetfairEvidence,
    BetfairSessionCredentials,
)
from autosport.betfair_market_commission_authority import (
    BetfairMarketCommissionAuthority,
    BetfairMarketCommissionReceipt,
)


UTC = timezone.utc
MARKET = "1.234"
T0 = "2026-09-20T00:00:00.000Z"
T1 = "2026-09-21T00:00:00.000Z"
NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
ACCOUNT_SHA = "a" * 64
ACCOUNT_ID = f"betfair-account-evidence:{ACCOUNT_SHA}"


def _evidence(digit: str) -> BetfairEvidence:
    return BetfairEvidence("2026-09-21T10:00:00+00:00", digit * 64)


def _source(tmp_path) -> BetfairMarketCommissionAuthority:
    return BetfairMarketCommissionAuthority(
        tmp_path / "commission",
        BetfairSessionCredentials("app-key", "session-token"),
    )


def _receipt(*, market_profit: str) -> BetfairMarketCommissionReceipt:
    date_range = {"from": T0, "to": T1}
    request_scope = {
        "method": "SportsAPING/v1.0/listClearedOrders",
        "betStatus": "SETTLED",
        "groupBy": "MARKET",
        "marketIds": [MARKET],
        "settledDateRange": date_range,
        "fromRecord": 0,
        "recordCount": 1000,
        "venue_id": "betfair",
        "account_id": ACCOUNT_ID,
        "adapter_id": "betfair-exchange-jsonrpc-readonly",
        "adapter_version": "1",
    }
    return BetfairMarketCommissionReceipt(
        venue_id="betfair",
        account_id=ACCOUNT_ID,
        adapter_id="betfair-exchange-jsonrpc-readonly",
        adapter_version="1",
        market_id=MARKET,
        commission=Decimal("2"),
        profit=Decimal(market_profit),
        currency="EUR",
        settled_at=datetime(2026, 9, 20, 23, tzinfo=UTC),
        observed_at=datetime(2026, 9, 21, 1, tzinfo=UTC),
        available_at=datetime(2026, 9, 21, 1, tzinfo=UTC),
        account_details_sha256=ACCOUNT_SHA,
        cleared_orders_sha256="b" * 64,
        request_scope_sha256=population._digest(request_scope),
    )


def _settled_row(*, profit: str) -> BetfairClearedOrderObservation:
    return BetfairClearedOrderObservation(
        bet_id="bet-1",
        market_id=MARKET,
        selection_id=7,
        side="BACK",
        bet_status="SETTLED",
        placed_date="2026-09-20T10:00:00+00:00",
        settled_date="2026-09-20T20:00:00+00:00",
        price_requested=Decimal("2.5"),
        price_matched=Decimal("2.5"),
        size_settled=Decimal("2"),
        profit=Decimal(profit),
        customer_order_ref=None,
        customer_strategy_ref=None,
        evidence=_evidence("c"),
        event_id="event-1",
    )


def _bind_static_receipt(monkeypatch, source, receipt) -> None:
    monkeypatch.setattr(population, "_now_utc", lambda: NOW)

    def resolve(bound_source, *, receipt_id, record_sha256, as_of):
        assert bound_source is source
        assert receipt_id == receipt.receipt_id
        assert record_sha256 == receipt.record_sha256
        assert as_of == NOW
        return receipt, source._client

    monkeypatch.setattr(
        population._commission_origin,
        "resolve_bound_receipt",
        resolve,
    )


def _bind_stable_bet_population(monkeypatch, *, settled_profit: str) -> None:
    def read_page(**kwargs):
        status = kwargs["bet_status"]
        rows = (_settled_row(profit=settled_profit),) if status == "SETTLED" else ()
        return BetfairClearedOrderPage(
            orders=rows,
            more_available=False,
            from_record=kwargs["from_record"],
            record_count=kwargs["record_count"],
            evidence=_evidence("d"),
        )

    monkeypatch.setattr(population, "_read_page", read_page)


def _bind_current_market_rollup(source, monkeypatch, *, market_profit: str) -> None:
    def details():
        return BetfairAccountDetailsObservation(
            "EUR",
            "en",
            "GBR",
            "Europe/London",
            _evidence("a"),
        )

    def rpc(method, params):
        assert params["groupBy"] == "MARKET"
        return SimpleNamespace(
            result={
                "clearedOrders": [
                    {
                        "marketId": MARKET,
                        "settledDate": "2026-09-20T23:00:00.000Z",
                        "commission": Decimal("2"),
                        "profit": Decimal(market_profit),
                    }
                ],
                "moreAvailable": False,
            },
            evidence=_evidence("e"),
        )

    monkeypatch.setattr(source._client, "read_account_details", details)
    monkeypatch.setattr(source._client, "_rpc", rpc)


def _capture(source, receipt):
    return population.BetfairClearedMarketPopulationAuthority(source).capture(
        commission_receipt_id=receipt.receipt_id,
        commission_record_sha256=receipt.record_sha256,
        settled_from=T0,
        settled_to=T1,
    )


def test_complete_bet_population_must_conserve_target_market_profit(
    monkeypatch, tmp_path
) -> None:
    """A complete BET denominator cannot be paired with incompatible MARKET gross."""

    source = _source(tmp_path)
    receipt = _receipt(market_profit="20")
    _bind_static_receipt(monkeypatch, source, receipt)
    _bind_stable_bet_population(monkeypatch, settled_profit="15")
    _bind_current_market_rollup(source, monkeypatch, market_profit="20")

    with pytest.raises(
        population.BetfairClearedMarketPopulationError,
        match="profit|conserv",
    ):
        _capture(source, receipt)


def test_static_receipt_reresolution_is_not_provider_market_revision_revalidation(
    monkeypatch, tmp_path
) -> None:
    """A provider correction after the receipt must be observed before positive issue."""

    source = _source(tmp_path)
    receipt = _receipt(market_profit="20")
    _bind_static_receipt(monkeypatch, source, receipt)
    _bind_stable_bet_population(monkeypatch, settled_profit="20")

    # The canonical durable receipt remains R1, but a fresh provider MARKET read
    # would now show R2. Re-resolving R1 in memory is therefore insufficient.
    _bind_current_market_rollup(source, monkeypatch, market_profit="21")

    with pytest.raises(
        population.BetfairClearedMarketPopulationError,
        match="MARKET|commission|revision|changed",
    ):
        _capture(source, receipt)
