from decimal import Decimal

import pytest

from autosport.prophetx_order_admission import (
    AdmissionState,
    ProphetXEnvironment,
    ProphetXOrderShape,
    ProphetXOrderShapeError,
    ProphetXOrderType,
    ProphetXTimeInForce,
    ProphetXTransportProfile,
    assess_batch,
    assess_order_shape,
)


def _fix_limit(**overrides):
    values = {
        "transport": ProphetXTransportProfile.FIX,
        "environment": ProphetXEnvironment.SANDBOX,
        "account_id": "account-1",
        "strike_id": "strike-1",
        "quantity": Decimal("10"),
        "order_type": ProphetXOrderType.LIMIT,
        "american_price": -110,
        "time_in_force": ProphetXTimeInForce.GTC,
    }
    values.update(overrides)
    return ProphetXOrderShape(**values)


def _rest_limit(**overrides):
    values = {
        "transport": ProphetXTransportProfile.DIRECT_LINK_REST,
        "environment": ProphetXEnvironment.SANDBOX,
        "account_id": "account-1",
        "strike_id": "strike-1",
        "quantity": "10.00",
        "order_type": ProphetXOrderType.LIMIT,
        "american_price": "125",
        "fill_or_kill": True,
    }
    values.update(overrides)
    return ProphetXOrderShape(**values)


def test_valid_fix_limit_is_only_structurally_proven():
    result = assess_order_shape(_fix_limit())

    assert result.quantity_format is AdmissionState.PROVEN
    assert result.order_type_tif is AdmissionState.PROVEN
    assert result.batch_shape is AdmissionState.PROVEN
    assert result.instrument_identity is AdmissionState.UNKNOWN
    assert result.price_ladder is AdmissionState.UNKNOWN
    assert result.min_max_stake is AdmissionState.UNKNOWN
    assert result.exposure_limit is AdmissionState.UNKNOWN
    assert result.provider_execution_admissible is False
    assert result.structurally_blocked is False


def test_missing_fix_tif_is_provider_gtc_but_cannot_satisfy_immediate_fill():
    resting = assess_order_shape(_fix_limit(time_in_force=None))
    immediate = assess_order_shape(
        _fix_limit(time_in_force=None, require_immediate_fill=True)
    )

    assert resting.order_type_tif is AdmissionState.PROVEN
    assert immediate.order_type_tif is AdmissionState.BLOCKED


@pytest.mark.parametrize("tif", ["DAY", "IOC"])
def test_unsupported_fix_time_in_force_blocks_before_transport(tif):
    result = assess_order_shape(_fix_limit(time_in_force=tif))
    assert result.order_type_tif is AdmissionState.BLOCKED


def test_fix_fok_must_be_explicit_for_immediate_fill():
    result = assess_order_shape(
        _fix_limit(
            time_in_force=ProphetXTimeInForce.FOK,
            require_immediate_fill=True,
        )
    )
    assert result.order_type_tif is AdmissionState.PROVEN


def test_fix_market_order_requires_quote_id_and_forbids_explicit_price():
    missing_quote = assess_order_shape(
        _fix_limit(
            order_type=ProphetXOrderType.MARKET,
            american_price=None,
            time_in_force=ProphetXTimeInForce.FOK,
        )
    )
    explicit_price = assess_order_shape(
        _fix_limit(
            order_type=ProphetXOrderType.MARKET,
            american_price=-110,
            quote_id="quote-1",
            time_in_force=ProphetXTimeInForce.FOK,
        )
    )
    valid_shape = assess_order_shape(
        _fix_limit(
            order_type=ProphetXOrderType.MARKET,
            american_price=None,
            quote_id="quote-1",
            time_in_force=ProphetXTimeInForce.FOK,
        )
    )

    assert missing_quote.order_type_tif is AdmissionState.BLOCKED
    assert explicit_price.order_type_tif is AdmissionState.BLOCKED
    assert valid_shape.order_type_tif is AdmissionState.PROVEN
    assert valid_shape.price_ladder is AdmissionState.UNKNOWN
    assert valid_shape.provider_execution_admissible is False


def test_fix_rejects_rest_fill_or_kill_field():
    result = assess_order_shape(_fix_limit(fill_or_kill=True))
    assert result.order_type_tif is AdmissionState.BLOCKED


def test_rest_requires_explicit_fill_or_kill_and_does_not_import_fix_fields():
    missing_fok = assess_order_shape(_rest_limit(fill_or_kill=None))
    fix_tif = assess_order_shape(
        _rest_limit(time_in_force=ProphetXTimeInForce.FOK)
    )
    quote_id = assess_order_shape(_rest_limit(quote_id="quote-1"))

    assert missing_fok.order_type_tif is AdmissionState.BLOCKED
    assert fix_tif.order_type_tif is AdmissionState.BLOCKED
    assert quote_id.order_type_tif is AdmissionState.BLOCKED


