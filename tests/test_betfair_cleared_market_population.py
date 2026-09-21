from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import json

import pytest

from autosport import betfair_cleared_market_population as population
from autosport.betfair_account_readonly import (
    BetfairClearedOrderObservation,
    BetfairClearedOrderPage,
    BetfairEvidence,
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)
from autosport.betfair_market_commission_authority import (
    BetfairMarketCommissionAuthority,
    BetfairMarketCommissionReceipt,
)


A = "a" * 64
B = "b" * 64
T0 = "2026-09-20T00:00:00.000Z"
T1 = "2026-09-21T00:00:00.000Z"
NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def _source(tmp_path) -> BetfairMarketCommissionAuthority:
    return BetfairMarketCommissionAuthority(
        tmp_path / "commission",
        BetfairSessionCredentials("app-key", "session-token"),
    )


def _receipt(*, settled_from=T0, settled_to=T1) -> BetfairMarketCommissionReceipt:
    date_range = {"from": settled_from, "to": settled_to}
    account_id = f"betfair-account-evidence:{A}"
    scope = {
        "method": "SportsAPING/v1.0/listClearedOrders",
        "betStatus": "SETTLED",
        "groupBy": "MARKET",
        "marketIds": ["1.234"],
        "settledDateRange": date_range,
        "fromRecord": 0,
        "recordCount": 1000,
        "venue_id": "betfair",
        "account_id": account_id,
        "adapter_id": "betfair-exchange-jsonrpc-readonly",
        "adapter_version": "1",
    }
    return BetfairMarketCommissionReceipt(
        venue_id="betfair",
        account_id=account_id,
        adapter_id="betfair-exchange-jsonrpc-readonly",
        adapter_version="1",
        market_id="1.234",
        commission=Decimal("2"),
        profit=Decimal("20"),
        currency="EUR",
        settled_at=datetime(2026, 9, 20, 23, tzinfo=timezone.utc),
        observed_at=datetime(2026, 9, 21, 1, tzinfo=timezone.utc),
        available_at=datetime(2026, 9, 21, 1, tzinfo=timezone.utc),
        account_details_sha256=A,
        cleared_orders_sha256=B,
        request_scope_sha256=population._digest(scope),
    )


def _row(
    bet_id: str,
    *,
    status: str = "SETTLED",
    profit: str = "5",
    size: str = "2",
    market_id: str = "1.234",
) -> BetfairClearedOrderObservation:
    return BetfairClearedOrderObservation(
        bet_id=bet_id,
        market_id=market_id,
        selection_id=7,
        side="BACK",
        bet_status=status,
        placed_date="2026-09-20T10:00:00+00:00",
        settled_date="2026-09-20T20:00:00+00:00",
        price_requested=Decimal("2.5"),
        price_matched=Decimal("2.5"),
        size_settled=Decimal(size),
        profit=Decimal(profit),
        customer_order_ref=None,
        customer_strategy_ref=None,
        evidence=BetfairEvidence("2026-09-21T10:00:00+00:00", A),
        event_id="event-1",
    )


def _page(
    rows,
    *,
    status: str,
    offset: int = 0,
    more: bool = False,
    page_size: int = 1000,
    marker: int = 0,
) -> BetfairClearedOrderPage:
    normalized = tuple(
        row if row.bet_status == status else replace(row, bet_status=status)
        for row in rows
    )
    return BetfairClearedOrderPage(
        orders=normalized,
        more_available=more,
        from_record=offset,
        record_count=page_size,
        evidence=BetfairEvidence(
            f"2026-09-21T10:{marker:02d}:00+00:00",
            f"{marker + 1:064x}"[-64:],
        ),
    )


def _bind_source(monkeypatch, source, receipt):
    monkeypatch.setattr(population, "_now_utc", lambda: NOW)

    def resolve(bound_source, *, receipt_id, record_sha256, as_of):
        assert bound_source is source
        assert receipt_id == receipt.receipt_id
        assert record_sha256 == receipt.record_sha256
        assert as_of == NOW
        return receipt, source._client

    monkeypatch.setattr(population._commission_origin, "resolve_bound_receipt", resolve)


def _capture(authority, receipt, **kwargs):
    return authority.capture(
        commission_receipt_id=receipt.receipt_id,
        commission_record_sha256=receipt.record_sha256,
        settled_from=T0,
        settled_to=T1,
        **kwargs,
    )


def test_complete_two_pass_population_keeps_external_bet_and_truth_narrow(
    tmp_path, monkeypatch
) -> None:
    source = _source(tmp_path)
    receipt = _receipt()
    _bind_source(monkeypatch, source, receipt)
    calls = []

    def read_page(**kwargs):
        calls.append(kwargs)
        status = kwargs["bet_status"]
        offset = kwargs["from_record"]
        if status == "SETTLED":
            if offset == 0:
                return _page(
                    [_row("autosport-a"), _row("manual-c")],
                    status=status,
                    offset=0,
                    more=True,
                    marker=len(calls),
                )
            return _page(
                [_row("autosport-b")],
                status=status,
                offset=2,
                marker=len(calls),
            )
        return _page([], status=status, offset=0, marker=len(calls))

    monkeypatch.setattr(population, "_read_page", read_page)
    value = _capture(population.BetfairClearedMarketPopulationAuthority(source), receipt)

    assert value.bet_ids == ("autosport-a", "autosport-b", "manual-c")
    assert value.bounded_revalidation_proven is True
    assert value.cross_call_atomicity_proven is False
    assert value.permanent_finality_proven is False
    assert value.grants_execution_authority is False
    assert value.statuses == ("SETTLED", "VOIDED", "LAPSED", "CANCELLED")
    assert all(call["market_id"] == "1.234" for call in calls)
    population.assert_betfair_cleared_market_population_authoritative(value)


