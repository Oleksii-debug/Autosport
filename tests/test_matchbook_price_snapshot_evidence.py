from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from autosport.matchbook_price_snapshot_evidence import (
    AGGREGATE_TAIL_LEVEL,
    EXACT_RETURNED_LEVEL,
    NO_PRICE_WITHIN_FILTERED_SCOPE,
    OUTSIDE_DEPTH_SCOPE,
    OUTSIDE_SIDE_SCOPE,
    RETURNED_PRICE,
    MatchbookObservedPrice,
    MatchbookPriceEvidenceError,
    MatchbookPriceRequestScope,
    build_matchbook_price_snapshot_evidence,
)


UTC = timezone.utc
OBSERVED = datetime(2026, 9, 22, 5, 45, 0, tzinfo=UTC)


def scope(**changes: object) -> MatchbookPriceRequestScope:
    values: dict[str, object] = {
        "price_depth": 3,
        "price_mode": "expanded",
        "minimum_liquidity": Decimal("2.00"),
        "provider_default_policy_ref": None,
        "side_scope": "both",
        "exclude_mirrored_prices": True,
        "currency_code": "GBP",
    }
    values.update(changes)
    return MatchbookPriceRequestScope(**values)


def price(
    side: str,
    level: int,
    odds: str,
    amount: str,
) -> MatchbookObservedPrice:
    return MatchbookObservedPrice(
        side=side,
        level=level,
        decimal_odds=Decimal(odds),
        available_amount=Decimal(amount),
    )


def evidence(
    *,
    request_scope: MatchbookPriceRequestScope | None = None,
    prices: tuple[MatchbookObservedPrice, ...] | None = None,
    raw_response: bytes = b'{"events":[]}',
):
    return build_matchbook_price_snapshot_evidence(
        account_scope_ref="acct-scope-01",
        session_generation=7,
        event_id=101,
        market_id=202,
        runner_id=303,
        request_scope=request_scope or scope(),
        observed_at=OBSERVED,
        raw_response=raw_response,
        prices=prices
        if prices is not None
        else (
            price("back", 1, "2.10", "15.00"),
            price("lay", 1, "2.12", "11.00"),
        ),
    )


def test_projection_binds_request_censoring_and_hard_false_authorities() -> None:
    item = evidence()
    projection = item.projection()
    assert projection["provider_id"] == "matchbook"
    assert projection["request_scope"]["price_depth"] == 3
    assert projection["request_scope"]["minimum_liquidity"] == "2.00"
    assert projection["proves_market_has_no_liquidity"] is False
    assert projection["parsed_rows_bound_to_raw_response"] is False
    assert projection["grants_provider_acquisition_authority"] is False
    assert projection["grants_execution_authority"] is False
    assert projection["grants_settlement_authority"] is False
    assert projection["grants_strategy_promotion_authority"] is False


def test_request_scope_identity_changes_with_minimum_liquidity() -> None:
    assert scope(minimum_liquidity=Decimal("2")).scope_sha256 != scope(
        minimum_liquidity=Decimal("10")
    ).scope_sha256


def test_request_scope_identity_changes_with_depth_mode_side_and_mirroring() -> None:
    base = scope()
    variants = (
        scope(price_depth=1),
        scope(price_mode="aggregated"),
        scope(side_scope="back"),
        scope(exclude_mirrored_prices=False),
    )
    assert all(item.scope_sha256 != base.scope_sha256 for item in variants)


def test_provider_default_requires_versioned_policy_reference() -> None:
    with pytest.raises(MatchbookPriceEvidenceError):
        scope(minimum_liquidity=None, provider_default_policy_ref=None)


def test_omitted_minimum_liquidity_binds_provider_policy_version() -> None:
    first = scope(
        minimum_liquidity=None,
        provider_default_policy_ref="matchbook-min-liq-policy-2026-01",
    )
    second = scope(
        minimum_liquidity=None,
        provider_default_policy_ref="matchbook-min-liq-policy-2026-09",
    )
    assert first.scope_sha256 != second.scope_sha256


