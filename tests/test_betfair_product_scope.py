import pytest

from autosport.betfair_product_scope import (
    BetfairProductDomain,
    BetfairProductIdentity,
    BetfairProductOperation,
    BetfairProductScopeError,
    BetfairProductScopeState,
    evaluate_betfair_product_scope,
)


def scope(
    *,
    domain=BetfairProductDomain.EXCHANGE,
    operation=BetfairProductOperation.MARKET_READ,
):
    return evaluate_betfair_product_scope(
        domain=domain,
        operation=operation,
    )


@pytest.mark.parametrize(
    "operation",
    [BetfairProductOperation.MARKET_READ, BetfairProductOperation.PLACE_BET],
)
def test_exchange_product_family_owns_exchange_read_and_bet_surfaces(operation):
    result = scope(operation=operation)
    assert result.state is BetfairProductScopeState.COMPATIBLE
    assert result.provenance_domain == "betfair.exchange"
    assert result.product_bet_placement_surface_available is True
    assert result.sportsbook_read_only is False
    assert result.requires_external_affiliate_entitlement is False
    assert result.requires_separate_sportsbook_adapter is False
    assert result.execution_authorized is False
    assert result.provider_entitlement_authorized is False


def test_sportsbook_place_bet_is_never_api_compatible():
    result = scope(
        domain=BetfairProductDomain.SPORTSBOOK,
        operation=BetfairProductOperation.PLACE_BET,
    )
    assert result.state is BetfairProductScopeState.DENIED
    assert result.reason_codes == ("SPORTSBOOK_API_READ_ONLY",)
    assert result.product_bet_placement_surface_available is False
    assert result.sportsbook_read_only is True
    assert result.requires_external_affiliate_entitlement is True
    assert result.requires_separate_sportsbook_adapter is True
    assert result.execution_authorized is False


def test_sportsbook_read_never_accepts_caller_minted_entitlement():
    result = scope(
        domain=BetfairProductDomain.SPORTSBOOK,
        operation=BetfairProductOperation.MARKET_READ,
    )
    assert (
        result.state
        is BetfairProductScopeState.REQUIRES_EXTERNAL_ENTITLEMENT
    )
    assert result.scope_compatible is False
    assert result.reason_codes == (
        "SPORTSBOOK_AFFILIATE_ENTITLEMENT_REQUIRED",
    )
    assert result.provenance_domain == "betfair.sportsbook"
    assert result.product_bet_placement_surface_available is False
    assert result.sportsbook_read_only is True
    assert result.requires_external_affiliate_entitlement is True
    assert result.requires_separate_sportsbook_adapter is True
    assert result.provider_entitlement_authorized is False
    assert result.execution_authorized is False


def test_same_external_id_never_cross_dedupes_between_products():
    exchange = BetfairProductIdentity(
        BetfairProductDomain.EXCHANGE,
        "event-123",
    )
    sportsbook = BetfairProductIdentity(
        BetfairProductDomain.SPORTSBOOK,
        "event-123",
    )
    assert exchange.provenance_key == "betfair.exchange:event-123"
    assert sportsbook.provenance_key == "betfair.sportsbook:event-123"
    assert exchange.provenance_key != sportsbook.provenance_key


@pytest.mark.parametrize("bad_external_id", ["", " event", "event ", "a\x00b"])
def test_product_identity_rejects_noncanonical_external_id(bad_external_id):
    with pytest.raises(BetfairProductScopeError, match="external_id"):
        BetfairProductIdentity(
            BetfairProductDomain.EXCHANGE,
            bad_external_id,
        )


def test_generic_betfair_string_cannot_substitute_for_product_family():
    with pytest.raises(BetfairProductScopeError, match="domain"):
        evaluate_betfair_product_scope(
            domain="BETFAIR",  # type: ignore[arg-type]
            operation=BetfairProductOperation.PLACE_BET,
        )


def test_raw_operation_string_is_not_accepted():
    with pytest.raises(BetfairProductScopeError, match="operation"):
        evaluate_betfair_product_scope(
            domain=BetfairProductDomain.EXCHANGE,
            operation="PLACE_BET",  # type: ignore[arg-type]
        )
