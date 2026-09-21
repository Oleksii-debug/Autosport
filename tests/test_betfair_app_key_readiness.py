from dataclasses import fields, replace

import pytest

from autosport.betfair_app_key_readiness import (
    BetfairAppKeyPurposeAssessment,
    BetfairAppKeyPurposeConfig,
    BetfairAppKeyReadinessError,
    BetfairDistributionMode,
    BetfairKeyPurposeState,
    BetfairUsageIntent,
    assess_betfair_app_key_purpose,
)
from autosport.betfair_capability_freshness import (
    BetfairApplicationKeyClass,
    BetfairCapabilityFreshnessEvidence,
    BetfairMarketDataDelayState,
    BetfairProviderEnvironment,
    BetfairStreamFreshnessMode,
)


_HASH = "a" * 64
_CTX = "b" * 64


def _config(**overrides) -> BetfairAppKeyPurposeConfig:
    values = {
        "usage_intent": BetfairUsageIntent.READ_ONLY,
        "distribution_mode": BetfairDistributionMode.PERSONAL,
    }
    values.update(overrides)
    return BetfairAppKeyPurposeConfig(**values)


def _source(
    key_class: BetfairApplicationKeyClass = BetfairApplicationKeyClass.DELAYED,
    *,
    authenticated: bool = True,
) -> BetfairCapabilityFreshnessEvidence:
    delay_state = (
        BetfairMarketDataDelayState.FRESH
        if key_class is BetfairApplicationKeyClass.LIVE
        else BetfairMarketDataDelayState.DELAYED
    )
    return BetfairCapabilityFreshnessEvidence(
        profile_id=_HASH,
        venue_id="BETFAIR_EXCHANGE",
        configured_account_ref="configured-account",
        adapter_id="betfair-exchange-jsonrpc-readonly",
        adapter_version="1",
        profile_version=1,
        market_id="1.234",
        environment=BetfairProviderEnvironment.GLOBAL_PRODUCTION_EXCHANGE,
        application_key_class=key_class,
        market_data_delay_state=delay_state,
        stream_freshness_mode=BetfairStreamFreshnessMode.UNKNOWN,
        observed_at="2026-09-21T18:30:00+00:00",
        source_ref="betfair://market-book/1.234",
        source_payload_sha256=_HASH,
        authenticated_context_sha256=_CTX if authenticated else None,
    )


@pytest.fixture
def product_issued(monkeypatch):
    monkeypatch.setattr(
        BetfairCapabilityFreshnessEvidence,
        "_assert_product_issued",
        lambda self: None,
    )


def test_direct_caller_constructed_upstream_evidence_is_rejected() -> None:
    with pytest.raises(BetfairAppKeyReadinessError, match="product-issued authority"):
        assess_betfair_app_key_purpose(_source(), _config())


def test_live_key_with_read_only_intent_fails_closed(product_issued) -> None:
    assessment = assess_betfair_app_key_purpose(
        _source(BetfairApplicationKeyClass.LIVE),
        _config(usage_intent=BetfairUsageIntent.READ_ONLY),
    )

    assert assessment.state is BetfairKeyPurposeState.READINESS_BLOCKED
    assert assessment.key_purpose_gate_passed is False
    assert "LIVE_KEY_READ_ONLY_PURPOSE_CONFLICT" in assessment.reasons
    assert assessment.realtime_price_authority_granted is False
    assert assessment.provider_write_authority_granted is False
    assert assessment.execution_authority_granted is False


def test_delayed_read_only_is_development_only_on_production_exchange(product_issued) -> None:
    assessment = assess_betfair_app_key_purpose(_source(), _config())

    assert assessment.source.environment is BetfairProviderEnvironment.GLOBAL_PRODUCTION_EXCHANGE
    assert assessment.state is BetfairKeyPurposeState.READ_ONLY_DEVELOPMENT
    assert assessment.key_purpose_gate_passed is True
    assert assessment.development_read_only_supported is True
    assert assessment.realtime_price_authority_granted is False
    assert "DELAYED_KEY_HAS_NO_REALTIME_PRICE_AUTHORITY" in assessment.reasons


def test_live_betting_is_only_key_purpose_compatible(product_issued) -> None:
    assessment = assess_betfair_app_key_purpose(
        _source(BetfairApplicationKeyClass.LIVE),
        _config(usage_intent=BetfairUsageIntent.BETTING),
    )

    assert assessment.state is BetfairKeyPurposeState.BETTING_KEY_PURPOSE_COMPATIBLE
    assert assessment.live_betting_key_purpose_compatible is True
    assert assessment.realtime_price_authority_granted is False
    assert assessment.provider_write_authority_granted is False
    assert assessment.execution_authority_granted is False
    assert assessment.licence_authority_granted is False
    assert assessment.whole_product_readiness_granted is False


