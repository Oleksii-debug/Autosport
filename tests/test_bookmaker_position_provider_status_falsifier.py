from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.bookmaker_capability import (
    BookmakerCapabilityError,
    BookmakerPositionObservation,
    BookmakerPositionState,
)


_SOURCE_SHA = "a" * 64


def _position(**overrides: object) -> BookmakerPositionObservation:
    values: dict[str, object] = {
        "venue_id": "provider-a",
        "account_id": "acct-1",
        "adapter_id": "adapter-v1",
        "observation_id": "obs-1",
        "external_position_id": "position-1",
        "state": BookmakerPositionState.OPEN,
        "currency": "USD",
        "observed_at": "2026-09-21T20:00:00+00:00",
        "source_payload_sha256": _SOURCE_SHA,
        "provider_amount": Decimal("10"),
        "provider_amount_semantics": "backer_stake",
    }
    values.update(overrides)
    return BookmakerPositionObservation(**values)  # type: ignore[arg-type]


def test_legacy_position_without_provider_status_remains_compatible() -> None:
    position = _position()

    assert position.state is BookmakerPositionState.OPEN
    assert position.provider_amount == Decimal("10")


def test_provider_native_push_variants_are_preserved_verbatim() -> None:
    for status in ("PUSH", "PUSH_WIN", "PUSH_LOSE"):
        position = _position(
            observation_id=f"obs-{status}",
            external_position_id=f"position-{status}",
            provider_status=status,
        )

        assert position.provider_status == status


def test_unknown_well_formed_provider_status_remains_opaque_evidence() -> None:
    position = _position(provider_status="PROVIDER_FUTURE_STATUS_V7")

    assert position.provider_status == "PROVIDER_FUTURE_STATUS_V7"


@pytest.mark.parametrize("provider_status", ["", " PUSH_WIN", "PUSH_WIN ", "   "])
def test_empty_or_untrimmed_provider_status_fails_closed(provider_status: str) -> None:
    with pytest.raises(BookmakerCapabilityError, match="provider_status"):
        _position(provider_status=provider_status)


@pytest.mark.parametrize("provider_status", [7, True, object()])
def test_non_string_provider_status_fails_closed(provider_status: object) -> None:
    with pytest.raises(BookmakerCapabilityError, match="provider_status"):
        _position(provider_status=provider_status)


def test_provider_status_is_evidence_only_and_cannot_mint_settlement_or_return() -> None:
    position = _position(provider_status="WIN")

    assert position.provider_status == "WIN"
    assert position.state is BookmakerPositionState.OPEN
    assert position.gross_return is None


def test_provider_status_does_not_rewrite_explicit_canonical_state() -> None:
    settled = _position(
        observation_id="obs-settled",
        external_position_id="position-settled",
        state=BookmakerPositionState.SETTLED,
        provider_status="PUSH_LOSE",
        gross_return=Decimal("0"),
    )

    assert settled.provider_status == "PUSH_LOSE"
    assert settled.state is BookmakerPositionState.SETTLED
    assert settled.gross_return == Decimal("0")
