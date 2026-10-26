from autosport.bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
    UnknownBookmakerCapability,
)
from autosport.bookmaker_integration_boundary import (
    BookmakerIntegrationEvidence,
    BookmakerIntegrationKind,
)
from autosport.bookmaker_capability_source import (
    CapabilitySourceBoundaryError,
    CapabilitySourceClass,
    CapabilitySourceMatrix,
    SourceBoundCapabilityProfile,
)
import pytest

_TS = "2026-09-21T10:00:00+00:00"
_HASH_A = "a" * 64
_HASH_B = "b" * 64
_HASH_C = "c" * 64


def _profile(adapter_id: str, *supported: BookmakerCapability, account_id: str = "acct-1", unsupported: tuple[BookmakerCapability, ...] = ()) -> BookmakerCapabilityProfile:
    facts = tuple(BookmakerCapabilityFact(capability=c, state=BookmakerCapabilityState.SUPPORTED) for c in supported) + tuple(BookmakerCapabilityFact(capability=c, state=BookmakerCapabilityState.UNSUPPORTED) for c in unsupported)
    return BookmakerCapabilityProfile(
        venue_id="betfair", account_id=account_id, adapter_id=adapter_id,
        adapter_version="1.0", profile_version=1, facts=facts,
        observed_at=_TS, source_ref=f"{adapter_id}-capability-probe",
        source_payload_sha256=_HASH_A,
    )


def _integration(profile: BookmakerCapabilityProfile, kind: BookmakerIntegrationKind, *, observed_at: str = _TS) -> BookmakerIntegrationEvidence:
    return BookmakerIntegrationEvidence(
        venue_id=profile.venue_id, adapter_id=profile.adapter_id,
        adapter_version=profile.adapter_version, profile_id=profile.profile_id,
        integration_kind=kind, observed_at=observed_at,
        source_ref=f"{kind.value}-integration", source_payload_sha256=_HASH_B,
    )


def _bound_integration(profile: BookmakerCapabilityProfile, kind: BookmakerIntegrationKind, *, observed_at: str = _TS) -> SourceBoundCapabilityProfile:
    return SourceBoundCapabilityProfile(
        profile=profile, source_class=CapabilitySourceClass.INTEGRATION,
        observed_at=observed_at, source_ref=f"{kind.value}-binding",
        source_payload_sha256=_HASH_C, integration_evidence=_integration(profile, kind),
    )


def _bound_historical(profile: BookmakerCapabilityProfile) -> SourceBoundCapabilityProfile:
    return SourceBoundCapabilityProfile(
        profile=profile, source_class=CapabilitySourceClass.HISTORICAL_FIXTURE,
        observed_at=_TS, source_ref="fixture-binding", source_payload_sha256=_HASH_C,
    )


def test_api_supported_fact_does_not_transfer_to_browser_channel() -> None:
    official = _bound_integration(_profile("shared-adapter", BookmakerCapability.PLACE_BET), BookmakerIntegrationKind.OFFICIAL_API)
    browser = _bound_integration(_profile("shared-adapter"), BookmakerIntegrationKind.BROWSER_AUTOMATION)
    matrix = CapabilitySourceMatrix((official, browser))
    assert matrix.state_of(CapabilitySourceClass.INTEGRATION, "shared-adapter", BookmakerCapability.PLACE_BET, integration_kind=BookmakerIntegrationKind.OFFICIAL_API) is BookmakerCapabilityState.SUPPORTED
    assert matrix.state_of(CapabilitySourceClass.INTEGRATION, "shared-adapter", BookmakerCapability.PLACE_BET, integration_kind=BookmakerIntegrationKind.BROWSER_AUTOMATION) is BookmakerCapabilityState.UNKNOWN
    with pytest.raises(UnknownBookmakerCapability, match="browser_automation"):
        matrix.require(CapabilitySourceClass.INTEGRATION, "shared-adapter", BookmakerCapability.PLACE_BET, integration_kind=BookmakerIntegrationKind.BROWSER_AUTOMATION)


def test_integration_kind_is_canonical_bookmaker_integration_kind() -> None:
    bound = _bound_integration(_profile("api", BookmakerCapability.LIVE_QUOTES_READ), BookmakerIntegrationKind.OFFICIAL_API)
    assert bound.integration_kind is BookmakerIntegrationKind.OFFICIAL_API
    assert not hasattr(CapabilitySourceClass, "OFFICIAL_API")
    assert not hasattr(CapabilitySourceClass, "BROWSER_UI_ADAPTER")