def test_rest_immediate_fill_requires_fill_or_kill_true():
    blocked = assess_order_shape(
        _rest_limit(fill_or_kill=False, require_immediate_fill=True)
    )
    accepted_shape = assess_order_shape(
        _rest_limit(fill_or_kill=True, require_immediate_fill=True)
    )

    assert blocked.order_type_tif is AdmissionState.BLOCKED
    assert accepted_shape.order_type_tif is AdmissionState.PROVEN
    assert accepted_shape.provider_execution_admissible is False


def test_rest_market_order_semantics_are_unqualified():
    result = assess_order_shape(
        _rest_limit(order_type=ProphetXOrderType.MARKET, american_price=None)
    )
    assert result.order_type_tif is AdmissionState.BLOCKED


def test_non_buy_side_blocks_without_guessing_sell_semantics():
    result = assess_order_shape(_fix_limit(side="SELL"))
    assert result.order_type_tif is AdmissionState.BLOCKED


@pytest.mark.parametrize("quantity", [True, 1.5, "0", "-1", "NaN", "Infinity"])
def test_quantity_rejects_inexact_nonpositive_or_nonfinite_ingress(quantity):
    with pytest.raises(ProphetXOrderShapeError):
        _fix_limit(quantity=quantity)


@pytest.mark.parametrize("price", [True, -109.5, "99", "NaN", "Infinity"])
def test_american_price_rejects_non_integer_or_invalid_domain(price):
    with pytest.raises(ProphetXOrderShapeError):
        _fix_limit(american_price=price)


def test_fingerprint_is_decimal_representation_independent_and_binds_fok():
    a = _rest_limit(quantity="10.00", fill_or_kill=True)
    b = _rest_limit(quantity=Decimal("10"), fill_or_kill=True)
    changed = _rest_limit(quantity="10", fill_or_kill=False)

    assert a.fingerprint == b.fingerprint
    assert a.fingerprint != changed.fingerprint


def test_fix_batch_maximum_is_twenty_and_fails_atomically_above_it():
    twenty = tuple(
        _fix_limit(strike_id=f"strike-{index}")
        for index in range(20)
    )
    twenty_one = (*twenty, _fix_limit(strike_id="strike-20"))

    assert assess_batch(twenty).batch_shape is AdmissionState.PROVEN
    result = assess_batch(twenty_one)
    assert result.batch_shape is AdmissionState.BLOCKED
    assert len(result.order_fingerprints) == 21


def test_empty_batch_is_blocked():
    result = assess_batch(())
    assert result.batch_shape is AdmissionState.BLOCKED


def test_batch_cannot_cross_account_environment_or_transport_boundaries():
    cross_account = assess_batch(
        (
            _fix_limit(strike_id="a"),
            _fix_limit(strike_id="b", account_id="account-2"),
        )
    )
    cross_environment = assess_batch(
        (
            _fix_limit(strike_id="a"),
            _fix_limit(
                strike_id="b",
                environment=ProphetXEnvironment.PRODUCTION,
            ),
        )
    )
    cross_transport = assess_batch(
        (
            _fix_limit(strike_id="a"),
            _rest_limit(strike_id="b"),
        )
    )

    assert cross_account.batch_shape is AdmissionState.BLOCKED
    assert cross_environment.batch_shape is AdmissionState.BLOCKED
    assert cross_transport.batch_shape is AdmissionState.BLOCKED


def test_rest_multi_order_batch_remains_unknown_not_fabricated_supported():
    result = assess_batch(
        (
            _rest_limit(strike_id="a"),
            _rest_limit(strike_id="b"),
        )
    )
    assert result.batch_shape is AdmissionState.UNKNOWN


def test_sandbox_and_production_shapes_never_alias():
    sandbox = _fix_limit(environment=ProphetXEnvironment.SANDBOX)
    production = _fix_limit(environment=ProphetXEnvironment.PRODUCTION)

    assert sandbox.fingerprint != production.fingerprint
    assert assess_order_shape(sandbox).provider_execution_admissible is False
    assert assess_order_shape(production).provider_execution_admissible is False


def test_strike_id_presence_does_not_mint_current_provider_identity():
    result = assess_order_shape(_fix_limit(strike_id="looks-valid"))
    assert result.instrument_identity is AdmissionState.UNKNOWN
    assert any(
        "product-issued provider evidence" in reason
        for reason in result.reasons
    )
