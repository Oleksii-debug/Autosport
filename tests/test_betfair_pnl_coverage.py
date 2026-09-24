from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json

import pytest

from autosport.betfair_account_readonly import (
    BETTING_JSON_RPC_ENDPOINT,
    BetfairClearedMarketPnlCoveragePage,
    BetfairEvidence,
    BetfairMarketPnlCoverageBatch,
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
    BetfairSessionCredentials,
)
from autosport.betfair_pnl_coverage import (
    BetfairPnlCoverageError,
    MarketDescriptor,
    MarketStatus,
    ScopeIdentity,
    assert_complete,
    build_coverage_witness,
    build_open_market_batches,
    collect_coverage_witness,
    verify_coverage_witness,
)


FIXED_NOW = datetime(2026, 9, 22, 14, 0, tzinfo=timezone.utc)
EVIDENCE = BetfairEvidence(FIXED_NOW.isoformat(), "a" * 64)


class FakeTransport:
    def __init__(self, responses: list[bytes]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def post(
        self,
        url: str,
        *,
        headers,
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout_seconds": timeout_seconds,
            }
        )
        if not self.responses:
            raise AssertionError("unexpected transport call")
        return self.responses.pop(0)


def response(result: object, request_id: int) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "result": result, "id": request_id},
        separators=(",", ":"),
    ).encode("utf-8")


def client_for(*responses: bytes) -> tuple[BetfairReadOnlyClient, FakeTransport]:
    transport = FakeTransport(list(responses))
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=transport,
        clock=lambda: FIXED_NOW,
        venue_id="betfair",
        account_id="acct-a",
    )
    return client, transport


def scope(**overrides: object) -> ScopeIdentity:
    values: dict[str, object] = {
        "provider": "BETFAIR",
        "account_id_hash": sha256(b"acct-a").hexdigest(),
        "evidence_not_before_utc": FIXED_NOW.isoformat(),
        "include_settled_bets": False,
        "include_bsp_bets": False,
        "net_of_commission": False,
    }
    values.update(overrides)
    return ScopeIdentity(**values)


def market(
    market_id: str,
    *,
    status: MarketStatus = MarketStatus.OPEN,
    betting_type: str = "ODDS",
) -> MarketDescriptor:
    return MarketDescriptor(market_id, status, betting_type)


def open_batch(
    requested: tuple[str, ...],
    *,
    returned: tuple[str, ...] | None = None,
    include_settled_bets: bool = False,
    include_bsp_bets: bool = False,
    net_of_commission: bool = False,
) -> BetfairMarketPnlCoverageBatch:
    returned_ids = requested if returned is None else returned
    client, _ = client_for(
        response([{"marketId": market_id} for market_id in returned_ids], 1)
    )
    return client.read_market_profit_and_loss_coverage(
        market_ids=requested,
        include_settled_bets=include_settled_bets,
        include_bsp_bets=include_bsp_bets,
        net_of_commission=net_of_commission,
    )


def closed_page(
    requested: tuple[str, ...],
    returned: tuple[str, ...],
    *,
    from_record: int = 0,
    record_count: int = 1000,
    more_available: bool = False,
) -> BetfairClearedMarketPnlCoveragePage:
    client, _ = client_for(
        response(
            {
                "clearedOrders": [
                    {"marketId": market_id}
                    for market_id in returned
                ],
                "moreAvailable": more_available,
            },
            1,
        )
    )
    return client.read_cleared_market_profit_and_loss_coverage_page(
        market_ids=requested,
        from_record=from_record,
        record_count=record_count,
    )


