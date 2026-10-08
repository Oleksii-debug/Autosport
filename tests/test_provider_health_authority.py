from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from autosport.provider_health_authority import (
    FallbackReadEvidence,
    ProviderHealthAuthority,
    ProviderHealthError,
    ProviderHealthEvent,
    ProviderHealthOutcome,
    ProviderHealthPolicy,
    ProviderHealthStatus,
    ProviderWriteBinding,
)


BASE = datetime(2026, 9, 22, 14, 0, tzinfo=timezone.utc)


def event(
    sequence: int,
    outcome: ProviderHealthOutcome,
    *,
    provider: str = "betfair",
    event_id: str | None = None,
    at: datetime | None = None,
) -> ProviderHealthEvent:
    return ProviderHealthEvent(
        provider_id=provider,
        sequence=sequence,
        event_id=event_id or f"e-{sequence}",
        occurred_at=at or (BASE + timedelta(seconds=sequence)),
        outcome=outcome,
    )


def healthy(authority: ProviderHealthAuthority, provider: str = "betfair"):
    return authority.apply(event(1, ProviderHealthOutcome.SUCCESS, provider=provider))


def test_initial_state_is_unknown_and_pristine() -> None:
    authority = ProviderHealthAuthority()
    state = authority.state("betfair")
    assert state.status is ProviderHealthStatus.UNKNOWN
    assert state.health_epoch == 0
    assert state.last_sequence == 0


def test_first_success_establishes_healthy_epoch() -> None:
    authority = ProviderHealthAuthority()
    state = healthy(authority)
    assert state.status is ProviderHealthStatus.HEALTHY
    assert state.health_epoch == 1


def test_healthy_success_does_not_rotate_epoch() -> None:
    authority = ProviderHealthAuthority()
    first = healthy(authority)
    second = authority.apply(event(2, ProviderHealthOutcome.SUCCESS))
    assert second.status is ProviderHealthStatus.HEALTHY
    assert second.health_epoch == first.health_epoch


def test_timeout_degrades_then_threshold_opens() -> None:
    authority = ProviderHealthAuthority(ProviderHealthPolicy(operational_failures_to_open=3))
    healthy(authority)
    one = authority.apply(event(2, ProviderHealthOutcome.TIMEOUT))
    two = authority.apply(event(3, ProviderHealthOutcome.TRANSPORT_FAILURE))
    three = authority.apply(event(4, ProviderHealthOutcome.TIMEOUT))
    assert one.status is ProviderHealthStatus.DEGRADED
    assert one.consecutive_operational_failures == 1
    assert two.status is ProviderHealthStatus.DEGRADED
    assert two.consecutive_operational_failures == 2
    assert three.status is ProviderHealthStatus.OPEN
    assert three.consecutive_operational_failures == 3


@pytest.mark.parametrize("outcome", [ProviderHealthOutcome.AUTH_FAILURE, ProviderHealthOutcome.RATE_LIMIT])
def test_auth_and_rate_limit_open_immediately(outcome: ProviderHealthOutcome) -> None:
    authority = ProviderHealthAuthority()
    healthy(authority)
    state = authority.apply(event(2, outcome))
    assert state.status is ProviderHealthStatus.OPEN
    assert state.consecutive_operational_failures == 0


def test_open_state_is_not_downgraded_by_later_operational_failure() -> None:
    authority = ProviderHealthAuthority()
    healthy(authority)
    opened = authority.apply(event(2, ProviderHealthOutcome.AUTH_FAILURE))
    assert opened.status is ProviderHealthStatus.OPEN
    later = authority.apply(event(3, ProviderHealthOutcome.TIMEOUT))
    assert later.status is ProviderHealthStatus.OPEN
    assert later.consecutive_operational_failures == 1


