from datetime import datetime, timezone
import json

import pytest

from autosport.bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
    UnknownBookmakerCapability,
)
from autosport.betfair_account_readonly import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)
from autosport.betfair_marketbook_freshness import (
    BetfairMarketBookDelayObservation,
    BetfairMarketBookFreshnessError,
    read_market_book_delay,
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


OBSERVED = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)
AS_OF = "2026-09-21T10:00:30+00:00"


class FakeTransport:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def post(self, url, *, headers, body, timeout_seconds):
        return self.payload


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
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        profile_version=version,
        facts=tuple(facts),
        observed_at="2026-09-21T09:59:00+00:00",
        source_ref="betfair://capability-profile",
        source_payload_sha256=source_hash,
    )


def _observation(*, delayed: bool) -> BetfairMarketBookDelayObservation:
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "result": [
                {
                    "marketId": "1.234",
                    "isMarketDataDelayed": delayed,
                }
            ],
            "id": 1,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=FakeTransport(payload),
        clock=lambda: OBSERVED,
        venue_id="betfair-global",
        account_id="acct-1",
    )
    return read_market_book_delay(client, "1.234")


def _evidence(
    *,
    delayed: bool = False,
    key_class: BetfairApplicationKeyClass = BetfairApplicationKeyClass.LIVE,
    profile: BookmakerCapabilityProfile | None = None,
) -> BetfairCapabilityFreshnessEvidence:
    return BetfairCapabilityFreshnessEvidence.from_market_book_observation(
        profile or _profile(),
        _observation(delayed=delayed),
        application_key_class=key_class,
    )


def _require_live(
    evidence: BetfairCapabilityFreshnessEvidence,
    profile: BookmakerCapabilityProfile,
    *,
    as_of: str = AS_OF,
    max_age_seconds: int = 60,
    market_id: str = "1.234",
) -> None:
    evidence.require_live_market_data(
        profile,
        market_id=market_id,
        as_of=as_of,
        max_age_seconds=max_age_seconds,
    )


def test_non_delayed_provider_observation_can_issue_bounded_exact_market_freshness():
    profile = _profile()
    evidence = _evidence(profile=profile)

    _require_live(evidence, profile)

    assert evidence.market_data_delay_state is BetfairMarketDataDelayState.FRESH
    assert evidence.stream_freshness_mode is BetfairStreamFreshnessMode.UNKNOWN
    assert evidence.environment is BetfairProviderEnvironment.GLOBAL_PRODUCTION_EXCHANGE
    assert evidence.market_id == "1.234"
    assert evidence.grants_product_write_authority is False


def test_live_key_with_provider_delayed_true_fails_closed():
    profile = _profile()
    evidence = _evidence(delayed=True, profile=profile)

    assert evidence.market_data_delay_state is BetfairMarketDataDelayState.DELAYED
    with pytest.raises(DelayedBetfairMarketData, match="market data is delayed"):
        _require_live(evidence, profile)


def test_delayed_key_is_production_but_cannot_contradict_non_delayed_observation():
    with pytest.raises(
        BetfairCapabilityFreshnessError,
        match="delayed application key cannot assert fresh market data",
    ):
        _evidence(
            delayed=False,
            key_class=BetfairApplicationKeyClass.DELAYED,
        )

    delayed = _evidence(
        delayed=True,
        key_class=BetfairApplicationKeyClass.DELAYED,
    )
    assert delayed.is_production_exchange is True
    assert delayed.grants_product_write_authority is False


def test_application_key_class_alone_does_not_create_stream_truth():
    profile = _profile()
    evidence = _evidence(profile=profile, key_class=BetfairApplicationKeyClass.LIVE)

    with pytest.raises(
        UnknownBetfairMarketDataFreshness,
        match="Stream freshness has not been observed",
    ):
        evidence.require_subminute_stream_evidence(
            profile,
            market_id="1.234",
            as_of=AS_OF,
            max_age_seconds=60,
        )


def test_direct_caller_constructed_fresh_evidence_cannot_pass_positive_gate():
    profile = _profile()
    forged = BetfairCapabilityFreshnessEvidence(
        profile_id=profile.profile_id,
        venue_id=profile.venue_id,
        account_id=profile.account_id,
        adapter_id=profile.adapter_id,
        adapter_version=profile.adapter_version,
        profile_version=profile.profile_version,
        market_id="1.234",
        environment=BetfairProviderEnvironment.GLOBAL_PRODUCTION_EXCHANGE,
        application_key_class=BetfairApplicationKeyClass.LIVE,
        market_data_delay_state=BetfairMarketDataDelayState.FRESH,
        stream_freshness_mode=BetfairStreamFreshnessMode.UNKNOWN,
        observed_at=OBSERVED.isoformat(),
        source_ref="betfair://market-book/1.234",
        source_payload_sha256="c" * 64,
    )

    with pytest.raises(
        BetfairCapabilityFreshnessError,
        match="not product-issued",
    ):
        _require_live(forged, profile)


def test_direct_caller_constructed_marketbook_observation_cannot_issue_freshness():
    profile = _profile()
    forged = BetfairMarketBookDelayObservation(
        venue_id=profile.venue_id,
        account_id=profile.account_id,
        adapter_id=profile.adapter_id,
        adapter_version=profile.adapter_version,
        market_id="1.234",
        is_market_data_delayed=False,
        observed_at=OBSERVED.isoformat(),
        source_payload_sha256="d" * 64,
    )

    with pytest.raises(
        BetfairMarketBookFreshnessError,
        match="not issued by canonical Betfair adapter",
    ):
        BetfairCapabilityFreshnessEvidence.from_market_book_observation(
            profile,
            forged,
            application_key_class=BetfairApplicationKeyClass.LIVE,
        )