def test_second_pass_change_fails_closed(tmp_path, monkeypatch) -> None:
    source = _source(tmp_path)
    receipt = _receipt()
    _bind_source(monkeypatch, source, receipt)
    calls = 0

    def read_page(**kwargs):
        nonlocal calls
        calls += 1
        status = kwargs["bet_status"]
        if status == "SETTLED":
            profit = "5" if calls <= 4 else "6"
            return _page([_row("a", profit=profit)], status=status, marker=calls)
        return _page([], status=status, marker=calls)

    monkeypatch.setattr(population, "_read_page", read_page)
    with pytest.raises(population.BetfairClearedMarketPopulationError, match="changed"):
        _capture(population.BetfairClearedMarketPopulationAuthority(source), receipt)


def test_more_available_empty_page_is_not_complete(tmp_path, monkeypatch) -> None:
    source = _source(tmp_path)
    receipt = _receipt()
    _bind_source(monkeypatch, source, receipt)
    monkeypatch.setattr(
        population,
        "_read_page",
        lambda **kwargs: _page(
            [],
            status=kwargs["bet_status"],
            offset=kwargs["from_record"],
            more=True,
            marker=1,
        ),
    )
    with pytest.raises(population.BetfairClearedMarketPopulationError, match="empty page"):
        _capture(population.BetfairClearedMarketPopulationAuthority(source), receipt)


def test_page_budget_exhaustion_never_mints_completeness(tmp_path, monkeypatch) -> None:
    source = _source(tmp_path)
    receipt = _receipt()
    _bind_source(monkeypatch, source, receipt)

    def read_page(**kwargs):
        offset = kwargs["from_record"]
        return _page(
            [_row(f"bet-{offset}")],
            status=kwargs["bet_status"],
            offset=offset,
            more=True,
            page_size=1,
            marker=offset + 1,
        )

    monkeypatch.setattr(population, "_read_page", read_page)
    with pytest.raises(population.BetfairClearedMarketPopulationError, match="max_pages"):
        _capture(
            population.BetfairClearedMarketPopulationAuthority(source),
            receipt,
            page_size=1,
            max_pages_per_status=2,
        )


def test_duplicate_bet_across_pages_is_rejected(tmp_path, monkeypatch) -> None:
    source = _source(tmp_path)
    receipt = _receipt()
    _bind_source(monkeypatch, source, receipt)

    def read_page(**kwargs):
        status = kwargs["bet_status"]
        offset = kwargs["from_record"]
        if status == "SETTLED" and offset == 0:
            return _page(
                [_row("dup")],
                status=status,
                offset=0,
                more=True,
                page_size=1,
                marker=1,
            )
        if status == "SETTLED":
            return _page(
                [_row("dup")],
                status=status,
                offset=1,
                page_size=1,
                marker=2,
            )
        return _page([], status=status, offset=0, page_size=1, marker=3)

    monkeypatch.setattr(population, "_read_page", read_page)
    with pytest.raises(population.BetfairClearedMarketPopulationError, match="repeated"):
        _capture(
            population.BetfairClearedMarketPopulationAuthority(source),
            receipt,
            page_size=1,
        )


def test_zero_residual_terminal_row_can_coexist_with_one_economic_row(
    tmp_path, monkeypatch
) -> None:
    source = _source(tmp_path)
    receipt = _receipt()
    _bind_source(monkeypatch, source, receipt)
    calls = 0

    def read_page(**kwargs):
        nonlocal calls
        calls += 1
        status = kwargs["bet_status"]
        if status == "SETTLED":
            rows = [_row("same", status=status, profit="5", size="2")]
        elif status == "CANCELLED":
            rows = [_row("same", status=status, profit="0", size="0")]
        else:
            rows = []
        return _page(rows, status=status, marker=calls)

    monkeypatch.setattr(population, "_read_page", read_page)
    value = _capture(population.BetfairClearedMarketPopulationAuthority(source), receipt)
    assert len([row for row in value.rows if row.bet_id == "same"]) == 2
    population.assert_betfair_cleared_market_population_authoritative(value)