def test_recovery_requires_configured_consecutive_successes() -> None:
    authority = ProviderHealthAuthority(ProviderHealthPolicy(consecutive_successes_to_recover=3))
    healthy(authority)
    opened = authority.apply(event(2, ProviderHealthOutcome.AUTH_FAILURE))
    assert opened.status is ProviderHealthStatus.OPEN
    one = authority.apply(event(3, ProviderHealthOutcome.SUCCESS))
    two = authority.apply(event(4, ProviderHealthOutcome.SUCCESS))
    three = authority.apply(event(5, ProviderHealthOutcome.SUCCESS))
    assert one.status is ProviderHealthStatus.OPEN
    assert one.consecutive_recovery_successes == 1
    assert two.status is ProviderHealthStatus.OPEN
    assert two.consecutive_recovery_successes == 2
    assert three.status is ProviderHealthStatus.HEALTHY
    assert three.consecutive_recovery_successes == 0


def test_failure_resets_partial_recovery_streak() -> None:
    authority = ProviderHealthAuthority(ProviderHealthPolicy(consecutive_successes_to_recover=2))
    healthy(authority)
    authority.apply(event(2, ProviderHealthOutcome.AUTH_FAILURE))
    one = authority.apply(event(3, ProviderHealthOutcome.SUCCESS))
    assert one.consecutive_recovery_successes == 1
    failed = authority.apply(event(4, ProviderHealthOutcome.TIMEOUT))
    assert failed.consecutive_recovery_successes == 0


def test_exact_duplicate_event_is_idempotent() -> None:
    authority = ProviderHealthAuthority()
    original = event(1, ProviderHealthOutcome.SUCCESS)
    first = authority.apply(original)
    second = authority.apply(original)
    assert second == first


def test_conflicting_event_id_reuse_is_rejected() -> None:
    authority = ProviderHealthAuthority()
    authority.apply(event(1, ProviderHealthOutcome.SUCCESS, event_id="same"))
    with pytest.raises(ProviderHealthError, match="event_id"):
        authority.apply(event(2, ProviderHealthOutcome.TIMEOUT, event_id="same"))


def test_conflicting_sequence_reuse_is_rejected() -> None:
    authority = ProviderHealthAuthority()
    authority.apply(event(1, ProviderHealthOutcome.SUCCESS, event_id="first"))
    with pytest.raises(ProviderHealthError, match="sequence"):
        authority.apply(event(1, ProviderHealthOutcome.SUCCESS, event_id="second"))


def test_unseen_sequence_rollback_is_rejected() -> None:
    authority = ProviderHealthAuthority()
    authority.apply(event(10, ProviderHealthOutcome.SUCCESS))
    with pytest.raises(ProviderHealthError, match="sequence rollback"):
        authority.apply(event(9, ProviderHealthOutcome.SUCCESS, event_id="unseen-lower"))


def test_time_rollback_is_rejected() -> None:
    authority = ProviderHealthAuthority()
    authority.apply(event(1, ProviderHealthOutcome.SUCCESS, at=BASE + timedelta(seconds=10)))
    with pytest.raises(ProviderHealthError, match="time rollback"):
        authority.apply(event(2, ProviderHealthOutcome.SUCCESS, at=BASE + timedelta(seconds=9)))


def test_equal_timestamps_are_allowed_when_sequence_advances() -> None:
    authority = ProviderHealthAuthority()
    authority.apply(event(1, ProviderHealthOutcome.SUCCESS, at=BASE))
    state = authority.apply(event(2, ProviderHealthOutcome.SUCCESS, at=BASE))
    assert state.last_sequence == 2


def test_provider_streams_are_independent() -> None:
    authority = ProviderHealthAuthority()
    healthy(authority, "book-a")
    opened = authority.apply(event(1, ProviderHealthOutcome.AUTH_FAILURE, provider="book-b"))
    assert authority.state("book-a").status is ProviderHealthStatus.HEALTHY
    assert opened.status is ProviderHealthStatus.OPEN


def test_write_binding_requires_healthy_provider() -> None:
    authority = ProviderHealthAuthority()
    with pytest.raises(ProviderHealthError, match="HEALTHY"):
        authority.bind_write_decision(provider_id="betfair", decision_id="d1")


def test_healthy_advisory_state_cannot_mint_provider_write_authority() -> None:
    authority = ProviderHealthAuthority()
    healthy(authority)
    authority.apply(event(2, ProviderHealthOutcome.SUCCESS))
    with pytest.raises(ProviderHealthError, match="provider-origin authority"):
        authority.bind_write_decision(provider_id="betfair", decision_id="d1")


