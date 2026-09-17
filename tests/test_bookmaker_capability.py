from decimal import Decimal

import pytest

from autosport.bookmaker_capability import (
    BookmakerAccountSnapshot,
    BookmakerBalanceObservation,
    BookmakerCapability,
    BookmakerCapabilityError,
    BookmakerCapabilityProfile,
    BookmakerPositionObservation,
    BookmakerPositionState,
    ReadOnlyBookmakerAdapter,
    UnsupportedBookmakerCapability,
)


_TS = "2026-09-17T16:00:00+00:00"
_HASH = "a" * 64


def _profile(*capabilities: BookmakerCapability) -> BookmakerCapabilityProfile:
    return BookmakerCapabilityProfile(
        venue_id="book-a",
        account_id="acct-a",
        adapter_id="adapter-a",
        adapter_version="1.0",
        capabilities=frozenset(capabilities),
        observed_at=_TS,
    )


def _balance(**overrides) -> BookmakerBalanceObservation:
    values = {
        "venue_id": "book-a",
        "account_id": "acct-a",
        "adapter_id": "adapter-a",
        "observation_id": "balance-1",
        "currency": "EUR",
        "available_balance": Decimal("90"),
        "total_balance": Decimal("100"),
        "observed_at": _TS,
        "source_payload_sha256": _HASH,
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


def test_profile_fails_closed_for_unadvertised_capability() -> None:
    profile = _profile(BookmakerCapability.BALANCE_READ)
    assert profile.supports(BookmakerCapability.BALANCE_READ)
    with pytest.raises(
        UnsupportedBookmakerCapability,
        match="open_positions_read",
    ):
        profile.require(BookmakerCapability.OPEN_POSITIONS_READ)


def test_balance_observation_rejects_impossible_or_non_finite_money() -> None:
    with pytest.raises(BookmakerCapabilityError, match="cannot exceed"):
        _balance(available_balance=Decimal("101"))
    with pytest.raises(BookmakerCapabilityError, match="finite Decimal"):
        _balance(total_balance=Decimal("NaN"))


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


def test_empty_open_position_result_is_valid_when_capability_was_observed() -> None:
    profile = _profile(BookmakerCapability.OPEN_POSITIONS_READ)
    snapshot = BookmakerAccountSnapshot(
        profile=profile,
        observed_capabilities=frozenset(
            {BookmakerCapability.OPEN_POSITIONS_READ}
        ),
        observed_at=_TS,
        open_positions=(),
    )
    assert snapshot.open_positions == ()


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


def test_observed_capability_must_be_advertised_by_profile() -> None:
    profile = _profile()
    with pytest.raises(
        UnsupportedBookmakerCapability,
        match="settled_positions_read",
    ):
        BookmakerAccountSnapshot(
            profile=profile,
            observed_capabilities=frozenset(
                {BookmakerCapability.SETTLED_POSITIONS_READ}
            ),
            observed_at=_TS,
        )


def test_adapter_protocol_is_read_only_and_structural() -> None:
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
