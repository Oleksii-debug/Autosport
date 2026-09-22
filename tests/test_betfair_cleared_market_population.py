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


def _receipt(
    *,
    settled_from=T0,
    settled_to=T1,
    profit: str = "0",
    commission: str = "2",
) -> BetfairMarketCommissionReceipt:
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
        commission=Decimal(commission),
        profit=Decimal(profit),
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


def _bind_source(
    monkeypatch,
    source,
    receipt,
    *,
    market_bet_count: int = 0,
    market_profit: str | None = None,
    market_commission: str | None = None,
):
    monkeypatch.setattr(population, "_now_utc", lambda: NOW)

    def resolve(bound_source, *, receipt_id, record_sha256, as_of):
        assert bound_source is source
        assert receipt_id == receipt.receipt_id
        assert record_sha256 == receipt.record_sha256
        assert as_of == NOW
        return receipt, source._client

    monkeypatch.setattr(population._commission_origin, "resolve_bound_receipt", resolve)

    def read_market_rollup(**kwargs):
        assert kwargs["client"] is source._client
        assert kwargs["market_id"] == receipt.market_id
        return population.ClearedMarketRollupWitness(
            bet_count=market_bet_count,
            profit=Decimal(
                market_profit
                if market_profit is not None
                else str(receipt.profit)
            ),
            commission=Decimal(
                market_commission
                if market_commission is not None
                else str(receipt.commission)
            ),
            settled_date=receipt.settled_at.isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z"),
            response_sha256="c" * 64,
            observed_at="2026-09-21T11:00:00+00:00",
        )

    monkeypatch.setattr(population, "_read_market_rollup", read_market_rollup)


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
    receipt = _receipt(profit="15")
    _bind_source(monkeypatch, source, receipt, market_bet_count=3)
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
    assert value.economic_scope_coextensive_proven is True
    assert value.market_rollup_witness.bet_count == 3
    assert value.cross_call_atomicity_proven is False
    assert value.permanent_finality_proven is False
    assert value.grants_execution_authority is False
    assert value.statuses == ("SETTLED", "VOIDED", "LAPSED", "CANCELLED")
    assert all(call["market_id"] == "1.234" for call in calls)
    population.assert_betfair_cleared_market_population_authoritative(value)


def test_second_pass_change_fails_closed(tmp_path, monkeypatch) -> None:
    source = _source(tmp_path)
    receipt = _receipt(profit="5")
    _bind_source(monkeypatch, source, receipt, market_bet_count=1)
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
    receipt = _receipt(profit="5")
    _bind_source(monkeypatch, source, receipt, market_bet_count=1)
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
    receipt = _receipt(profit="10")
    _bind_source(monkeypatch, source, receipt, market_bet_count=2)
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


def test_fresh_market_witness_cannot_backdate_completed_bet_passes(
    tmp_path, monkeypatch
) -> None:
    source = _source(tmp_path)
    receipt = _receipt(profit="5")
    _bind_source(monkeypatch, source, receipt, market_bet_count=1)

    def read_page(**kwargs):
        status = kwargs["bet_status"]
        rows = [_row("bet-1", profit="5")] if status == "SETTLED" else []
        return _page(rows, status=status, marker=10)

    monkeypatch.setattr(population, "_read_page", read_page)
    monkeypatch.setattr(
        population,
        "_read_market_rollup",
        lambda **kwargs: population.ClearedMarketRollupWitness(
            bet_count=1,
            profit=receipt.profit,
            commission=receipt.commission,
            settled_date=receipt.settled_at.isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z"),
            response_sha256="d" * 64,
            observed_at="2026-09-21T10:09:59+00:00",
        ),
    )

    with pytest.raises(
        population.BetfairClearedMarketPopulationError,
        match="predate|causally",
    ):
        _capture(population.BetfairClearedMarketPopulationAuthority(source), receipt)