def test_caller_constructed_matching_epoch_binding_is_never_current() -> None:
    authority = ProviderHealthAuthority()
    state = healthy(authority)
    forged = ProviderWriteBinding("betfair", state.health_epoch, "d1")
    assert not authority.write_binding_is_current(forged)
    authority.apply(event(2, ProviderHealthOutcome.TIMEOUT))
    assert not authority.write_binding_is_current(forged)


def test_recovery_rotates_epoch_but_still_cannot_mint_write_authority() -> None:
    authority = ProviderHealthAuthority(ProviderHealthPolicy(consecutive_successes_to_recover=2))
    first = healthy(authority)
    forged = ProviderWriteBinding("betfair", first.health_epoch, "d1")
    authority.apply(event(2, ProviderHealthOutcome.AUTH_FAILURE))
    authority.apply(event(3, ProviderHealthOutcome.SUCCESS))
    recovered = authority.apply(event(4, ProviderHealthOutcome.SUCCESS))
    assert recovered.status is ProviderHealthStatus.HEALTHY
    assert recovered.health_epoch > first.health_epoch
    assert not authority.write_binding_is_current(forged)
    with pytest.raises(ProviderHealthError, match="provider-origin authority"):
        authority.bind_write_decision(provider_id="betfair", decision_id="d2")


def test_caller_binding_cannot_use_healthy_fallback_or_original_provider() -> None:
    authority = ProviderHealthAuthority()
    state_a = healthy(authority, "book-a")
    healthy(authority, "book-b")
    forged = ProviderWriteBinding("book-a", state_a.health_epoch, "d1")
    assert not authority.write_binding_is_current(forged)
    assert not authority.write_binding_is_current(forged, target_provider_id="book-b")


def test_fallback_read_evidence_is_always_advisory_and_non_authorizing() -> None:
    evidence = FallbackReadEvidence(
        requested_provider_id="book-a",
        fallback_provider_id="book-b",
        observed_at=BASE,
        expires_at=BASE + timedelta(seconds=30),
        provenance_sha256="a" * 64,
        payload_sha256="b" * 64,
    )
    assert evidence.advisory_only is True
    assert evidence.write_authorized is False
    assert evidence.is_fresh(as_of=BASE + timedelta(seconds=10))


def test_stale_fallback_read_remains_non_authorizing() -> None:
    evidence = FallbackReadEvidence(
        requested_provider_id="book-a",
        fallback_provider_id="book-b",
        observed_at=BASE,
        expires_at=BASE + timedelta(seconds=30),
        provenance_sha256="a" * 64,
        payload_sha256="b" * 64,
    )
    assert not evidence.is_fresh(as_of=BASE + timedelta(seconds=31))
    assert evidence.write_authorized is False


def test_fallback_freshness_uses_half_open_expiry_boundary() -> None:
    evidence = FallbackReadEvidence(
        requested_provider_id="book-a",
        fallback_provider_id="book-b",
        observed_at=BASE,
        expires_at=BASE + timedelta(seconds=30),
        provenance_sha256="a" * 64,
        payload_sha256="b" * 64,
    )
    assert not evidence.is_fresh(as_of=BASE - timedelta(microseconds=1))
    assert evidence.is_fresh(as_of=BASE)
    assert evidence.is_fresh(as_of=BASE + timedelta(seconds=30) - timedelta(microseconds=1))
    assert not evidence.is_fresh(as_of=BASE + timedelta(seconds=30))


def test_fallback_evidence_rejects_same_provider_alias() -> None:
    with pytest.raises(ProviderHealthError, match="must differ"):
        FallbackReadEvidence(
            requested_provider_id="book-a",
            fallback_provider_id="book-a",
            observed_at=BASE,
            expires_at=BASE + timedelta(seconds=1),
            provenance_sha256="a" * 64,
            payload_sha256="b" * 64,
        )


