from datetime import datetime, timezone
import json

import pytest

import autosport.betfair_account_readonly as betfair_account_readonly
from autosport.bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
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


class FakeNetworkResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, max_bytes: int) -> bytes:
        return self.payload[:max_bytes]


def _profile(
    *,
    live_quotes: BookmakerCapabilityState = BookmakerCapabilityState.SUPPORTED,
    place_bet: BookmakerCapabilityState = BookmakerCapabilityState.UNKNOWN,
    version: int = 1,
    source_hash: str = "a" * 64,
    account_ref: str = "acct-1",
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
        account_id=account_ref,
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


def _canonical_network_observation_and_client(
    monkeypatch,
    *,
    delayed: bool = False,
    account_ref: str = "configured-account-B",
    app_delay_data: bool = False,
    app_active: bool = True,
) -> tuple[BetfairMarketBookDelayObservation, BetfairReadOnlyClient]:
    def fake_urlopen(request, timeout):
        body = json.loads(request.data.decode("utf-8"))
        request_id = body["id"]
        method = body["method"]
        if method == "AccountAPING/v1.0/getDeveloperAppKeys":
            payload = {
                "jsonrpc": "2.0",
                "result": [
                    {
                        "appName": "autosport-test",
                        "appId": 101,
                        "appVersions": [
                            {
                                "owner": "synthetic-owner",
                                "versionId": 202,
                                "version": "synthetic-1",
                                "applicationKey": "app-secret-A",
                                "delayData": app_delay_data,
                                "subscriptionRequired": False,
                                "ownerManaged": False,
                                "active": app_active,
                            }
                        ],
                    }
                ],
                "id": request_id,
            }
        elif method == "SportsAPING/v1.0/listMarketBook":
            payload = {
                "jsonrpc": "2.0",
                "result": [
                    {
                        "marketId": "1.234",
                        "isMarketDataDelayed": delayed,
                    }
                ],
                "id": request_id,
            }
        else:
            raise AssertionError(f"unexpected Betfair method: {method}")
        return FakeNetworkResponse(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        )

    monkeypatch.setattr(
        betfair_account_readonly,
        "urlopen",
        fake_urlopen,
    )
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret-A", "session-secret-A"),
        venue_id="betfair-global",
        account_id=account_ref,
    )
    return read_market_book_delay(client, "1.234"), client


def _canonical_network_observation(
    monkeypatch,
    *,
    delayed: bool = False,
    account_ref: str = "configured-account-B",
    app_delay_data: bool = False,
    app_active: bool = True,
) -> BetfairMarketBookDelayObservation:
    observation, _client = _canonical_network_observation_and_client(
        monkeypatch,
        delayed=delayed,
        account_ref=account_ref,
        app_delay_data=app_delay_data,
        app_active=app_active,
    )
    return observation


def _delayed_evidence(
    *,
    profile: BookmakerCapabilityProfile | None = None,
    key_class: BetfairApplicationKeyClass = BetfairApplicationKeyClass.LIVE,
) -> BetfairCapabilityFreshnessEvidence:
    return BetfairCapabilityFreshnessEvidence.from_market_book_observation(
        profile or _profile(),
        _observation(delayed=True),
        application_key_class=key_class,
    )


