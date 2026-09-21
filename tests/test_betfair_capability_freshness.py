import pytest

from autosport.bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
    UnknownBookmakerCapability,
)
from autosport.betfair_capability_freshness import (
    BetfairApplicationKeyClass,
    BetfairCapabilityFreshnessError,
    BetfairCapabilityFreshnessEvidence,
    BetfairMarketDataDelayState,
    BetfairProviderEnvironment,
    BetfairStreamFreshnessMode,
    DelayedBetfairMarketData,
    UnknownBetfairMarketDataFreshness,
)


_TS = "2026-09-21T08:30:00+00:00"
_HASH = "c" * 64


def _profile(
    *,
    live_quotes: BookmakerCapabilityState = BookmakerCapabilityState.SUPPORTED,
    place_bet: BookmakerCapabilityState = BookmakerCapabilityState.UNKNOWN,
    version: int = 1,
    source_hash: str = "a" * 64,
) -> BookmakerCapabilityProfile:
    facts = [
        BookmakerCapabilityFact(
            BookmakerCapability.LIVE_QUOTES_READ,
            live_quotes,
        )
    ]
    if place_bet is not BookmakerCapabilityState.UNKNOWN:
        facts.append(
            BookmakerCapabilityFact(
                BookmakerCapability.PLACE_BET,
                place_bet,
            )
        )
    return BookmakerCapabilityProfile(
        venue_id="betfair-global",
        account_id="acct-1",
        adapter_id="betfair-readonly",
        adapter_version="1.0",
        profile_version=version,
        facts=tuple(facts),
        observed_at="2026-09-21T08:00:00+00:00",
        source_ref="betfair://capability-profile",
        source_payload_sha256=source_hash,
    )


def _evidence(
    profile: BookmakerCapabilityProfile | None = None,
    *,
    key_class: BetfairApplicationKeyClass = BetfairApplicationKeyClass.LIVE,
    delay: BetfairMarketDataDelayState = BetfairMarketDataDelayState.FRESH,
    stream: BetfairStreamFreshnessMode = BetfairStreamFreshnessMode.LIVE,
) -> BetfairCapabilityFreshnessEvidence:
    profile = profile or _profile()
    return BetfairCapabilityFreshnessEvidence.from_profile(
        profile,
        environment=BetfairProviderEnvironment.GLOBAL_PRODUCTION_EXCHANGE,
        application_key_class=key_class,
        market_data_delay_state=delay,
        stream_freshness_mode=stream,
        observed_at=_TS,
        source_ref="betfair://market-book/1.234",
        source_payload_sha256=_HASH,
    )


def test_delayed_key_is_still_explicitly_production_not_sandbox() -> None:
    evidence = _evidence(
        key_class=BetfairApplicationKeyClass.DELAYED,
        delay=BetfairMarketDataDelayState.DELAYED,
        stream=BetfairStreamFreshnessMode.DELAYED_CONFLATED,
    )

    assert evidence.is_production_exchange is True
    assert evidence.environment is BetfairProviderEnvironment.GLOBAL_PRODUCTION_EXCHANGE
    assert evidence.grants_product_write_authority is False


def test_live_key_alone_never_proves_fresh_market_data() -> None:
    profile = _profile()
    evidence = _evidence(
        profile,
        key_class=BetfairApplicationKeyClass.LIVE,
        delay=BetfairMarketDataDelayState.UNKNOWN,
        stream=BetfairStreamFreshnessMode.UNKNOWN,
    )

    with pytest.raises(
        UnknownBetfairMarketDataFreshness,
        match="delay state is unknown",
    ):
        evidence.require_live_market_data(profile)


def test_live_key_can_still_observe_delayed_data_and_fails_closed() -> None:
    profile = _profile()
    evidence = _evidence(
        profile,
        key_class=BetfairApplicationKeyClass.LIVE,
        delay=BetfairMarketDataDelayState.DELAYED,
        stream=BetfairStreamFreshnessMode.DELAYED_CONFLATED,
    )

    with pytest.raises(DelayedBetfairMarketData, match="market data is delayed"):
        evidence.require_live_market_data(profile)


def test_delayed_key_cannot_claim_fresh_rest_data() -> None:
    with pytest.raises(
        BetfairCapabilityFreshnessError,
        match="delayed application key cannot assert fresh market data",
    ):
        _evidence(
            key_class=BetfairApplicationKeyClass.DELAYED,
            delay=BetfairMarketDataDelayState.FRESH,
            stream=BetfairStreamFreshnessMode.UNKNOWN,
        )


def test_delayed_key_cannot_claim_live_stream() -> None:
    with pytest.raises(
        BetfairCapabilityFreshnessError,
        match="cannot assert live stream freshness",
    ):
        _evidence(
            key_class=BetfairApplicationKeyClass.DELAYED,
            delay=BetfairMarketDataDelayState.DELAYED,
            stream=BetfairStreamFreshnessMode.LIVE,
        )