def test_market_evidence_instance_does_not_change_semantic_population_identity(
    tmp_path, monkeypatch
) -> None:
    source = _source(tmp_path)
    receipt = _receipt(profit="5")
    _bind_source(monkeypatch, source, receipt, market_bet_count=1)

    def read_page(**kwargs):
        status = kwargs["bet_status"]
        rows = [_row("bet-1", profit="5")] if status == "SETTLED" else []
        return _page(rows, status=status, marker=1)

    monkeypatch.setattr(population, "_read_page", read_page)
    evidence_counter = 0

    def read_market_rollup(**kwargs):
        nonlocal evidence_counter
        evidence_counter += 1
        return population.ClearedMarketRollupWitness(
            bet_count=1,
            profit=Decimal("5"),
            commission=receipt.commission,
            settled_date=receipt.settled_at.isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z"),
            response_sha256=f"{evidence_counter:064x}",
            observed_at=f"2026-09-21T11:0{evidence_counter}:00+00:00",
        )

    monkeypatch.setattr(population, "_read_market_rollup", read_market_rollup)
    authority = population.BetfairClearedMarketPopulationAuthority(source)
    first = _capture(authority, receipt)
    second = _capture(authority, receipt)

    assert first.population_sha256 == second.population_sha256
    assert first.evidence_sha256 != second.evidence_sha256


def test_market_bet_count_rejects_range_subset_even_when_profit_matches(
    tmp_path, monkeypatch
) -> None:
    source = _source(tmp_path)
    receipt = _receipt(profit="5")
    _bind_source(
        monkeypatch,
        source,
        receipt,
        market_bet_count=2,
        market_profit="5",
    )

    def read_page(**kwargs):
        status = kwargs["bet_status"]
        rows = [_row("inside", profit="5")] if status == "SETTLED" else []
        return _page(rows, status=status, marker=1)

    monkeypatch.setattr(population, "_read_page", read_page)
    with pytest.raises(
        population.BetfairClearedMarketPopulationError,
        match="betCount|coextensive",
    ):
        _capture(population.BetfairClearedMarketPopulationAuthority(source), receipt)


def test_complete_settled_bets_must_conserve_fresh_market_profit(
    tmp_path, monkeypatch
) -> None:
    source = _source(tmp_path)
    receipt = _receipt(profit="20")
    _bind_source(monkeypatch, source, receipt, market_bet_count=1)

    def read_page(**kwargs):
        status = kwargs["bet_status"]
        rows = [_row("bet-1", profit="15")] if status == "SETTLED" else []
        return _page(rows, status=status, marker=1)

    monkeypatch.setattr(population, "_read_page", read_page)
    with pytest.raises(
        population.BetfairClearedMarketPopulationError,
        match="profit|conserve",
    ):
        _capture(population.BetfairClearedMarketPopulationAuthority(source), receipt)


def test_fresh_market_revision_change_fails_closed(
    tmp_path, monkeypatch
) -> None:
    source = _source(tmp_path)
    receipt = _receipt(profit="20")
    _bind_source(
        monkeypatch,
        source,
        receipt,
        market_bet_count=1,
        market_profit="21",
    )

    def read_page(**kwargs):
        status = kwargs["bet_status"]
        rows = [_row("bet-1", profit="20")] if status == "SETTLED" else []
        return _page(rows, status=status, marker=1)

    monkeypatch.setattr(population, "_read_page", read_page)
    with pytest.raises(
        population.BetfairClearedMarketPopulationError,
        match="revision changed",
    ):
        _capture(population.BetfairClearedMarketPopulationAuthority(source), receipt)


def test_gross_conservation_is_independent_of_ambient_decimal_context(
    tmp_path, monkeypatch
) -> None:
    from decimal import localcontext

    source = _source(tmp_path)
    receipt = _receipt(profit="1")
    _bind_source(monkeypatch, source, receipt, market_bet_count=10)

    rows = [_row(f"bet-{index}", profit="0.1") for index in range(10)]

    def read_page(**kwargs):
        status = kwargs["bet_status"]
        return _page(rows if status == "SETTLED" else [], status=status, marker=1)

    monkeypatch.setattr(population, "_read_page", read_page)
    with localcontext() as context:
        context.prec = 2
        value = _capture(
            population.BetfairClearedMarketPopulationAuthority(source),
            receipt,
        )
    population.assert_betfair_cleared_market_population_authoritative(value)


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


