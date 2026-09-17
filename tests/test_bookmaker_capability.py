from decimal import Decimal

import pytest

from autosport.bookmaker_capability import (
    BookmakerAccountSnapshot,
    BookmakerBalanceObservation,
    BookmakerCapability,
    BookmakerCapabilityError,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
    BookmakerPositionObservation,
    BookmakerPositionState,
    ReadOnlyBookmakerAdapter,
    UnknownBookmakerCapability,
    UnsupportedBookmakerCapability,
)


_TS = "2026-09-17T16:00:00+00:00"
_HASH = "a" * 64


def _profile(
    *supported: BookmakerCapability,
    unsupported: tuple[BookmakerCapability, ...] = (),
    facts: tuple[BookmakerCapabilityFact, ...] | None = None,
    version: int = 1,
) -> BookmakerCapabilityProfile:
    if facts is None:
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
        venue_id="book-a",
        account_id="acct-a",
        adapter_id="adapter-a",
        adapter_version="1.0",
        profile_version=version,
        facts=facts,
        observed_at=_TS,
        source_ref="provider-capability-probe",
        source_payload_sha256=_HASH,
    )


def _balance(**overrides) -> BookmakerBalanceObservation:
    values = {
        "venue_id": "book-a",
        "account_id": "acct-a",
        "adapter_id": "adapter-a",
        "observation_id": "balance-1",
        "currency": "EUR",
        "available_balance": Decimal("90"),
        "observed_at": _TS,
        "source_payload_sha256": _HASH,
        "total_balance": Decimal("100"),
        "total_balance_source_ref": "provider-total-balance-field",
    }
    values.update(overrides)
    return BookmakerBalanceObservation(**values)


def _position(
    state: BookmakerPositionState,
    **overrides,
) -> BookmakerPositionObservation:
    values = {
        "venue_id": "book-a",
        "account_id": "acct-a",
        "adapter_id": "adapter-a",
        "observation_id": f"position-{state.value}-1",
        "external_position_id": "external-1",
        "state": state,
        "stake": Decimal("10"),
        "currency": "EUR",
        "observed_at": _TS,
        "source_payload_sha256": _HASH,
        "decimal_odds": Decimal("2.00"),
    }
    values.update(overrides)
    return BookmakerPositionObservation(**values)


def test_profile_distinguishes_unknown_from_unsupported() -> None:
    profile = _profile(
        BookmakerCapability.BALANCE_READ,
        unsupported=(BookmakerCapability.CANCEL_BET,),
    )

    assert (
        profile.state_of(BookmakerCapability.BALANCE_READ)
        is BookmakerCapabilityState.SUPPORTED
    )
    assert (
        profile.state_of(BookmakerCapability.LIVE_QUOTES_READ)
        is BookmakerCapabilityState.UNKNOWN
    )
    assert (
        profile.state_of(BookmakerCapability.CANCEL_BET)
        is BookmakerCapabilityState.UNSUPPORTED
    )

    with pytest.raises(UnknownBookmakerCapability, match="is unknown"):
        profile.require(BookmakerCapability.LIVE_QUOTES_READ)
    with pytest.raises(UnsupportedBookmakerCapability, match="does not support"):
        profile.require(BookmakerCapability.CANCEL_BET)


def test_profile_identity_is_deterministic_across_fact_order() -> None:
    one = BookmakerCapabilityFact(
        BookmakerCapability.BALANCE_READ,
        BookmakerCapabilityState.SUPPORTED,
    )
    two = BookmakerCapabilityFact(
        BookmakerCapability.LIMITS_READ,
        BookmakerCapabilityState.UNSUPPORTED,
    )
    assert _profile(facts=(one, two)).profile_id == _profile(
        facts=(two, one)
    ).profile_id


def test_profile_rejects_duplicate_capability_facts() -> None:
    fact = BookmakerCapabilityFact(
        BookmakerCapability.BALANCE_READ,
        BookmakerCapabilityState.SUPPORTED,
    )
    with pytest.raises(BookmakerCapabilityError, match="duplicate capability fact"):
        _profile(facts=(fact, fact))


def test_profile_requires_provenance_and_positive_version() -> None:
    with pytest.raises(BookmakerCapabilityError, match="positive integer"):
        BookmakerCapabilityProfile(
            venue_id="book-a",
            account_id="acct-a",
            adapter_id="adapter-a",
            adapter_version="1",
            profile_version=0,
            facts=(),
            observed_at=_TS,
            source_ref="probe",
            source_payload_sha256=_HASH,
        )
    with pytest.raises(BookmakerCapabilityError, match="source_ref"):
        BookmakerCapabilityProfile(
            venue_id="book-a",
            account_id="acct-a",
            adapter_id="adapter-a",
            adapter_version="1",
            profile_version=1,
            facts=(),
            observed_at=_TS,
            source_ref="",
            source_payload_sha256=_HASH,
        )


def test_balance_observation_accepts_provider_native_funds_without_total() -> None:
    observation = _balance(
        total_balance=None,
        total_balance_source_ref=None,
        exposure=Decimal("-12.50"),
        retained_commission=Decimal("1.25"),
        exposure_limit=Decimal("-500"),
    )

    assert observation.available_balance == Decimal("90")
    assert observation.total_balance is None
    assert observation.total_balance_source_ref is None
    assert observation.exposure == Decimal("-12.50")
    assert observation.retained_commission == Decimal("1.25")
    assert observation.exposure_limit == Decimal("-500")