def test_delayed_conflated_stream_cannot_satisfy_subminute_evidence() -> None:
    profile = _profile()
    evidence = _evidence(
        profile,
        key_class=BetfairApplicationKeyClass.LIVE,
        delay=BetfairMarketDataDelayState.FRESH,
        stream=BetfairStreamFreshnessMode.DELAYED_CONFLATED,
    )

    with pytest.raises(
        DelayedBetfairMarketData,
        match="cannot prove sub-minute freshness",
    ):
        evidence.require_subminute_stream_evidence(profile)


def test_observed_fresh_data_plus_live_stream_and_capability_passes() -> None:
    profile = _profile()
    evidence = _evidence(profile)

    evidence.require_live_market_data(profile)
    evidence.require_subminute_stream_evidence(profile)


def test_market_freshness_does_not_replace_missing_live_quote_capability() -> None:
    profile = _profile(live_quotes=BookmakerCapabilityState.UNKNOWN)
    evidence = _evidence(profile)

    with pytest.raises(UnknownBookmakerCapability, match="live_quotes_read"):
        evidence.require_live_market_data(profile)


def test_profile_identity_drift_fails_closed() -> None:
    original = _profile()
    changed = _profile(version=2, source_hash="b" * 64)
    evidence = _evidence(original)

    with pytest.raises(
        BetfairCapabilityFreshnessError,
        match="does not match capability profile identity",
    ):
        evidence.assert_matches_profile(changed)


def test_profile_subclass_is_rejected_before_overridable_dispatch() -> None:
    class ForgedProfile(BookmakerCapabilityProfile):
        @property
        def profile_id(self) -> str:
            return "f" * 64

        def state_of(self, capability: BookmakerCapability) -> BookmakerCapabilityState:
            return BookmakerCapabilityState.SUPPORTED

    base = _profile()
    forged = ForgedProfile(
        venue_id=base.venue_id,
        account_id=base.account_id,
        adapter_id=base.adapter_id,
        adapter_version=base.adapter_version,
        profile_version=base.profile_version,
        facts=base.facts,
        observed_at=base.observed_at,
        source_ref=base.source_ref,
        source_payload_sha256=base.source_payload_sha256,
    )

    with pytest.raises(
        BetfairCapabilityFreshnessError,
        match="exact BookmakerCapabilityProfile",
    ):
        BetfairCapabilityFreshnessEvidence.from_profile(
            forged,
            environment=BetfairProviderEnvironment.GLOBAL_PRODUCTION_EXCHANGE,
            application_key_class=BetfairApplicationKeyClass.LIVE,
            market_data_delay_state=BetfairMarketDataDelayState.FRESH,
            stream_freshness_mode=BetfairStreamFreshnessMode.LIVE,
            observed_at=_TS,
            source_ref="betfair://market-book/1.234",
            source_payload_sha256=_HASH,
        )


def test_technical_place_bet_support_never_grants_product_write_authority() -> None:
    profile = _profile(place_bet=BookmakerCapabilityState.SUPPORTED)
    evidence = _evidence(profile)

    assert (
        evidence.technical_place_bet_state(profile)
        is BookmakerCapabilityState.SUPPORTED
    )
    assert evidence.grants_product_write_authority is False


def test_freshness_evidence_cannot_predate_bound_capability_profile() -> None:
    profile = _profile()
    with pytest.raises(
        BetfairCapabilityFreshnessError,
        match="cannot predate",
    ):
        BetfairCapabilityFreshnessEvidence.from_profile(
            profile,
            environment=BetfairProviderEnvironment.GLOBAL_PRODUCTION_EXCHANGE,
            application_key_class=BetfairApplicationKeyClass.LIVE,
            market_data_delay_state=BetfairMarketDataDelayState.FRESH,
            stream_freshness_mode=BetfairStreamFreshnessMode.LIVE,
            observed_at="2026-09-21T07:59:59+00:00",
            source_ref="betfair://market-book/1.234",
            source_payload_sha256=_HASH,
        )


def test_nested_capability_fact_subclass_is_rejected() -> None:
    class ForgedFact(BookmakerCapabilityFact):
        pass

    forged_fact = ForgedFact(
        BookmakerCapability.LIVE_QUOTES_READ,
        BookmakerCapabilityState.UNKNOWN,
    )
    profile = BookmakerCapabilityProfile(
        venue_id="betfair-global",
        account_id="acct-1",
        adapter_id="betfair-readonly",
        adapter_version="1.0",
        profile_version=1,
        facts=(forged_fact,),
        observed_at="2026-09-21T08:00:00+00:00",
        source_ref="betfair://capability-profile",
        source_payload_sha256="a" * 64,
    )

    with pytest.raises(
        BetfairCapabilityFreshnessError,
        match="exact BookmakerCapabilityFact",
    ):
        _evidence(profile)


def test_evidence_identity_is_deterministic_and_secret_free() -> None:
    one = _evidence()
    two = _evidence()

    assert one.evidence_id == two.evidence_id
    fields = set(BetfairCapabilityFreshnessEvidence.__dataclass_fields__)
    assert "application_key" not in fields
    assert "session_token" not in fields
    assert "secret" not in fields
    assert "credentials" not in fields