def test_fresh_market_rollup_uses_exact_market_scope_and_reads_bet_count() -> None:
    captured = []

    class Transport:
        def post(self, url, *, headers, body, timeout_seconds):
            request = json.loads(body.decode("utf-8"))
            captured.append(request)
            return json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {
                        "clearedOrders": [
                            {
                                "marketId": "1.234",
                                "betCount": 2,
                                "settledDate": "2026-09-20T23:00:00.000Z",
                                "commission": 2,
                                "profit": 7,
                            }
                        ],
                        "moreAvailable": False,
                    },
                }
            ).encode("utf-8")

    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app", "token"),
        transport=Transport(),
        clock=lambda: NOW,
    )
    witness = population._read_market_rollup(
        client=client,
        market_id="1.234",
        date_range={"from": T0, "to": T1},
    )
    assert witness.bet_count == 2
    assert witness.profit == Decimal("7")
    assert witness.commission == Decimal("2")
    assert captured[0]["params"] == {
        "betStatus": "SETTLED",
        "groupBy": "MARKET",
        "marketIds": ["1.234"],
        "fromRecord": 0,
        "recordCount": 1000,
        "settledDateRange": {"from": T0, "to": T1},
    }


def test_durable_reopen_preserves_evidence_identity_without_minting_source_authority(
    tmp_path, monkeypatch
) -> None:
    source = _source(tmp_path)
    receipt = _receipt()
    _bind_source(monkeypatch, source, receipt)
    monkeypatch.setattr(
        population,
        "_read_page",
        lambda **kwargs: _page([], status=kwargs["bet_status"], marker=1),
    )
    value = _capture(population.BetfairClearedMarketPopulationAuthority(source), receipt)

    reopened = population.BetfairClearedMarketPopulation.from_dict(
        json.loads(json.dumps(value.to_dict()))
    )

    assert reopened.evidence_sha256 == value.evidence_sha256
    assert reopened.population_sha256 == value.population_sha256
    assert reopened.request_scope_sha256 == value.request_scope_sha256
    reopened.assert_integrity()
    with pytest.raises(population.BetfairClearedMarketPopulationError, match="not issued"):
        population.assert_betfair_cleared_market_population_authoritative(reopened)


def test_durable_page_substitution_breaks_population_integrity(
    tmp_path, monkeypatch
) -> None:
    source = _source(tmp_path)
    receipt = _receipt()
    _bind_source(monkeypatch, source, receipt)
    monkeypatch.setattr(
        population,
        "_read_page",
        lambda **kwargs: _page([], status=kwargs["bet_status"], marker=1),
    )
    value = _capture(population.BetfairClearedMarketPopulationAuthority(source), receipt)
    raw = json.loads(json.dumps(value.to_dict()))
    raw["first_pass_pages"][0]["response_sha256"] = "f" * 64

    with pytest.raises(
        population.BetfairClearedMarketPopulationError,
        match="evidence digest mismatch",
    ):
        population.BetfairClearedMarketPopulation.from_dict(raw)


def test_durable_market_rollup_tamper_breaks_population_integrity(
    tmp_path, monkeypatch
) -> None:
    source = _source(tmp_path)
    receipt = _receipt()
    _bind_source(monkeypatch, source, receipt)
    monkeypatch.setattr(
        population,
        "_read_page",
        lambda **kwargs: _page([], status=kwargs["bet_status"], marker=1),
    )
    value = _capture(population.BetfairClearedMarketPopulationAuthority(source), receipt)
    raw = json.loads(json.dumps(value.to_dict()))
    raw["market_rollup_witness"]["bet_count"] = 1

    with pytest.raises(
        population.BetfairClearedMarketPopulationError,
        match="betCount|coextensive|digest",
    ):
        population.BetfairClearedMarketPopulation.from_dict(raw)


def test_durable_population_missing_field_is_rejected(tmp_path, monkeypatch) -> None:
    source = _source(tmp_path)
    receipt = _receipt()
    _bind_source(monkeypatch, source, receipt)
    monkeypatch.setattr(
        population,
        "_read_page",
        lambda **kwargs: _page([], status=kwargs["bet_status"], marker=1),
    )
    value = _capture(population.BetfairClearedMarketPopulationAuthority(source), receipt)
    raw = json.loads(json.dumps(value.to_dict()))
    del raw["population_sha256"]

    with pytest.raises(
        population.BetfairClearedMarketPopulationError,
        match="unexpected fields",
    ):
        population.BetfairClearedMarketPopulation.from_dict(raw)
