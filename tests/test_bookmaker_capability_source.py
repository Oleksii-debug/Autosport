from autosport.bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
    UnknownBookmakerCapability,
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


def _profile(
    adapter_id: str,
    *supported: BookmakerCapability,
    unsupported: tuple[BookmakerCapability, ...] = (),
) -> BookmakerCapabilityProfile:
    facts = tuple(
        BookmakerCapabilityFact(
            capability=capability,
            state=BookmakerCapabilityState.SUPPORTED,
        )
        for capability in supported
    ) + tuple(
        BookmakerCapabilityFact(
            capability=capability,
            state=BookmakerCapabilityState.UNSUPPORTED,
        )
        for capability in unsupported
    )
    return BookmakerCapabilityProfile(
        venue_id="betfair",
        account_id="acct-1",
        adapter_id=adapter_id,
        adapter_version="1.0",
        profile_version=1,
        facts=facts,
        observed_at=_TS,
        source_ref=f"{adapter_id}-capability-probe",
        source_payload_sha256=_HASH_A,
    )


def _bound(
    profile: BookmakerCapabilityProfile,
    source_class: CapabilitySourceClass,
    *,
    digest: str = _HASH_B,
) -> SourceBoundCapabilityProfile:
    return SourceBoundCapabilityProfile(
        profile=profile,
        source_class=source_class,
        observed_at=_TS,
        source_ref=f"{source_class.value}-binding",
        source_payload_sha256=digest,
    )


def test_official_supported_fact_does_not_transfer_to_browser_fallback() -> None:
    official = _bound(
        _profile("api-adapter", BookmakerCapability.PLACE_BET),
        CapabilitySourceClass.OFFICIAL_API,
    )
    browser = _bound(
        _profile("browser-adapter"),
        CapabilitySourceClass.BROWSER_UI_ADAPTER,
    )
    matrix = CapabilitySourceMatrix((official, browser))

    assert (
        matrix.state_of(
            CapabilitySourceClass.OFFICIAL_API,
            "api-adapter",
            BookmakerCapability.PLACE_BET,
        )
        is BookmakerCapabilityState.SUPPORTED
    )
    assert (
        matrix.state_of(
            CapabilitySourceClass.BROWSER_UI_ADAPTER,
            "browser-adapter",
            BookmakerCapability.PLACE_BET,
        )
        is BookmakerCapabilityState.UNKNOWN
    )
    with pytest.raises(UnknownBookmakerCapability, match="browser_ui_adapter"):
        matrix.require(
            CapabilitySourceClass.BROWSER_UI_ADAPTER,
            "browser-adapter",
            BookmakerCapability.PLACE_BET,
        )


def test_browser_market_read_does_not_imply_authenticated_order_read() -> None:
    browser = _bound(
        _profile("browser-adapter", BookmakerCapability.LIVE_QUOTES_READ),
        CapabilitySourceClass.BROWSER_UI_ADAPTER,
    )
    matrix = CapabilitySourceMatrix((browser,))

    assert (
        matrix.state_of(
            CapabilitySourceClass.BROWSER_UI_ADAPTER,
            "browser-adapter",
            BookmakerCapability.LIVE_QUOTES_READ,
        )
        is BookmakerCapabilityState.SUPPORTED
    )
    assert (
        matrix.state_of(
            CapabilitySourceClass.BROWSER_UI_ADAPTER,
            "browser-adapter",
            BookmakerCapability.BET_READBACK,
        )
        is BookmakerCapabilityState.UNKNOWN
    )


def test_missing_fallback_adapter_is_unknown_not_borrowed() -> None:
    official = _bound(
        _profile("api-adapter", BookmakerCapability.BALANCE_READ),
        CapabilitySourceClass.OFFICIAL_API,
    )
    matrix = CapabilitySourceMatrix((official,))

    assert (
        matrix.state_of(
            CapabilitySourceClass.BROWSER_UI_ADAPTER,
            "browser-adapter",
            BookmakerCapability.BALANCE_READ,
        )
        is BookmakerCapabilityState.UNKNOWN
    )


