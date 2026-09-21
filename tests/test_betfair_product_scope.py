import pytest

from autosport.betfair_product_scope import (
    BetfairExchangeAppKeyTier,
    BetfairOperation,
    BetfairProductDomain,
    BetfairProductScopeError,
    BetfairScopeState,
    BetfairUsageIntent,
    evaluate_betfair_product_scope,
)


def scope(
    *,
    domain=BetfairProductDomain.EXCHANGE,
    operation=BetfairOperation.MARKET_READ,
    usage_intent=BetfairUsageIntent.MONITOR_ONLY,
    exchange_app_key_tier=BetfairExchangeAppKeyTier.DELAYED,
    sportsbook_affiliate_entitled=None,
):
    return evaluate_betfair_product_scope(
        domain=domain,
        operation=operation,
        usage_intent=usage_intent,
        exchange_app_key_tier=exchange_app_key_tier,
        sportsbook_affiliate_entitled=sportsbook_affiliate_entitled,
    )


def test_delayed_exchange_monitoring_is_production_not_sandbox():
    result = scope()
    assert result.state is BetfairScopeState.COMPATIBLE
    assert result.production_environment is True
    assert result.delayed_market_data is True
    assert result.provider_exchange_bet_placement_available is True
    assert result.provenance_domain == "betfair.exchange"
    assert result.execution_authorized is False


def test_delayed_key_is_technically_transaction_capable_but_not_authority():
    result = scope(
        operation=BetfairOperation.PLACE_BET,
        usage_intent=BetfairUsageIntent.TRANSACTIONAL,
    )
    assert result.scope_compatible is True
    assert result.provider_exchange_bet_placement_available is True
    assert result.execution_authorized is False
    assert result.provider_entitlement_authorized is False


def test_live_key_monitor_only_usage_is_denied():
    result = scope(exchange_app_key_tier=BetfairExchangeAppKeyTier.LIVE)
    assert result.state is BetfairScopeState.DENIED
    assert result.reason_codes == ("LIVE_KEY_MONITOR_ONLY_NOT_PERMITTED",)
    assert result.delayed_market_data is False


def test_live_key_read_inside_transactional_application_is_scope_compatible():
    result = scope(
        usage_intent=BetfairUsageIntent.TRANSACTIONAL,
        exchange_app_key_tier=BetfairExchangeAppKeyTier.LIVE,
    )
    assert result.scope_compatible is True
    assert result.delayed_market_data is False
    assert result.execution_authorized is False


def test_live_key_place_bet_scope_still_does_not_authorize_execution():
    result = scope(
        operation=BetfairOperation.PLACE_BET,
        usage_intent=BetfairUsageIntent.TRANSACTIONAL,
        exchange_app_key_tier=BetfairExchangeAppKeyTier.LIVE,
    )
    assert result.scope_compatible is True
    assert result.execution_authorized is False


def test_exchange_requires_explicit_key_tier():
    result = scope(exchange_app_key_tier=None)
    assert result.state is BetfairScopeState.DENIED
    assert result.reason_codes == ("EXCHANGE_APP_KEY_TIER_REQUIRED",)
    assert result.provider_exchange_bet_placement_available is False
    assert result.delayed_market_data is None


def test_exchange_rejects_cross_domain_sportsbook_entitlement_projection():
    result = scope(sportsbook_affiliate_entitled=True)
    assert result.state is BetfairScopeState.DENIED
    assert result.reason_codes == (
        "SPORTSBOOK_ENTITLEMENT_CANNOT_SCOPE_EXCHANGE",
    )


def test_place_bet_rejects_monitor_only_intent_even_with_delayed_key():
    result = scope(operation=BetfairOperation.PLACE_BET)
    assert result.state is BetfairScopeState.DENIED
    assert result.reason_codes == ("PLACE_BET_REQUIRES_TRANSACTIONAL_INTENT",)
    assert result.provider_exchange_bet_placement_available is True
    assert result.execution_authorized is False


def test_sportsbook_place_bet_is_never_api_compatible():
    result = scope(
        domain=BetfairProductDomain.SPORTSBOOK,
        operation=BetfairOperation.PLACE_BET,
        usage_intent=BetfairUsageIntent.TRANSACTIONAL,
        exchange_app_key_tier=None,
        sportsbook_affiliate_entitled=True,
    )
    assert result.state is BetfairScopeState.DENIED
    assert result.reason_codes == (
        "SPORTSBOOK_API_READ_ONLY",
        "SPORTSBOOK_API_HAS_NO_TRANSACTIONAL_SCOPE",
    )
    assert result.provider_exchange_bet_placement_available is False
    assert result.sportsbook_read_only is True
    assert result.execution_authorized is False


@pytest.mark.parametrize("entitled", [None, False])
def test_sportsbook_read_requires_affiliate_entitlement_projection(entitled):
    result = scope(
        domain=BetfairProductDomain.SPORTSBOOK,
        exchange_app_key_tier=None,
        sportsbook_affiliate_entitled=entitled,
    )
    assert result.state is BetfairScopeState.DENIED
    assert result.reason_codes == (
        "SPORTSBOOK_AFFILIATE_ENTITLEMENT_REQUIRED",
    )


def test_sportsbook_affiliate_read_is_only_static_scope_compatibility():
    result = scope(
        domain=BetfairProductDomain.SPORTSBOOK,
        exchange_app_key_tier=None,
        sportsbook_affiliate_entitled=True,
    )
    assert result.scope_compatible is True
    assert result.provenance_domain == "betfair.sportsbook"
    assert result.delayed_market_data is None
    assert result.requires_external_affiliate_entitlement is True
    assert result.provider_entitlement_authorized is False
    assert result.execution_authorized is False


def test_exchange_application_key_cannot_be_relabelled_as_sportsbook_access():
    result = scope(
        domain=BetfairProductDomain.SPORTSBOOK,
        exchange_app_key_tier=BetfairExchangeAppKeyTier.DELAYED,
        sportsbook_affiliate_entitled=True,
    )
    assert result.state is BetfairScopeState.DENIED
    assert result.reason_codes == ("EXCHANGE_APP_KEY_CANNOT_SCOPE_SPORTSBOOK",)


def test_exchange_and_sportsbook_provenance_domains_never_alias():
    exchange = scope()
    sportsbook = scope(
        domain=BetfairProductDomain.SPORTSBOOK,
        exchange_app_key_tier=None,
        sportsbook_affiliate_entitled=True,
    )
    assert exchange.provenance_domain != sportsbook.provenance_domain


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("domain", "EXCHANGE"),
        ("operation", "MARKET_READ"),
        ("usage_intent", "MONITOR_ONLY"),
        ("exchange_app_key_tier", "DELAYED"),
        ("sportsbook_affiliate_entitled", 1),
    ],
)
def test_raw_strings_and_truthy_integers_do_not_bypass_enum_or_bool_contract(
    field, value
):
    kwargs = {
        "domain": BetfairProductDomain.EXCHANGE,
        "operation": BetfairOperation.MARKET_READ,
        "usage_intent": BetfairUsageIntent.MONITOR_ONLY,
        "exchange_app_key_tier": BetfairExchangeAppKeyTier.DELAYED,
        "sportsbook_affiliate_entitled": None,
    }
    kwargs[field] = value
    with pytest.raises(BetfairProductScopeError):
        evaluate_betfair_product_scope(**kwargs)
