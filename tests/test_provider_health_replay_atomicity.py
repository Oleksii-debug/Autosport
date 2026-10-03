from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from autosport.provider_health_authority import (
    ProviderHealthAuthority,
    ProviderHealthError,
    ProviderHealthEvent,
    ProviderHealthOutcome,
    ProviderHealthStatus,
)


_T0 = datetime(2026, 9, 23, tzinfo=timezone.utc)


def _event(
    sequence: int,
    event_id: str,
    outcome: ProviderHealthOutcome,
    *,
    seconds: int | None = None,
) -> ProviderHealthEvent:
    return ProviderHealthEvent(
        provider_id="betfair",
        sequence=sequence,
        event_id=event_id,
        occurred_at=_T0 + timedelta(seconds=sequence if seconds is None else seconds),
        outcome=outcome,
    )


def test_failed_replay_does_not_commit_a_valid_prefix_from_pristine_state() -> None:
    authority = ProviderHealthAuthority()
    first = _event(1, "event-1", ProviderHealthOutcome.SUCCESS)
    conflicting = _event(1, "event-2", ProviderHealthOutcome.TIMEOUT)

    with pytest.raises(ProviderHealthError, match="conflicting reuse of sequence"):
        authority.replay([first, conflicting])

    state = authority.state("betfair")
    assert state.status is ProviderHealthStatus.UNKNOWN
    assert state.last_sequence == 0

    # Failed replay must not leak hidden event-id/sequence indexes either.
    committed = authority.apply(first)
    assert committed.status is ProviderHealthStatus.HEALTHY
    assert committed.last_sequence == 1


def test_failed_replay_preserves_preexisting_state_and_event_indexes() -> None:
    authority = ProviderHealthAuthority()
    original = authority.apply(
        _event(1, "event-1", ProviderHealthOutcome.SUCCESS)
    )
    next_event = _event(2, "event-2", ProviderHealthOutcome.TIMEOUT)
    conflicting = _event(2, "event-3", ProviderHealthOutcome.SUCCESS)

    with pytest.raises(ProviderHealthError, match="conflicting reuse of sequence"):
        authority.replay([next_event, conflicting])

    assert authority.state("betfair") == original

    # The valid prefix remains admissible after rollback, proving replay did not
    # retain either the staged state transition or its identity indexes.
    committed = authority.apply(next_event)
    assert committed.status is ProviderHealthStatus.DEGRADED
    assert committed.last_sequence == 2


def test_successful_replay_commits_the_complete_validated_batch() -> None:
    authority = ProviderHealthAuthority()
    events = [
        _event(1, "event-1", ProviderHealthOutcome.SUCCESS),
        _event(2, "event-2", ProviderHealthOutcome.TIMEOUT),
        _event(3, "event-3", ProviderHealthOutcome.TRANSPORT_FAILURE),
    ]

    states = authority.replay(events)

    assert states["betfair"] == authority.state("betfair")
    assert authority.state("betfair").status is ProviderHealthStatus.DEGRADED
    assert authority.state("betfair").last_sequence == 3

    # Exact durable replay remains idempotent after the atomic batch commits.
    assert authority.replay(events)["betfair"] == authority.state("betfair")
