from decimal import Decimal

import pytest

from autosport.bookmaker_capability import (
    BookmakerAccountSnapshot,
    BookmakerCapability,
    BookmakerCapabilityError,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
    BookmakerPositionObservation,
    BookmakerPositionState,
)


_TS = "2026-09-17T16:00:00+00:00"
_HASH = "a" * 64


def test_snapshot_rejects_same_provider_position_across_open_and_settled() -> None:
    profile = BookmakerCapabilityProfile(
        venue_id="book-a",
        account_id="acct-a",
        adapter_id="adapter-a",
        adapter_version="1.0",
        profile_version=1,
        facts=(
            BookmakerCapabilityFact(
                BookmakerCapability.OPEN_POSITIONS_READ,
                BookmakerCapabilityState.SUPPORTED,
            ),
            BookmakerCapabilityFact(
                BookmakerCapability.SETTLED_POSITIONS_READ,
                BookmakerCapabilityState.SUPPORTED,
            ),
        ),
        observed_at=_TS,
        source_ref="provider-capability-probe",
        source_payload_sha256=_HASH,
    )
    opened = BookmakerPositionObservation(
        venue_id="book-a",
        account_id="acct-a",
        adapter_id="adapter-a",
        observation_id="open-observation",
        external_position_id="provider-position-1",
        state=BookmakerPositionState.OPEN,
        currency="EUR",
        observed_at=_TS,
        source_payload_sha256=_HASH,
        provider_amount=Decimal("10.00"),
        provider_amount_semantics="backer_stake",
        provider_side="BACK",
        decimal_odds=Decimal("2.00"),
    )
    settled = BookmakerPositionObservation(
        venue_id="book-a",
        account_id="acct-a",
        adapter_id="adapter-a",
        observation_id="settled-observation",
        external_position_id="provider-position-1",
        state=BookmakerPositionState.SETTLED,
        currency="EUR",
        observed_at=_TS,
        source_payload_sha256=_HASH,
        provider_amount=Decimal("10.00"),
        provider_amount_semantics="backer_stake",
        provider_side="BACK",
        decimal_odds=Decimal("2.00"),
        gross_return=Decimal("20.00"),
    )

    with pytest.raises(
        BookmakerCapabilityError,
        match="same external_position_id provider-position-1",
    ):
        BookmakerAccountSnapshot(
            profile=profile,
            observed_capabilities=frozenset(
                {
                    BookmakerCapability.OPEN_POSITIONS_READ,
                    BookmakerCapability.SETTLED_POSITIONS_READ,
                }
            ),
            observed_at=_TS,
            open_positions=(opened,),
            settled_positions=(settled,),
        )