def test_explicit_minimum_liquidity_rejects_provider_default_reference() -> None:
    with pytest.raises(MatchbookPriceEvidenceError):
        scope(provider_default_policy_ref="should-not-coexist")


@pytest.mark.parametrize("bad", [2, 2.0, True, "2.0"])
def test_minimum_liquidity_requires_decimal(bad: object) -> None:
    with pytest.raises(MatchbookPriceEvidenceError):
        scope(minimum_liquidity=bad)


@pytest.mark.parametrize("bad", [2.1, 2, True, "2.1"])
def test_odds_require_decimal(bad: object) -> None:
    with pytest.raises(MatchbookPriceEvidenceError):
        MatchbookObservedPrice(
            side="back",
            level=1,
            decimal_odds=bad,
            available_amount=Decimal("1"),
        )


@pytest.mark.parametrize("bad", [10.0, 10, True, "10"])
def test_available_amount_requires_decimal(bad: object) -> None:
    with pytest.raises(MatchbookPriceEvidenceError):
        MatchbookObservedPrice(
            side="back",
            level=1,
            decimal_odds=Decimal("2"),
            available_amount=bad,
        )


def test_returned_side_cannot_escape_request_scope() -> None:
    with pytest.raises(MatchbookPriceEvidenceError):
        evidence(
            request_scope=scope(side_scope="back"),
            prices=(price("lay", 1, "2", "5"),),
        )


def test_returned_level_cannot_escape_requested_depth() -> None:
    with pytest.raises(MatchbookPriceEvidenceError):
        evidence(
            request_scope=scope(price_depth=1),
            prices=(
                price("back", 1, "2", "5"),
                price("back", 2, "2.1", "4"),
            ),
        )


def test_aggregated_mode_caps_visible_levels_at_three() -> None:
    with pytest.raises(MatchbookPriceEvidenceError):
        evidence(
            request_scope=scope(price_mode="aggregated", price_depth=5),
            prices=(
                price("back", 1, "2", "5"),
                price("back", 2, "2.1", "4"),
                price("back", 3, "2.2", "3"),
                price("back", 4, "2.3", "2"),
            ),
        )


def test_aggregated_third_level_is_not_raw_third_ladder_level() -> None:
    item = evidence(
        request_scope=scope(price_mode="aggregated"),
        prices=(
            price("back", 1, "2", "5"),
            price("back", 2, "2.1", "4"),
            price("back", 3, "2.2", "9"),
        ),
    )
    assert item.level_semantics("back", 1) == EXACT_RETURNED_LEVEL
    assert item.level_semantics("back", 3) == AGGREGATE_TAIL_LEVEL
    assert item.price_projection()[2]["level_semantics"] == AGGREGATE_TAIL_LEVEL


def test_duplicate_side_level_is_rejected() -> None:
    with pytest.raises(MatchbookPriceEvidenceError):
        evidence(
            prices=(
                price("back", 1, "2", "5"),
                price("back", 1, "2.1", "4"),
            )
        )


def test_gapped_levels_are_rejected() -> None:
    with pytest.raises(MatchbookPriceEvidenceError):
        evidence(
            prices=(
                price("back", 1, "2", "5"),
                price("back", 3, "2.2", "4"),
            )
        )


def test_absence_truth_never_claims_market_has_no_liquidity() -> None:
    item = evidence(prices=(price("back", 1, "2", "5"),))
    assert item.absence_truth("back", 1) == RETURNED_PRICE
    assert item.absence_truth("back", 2) == NO_PRICE_WITHIN_FILTERED_SCOPE
    assert "NO_LIQUIDITY" not in item.absence_truth("back", 2)