def test_two_economic_terminal_rows_for_same_bet_fail_closed(tmp_path, monkeypatch) -> None:
    source = _source(tmp_path)
    receipt = _receipt()
    _bind_source(monkeypatch, source, receipt)

    def read_page(**kwargs):
        status = kwargs["bet_status"]
        rows = []
        if status in {"SETTLED", "VOIDED"}:
            rows = [_row("same", status=status, profit="5", size="2")]
        return _page(rows, status=status, marker=1)

    monkeypatch.setattr(population, "_read_page", read_page)
    with pytest.raises(population.BetfairClearedMarketPopulationError, match="incompatible"):
        _capture(population.BetfairClearedMarketPopulationAuthority(source), receipt)


def test_caller_copy_cannot_mint_source_authority(tmp_path, monkeypatch) -> None:
    source = _source(tmp_path)
    receipt = _receipt()
    _bind_source(monkeypatch, source, receipt)
    monkeypatch.setattr(
        population,
        "_read_page",
        lambda **kwargs: _page([], status=kwargs["bet_status"], marker=1),
    )
    value = _capture(population.BetfairClearedMarketPopulationAuthority(source), receipt)
    forged = replace(value)
    with pytest.raises(population.BetfairClearedMarketPopulationError, match="not issued"):
        population.assert_betfair_cleared_market_population_authoritative(forged)


def test_mutated_record_identity_is_detected(tmp_path, monkeypatch) -> None:
    source = _source(tmp_path)
    receipt = _receipt()
    _bind_source(monkeypatch, source, receipt)
    monkeypatch.setattr(
        population,
        "_read_page",
        lambda **kwargs: _page([], status=kwargs["bet_status"], marker=1),
    )
    value = _capture(population.BetfairClearedMarketPopulationAuthority(source), receipt)
    object.__setattr__(value, "commission_record_sha256", "f" * 64)
    with pytest.raises(population.BetfairClearedMarketPopulationError, match="record identity"):
        population.assert_betfair_cleared_market_population_authoritative(value)


def test_wrong_market_row_is_rejected(tmp_path, monkeypatch) -> None:
    source = _source(tmp_path)
    receipt = _receipt()
    _bind_source(monkeypatch, source, receipt)

    def read_page(**kwargs):
        status = kwargs["bet_status"]
        rows = [_row("wrong", market_id="9.999")] if status == "SETTLED" else []
        return _page(rows, status=status, marker=1)

    monkeypatch.setattr(population, "_read_page", read_page)
    with pytest.raises(population.BetfairClearedMarketPopulationError, match="different market"):
        _capture(population.BetfairClearedMarketPopulationAuthority(source), receipt)


def test_revalidation_is_semantic_not_raw_page_partition_identity(
    tmp_path, monkeypatch
) -> None:
    source = _source(tmp_path)
    receipt = _receipt()
    _bind_source(monkeypatch, source, receipt)
    settled_calls = 0

    def read_page(**kwargs):
        nonlocal settled_calls
        status = kwargs["bet_status"]
        offset = kwargs["from_record"]
        if status != "SETTLED":
            return _page([], status=status, marker=8)
        settled_calls += 1
        if settled_calls == 1:
            return _page(
                [_row("a")],
                status=status,
                offset=0,
                more=True,
                page_size=2,
                marker=1,
            )
        if settled_calls == 2:
            return _page(
                [_row("b")],
                status=status,
                offset=1,
                page_size=2,
                marker=2,
            )
        return _page(
            [_row("b"), _row("a")],
            status=status,
            offset=0,
            page_size=2,
            marker=9,
        )

    monkeypatch.setattr(population, "_read_page", read_page)
    value = _capture(
        population.BetfairClearedMarketPopulationAuthority(source),
        receipt,
        page_size=2,
    )
    assert value.bet_ids == ("a", "b")


def test_settlement_range_must_match_commission_receipt(tmp_path, monkeypatch) -> None:
    source = _source(tmp_path)
    receipt = _receipt()
    _bind_source(monkeypatch, source, receipt)
    with pytest.raises(population.BetfairClearedMarketPopulationError, match="settlement range"):
        population.BetfairClearedMarketPopulationAuthority(source).capture(
            commission_receipt_id=receipt.receipt_id,
            commission_record_sha256=receipt.record_sha256,
            settled_from=T0,
            settled_to="2026-09-22T00:00:00.000Z",
        )


def test_fixed_page_read_has_no_local_ownership_filter() -> None:
    captured = []

    class Transport:
        def post(self, url, *, headers, body, timeout_seconds):
            request = json.loads(body.decode("utf-8"))
            captured.append(request)
            return json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {"clearedOrders": [], "moreAvailable": False},
                }
            ).encode("utf-8")

    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app", "token"),
        transport=Transport(),
        clock=lambda: NOW,
    )
    page = population._read_page(
        client=client,
        market_id="1.234",
        bet_status="SETTLED",
        date_range={"from": T0, "to": T1},
        from_record=0,
        record_count=1000,
    )
    assert page.more_available is False
    params = captured[0]["params"]
    assert params == {
        "betStatus": "SETTLED",
        "groupBy": "BET",
        "marketIds": ["1.234"],
        "fromRecord": 0,
        "recordCount": 1000,
        "settledDateRange": {"from": T0, "to": T1},
    }
    assert "customerOrderRefs" not in params
    assert "customerStrategyRefs" not in params
    assert "betIds" not in params