def test_market_rebinding_fails_closed():
    profile = _profile()
    evidence = _evidence(profile=profile)

    with pytest.raises(
        BetfairCapabilityFreshnessError,
        match="different market",
    ):
        _require_live(evidence, profile, market_id="9.999")


def test_stale_evidence_fails_at_decision_time():
    profile = _profile()
    evidence = _evidence(profile=profile)

    with pytest.raises(
        UnknownBetfairMarketDataFreshness,
        match="stale",
    ):
        _require_live(
            evidence,
            profile,
            as_of="2026-09-21T10:01:01+00:00",
            max_age_seconds=60,
        )


def test_future_evidence_fails_at_decision_time():
    profile = _profile()
    evidence = _evidence(profile=profile)

    with pytest.raises(
        UnknownBetfairMarketDataFreshness,
        match="future",
    ):
        _require_live(
            evidence,
            profile,
            as_of="2026-09-21T09:59:59+00:00",
            max_age_seconds=60,
        )


@pytest.mark.parametrize("max_age_seconds", [0, -1, True, 1.5])
def test_max_age_policy_is_explicit_and_exact(max_age_seconds):
    profile = _profile()
    evidence = _evidence(profile=profile)

    with pytest.raises(
        BetfairCapabilityFreshnessError,
        match="positive exact integer",
    ):
        _require_live(
            evidence,
            profile,
            max_age_seconds=max_age_seconds,
        )


def test_subminute_policy_refuses_more_than_sixty_seconds():
    profile = _profile()
    evidence = _evidence(profile=profile)

    with pytest.raises(
        BetfairCapabilityFreshnessError,
        match="cannot exceed 60",
    ):
        evidence.require_subminute_stream_evidence(
            profile,
            market_id="1.234",
            as_of=AS_OF,
            max_age_seconds=61,
        )


def test_missing_live_quote_capability_still_fails_closed():
    profile = _profile(live_quotes=BookmakerCapabilityState.UNKNOWN)
    evidence = _evidence(profile=profile)

    with pytest.raises(UnknownBookmakerCapability, match="live_quotes_read"):
        _require_live(evidence, profile)


def test_profile_identity_drift_fails_closed():
    original = _profile()
    changed = _profile(version=2, source_hash="b" * 64)
    evidence = _evidence(profile=original)

    with pytest.raises(
        BetfairCapabilityFreshnessError,
        match="does not match capability profile identity",
    ):
        _require_live(evidence, changed)


def test_profile_adapter_identity_must_match_marketbook_observation():
    profile = _profile()
    mismatched = BookmakerCapabilityProfile(
        venue_id=profile.venue_id,
        account_id=profile.account_id,
        adapter_id="different-adapter",
        adapter_version=profile.adapter_version,
        profile_version=profile.profile_version,
        facts=profile.facts,
        observed_at=profile.observed_at,
        source_ref=profile.source_ref,
        source_payload_sha256=profile.source_payload_sha256,
    )

    with pytest.raises(
        BetfairCapabilityFreshnessError,
        match="adapter identity",
    ):
        BetfairCapabilityFreshnessEvidence.from_market_book_observation(
            mismatched,
            _observation(delayed=False),
            application_key_class=BetfairApplicationKeyClass.LIVE,
        )


def test_profile_subclass_is_rejected_before_overridable_dispatch():
    class ForgedProfile(BookmakerCapabilityProfile):
        pass

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
        BetfairCapabilityFreshnessEvidence.from_market_book_observation(
            forged,
            _observation(delayed=False),
            application_key_class=BetfairApplicationKeyClass.LIVE,
        )


def test_nested_capability_fact_subclass_is_rejected():
    class ForgedFact(BookmakerCapabilityFact):
        pass

    profile = _profile()
    forged = BookmakerCapabilityProfile(
        venue_id=profile.venue_id,
        account_id=profile.account_id,
        adapter_id=profile.adapter_id,
        adapter_version=profile.adapter_version,
        profile_version=profile.profile_version,
        facts=(
            ForgedFact(
                BookmakerCapability.LIVE_QUOTES_READ,
                BookmakerCapabilityState.SUPPORTED,
            ),
        ),
        observed_at=profile.observed_at,
        source_ref=profile.source_ref,
        source_payload_sha256=profile.source_payload_sha256,
    )

    with pytest.raises(
        BetfairCapabilityFreshnessError,
        match="exact BookmakerCapabilityFact",
    ):
        BetfairCapabilityFreshnessEvidence.from_market_book_observation(
            forged,
            _observation(delayed=False),
            application_key_class=BetfairApplicationKeyClass.LIVE,
        )


def test_technical_place_bet_support_never_grants_product_write_authority():
    profile = _profile(place_bet=BookmakerCapabilityState.SUPPORTED)
    evidence = _evidence(profile=profile)

    assert (
        evidence.technical_place_bet_state(profile)
        is BookmakerCapabilityState.SUPPORTED
    )
    assert evidence.grants_product_write_authority is False


def test_evidence_identity_is_deterministic_and_secret_free():
    one = _evidence()
    two = _evidence()

    assert one.evidence_id == two.evidence_id
    fields = set(BetfairCapabilityFreshnessEvidence.__dataclass_fields__)
    assert "application_key" not in fields
    assert "session_token" not in fields
    assert "credentials" not in fields
