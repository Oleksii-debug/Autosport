from __future__ import annotations

from dataclasses import replace
from threading import Barrier, Thread

import pytest

from autosport.matchbook_session_lifecycle import (
    MatchbookSessionLifecycle,
    ReauthRole,
    ReauthStatus,
    SessionClockRollbackError,
    SessionLifecycleError,
    SessionReauthClaim,
    SessionState,
)


def ns(seconds: int) -> int:
    return seconds * 1_000_000_000


def active() -> MatchbookSessionLifecycle:
    lifecycle = MatchbookSessionLifecycle()
    lifecycle.record_login_200(generation_id="gen-1", monotonic_ns=ns(10))
    return lifecycle


def test_concurrent_401_claims_have_one_leader_and_shared_follower() -> None:
    lifecycle = active()
    worker_count = 12
    tickets = [
        lifecycle.capture_read_generation(monotonic_ns=ns(15))
        for _ in range(worker_count)
    ]
    gate = Barrier(worker_count + 1)
    claims: list[SessionReauthClaim] = []
    failures: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            gate.wait()
            claims.append(
                lifecycle.claim_read_reauth_after_401(
                    tickets[index], monotonic_ns=ns(20)
                )
            )
        except BaseException as exc:  # pragma: no cover - diagnostic collection
            failures.append(exc)

    threads = [Thread(target=worker, args=(index,)) for index in range(worker_count)]
    for thread in threads:
        thread.start()
    gate.wait()
    for thread in threads:
        thread.join()

    assert failures == []
    assert len(claims) == worker_count
    leaders = [claim for claim in claims if claim.role is ReauthRole.LEADER]
    followers = [claim for claim in claims if claim.role is ReauthRole.FOLLOWER]
    assert len(leaders) == 1
    assert len(followers) == worker_count - 1
    assert all(claim is followers[0] for claim in followers)
    assert lifecycle.state is SessionState.EXPIRED
    assert lifecycle.read_reauth_outcome(leaders[0]).status is ReauthStatus.IN_FLIGHT


def test_same_401_ticket_is_idempotent_within_epoch() -> None:
    lifecycle = active()
    ticket = lifecycle.capture_read_generation(monotonic_ns=ns(15))

    first = lifecycle.claim_read_reauth_after_401(ticket, monotonic_ns=ns(20))
    second = lifecycle.claim_read_reauth_after_401(ticket, monotonic_ns=ns(21))

    assert first is second
    assert first.role is ReauthRole.LEADER


def test_only_exact_leader_can_complete_and_followers_consume_success() -> None:
    lifecycle = active()
    leader_ticket = lifecycle.capture_read_generation(monotonic_ns=ns(15))
    follower_ticket = lifecycle.capture_read_generation(monotonic_ns=ns(16))
    leader = lifecycle.claim_read_reauth_after_401(leader_ticket, monotonic_ns=ns(20))
    follower = lifecycle.claim_read_reauth_after_401(
        follower_ticket, monotonic_ns=ns(21)
    )
    copied = replace(leader)
    caller_built = SessionReauthClaim(
        epoch_id=leader.epoch_id,
        generation_id=leader.generation_id,
        role=ReauthRole.LEADER,
    )

    for unauthorized in (follower, copied, caller_built):
        with pytest.raises(
            SessionLifecycleError, match="exact product-issued reauth leader"
        ):
            lifecycle.complete_read_reauth_success(
                unauthorized,
                successor_generation_id="gen-2",
                monotonic_ns=ns(30),
            )

    lifecycle.complete_read_reauth_success(
        leader,
        successor_generation_id="gen-2",
        monotonic_ns=ns(30),
    )

    assert lifecycle.generation_id == "gen-2"
    assert lifecycle.state is SessionState.ACTIVE
    assert lifecycle.is_active is True
    for claim in (leader, follower):
        outcome = lifecycle.read_reauth_outcome(claim)
        assert outcome.status is ReauthStatus.SUCCEEDED
        assert outcome.expired_generation_id == "gen-1"
        assert outcome.successor_generation_id == "gen-2"


def test_caller_constructed_or_already_committed_read_ticket_cannot_mint_reauth() -> None:
    lifecycle = active()
    issued = lifecycle.capture_read_generation(monotonic_ns=ns(15))
    copied = replace(issued)

    with pytest.raises(SessionLifecycleError, match="not issued here"):
        lifecycle.claim_read_reauth_after_401(copied, monotonic_ns=ns(20))

    assert (
        lifecycle.authorize_read_response_commit(issued, monotonic_ns=ns(20))
        == "gen-1"
    )
    with pytest.raises(SessionLifecycleError, match="not issued here"):
        lifecycle.claim_read_reauth_after_401(issued, monotonic_ns=ns(21))