def test_browser_market_read_does_not_imply_order_readback() -> None:
    browser = _bound_integration(_profile("browser", BookmakerCapability.LIVE_QUOTES_READ), BookmakerIntegrationKind.BROWSER_AUTOMATION)
    matrix = CapabilitySourceMatrix((browser,))
    assert matrix.state_of(CapabilitySourceClass.INTEGRATION, "browser", BookmakerCapability.LIVE_QUOTES_READ, integration_kind=BookmakerIntegrationKind.BROWSER_AUTOMATION) is BookmakerCapabilityState.SUPPORTED
    assert matrix.state_of(CapabilitySourceClass.INTEGRATION, "browser", BookmakerCapability.BET_READBACK, integration_kind=BookmakerIntegrationKind.BROWSER_AUTOMATION) is BookmakerCapabilityState.UNKNOWN


def test_missing_integration_channel_is_unknown_not_borrowed() -> None:
    official = _bound_integration(_profile("shared", BookmakerCapability.BALANCE_READ), BookmakerIntegrationKind.OFFICIAL_API)
    matrix = CapabilitySourceMatrix((official,))
    assert matrix.state_of(CapabilitySourceClass.INTEGRATION, "shared", BookmakerCapability.BALANCE_READ, integration_kind=BookmakerIntegrationKind.BROWSER_AUTOMATION) is BookmakerCapabilityState.UNKNOWN


def test_matrix_identity_is_input_order_independent() -> None:
    official = _bound_integration(_profile("api", BookmakerCapability.BALANCE_READ), BookmakerIntegrationKind.OFFICIAL_API)
    historical = _bound_historical(_profile("fixture", BookmakerCapability.LIVE_QUOTES_READ))
    assert CapabilitySourceMatrix((official, historical)).matrix_id == CapabilitySourceMatrix((historical, official)).matrix_id


def test_duplicate_same_provenance_channel_adapter_is_rejected() -> None:
    first = _bound_integration(_profile("api", BookmakerCapability.BALANCE_READ), BookmakerIntegrationKind.OFFICIAL_API)
    second = _bound_integration(_profile("api", BookmakerCapability.LIMITS_READ), BookmakerIntegrationKind.OFFICIAL_API)
    with pytest.raises(CapabilitySourceBoundaryError, match="duplicate provenance/integration/adapter"):
        CapabilitySourceMatrix((first, second))


def test_same_adapter_may_have_distinct_canonical_channels() -> None:
    official = _bound_integration(_profile("shared", BookmakerCapability.BALANCE_READ), BookmakerIntegrationKind.OFFICIAL_API)
    browser = _bound_integration(_profile("shared", BookmakerCapability.LIVE_QUOTES_READ), BookmakerIntegrationKind.BROWSER_AUTOMATION)
    CapabilitySourceMatrix((official, browser))
    assert official.binding_id != browser.binding_id


def test_cross_account_aggregation_is_rejected() -> None:
    first = _bound_integration(_profile("api", BookmakerCapability.BALANCE_READ), BookmakerIntegrationKind.OFFICIAL_API)
    second = _bound_historical(_profile("fixture", BookmakerCapability.LIVE_QUOTES_READ, account_id="acct-2"))
    with pytest.raises(CapabilitySourceBoundaryError, match="one exact venue/account"):
        CapabilitySourceMatrix((first, second))


def test_binding_cannot_predate_profile() -> None:
    profile = BookmakerCapabilityProfile(
        venue_id="betfair", account_id="acct-1", adapter_id="fixture",
        adapter_version="1.0", profile_version=1, facts=(),
        observed_at="2026-09-21T10:00:01+00:00", source_ref="profile",
        source_payload_sha256=_HASH_A,
    )
    with pytest.raises(CapabilitySourceBoundaryError, match="cannot predate capability profile"):
        SourceBoundCapabilityProfile(
            profile=profile, source_class=CapabilitySourceClass.HISTORICAL_FIXTURE,
            observed_at=_TS, source_ref="fixture", source_payload_sha256=_HASH_C,
        )