def test_market_profit_and_loss_coverage_uses_canonical_readonly_rpc_and_exact_flags() -> None:
    client, transport = client_for(
        response(
            [
                {"marketId": "1.100", "profitAndLosses": []},
                {"marketId": "1.200", "profitAndLosses": []},
            ],
            1,
        )
    )

    batch = client.read_market_profit_and_loss_coverage(
        market_ids=("1.100", "1.200"),
        include_settled_bets=True,
        include_bsp_bets=False,
        net_of_commission=True,
    )

    assert batch.requested_market_ids == ("1.100", "1.200")
    assert batch.returned_market_ids == ("1.100", "1.200")
    assert batch.include_settled_bets is True
    assert batch.include_bsp_bets is False
    assert batch.net_of_commission is True

    call = transport.calls[0]
    request = json.loads(call["body"])
    assert call["url"] == BETTING_JSON_RPC_ENDPOINT
    assert request["method"] == "SportsAPING/v1.0/listMarketProfitAndLoss"
    assert request["params"] == {
        "marketIds": ["1.100", "1.200"],
        "includeSettledBets": True,
        "includeBspBets": False,
        "netOfCommission": True,
    }


def test_market_profit_and_loss_silent_provider_omission_is_not_zero() -> None:
    client, _ = client_for(
        response([{"marketId": "1.100", "profitAndLosses": []}], 1)
    )

    with pytest.raises(BetfairReadOnlyError, match="exact requested market set"):
        client.read_market_profit_and_loss_coverage(
            market_ids=("1.100", "1.200"),
        )


def test_market_profit_and_loss_enforces_provider_weight_boundary_before_io() -> None:
    client, transport = client_for()
    market_ids = tuple(f"1.{index:03d}" for index in range(51))

    with pytest.raises(BetfairReadOnlyError, match="50 market IDs"):
        client.read_market_profit_and_loss_coverage(market_ids=market_ids)

    assert transport.calls == []


def test_cleared_market_rollup_accepts_documented_zero_record_count_and_uses_market_grouping() -> None:
    client, transport = client_for(
        response(
            {
                "clearedOrders": [{"marketId": "1.closed", "profit": 12.34}],
                "moreAvailable": False,
            },
            1,
        )
    )

    page = client.read_cleared_market_profit_and_loss_coverage_page(
        market_ids=("1.closed",),
        from_record=0,
        record_count=0,
    )

    assert page.requested_record_count == 0
    assert page.returned_market_ids == ("1.closed",)
    request = json.loads(transport.calls[0]["body"])
    assert request["method"] == "SportsAPING/v1.0/listClearedOrders"
    assert request["params"] == {
        "betStatus": "SETTLED",
        "groupBy": "MARKET",
        "marketIds": ["1.closed"],
        "fromRecord": 0,
        "recordCount": 0,
    }


def test_open_batching_is_deterministic_and_never_exceeds_fifty_markets() -> None:
    markets = tuple(market(f"1.{index:03d}") for index in range(51, 0, -1))

    batches = build_open_market_batches(markets)

    assert tuple(map(len, batches)) == (50, 1)
    assert batches[0] == tuple(f"1.{index:03d}" for index in range(1, 51))
    assert batches[1] == ("1.051",)


def test_complete_mixed_open_and_closed_scope_is_accepted() -> None:
    markets = (
        market("1.open"),
        market("1.closed", status=MarketStatus.CLOSED),
    )
    witness = build_coverage_witness(
        scope=scope(),
        markets=markets,
        open_batches=(open_batch(("1.open",)),),
        closed_pages=(closed_page(("1.closed",), ("1.closed",)),),
    )

    assert_complete(witness, scope=scope(), markets=markets)
    assert witness.unsupported_market_ids == ()
    assert witness.open_odds_market_ids == ("1.open",)
    assert witness.closed_market_ids == ("1.closed",)


def test_closed_non_odds_market_uses_cleared_orders_authority() -> None:
    markets = (
        market("1.closed-line", status=MarketStatus.CLOSED, betting_type="LINE"),
    )
    witness = build_coverage_witness(
        scope=scope(),
        markets=markets,
        open_batches=(),
        closed_pages=(
            closed_page(("1.closed-line",), ("1.closed-line",)),
        ),
    )

    assert_complete(witness, scope=scope(), markets=markets)
    assert witness.closed_market_ids == ("1.closed-line",)
    assert witness.unsupported_market_ids == ()