def test_reauth_failure_is_shared_and_cannot_be_completed_twice() -> None:
    lifecycle = active()
    first = lifecycle.capture_read_generation(monotonic_ns=ns(15))
    second = lifecycle.capture_read_generation(monotonic_ns=ns(16))
    leader = lifecycle.claim_read_reauth_after_401(first, monotonic_ns=ns(20))
    follower = lifecycle.claim_read_reauth_after_401(second, monotonic_ns=ns(21))

    lifecycle.complete_read_reauth_failure(leader, monotonic_ns=ns(30))

    assert lifecycle.is_active is False
    assert lifecycle.state is SessionState.EXPIRED
    assert lifecycle.read_reauth_outcome(leader).status is ReauthStatus.FAILED
    assert lifecycle.read_reauth_outcome(follower).status is ReauthStatus.FAILED
    with pytest.raises(SessionLifecycleError, match="already complete"):
        lifecycle.complete_read_reauth_failure(leader, monotonic_ns=ns(31))


def test_403_invalidates_ticket_and_never_mints_reauth_authority() -> None:
    lifecycle = active()
    ticket = lifecycle.capture_read_generation(monotonic_ns=ns(15))
    lifecycle.record_get_session_result(
        generation_id="gen-1", http_status=403, monotonic_ns=ns(20)
    )

    with pytest.raises(SessionLifecycleError, match="not issued here"):
        lifecycle.claim_read_reauth_after_401(ticket, monotonic_ns=ns(21))


def test_ticket_from_different_generation_cannot_join_active_reauth_epoch() -> None:
    lifecycle = active()
    stale_ticket = lifecycle.capture_read_generation(monotonic_ns=ns(12))
    lifecycle.record_login_200(generation_id="gen-2", monotonic_ns=ns(13))
    leader_ticket = lifecycle.capture_read_generation(monotonic_ns=ns(14))
    lifecycle.claim_read_reauth_after_401(leader_ticket, monotonic_ns=ns(20))

    with pytest.raises(SessionLifecycleError):
        lifecycle.claim_read_reauth_after_401(stale_ticket, monotonic_ns=ns(21))


def test_late_inflight_401_follower_can_join_after_leader_success() -> None:
    lifecycle = active()
    leader_ticket = lifecycle.capture_read_generation(monotonic_ns=ns(15))
    late_ticket = lifecycle.capture_read_generation(monotonic_ns=ns(16))
    leader = lifecycle.claim_read_reauth_after_401(leader_ticket, monotonic_ns=ns(20))
    lifecycle.complete_read_reauth_success(
        leader, successor_generation_id="gen-2", monotonic_ns=ns(30)
    )

    follower = lifecycle.claim_read_reauth_after_401(
        late_ticket, monotonic_ns=ns(21)
    )

    assert follower.role is ReauthRole.FOLLOWER
    outcome = lifecycle.read_reauth_outcome(follower)
    assert outcome.status is ReauthStatus.SUCCEEDED
    assert outcome.successor_generation_id == "gen-2"


def test_invalid_old_ticket_cannot_erase_completed_epoch_before_late_follower() -> None:
    lifecycle = active()
    leader_ticket = lifecycle.capture_read_generation(monotonic_ns=ns(15))
    late_ticket = lifecycle.capture_read_generation(monotonic_ns=ns(16))
    leader = lifecycle.claim_read_reauth_after_401(leader_ticket, monotonic_ns=ns(20))
    lifecycle.complete_read_reauth_failure(leader, monotonic_ns=ns(30))

    copied_late_ticket = replace(late_ticket)
    with pytest.raises(SessionLifecycleError, match="not issued here"):
        lifecycle.claim_read_reauth_after_401(
            copied_late_ticket, monotonic_ns=ns(21)
        )

    follower = lifecycle.claim_read_reauth_after_401(
        late_ticket, monotonic_ns=ns(21)
    )
    assert lifecycle.read_reauth_outcome(follower).status is ReauthStatus.FAILED


def test_direct_login_cannot_bypass_inflight_singleflight_leader() -> None:
    lifecycle = active()
    ticket = lifecycle.capture_read_generation(monotonic_ns=ns(15))
    leader = lifecycle.claim_read_reauth_after_401(ticket, monotonic_ns=ns(20))

    with pytest.raises(
        SessionLifecycleError, match="must be completed by its exact leader"
    ):
        lifecycle.record_login_200(generation_id="gen-2", monotonic_ns=ns(30))

    assert lifecycle.read_reauth_outcome(leader).status is ReauthStatus.IN_FLIGHT
    assert lifecycle.state is SessionState.EXPIRED