def test_binding_cannot_predate_integration_evidence() -> None:
    profile = _profile("api")
    evidence = _integration(profile, BookmakerIntegrationKind.OFFICIAL_API, observed_at="2026-09-21T10:00:02+00:00")
    with pytest.raises(CapabilitySourceBoundaryError, match="cannot predate integration evidence"):
        SourceBoundCapabilityProfile(
            profile=profile, source_class=CapabilitySourceClass.INTEGRATION,
            observed_at="2026-09-21T10:00:01+00:00", source_ref="binding",
            source_payload_sha256=_HASH_C, integration_evidence=evidence,
        )


def test_integration_binding_requires_exact_canonical_evidence() -> None:
    profile = _profile("api")
    with pytest.raises(CapabilitySourceBoundaryError, match="requires exact BookmakerIntegrationEvidence"):
        SourceBoundCapabilityProfile(
            profile=profile, source_class=CapabilitySourceClass.INTEGRATION,
            observed_at=_TS, source_ref="binding", source_payload_sha256=_HASH_C,
        )


def test_historical_fixture_rejects_integration_evidence() -> None:
    profile = _profile("fixture")
    evidence = _integration(profile, BookmakerIntegrationKind.OFFICIAL_API)
    with pytest.raises(CapabilitySourceBoundaryError, match="cannot carry integration evidence"):
        SourceBoundCapabilityProfile(
            profile=profile, source_class=CapabilitySourceClass.HISTORICAL_FIXTURE,
            observed_at=_TS, source_ref="fixture", source_payload_sha256=_HASH_C,
            integration_evidence=evidence,
        )


def test_integration_evidence_for_different_profile_is_rejected() -> None:
    profile = _profile("api")
    other = _profile("other")
    evidence = _integration(other, BookmakerIntegrationKind.OFFICIAL_API)
    with pytest.raises(CapabilitySourceBoundaryError, match="does not match capability profile"):
        SourceBoundCapabilityProfile(
            profile=profile, source_class=CapabilitySourceClass.INTEGRATION,
            observed_at=_TS, source_ref="binding", source_payload_sha256=_HASH_C,
            integration_evidence=evidence,
        )


def test_integration_query_requires_exact_canonical_kind() -> None:
    matrix = CapabilitySourceMatrix((_bound_integration(_profile("api"), BookmakerIntegrationKind.OFFICIAL_API),))
    with pytest.raises(CapabilitySourceBoundaryError, match="require exact BookmakerIntegrationKind"):
        matrix.state_of(CapabilitySourceClass.INTEGRATION, "api", BookmakerCapability.BALANCE_READ)


def test_historical_fixture_never_grants_execution_authority() -> None:
    historical = _bound_historical(_profile("fixture", BookmakerCapability.PLACE_BET))
    matrix = CapabilitySourceMatrix((historical,))
    assert historical.state_of(BookmakerCapability.PLACE_BET) is BookmakerCapabilityState.SUPPORTED
    assert matrix.state_of(CapabilitySourceClass.HISTORICAL_FIXTURE, "fixture", BookmakerCapability.PLACE_BET) is BookmakerCapabilityState.SUPPORTED
    assert historical.provider_write_authorized is False
    assert historical.execution_authorized is False
    assert historical.real_money_execution is False
    assert matrix.provider_write_authorized is False
    assert matrix.execution_authorized is False
    assert matrix.real_money_execution is False


def test_nested_bookmaker_capability_fact_subclass_is_rejected_before_dispatch() -> None:
    class ForgedFact(BookmakerCapabilityFact):
        def __getattribute__(self, name: str):
            if name == "capability":
                return BookmakerCapability.PLACE_BET
            if name == "state":
                return BookmakerCapabilityState.SUPPORTED
            return super().__getattribute__(name)

    forged = ForgedFact(BookmakerCapability.BALANCE_READ, BookmakerCapabilityState.UNSUPPORTED)
    profile = BookmakerCapabilityProfile(
        venue_id="betfair", account_id="acct-1", adapter_id="fixture",
        adapter_version="1.0", profile_version=1, facts=(forged,), observed_at=_TS,
        source_ref="forged-profile", source_payload_sha256=_HASH_A,
    )
    with pytest.raises(CapabilitySourceBoundaryError, match="exact BookmakerCapabilityFact"):
        _bound_historical(profile)