@pytest.mark.parametrize(
    ("status", "betting_type"),
    [
        (MarketStatus.OPEN, "LINE"),
        (MarketStatus.SUSPENDED, "ODDS"),
        (MarketStatus.INACTIVE, "ODDS"),
    ],
)
def test_non_authoritative_open_pnl_states_fail_closed(
    status: MarketStatus,
    betting_type: str,
) -> None:
    markets = (market("1.unsupported", status=status, betting_type=betting_type),)
    witness = build_coverage_witness(
        scope=scope(),
        markets=markets,
        open_batches=(),
        closed_pages=(),
    )

    assert witness.unsupported_market_ids == ("1.unsupported",)
    with pytest.raises(BetfairPnlCoverageError, match="unsupported markets"):
        assert_complete(witness, scope=scope(), markets=markets)


def test_closed_market_omission_is_not_interpreted_as_zero() -> None:
    markets = (
        market("1.closed-a", status=MarketStatus.CLOSED),
        market("1.closed-b", status=MarketStatus.CLOSED),
    )

    with pytest.raises(BetfairPnlCoverageError, match="omitted CLOSED markets"):
        build_coverage_witness(
            scope=scope(),
            markets=markets,
            open_batches=(),
            closed_pages=(
                closed_page(
                    ("1.closed-a", "1.closed-b"),
                    ("1.closed-a",),
                ),
            ),
        )


def test_closed_pagination_gap_is_rejected() -> None:
    markets = (
        market("1.closed-a", status=MarketStatus.CLOSED),
        market("1.closed-b", status=MarketStatus.CLOSED),
    )
    requested = ("1.closed-a", "1.closed-b")

    with pytest.raises(BetfairPnlCoverageError, match="pagination gap/overlap"):
        build_coverage_witness(
            scope=scope(),
            markets=markets,
            open_batches=(),
            closed_pages=(
                closed_page(
                    requested,
                    ("1.closed-a",),
                    from_record=0,
                    record_count=1,
                    more_available=True,
                ),
                closed_page(
                    requested,
                    ("1.closed-b",),
                    from_record=2,
                    record_count=1,
                ),
            ),
        )


def test_closed_cross_page_duplicate_market_rollup_is_rejected() -> None:
    markets = (
        market("1.closed-a", status=MarketStatus.CLOSED),
        market("1.closed-b", status=MarketStatus.CLOSED),
    )
    requested = ("1.closed-a", "1.closed-b")

    with pytest.raises(BetfairPnlCoverageError, match="duplicate MARKET roll-up"):
        build_coverage_witness(
            scope=scope(),
            markets=markets,
            open_batches=(),
            closed_pages=(
                closed_page(
                    requested,
                    ("1.closed-a",),
                    from_record=0,
                    record_count=1,
                    more_available=True,
                ),
                closed_page(
                    requested,
                    ("1.closed-a",),
                    from_record=1,
                    record_count=1,
                ),
            ),
        )


def test_open_pnl_view_flags_are_bound_to_scope() -> None:
    markets = (market("1.open"),)

    with pytest.raises(BetfairPnlCoverageError, match="P&L view flags mismatch"):
        build_coverage_witness(
            scope=scope(net_of_commission=False),
            markets=markets,
            open_batches=(
                open_batch(("1.open",), net_of_commission=True),
            ),
            closed_pages=(),
        )


def test_directly_constructed_dto_cannot_mint_provider_authority() -> None:
    markets = (market("1.open"),)
    forged = BetfairMarketPnlCoverageBatch(
        sha256(b"acct-a").hexdigest(),
        ("1.open",),
        ("1.open",),
        False,
        False,
        False,
        EVIDENCE,
    )

    with pytest.raises(BetfairPnlCoverageError, match="not issued by canonical"):
        build_coverage_witness(
            scope=scope(),
            markets=markets,
            open_batches=(forged,),
            closed_pages=(),
        )

    closed_markets = (market("1.closed", status=MarketStatus.CLOSED),)
    forged_closed = BetfairClearedMarketPnlCoveragePage(
        sha256(b"acct-a").hexdigest(),
        ("1.closed",),
        ("1.closed",),
        0,
        1000,
        False,
        EVIDENCE,
    )
    with pytest.raises(BetfairPnlCoverageError, match="not issued by canonical"):
        build_coverage_witness(
            scope=scope(),
            markets=closed_markets,
            open_batches=(),
            closed_pages=(forged_closed,),
        )