def test_unknown_or_unauthenticated_key_class_cannot_pass(product_issued) -> None:
    unknown = assess_betfair_app_key_purpose(
        _source(BetfairApplicationKeyClass.UNKNOWN, authenticated=False),
        _config(),
    )
    unauthenticated_live = assess_betfair_app_key_purpose(
        _source(BetfairApplicationKeyClass.LIVE, authenticated=False),
        _config(usage_intent=BetfairUsageIntent.BETTING),
    )

    assert unknown.state is BetfairKeyPurposeState.READINESS_BLOCKED
    assert unauthenticated_live.state is BetfairKeyPurposeState.READINESS_BLOCKED
    assert "AUTHENTICATED_APPLICATION_KEY_CLASS_REQUIRED" in unknown.reasons
    assert "AUTHENTICATED_APPLICATION_KEY_CLASS_REQUIRED" in unauthenticated_live.reasons


def test_delayed_key_cannot_qualify_live_betting_readiness(product_issued) -> None:
    assessment = assess_betfair_app_key_purpose(
        _source(BetfairApplicationKeyClass.DELAYED),
        _config(usage_intent=BetfairUsageIntent.BETTING),
    )

    assert assessment.state is BetfairKeyPurposeState.READINESS_BLOCKED
    assert "DELAYED_KEY_CANNOT_QUALIFY_LIVE_BETTING_READINESS" in assessment.reasons


def test_distributed_software_remains_blocked_without_vendor_licence_authority(product_issued) -> None:
    assessment = assess_betfair_app_key_purpose(
        _source(BetfairApplicationKeyClass.LIVE),
        _config(
            usage_intent=BetfairUsageIntent.BETTING,
            distribution_mode=BetfairDistributionMode.DISTRIBUTED_SOFTWARE,
        ),
    )

    assert assessment.state is BetfairKeyPurposeState.READINESS_BLOCKED
    assert assessment.licence_authority_granted is False
    assert "SOFTWARE_VENDOR_LICENCE_AUTHORITY_REQUIRED" in assessment.reasons


def test_config_drift_requires_reresolution(product_issued) -> None:
    config = _config()
    assessment = assess_betfair_app_key_purpose(_source(), config)

    assessment.assert_current_config(replace(config))
    for changed in (
        replace(config, usage_intent=BetfairUsageIntent.BETTING),
        replace(config, distribution_mode=BetfairDistributionMode.DISTRIBUTED_SOFTWARE),
    ):
        with pytest.raises(BetfairAppKeyReadinessError, match="must be re-resolved"):
            assessment.assert_current_config(changed)


def test_upstream_authority_is_rechecked_after_assessment_creation(monkeypatch) -> None:
    source = _source()
    monkeypatch.setattr(
        BetfairCapabilityFreshnessEvidence,
        "_assert_product_issued",
        lambda self: None,
    )
    assessment = assess_betfair_app_key_purpose(source, _config())
    assert assessment.state is BetfairKeyPurposeState.READ_ONLY_DEVELOPMENT

    def stale(self):
        raise ValueError("stale authenticated context")

    monkeypatch.setattr(BetfairCapabilityFreshnessEvidence, "_assert_product_issued", stale)
    with pytest.raises(BetfairAppKeyReadinessError, match="product-issued authority"):
        _ = assessment.state


def test_config_and_assessment_shapes_have_no_raw_credential_fields() -> None:
    config_names = {field.name for field in fields(BetfairAppKeyPurposeConfig)}
    assessment_names = {field.name for field in fields(BetfairAppKeyPurposeAssessment)}

    assert config_names == {"usage_intent", "distribution_mode", "schema_version"}
    assert assessment_names == {"source", "config"}
    forbidden = {"application_key", "session_token", "credentials", "password", "secret"}
    assert not (config_names & forbidden)


def test_malformed_config_fails_closed() -> None:
    with pytest.raises(BetfairAppKeyReadinessError, match="usage_intent"):
        _config(usage_intent="read_only")
    with pytest.raises(BetfairAppKeyReadinessError, match="distribution_mode"):
        _config(distribution_mode="personal")
    with pytest.raises(BetfairAppKeyReadinessError, match="schema_version"):
        _config(schema_version=2)