def test_restart_drops_process_local_reauth_claim_authority() -> None:
    lifecycle = active()
    ticket = lifecycle.capture_read_generation(monotonic_ns=ns(15))
    leader = lifecycle.claim_read_reauth_after_401(ticket, monotonic_ns=ns(20))
    restored = MatchbookSessionLifecycle.from_audit_snapshot(lifecycle.audit_snapshot())

    with pytest.raises(
        SessionLifecycleError, match="not issued here or is no longer current"
    ):
        restored.read_reauth_outcome(leader)
    with pytest.raises(
        SessionLifecycleError, match="exact product-issued reauth leader"
    ):
        restored.complete_read_reauth_failure(leader, monotonic_ns=1)
    assert restored.state is SessionState.RESTART_REAUTH_REQUIRED
    assert restored.restart_requires_reauth is True


def test_clock_rollback_during_leader_completion_fails_epoch_closed() -> None:
    lifecycle = active()
    first = lifecycle.capture_read_generation(monotonic_ns=ns(15))
    second = lifecycle.capture_read_generation(monotonic_ns=ns(16))
    leader = lifecycle.claim_read_reauth_after_401(first, monotonic_ns=ns(20))
    follower = lifecycle.claim_read_reauth_after_401(second, monotonic_ns=ns(19))

    with pytest.raises(SessionClockRollbackError):
        lifecycle.complete_read_reauth_success(
            leader,
            successor_generation_id="gen-2",
            monotonic_ns=ns(19),
        )

    assert lifecycle.state is SessionState.CLOCK_FAULT
    assert lifecycle.is_active is False
    assert lifecycle.read_reauth_outcome(follower).status is ReauthStatus.FAILED


def test_completed_epoch_slot_is_bounded_and_replaced_on_next_generation_401() -> None:
    lifecycle = active()
    first_ticket = lifecycle.capture_read_generation(monotonic_ns=ns(15))
    follower_ticket = lifecycle.capture_read_generation(monotonic_ns=ns(16))
    first_leader = lifecycle.claim_read_reauth_after_401(
        first_ticket, monotonic_ns=ns(20)
    )
    first_follower = lifecycle.claim_read_reauth_after_401(
        follower_ticket, monotonic_ns=ns(21)
    )
    lifecycle.complete_read_reauth_success(
        first_leader,
        successor_generation_id="gen-2",
        monotonic_ns=ns(30),
    )

    second_ticket = lifecycle.capture_read_generation(monotonic_ns=ns(35))
    second_leader = lifecycle.claim_read_reauth_after_401(
        second_ticket, monotonic_ns=ns(40)
    )

    assert second_leader.role is ReauthRole.LEADER
    assert second_leader.epoch_id == first_leader.epoch_id + 1
    with pytest.raises(
        SessionLifecycleError, match="not issued here or is no longer current"
    ):
        lifecycle.read_reauth_outcome(first_follower)


def test_successor_generation_must_be_fresh_and_terminal_generation_cannot_resurrect() -> None:
    lifecycle = active()
    lifecycle.record_login_200(generation_id="gen-old", monotonic_ns=ns(15))
    ticket = lifecycle.capture_read_generation(monotonic_ns=ns(16))
    leader = lifecycle.claim_read_reauth_after_401(ticket, monotonic_ns=ns(20))

    for invalid_successor in ("gen-old", "gen-1"):
        with pytest.raises(SessionLifecycleError):
            lifecycle.complete_read_reauth_success(
                leader,
                successor_generation_id=invalid_successor,
                monotonic_ns=ns(30),
            )

    assert lifecycle.read_reauth_outcome(leader).status is ReauthStatus.IN_FLIGHT


def test_401_observation_cannot_precede_its_read_ticket() -> None:
    lifecycle = active()
    ticket = lifecycle.capture_read_generation(monotonic_ns=ns(20))
    before = lifecycle.audit_snapshot()

    with pytest.raises(
        SessionLifecycleError, match="precedes its product-issued read ticket"
    ):
        lifecycle.claim_read_reauth_after_401(ticket, monotonic_ns=ns(19))

    assert lifecycle.audit_snapshot() == before
    assert (
        lifecycle.authorize_read_response_commit(ticket, monotonic_ns=ns(21))
        == "gen-1"
    )