def test_source_class_changes_binding_identity_for_same_profile() -> None:
    profile = _profile("shared-adapter", BookmakerCapability.LIVE_QUOTES_READ)
    official = _bound(profile, CapabilitySourceClass.OFFICIAL_API)
    historical = _bound(profile, CapabilitySourceClass.HISTORICAL_FIXTURE)

    assert official.binding_id != historical.binding_id
    assert official.to_canonical_dict()["source_class"] == "official_api"
    assert historical.to_canonical_dict()["source_class"] == "historical_fixture"


def test_matrix_identity_is_independent_of_binding_input_order() -> None:
    official = _bound(
        _profile("api-adapter", BookmakerCapability.BALANCE_READ),
        CapabilitySourceClass.OFFICIAL_API,
    )
    browser = _bound(
        _profile("browser-adapter", BookmakerCapability.LIVE_QUOTES_READ),
        CapabilitySourceClass.BROWSER_UI_ADAPTER,
    )

    assert CapabilitySourceMatrix((official, browser)).matrix_id == (
        CapabilitySourceMatrix((browser, official)).matrix_id
    )


def test_duplicate_source_class_and_adapter_is_rejected() -> None:
    first = _bound(
        _profile("api-adapter", BookmakerCapability.BALANCE_READ),
        CapabilitySourceClass.OFFICIAL_API,
        digest=_HASH_A,
    )
    second = _bound(
        _profile("api-adapter", BookmakerCapability.LIMITS_READ),
        CapabilitySourceClass.OFFICIAL_API,
        digest=_HASH_B,
    )

    with pytest.raises(
        CapabilitySourceBoundaryError,
        match="duplicate source_class/adapter_id",
    ):
        CapabilitySourceMatrix((first, second))


def test_matrix_rejects_cross_account_aggregation() -> None:
    official = _bound(
        _profile("api-adapter", BookmakerCapability.BALANCE_READ),
        CapabilitySourceClass.OFFICIAL_API,
    )
    other_profile = BookmakerCapabilityProfile(
        venue_id="betfair",
        account_id="acct-2",
        adapter_id="browser-adapter",
        adapter_version="1.0",
        profile_version=1,
        facts=(),
        observed_at=_TS,
        source_ref="browser-adapter-capability-probe",
        source_payload_sha256=_HASH_A,
    )
    browser = _bound(
        other_profile,
        CapabilitySourceClass.BROWSER_UI_ADAPTER,
    )

    with pytest.raises(
        CapabilitySourceBoundaryError,
        match="one exact venue/account",
    ):
        CapabilitySourceMatrix((official, browser))


def test_source_binding_cannot_predate_capability_profile() -> None:
    profile = BookmakerCapabilityProfile(
        venue_id="betfair",
        account_id="acct-1",
        adapter_id="api-adapter",
        adapter_version="1.0",
        profile_version=1,
        facts=(),
        observed_at="2026-09-21T10:00:01+00:00",
        source_ref="api-adapter-capability-probe",
        source_payload_sha256=_HASH_A,
    )

    with pytest.raises(
        CapabilitySourceBoundaryError,
        match="cannot predate",
    ):
        SourceBoundCapabilityProfile(
            profile=profile,
            source_class=CapabilitySourceClass.OFFICIAL_API,
            observed_at=_TS,
            source_ref="official_api-binding",
            source_payload_sha256=_HASH_B,
        )


def test_historical_fixture_never_grants_execution_authority() -> None:
    historical = _bound(
        _profile("fixture-adapter", BookmakerCapability.PLACE_BET),
        CapabilitySourceClass.HISTORICAL_FIXTURE,
    )
    matrix = CapabilitySourceMatrix((historical,))

    assert historical.state_of(BookmakerCapability.PLACE_BET) is (
        BookmakerCapabilityState.SUPPORTED
    )
    assert historical.provider_write_authorized is False
    assert historical.execution_authorized is False
    assert historical.real_money_execution is False
    assert matrix.provider_write_authorized is False
    assert matrix.execution_authorized is False
    assert matrix.real_money_execution is False