def test_balance_observation_rejects_impossible_or_non_finite_money() -> None:
    with pytest.raises(BookmakerCapabilityError, match="cannot exceed"):
        _balance(available_balance=Decimal("101"))
    with pytest.raises(BookmakerCapabilityError, match="finite Decimal"):
        _balance(total_balance=Decimal("NaN"))
    with pytest.raises(BookmakerCapabilityError, match="finite Decimal"):
        _balance(
            total_balance=None,
            total_balance_source_ref=None,
            exposure=Decimal("NaN"),
        )


def test_total_balance_requires_explicit_provenance() -> None:
    with pytest.raises(
        BookmakerCapabilityError,
        match="total_balance requires explicit",
    ):
        _balance(total_balance_source_ref=None)
    with pytest.raises(
        BookmakerCapabilityError,
        match="total_balance_source_ref requires total_balance",
    ):
        _balance(total_balance=None)


def test_evidence_requires_timezone_and_payload_hash() -> None:
    with pytest.raises(BookmakerCapabilityError, match="timezone"):
        _balance(observed_at="2026-09-17T16:00:00")
    with pytest.raises(BookmakerCapabilityError, match="SHA-256"):
        _balance(source_payload_sha256="not-a-hash")


def test_snapshot_binds_balance_to_profile_identity() -> None:
    profile = _profile(BookmakerCapability.BALANCE_READ)
    with pytest.raises(BookmakerCapabilityError, match="identity"):
        BookmakerAccountSnapshot(
            profile=profile,
            observed_capabilities=frozenset(
                {BookmakerCapability.BALANCE_READ}
            ),
            observed_at=_TS,
            balance=_balance(account_id="wrong-account"),
        )


def test_snapshot_requires_balance_exactly_when_balance_was_observed() -> None:
    profile = _profile(BookmakerCapability.BALANCE_READ)
    with pytest.raises(BookmakerCapabilityError, match="exactly"):
        BookmakerAccountSnapshot(
            profile=profile,
            observed_capabilities=frozenset(
                {BookmakerCapability.BALANCE_READ}
            ),
            observed_at=_TS,
        )
    with pytest.raises(BookmakerCapabilityError, match="exactly"):
        BookmakerAccountSnapshot(
            profile=profile,
            observed_capabilities=frozenset(),
            observed_at=_TS,
            balance=_balance(),
        )


def test_snapshot_fails_closed_when_observed_capability_is_unknown() -> None:
    profile = _profile()
    with pytest.raises(UnknownBookmakerCapability, match="open_positions_read"):
        BookmakerAccountSnapshot(
            profile=profile,
            observed_capabilities=frozenset(
                {BookmakerCapability.OPEN_POSITIONS_READ}
            ),
            observed_at=_TS,
        )


def test_position_records_require_matching_state_and_observed_capability() -> None:
    profile = _profile(
        BookmakerCapability.OPEN_POSITIONS_READ,
        BookmakerCapability.SETTLED_POSITIONS_READ,
    )
    settled = _position(BookmakerPositionState.SETTLED)
    with pytest.raises(
        BookmakerCapabilityError,
        match="open_positions contains a settled",
    ):
        BookmakerAccountSnapshot(
            profile=profile,
            observed_capabilities=frozenset(
                {BookmakerCapability.OPEN_POSITIONS_READ}
            ),
            observed_at=_TS,
            open_positions=(settled,),
        )

    opened = _position(BookmakerPositionState.OPEN)
    with pytest.raises(
        BookmakerCapabilityError,
        match="unless open_positions_read",
    ):
        BookmakerAccountSnapshot(
            profile=profile,
            observed_capabilities=frozenset(),
            observed_at=_TS,
            open_positions=(opened,),
        )


def test_duplicate_position_evidence_identity_is_rejected() -> None:
    profile = _profile(BookmakerCapability.OPEN_POSITIONS_READ)
    position = _position(BookmakerPositionState.OPEN)
    with pytest.raises(
        BookmakerCapabilityError,
        match="duplicate observation_id",
    ):
        BookmakerAccountSnapshot(
            profile=profile,
            observed_capabilities=frozenset(
                {BookmakerCapability.OPEN_POSITIONS_READ}
            ),
            observed_at=_TS,
            open_positions=(position, position),
        )


def test_adapter_protocol_remains_read_only_and_structural() -> None:
    class Adapter:
        def capability_profile(self) -> BookmakerCapabilityProfile:
            return _profile(BookmakerCapability.BALANCE_READ)

        def read_account_snapshot(
            self,
            requested_capabilities: frozenset[BookmakerCapability],
            /,
        ) -> BookmakerAccountSnapshot:
            profile = self.capability_profile()
            return BookmakerAccountSnapshot(
                profile=profile,
                observed_capabilities=requested_capabilities,
                observed_at=_TS,
                balance=(
                    _balance()
                    if BookmakerCapability.BALANCE_READ
                    in requested_capabilities
                    else None
                ),
            )

    assert isinstance(Adapter(), ReadOnlyBookmakerAdapter)
    assert "place" not in ReadOnlyBookmakerAdapter.__dict__
    assert "cancel" not in ReadOnlyBookmakerAdapter.__dict__
    assert "cashout" not in ReadOnlyBookmakerAdapter.__dict__
