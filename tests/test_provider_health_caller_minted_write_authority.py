from __future__ import annotations

from datetime import datetime, timezone

import pytest

from autosport.provider_health_authority import (
    ProviderHealthAuthority,
    ProviderHealthError,
    ProviderHealthEvent,
    ProviderHealthOutcome,
)


def test_caller_success_event_cannot_mint_provider_write_authority() -> None:
    """Positive write authority needs provider-origin health evidence.

    A syntactically valid event assembled by an ordinary caller is not proof that
    any provider request actually succeeded. The generic health state machine may
    consume such events for structural simulation, but they must not be enough to
    unlock a positive write binding.
    """

    authority = ProviderHealthAuthority()
    fabricated_success = ProviderHealthEvent(
        provider_id="betfair",
        sequence=1,
        event_id="caller-invented-success",
        occurred_at=datetime(2026, 9, 22, 14, 35, tzinfo=timezone.utc),
        outcome=ProviderHealthOutcome.SUCCESS,
    )

    authority.apply(fabricated_success)

    with pytest.raises(ProviderHealthError):
        authority.bind_write_decision(
            provider_id="betfair",
            decision_id="decision-that-must-not-gain-write-authority",
        )