def test_fallback_evidence_rejects_non_sha256_provenance() -> None:
    with pytest.raises(ProviderHealthError, match="SHA-256"):
        FallbackReadEvidence(
            requested_provider_id="book-a",
            fallback_provider_id="book-b",
            observed_at=BASE,
            expires_at=BASE + timedelta(seconds=1),
            provenance_sha256="not-a-digest",
            payload_sha256="b" * 64,
        )


def test_authority_rejects_hostile_policy_without_calling_bool() -> None:
    class HostilePolicy:
        def __bool__(self):
            raise RuntimeError("caller hook executed")

    with pytest.raises(ProviderHealthError, match="policy"):
        ProviderHealthAuthority(HostilePolicy())  # type: ignore[arg-type]


def test_policy_rejects_bool_and_zero_thresholds() -> None:
    for kwargs in (
        {"operational_failures_to_open": 0},
        {"consecutive_successes_to_recover": 0},
        {"operational_failures_to_open": True},
        {"consecutive_successes_to_recover": True},
    ):
        with pytest.raises(ProviderHealthError, match="positive exact integer"):
            ProviderHealthPolicy(**kwargs)


def test_event_rejects_naive_time() -> None:
    with pytest.raises(ProviderHealthError, match="timezone-aware"):
        event(1, ProviderHealthOutcome.SUCCESS, at=datetime(2026, 9, 22, 14, 0))


def test_event_rejects_string_subclass_provider_identity() -> None:
    class Hostile(str):
        pass

    with pytest.raises(ProviderHealthError, match="exact string"):
        ProviderHealthEvent(Hostile("betfair"), 1, "e1", BASE, ProviderHealthOutcome.SUCCESS)


def test_sequence_rejects_bool_and_out_of_range() -> None:
    with pytest.raises(ProviderHealthError, match="positive exact integer"):
        ProviderHealthEvent("betfair", True, "e1", BASE, ProviderHealthOutcome.SUCCESS)
    with pytest.raises(ProviderHealthError, match="signed 64-bit"):
        ProviderHealthEvent("betfair", (1 << 63), "e1", BASE, ProviderHealthOutcome.SUCCESS)


def test_replay_matches_incremental_application() -> None:
    events = [
        event(1, ProviderHealthOutcome.SUCCESS),
        event(2, ProviderHealthOutcome.TIMEOUT),
        event(3, ProviderHealthOutcome.SUCCESS),
        event(4, ProviderHealthOutcome.SUCCESS),
    ]
    a = ProviderHealthAuthority()
    for item in events:
        a.apply(item)
    b = ProviderHealthAuthority()
    b.replay(events)
    assert b.state("betfair") == a.state("betfair")


def test_tampered_copy_of_event_is_not_idempotent() -> None:
    authority = ProviderHealthAuthority()
    original = event(1, ProviderHealthOutcome.SUCCESS)
    authority.apply(original)
    tampered = replace(original, outcome=ProviderHealthOutcome.TIMEOUT)
    with pytest.raises(ProviderHealthError, match="conflicting reuse"):
        authority.apply(tampered)


def test_large_healthy_stream_keeps_epoch_stable() -> None:
    authority = ProviderHealthAuthority()
    initial = healthy(authority)
    for sequence in range(2, 2002):
        authority.apply(event(sequence, ProviderHealthOutcome.SUCCESS))
    final = authority.state("betfair")
    assert final.status is ProviderHealthStatus.HEALTHY
    assert final.health_epoch == initial.health_epoch
    assert final.last_sequence == 2001


def test_caller_success_event_cannot_mint_provider_write_authority() -> None:
    """Regression absorbed from dependent expected-RED PR #1607."""
    authority = ProviderHealthAuthority()
    fabricated_success = ProviderHealthEvent(
        provider_id="betfair",
        sequence=1,
        event_id="caller-invented-success",
        occurred_at=datetime(2026, 9, 22, 14, 35, tzinfo=timezone.utc),
        outcome=ProviderHealthOutcome.SUCCESS,
    )

    authority.apply(fabricated_success)

    with pytest.raises(ProviderHealthError, match="provider-origin authority"):
        authority.bind_write_decision(
            provider_id="betfair",
            decision_id="decision-that-must-not-gain-write-authority",
        )