def test_persisted_forged_summary_or_digest_cannot_mint_completeness() -> None:
    markets = (market("1.open"),)
    witness = build_coverage_witness(
        scope=scope(),
        markets=markets,
        open_batches=(open_batch(("1.open",)),),
        closed_pages=(),
    )

    forged_digest = replace(witness, evidence_digest="f" * 64)
    with pytest.raises(BetfairPnlCoverageError, match="canonical rebuilt proof"):
        verify_coverage_witness(
            witness=forged_digest,
            scope=scope(),
            markets=markets,
        )

    forged_summary = replace(witness, open_odds_market_ids=())
    with pytest.raises(BetfairPnlCoverageError, match="canonical rebuilt proof"):
        assert_complete(forged_summary, scope=scope(), markets=markets)


def test_provider_issued_batch_cannot_be_rebound_to_another_account() -> None:
    markets = (market("1.open"),)
    batch = open_batch(("1.open",))

    with pytest.raises(BetfairPnlCoverageError, match="account identity mismatch"):
        build_coverage_witness(
            scope=scope(account_id_hash=sha256(b"acct-b").hexdigest()),
            markets=markets,
            open_batches=(batch,),
            closed_pages=(),
        )


def test_witness_cannot_be_replayed_into_another_account_scope() -> None:
    markets = (market("1.open"),)
    original_scope = scope(account_id_hash=sha256(b"acct-a").hexdigest())
    witness = build_coverage_witness(
        scope=original_scope,
        markets=markets,
        open_batches=(open_batch(("1.open",)),),
        closed_pages=(),
    )

    with pytest.raises(BetfairPnlCoverageError, match="account identity mismatch"):
        verify_coverage_witness(
            witness=witness,
            scope=scope(account_id_hash=sha256(b"acct-b").hexdigest()),
            markets=markets,
        )


def test_old_provider_capture_cannot_be_rebound_to_a_new_freshness_fence() -> None:
    markets = (market("1.open"),)
    batch = open_batch(("1.open",))
    later_scope = scope(
        evidence_not_before_utc=(FIXED_NOW + timedelta(seconds=1)).isoformat()
    )

    with pytest.raises(BetfairPnlCoverageError, match="freshness fence"):
        build_coverage_witness(
            scope=later_scope,
            markets=markets,
            open_batches=(batch,),
            closed_pages=(),
        )


def test_collect_coverage_witness_composes_both_provider_authorities_end_to_end() -> None:
    client, transport = client_for(
        response([{"marketId": "1.open", "profitAndLosses": []}], 1),
        response(
            {
                "clearedOrders": [{"marketId": "1.closed", "profit": 3.25}],
                "moreAvailable": False,
            },
            2,
        ),
    )
    markets = (
        market("1.open"),
        market("1.closed", status=MarketStatus.CLOSED),
    )

    witness = collect_coverage_witness(
        client=client,
        scope=scope(),
        markets=markets,
        closed_record_count=0,
    )

    assert_complete(witness, scope=scope(), markets=markets)
    assert len(transport.calls) == 2
    first = json.loads(transport.calls[0]["body"])
    second = json.loads(transport.calls[1]["body"])
    assert first["method"] == "SportsAPING/v1.0/listMarketProfitAndLoss"
    assert second["method"] == "SportsAPING/v1.0/listClearedOrders"
    assert second["params"]["groupBy"] == "MARKET"
    assert second["params"]["recordCount"] == 0