def test_absence_outside_side_scope_is_unknown_outside_scope() -> None:
    item = evidence(
        request_scope=scope(side_scope="back"),
        prices=(price("back", 1, "2", "5"),),
    )
    assert item.absence_truth("lay", 1) == OUTSIDE_SIDE_SCOPE


def test_absence_beyond_requested_depth_is_unknown_outside_scope() -> None:
    item = evidence(
        request_scope=scope(price_depth=1),
        prices=(price("back", 1, "2", "5"),),
    )
    assert item.absence_truth("back", 2) == OUTSIDE_DEPTH_SCOPE


def test_raw_response_changes_acquisition_identity() -> None:
    assert evidence(raw_response=b'{"a":1}').evidence_sha256 != evidence(
        raw_response=b'{"a":2}'
    ).evidence_sha256


def test_input_price_order_does_not_change_canonical_identity() -> None:
    first = evidence(
        prices=(
            price("lay", 1, "2.2", "4"),
            price("back", 1, "2.0", "5"),
        )
    )
    second = evidence(
        prices=(
            price("back", 1, "2.0", "5"),
            price("lay", 1, "2.2", "4"),
        )
    )
    assert first.evidence_sha256 == second.evidence_sha256


def test_observed_at_must_be_utc() -> None:
    with pytest.raises(MatchbookPriceEvidenceError):
        build_matchbook_price_snapshot_evidence(
            account_scope_ref="acct",
            session_generation=1,
            event_id=1,
            market_id=2,
            runner_id=3,
            request_scope=scope(),
            observed_at=datetime(
                2026,
                9,
                22,
                7,
                45,
                tzinfo=timezone(timedelta(hours=2)),
            ),
            raw_response=b"{}",
            prices=(),
        )


@pytest.mark.parametrize("bad", [b"", "{}", bytearray(b"{}"), memoryview(b"{}")])
def test_raw_response_must_be_non_empty_bytes(bad: object) -> None:
    with pytest.raises(MatchbookPriceEvidenceError):
        build_matchbook_price_snapshot_evidence(
            account_scope_ref="acct",
            session_generation=1,
            event_id=1,
            market_id=2,
            runner_id=3,
            request_scope=scope(),
            observed_at=OBSERVED,
            raw_response=bad,
            prices=(),
        )


@pytest.mark.parametrize("bad", ["gbp", "EURO", "G1P", " GBP"])
def test_currency_code_is_strict(bad: str) -> None:
    with pytest.raises(MatchbookPriceEvidenceError):
        scope(currency_code=bad)


@pytest.mark.parametrize("bad", [True, 0, -1, 1.0])
def test_session_generation_is_positive_non_boolean_integer(bad: object) -> None:
    with pytest.raises(MatchbookPriceEvidenceError):
        build_matchbook_price_snapshot_evidence(
            account_scope_ref="acct",
            session_generation=bad,
            event_id=1,
            market_id=2,
            runner_id=3,
            request_scope=scope(),
            observed_at=OBSERVED,
            raw_response=b"{}",
            prices=(),
        )


def test_identity_binds_native_event_market_runner_scope() -> None:
    base = evidence()
    changed = build_matchbook_price_snapshot_evidence(
        account_scope_ref="acct-scope-01",
        session_generation=7,
        event_id=101,
        market_id=202,
        runner_id=304,
        request_scope=scope(),
        observed_at=OBSERVED,
        raw_response=b'{"events":[]}',
        prices=(
            price("back", 1, "2.10", "15.00"),
            price("lay", 1, "2.12", "11.00"),
        ),
    )
    assert base.evidence_sha256 != changed.evidence_sha256


def test_projection_uses_raw_hash_not_raw_payload() -> None:
    item = evidence(raw_response=b'{"secretish-provider-payload":"x"}')
    projection = item.projection()
    assert "raw_response" not in projection
    assert len(projection["raw_response_sha256"]) == 64
    assert projection["raw_response_size_bytes"] > 0