def _direct_fresh_evidence(
    profile: BookmakerCapabilityProfile | None = None,
) -> BetfairCapabilityFreshnessEvidence:
    profile = profile or _profile()
    return BetfairCapabilityFreshnessEvidence(
        profile_id=profile.profile_id,
        venue_id=profile.venue_id,
        configured_account_ref=profile.account_id,
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


def test_injected_transport_non_delayed_observation_cannot_issue_fresh_truth():
    profile = _profile()
    observation = _observation(delayed=False)

    with pytest.raises(
        BetfairMarketBookFreshnessError,
        match="lacks canonical production network origin",
    ):
        BetfairCapabilityFreshnessEvidence.from_market_book_observation(
            profile,
            observation,
            application_key_class=BetfairApplicationKeyClass.LIVE,
        )


def test_provider_delayed_true_from_test_transport_is_safe_negative_evidence():
    profile = _profile()
    evidence = _delayed_evidence(profile=profile)

    assert evidence.market_data_delay_state is BetfairMarketDataDelayState.DELAYED
    assert evidence.stream_freshness_mode is BetfairStreamFreshnessMode.UNKNOWN
    assert evidence.environment is BetfairProviderEnvironment.GLOBAL_PRODUCTION_EXCHANGE
    assert evidence.grants_product_write_authority is False
    with pytest.raises(DelayedBetfairMarketData, match="market data is delayed"):
        _require_live(evidence, profile)


def test_delayed_key_is_still_explicitly_production_not_sandbox():
    evidence = _delayed_evidence(
        key_class=BetfairApplicationKeyClass.DELAYED,
    )

    assert evidence.is_production_exchange is True
    assert evidence.application_key_class is BetfairApplicationKeyClass.DELAYED
    assert evidence.grants_product_write_authority is False


def test_marketbook_evidence_cannot_assert_stream_live_even_by_direct_constructor():
    profile = _profile()
    with pytest.raises(
        BetfairCapabilityFreshnessError,
        match="cannot assert Stream freshness",
    ):
        BetfairCapabilityFreshnessEvidence(
            profile_id=profile.profile_id,
            venue_id=profile.venue_id,
            configured_account_ref=profile.account_id,
            adapter_id=profile.adapter_id,
            adapter_version=profile.adapter_version,
            profile_version=profile.profile_version,
            market_id="1.234",
            environment=BetfairProviderEnvironment.GLOBAL_PRODUCTION_EXCHANGE,
            application_key_class=BetfairApplicationKeyClass.LIVE,
            market_data_delay_state=BetfairMarketDataDelayState.FRESH,
            stream_freshness_mode=BetfairStreamFreshnessMode.LIVE,
            observed_at=OBSERVED.isoformat(),
            source_ref="betfair://market-book/1.234",
            source_payload_sha256="c" * 64,
        )


def test_direct_caller_constructed_fresh_evidence_cannot_pass_positive_gate():
    profile = _profile()
    forged = _direct_fresh_evidence(profile)

    with pytest.raises(
        BetfairCapabilityFreshnessError,
        match="not product-issued",
    ):
        _require_live(forged, profile)


def test_direct_caller_constructed_marketbook_observation_cannot_issue_freshness():
    profile = _profile()
    forged = BetfairMarketBookDelayObservation(
        venue_id=profile.venue_id,
        configured_account_ref=profile.account_id,
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


def test_market_rebinding_fails_closed_before_delayed_state_is_consumed():
    profile = _profile()
    evidence = _delayed_evidence(profile=profile)

    with pytest.raises(
        BetfairCapabilityFreshnessError,
        match="different market",
    ):
        _require_live(evidence, profile, market_id="9.999")


def test_stale_policy_rejects_t0_plus_sixty_one_without_needing_live_network():
    evidence = _direct_fresh_evidence()

    with pytest.raises(
        UnknownBetfairMarketDataFreshness,
        match="stale",
    ):
        evidence._assert_current(
            as_of="2026-09-21T10:01:01+00:00",
            max_age_seconds=60,
        )


def test_future_policy_rejects_observation_after_decision_cutoff():
    evidence = _direct_fresh_evidence()

    with pytest.raises(
        UnknownBetfairMarketDataFreshness,
        match="future",
    ):
        evidence._assert_current(
            as_of="2026-09-21T09:59:59+00:00",
            max_age_seconds=60,
        )


@pytest.mark.parametrize("max_age_seconds", [0, -1, True, 1.5])
def test_max_age_policy_is_explicit_and_exact(max_age_seconds):
    evidence = _direct_fresh_evidence()

    with pytest.raises(
        BetfairCapabilityFreshnessError,
        match="positive exact integer",
    ):
        evidence._assert_current(
            as_of=AS_OF,
            max_age_seconds=max_age_seconds,
        )


def test_subminute_policy_refuses_more_than_sixty_seconds_before_authority_use():
    profile = _profile()
    evidence = _direct_fresh_evidence(profile)

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


def test_caller_profile_live_quote_state_is_not_positive_freshness_authority(monkeypatch):
    profile = _profile(
        live_quotes=BookmakerCapabilityState.UNKNOWN,
        account_ref="configured-account-B",
    )
    observation = _canonical_network_observation(
        monkeypatch,
        account_ref="configured-account-B",
    )
    evidence = BetfairCapabilityFreshnessEvidence.from_market_book_observation(
        profile,
        observation,
        application_key_class=BetfairApplicationKeyClass.LIVE,
    )

    _require_live(
        evidence,
        profile,
        as_of=evidence.observed_at,
    )
    assert evidence.configured_account_ref == "configured-account-B"
    assert evidence.proves_provider_account_identity is False
    canonical = evidence.to_canonical_dict()
    assert canonical["configured_account_ref"] == "configured-account-B"
    assert "account_id" not in canonical
    fields = set(BetfairCapabilityFreshnessEvidence.__dataclass_fields__)
    assert "configured_account_ref" in fields
    assert "account_id" not in fields


def test_profile_identity_drift_fails_closed():
    original = _profile()
    changed = _profile(version=2, source_hash="b" * 64)
    evidence = _delayed_evidence(profile=original)

    with pytest.raises(
        BetfairCapabilityFreshnessError,
        match="does not match configured capability profile scope",
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
            _observation(delayed=True),
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
            _observation(delayed=True),
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
            _observation(delayed=True),
            application_key_class=BetfairApplicationKeyClass.LIVE,
        )


def test_technical_place_bet_support_never_grants_product_write_authority():
    profile = _profile(place_bet=BookmakerCapabilityState.SUPPORTED)
    evidence = _delayed_evidence(profile=profile)

    assert (
        evidence.technical_place_bet_state(profile)
        is BookmakerCapabilityState.SUPPORTED
    )
    assert evidence.grants_product_write_authority is False


def test_delayed_evidence_identity_is_deterministic_and_secret_free():
    one = _delayed_evidence()
    two = _delayed_evidence()

    assert one.evidence_id == two.evidence_id
    fields = set(BetfairCapabilityFreshnessEvidence.__dataclass_fields__)
    assert "configured_account_ref" in fields
    assert "account_id" not in fields
    assert "application_key" not in fields
    assert "session_token" not in fields
    assert "credentials" not in fields


def test_authenticated_provider_metadata_derives_live_key_and_secret_free_context(monkeypatch):
    profile = _profile(account_ref="configured-account-B")
    observation = _canonical_network_observation(monkeypatch)

    evidence = BetfairCapabilityFreshnessEvidence.from_market_book_observation(
        profile,
        observation,
    )

    assert observation.application_key_class == "live"
    assert observation.authenticated_context_sha256 is not None
    assert evidence.application_key_class is BetfairApplicationKeyClass.LIVE
    assert (
        evidence.authenticated_context_sha256
        == observation.authenticated_context_sha256
    )
    serialized = json.dumps(evidence.to_canonical_dict(), sort_keys=True)
    assert "app-secret-A" not in serialized
    assert "session-secret-A" not in serialized
    _require_live(evidence, profile, as_of=evidence.observed_at)


def test_caller_cannot_relabel_authenticated_live_key_as_unknown(monkeypatch):
    profile = _profile(account_ref="configured-account-B")
    observation = _canonical_network_observation(monkeypatch)

    with pytest.raises(
        BetfairCapabilityFreshnessError,
        match="conflicts with authenticated provider metadata",
    ):
        BetfairCapabilityFreshnessEvidence.from_market_book_observation(
            profile,
            observation,
            application_key_class=BetfairApplicationKeyClass.UNKNOWN,
        )


def test_caller_cannot_relabel_authenticated_live_key_as_delayed(monkeypatch):
    profile = _profile(account_ref="configured-account-B")
    observation = _canonical_network_observation(monkeypatch)

    with pytest.raises(
        BetfairCapabilityFreshnessError,
        match="conflicts with authenticated provider metadata",
    ):
        BetfairCapabilityFreshnessEvidence.from_market_book_observation(
            profile,
            observation,
            application_key_class=BetfairApplicationKeyClass.DELAYED,
        )


def test_session_rotation_invalidates_pre_rotation_positive_freshness(monkeypatch):
    profile = _profile(account_ref="configured-account-B")
    observation, client = _canonical_network_observation_and_client(monkeypatch)
    evidence = BetfairCapabilityFreshnessEvidence.from_market_book_observation(
        profile,
        observation,
    )

    _require_live(evidence, profile, as_of=evidence.observed_at)
    client._credentials = BetfairSessionCredentials(
        "app-secret-B",
        "session-secret-B",
    )

    with pytest.raises(
        BetfairMarketBookFreshnessError,
        match="authenticated Betfair context changed",
    ):
        _require_live(evidence, profile, as_of=evidence.observed_at)


def test_provider_delayed_app_key_cannot_mint_positive_non_delayed_evidence(monkeypatch):
    profile = _profile(account_ref="configured-account-B")
    observation = _canonical_network_observation(
        monkeypatch,
        delayed=False,
        app_delay_data=True,
    )

    assert observation.application_key_class == "delayed"
    with pytest.raises(
        BetfairMarketBookFreshnessError,
        match="requires provider LIVE application-key tier",
    ):
        BetfairCapabilityFreshnessEvidence.from_market_book_observation(
            profile,
            observation,
        )


def test_inactive_provider_live_key_cannot_mint_positive_freshness(monkeypatch):
    profile = _profile(account_ref="configured-account-B")
    observation = _canonical_network_observation(
        monkeypatch,
        delayed=False,
        app_delay_data=False,
        app_active=False,
    )

    assert observation.application_key_class == "live"
    with pytest.raises(
        BetfairMarketBookFreshnessError,
        match="requires active provider application key",
    ):
        BetfairCapabilityFreshnessEvidence.from_market_book_observation(
            profile,
            observation,
        )
