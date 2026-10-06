from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Barrier, Thread

import pytest

from autosport.betfair_marketbook_projection_concurrency import (
    BETFAIR_MARKETBOOK_PROJECTION_CONCURRENCY_POLICY_VERSION,
    BetfairMarketBookProjectionConcurrencyGate,
    MarketBookProjectionConcurrencyState,
    MarketBookProjectionLease,
)


T0 = datetime(2026, 9, 22, tzinfo=timezone.utc)


def gate() -> BetfairMarketBookProjectionConcurrencyGate:
    return BetfairMarketBookProjectionConcurrencyGate()


def begin_projected(
    value: BetfairMarketBookProjectionConcurrencyGate,
    request_id: str,
    *,
    at: datetime = T0,
):
    return value.begin(
        request_id,
        observed_at=at,
        has_order_projection=True,
        has_match_projection=False,
    )


def active_generation(
    value: BetfairMarketBookProjectionConcurrencyGate,
    request_id: str,
) -> int:
    return next(
        lease.generation
        for lease in value.snapshot().active
        if lease.request_id == request_id
    )


def test_first_three_projection_requests_are_allowed_and_fourth_is_denied() -> None:
    value = gate()
    for index in range(3):
        decision = begin_projected(value, f"r{index}")
        assert decision.allowed is True
        assert decision.active_projection_requests == index + 1

    denied = begin_projected(value, "r3")
    assert denied.allowed is False
    assert denied.active_projection_requests == 3
    assert [item.request_id for item in value.snapshot().active] == [
        "r0",
        "r1",
        "r2",
    ]


@pytest.mark.parametrize(
    ("order", "match"),
    [(True, False), (False, True), (True, True)],
)
def test_any_order_or_match_projection_consumes_bucket(
    order: bool,
    match: bool,
) -> None:
    value = gate()
    decision = value.begin(
        "r",
        observed_at=T0,
        has_order_projection=order,
        has_match_projection=match,
    )
    assert decision.projection_bearing is True
    assert len(value.snapshot().active) == 1


def test_price_only_request_bypasses_projection_bucket_even_when_full() -> None:
    value = gate()
    for index in range(3):
        assert begin_projected(value, f"projected-{index}").allowed

    for index in range(20):
        decision = value.begin(
            f"price-{index}",
            observed_at=T0,
            has_order_projection=False,
            has_match_projection=False,
        )
        assert decision.allowed is True
        assert decision.projection_bearing is False
        assert decision.active_projection_requests == 3
    assert len(value.snapshot().active) == 3


def test_process_control_after_lease_mutation_rolls_back_only_exact_lease() -> None:
    value = BetfairMarketBookProjectionConcurrencyGate()
    interrupt = KeyboardInterrupt("begin interrupted after mutation")
    original_lock = value._lock

    class InterruptAfterMutationLock:
        def __init__(self) -> None:
            self.raise_once = True

        def __enter__(self):
            return original_lock.__enter__()

        def __exit__(self, exc_type, exc, tb):
            result = original_lock.__exit__(exc_type, exc, tb)
            if self.raise_once and exc_type is None:
                self.raise_once = False
                raise interrupt
            return result

    value._lock = InterruptAfterMutationLock()

    with pytest.raises(KeyboardInterrupt) as exc_info:
        value.begin(
            "interrupted",
            observed_at=T0,
            has_order_projection=True,
            has_match_projection=False,
        )

    assert exc_info.value is interrupt
    state = value.snapshot()
    assert state.active == ()
    assert state.next_lease_generation == 2
    assert state.last_observed_at_utc_us == int(T0.timestamp() * 1_000_000)

    successor = value.begin(
        "successor",
        observed_at=T0,
        has_order_projection=True,
        has_match_projection=False,
    )
    assert successor.allowed is True
    assert successor.lease_generation == 2


def test_completion_releases_exactly_one_slot() -> None:
    value = gate()
    for index in range(3):
        assert begin_projected(value, f"r{index}").allowed

    value.complete(
        "r1",
        lease_generation=active_generation(value, "r1"),
        observed_at=T0 + timedelta(seconds=1),
    )
    assert [item.request_id for item in value.snapshot().active] == ["r0", "r2"]
    replacement = value.begin(
        "r3",
        observed_at=T0 + timedelta(seconds=1),
        has_order_projection=False,
        has_match_projection=True,
    )
    assert replacement.allowed is True
    assert replacement.active_projection_requests == 3


def test_duplicate_completion_cannot_double_release() -> None:
    value = gate()
    assert begin_projected(value, "r0").allowed
    generation = active_generation(value, "r0")
    value.complete(
        "r0",
        lease_generation=generation,
        observed_at=T0 + timedelta(seconds=1),
    )
    with pytest.raises(ValueError, match="not an active"):
        value.complete(
            "r0",
            lease_generation=generation,
            observed_at=T0 + timedelta(seconds=1),
        )
    assert value.snapshot().active == ()


def test_duplicate_active_request_id_is_rejected_without_extra_slot() -> None:
    value = gate()
    assert begin_projected(value, "same").allowed
    with pytest.raises(ValueError, match="already active"):
        value.begin(
            "same",
            observed_at=T0,
            has_order_projection=False,
            has_match_projection=True,
        )
    assert len(value.snapshot().active) == 1


def test_time_advance_never_auto_expires_unresolved_leases() -> None:
    value = gate()
    for index in range(3):
        assert begin_projected(value, f"r{index}").allowed

    denied = begin_projected(value, "r3", at=T0 + timedelta(days=365))
    assert denied.allowed is False
    assert denied.active_projection_requests == 3
    assert [item.request_id for item in value.snapshot().active] == [
        "r0",
        "r1",
        "r2",
    ]


def test_explicit_completion_can_release_old_unresolved_lease() -> None:
    value = gate()
    assert begin_projected(value, "r0").allowed
    value.complete(
        "r0",
        lease_generation=active_generation(value, "r0"),
        observed_at=T0 + timedelta(days=365),
    )
    assert value.snapshot().active == ()


def test_restart_preserves_unresolved_slots_and_denial() -> None:
    value = gate()
    for index in range(3):
        assert begin_projected(
            value,
            f"r{index}",
            at=T0 + timedelta(seconds=index),
        ).allowed
    restored = BetfairMarketBookProjectionConcurrencyGate(state=value.snapshot())
    when = T0 + timedelta(days=1)
    original = begin_projected(value, "r3", at=when)
    replay = begin_projected(restored, "r3", at=when)
    assert original == replay
    assert original.allowed is False
    assert value.snapshot() == restored.snapshot()


def test_observed_time_cannot_move_backwards() -> None:
    value = gate()
    assert begin_projected(value, "r0", at=T0 + timedelta(seconds=1)).allowed
    with pytest.raises(ValueError, match="must not move backwards"):
        begin_projected(value, "r1", at=T0)


def test_timezone_equivalent_instants_normalize_identically() -> None:
    value = gate()
    plus_two = timezone(timedelta(hours=2))
    first = begin_projected(value, "r0")
    second = value.begin(
        "price",
        observed_at=T0.astimezone(plus_two),
        has_order_projection=False,
        has_match_projection=False,
    )
    assert first.observed_at_utc_us == second.observed_at_utc_us


@pytest.mark.parametrize("bad", ["", " x", "x ", 1, None])
def test_bad_request_id_fails_closed(bad: object) -> None:
    value = gate()
    with pytest.raises((TypeError, ValueError)):
        value.begin(
            bad,  # type: ignore[arg-type]
            observed_at=T0,
            has_order_projection=True,
            has_match_projection=False,
        )


@pytest.mark.parametrize(
    "name",
    ["has_order_projection", "has_match_projection"],
)
def test_projection_flags_require_exact_bool(name: str) -> None:
    value = gate()
    kwargs = {
        "request_id": "r",
        "observed_at": T0,
        "has_order_projection": False,
        "has_match_projection": False,
    }
    kwargs[name] = 1
    with pytest.raises(TypeError, match="must be bool"):
        value.begin(**kwargs)  # type: ignore[arg-type]


def test_naive_time_fails_closed() -> None:
    value = gate()
    with pytest.raises(ValueError, match="timezone-aware"):
        value.begin(
            "r",
            observed_at=datetime(2026, 9, 22),
            has_order_projection=True,
            has_match_projection=False,
        )


def test_state_rejects_wrong_policy_version() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        MarketBookProjectionConcurrencyState("wrong", None, (), 1)


def test_state_rejects_more_than_three_active_projection_requests() -> None:
    leases = tuple(
        MarketBookProjectionLease(f"r{index}", 0, index + 1)
        for index in range(4)
    )
    with pytest.raises(ValueError, match="exceeds"):
        MarketBookProjectionConcurrencyState(
            BETFAIR_MARKETBOOK_PROJECTION_CONCURRENCY_POLICY_VERSION,
            0,
            leases,
            5,
        )


def test_state_rejects_unsorted_or_duplicate_active_ids() -> None:
    a = MarketBookProjectionLease("a", 0, 1)
    b = MarketBookProjectionLease("b", 0, 2)
    with pytest.raises(ValueError, match="sorted"):
        MarketBookProjectionConcurrencyState(
            BETFAIR_MARKETBOOK_PROJECTION_CONCURRENCY_POLICY_VERSION,
            0,
            (b, a),
            3,
        )
    with pytest.raises(ValueError, match="duplicate"):
        MarketBookProjectionConcurrencyState(
            BETFAIR_MARKETBOOK_PROJECTION_CONCURRENCY_POLICY_VERSION,
            0,
            (a, a),
            2,
        )


def test_state_rejects_lease_acquired_after_last_observed_time() -> None:
    lease = MarketBookProjectionLease("a", 2, 1)
    with pytest.raises(ValueError, match="after last"):
        MarketBookProjectionConcurrencyState(
            BETFAIR_MARKETBOOK_PROJECTION_CONCURRENCY_POLICY_VERSION,
            1,
            (lease,),
            2,
        )


def test_denial_adds_no_lease() -> None:
    value = gate()
    for index in range(3):
        assert begin_projected(value, f"r{index}").allowed
    before = value.snapshot().active
    assert not begin_projected(
        value,
        "denied",
        at=T0 + timedelta(seconds=1),
    ).allowed
    assert value.snapshot().active == before


def test_price_only_request_never_needs_completion() -> None:
    value = gate()
    decision = value.begin(
        "price",
        observed_at=T0,
        has_order_projection=False,
        has_match_projection=False,
    )
    assert decision.allowed
    assert value.snapshot().active == ()
    with pytest.raises(ValueError, match="not an active"):
        value.complete("price", lease_generation=1, observed_at=T0)


def test_local_gate_never_claims_complete_provider_limit_or_dispatch_authority() -> None:
    value = gate()

    projected = begin_projected(value, "projected")
    price_only = value.begin(
        "price-only",
        observed_at=T0,
        has_order_projection=False,
        has_match_projection=False,
    )

    for decision in (projected, price_only):
        assert decision.provider_limit_coverage_complete is False
        assert decision.provider_dispatch_authorized is False



def test_competing_third_slot_begins_are_serialized() -> None:
    value = gate()
    assert begin_projected(value, "r0").allowed
    assert begin_projected(value, "r1").allowed

    start = Barrier(3)
    decisions = []
    errors: list[BaseException] = []

    def compete(request_id: str) -> None:
        try:
            start.wait()
            decisions.append(
                begin_projected(
                    value,
                    request_id,
                    at=T0 + timedelta(microseconds=1),
                )
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    workers = [
        Thread(target=compete, args=("r2",)),
        Thread(target=compete, args=("r3",)),
    ]
    for worker in workers:
        worker.start()
    start.wait()
    for worker in workers:
        worker.join()

    assert errors == []
    assert len(decisions) == 2
    assert sum(decision.allowed for decision in decisions) == 1
    assert sum(not decision.allowed for decision in decisions) == 1
    assert len(value.snapshot().active) == 3


def test_stale_completion_cannot_release_reused_request_id_generation() -> None:
    value = gate()
    first = begin_projected(value, "same")
    assert first.lease_generation is not None
    value.complete(
        "same",
        lease_generation=first.lease_generation,
        observed_at=T0 + timedelta(seconds=1),
    )

    second = begin_projected(
        value,
        "same",
        at=T0 + timedelta(seconds=2),
    )
    assert second.allowed
    assert second.lease_generation is not None
    assert second.lease_generation != first.lease_generation

    before = value.snapshot()
    with pytest.raises(ValueError, match="does not match"):
        value.complete(
            "same",
            lease_generation=first.lease_generation,
            observed_at=T0 + timedelta(seconds=3),
        )
    after = value.snapshot()
    assert after == before
    assert active_generation(value, "same") == second.lease_generation


def test_restart_preserves_generation_against_stale_completion() -> None:
    value = gate()
    first = begin_projected(value, "same")
    assert first.lease_generation is not None
    value.complete(
        "same",
        lease_generation=first.lease_generation,
        observed_at=T0 + timedelta(seconds=1),
    )

    restored = BetfairMarketBookProjectionConcurrencyGate(state=value.snapshot())
    second = begin_projected(
        restored,
        "same",
        at=T0 + timedelta(seconds=2),
    )
    assert second.lease_generation is not None
    assert second.lease_generation > first.lease_generation

    before = restored.snapshot()
    with pytest.raises(ValueError, match="does not match"):
        restored.complete(
            "same",
            lease_generation=first.lease_generation,
            observed_at=T0 + timedelta(seconds=3),
        )
    assert restored.snapshot() == before


def test_invalid_completion_does_not_advance_causal_time() -> None:
    value = gate()
    first = begin_projected(value, "r0")
    assert first.lease_generation is not None
    before = value.snapshot()
    with pytest.raises(ValueError, match="does not match"):
        value.complete(
            "r0",
            lease_generation=first.lease_generation + 1,
            observed_at=T0 + timedelta(days=1),
        )
    assert value.snapshot() == before


def test_state_rejects_generation_rewind_or_duplicate_active_generation() -> None:
    a = MarketBookProjectionLease("a", 0, 1)
    b = MarketBookProjectionLease("b", 0, 1)
    with pytest.raises(ValueError, match="precede next"):
        MarketBookProjectionConcurrencyState(
            BETFAIR_MARKETBOOK_PROJECTION_CONCURRENCY_POLICY_VERSION,
            0,
            (a,),
            1,
        )
    with pytest.raises(ValueError, match="duplicate active lease generation"):
        MarketBookProjectionConcurrencyState(
            BETFAIR_MARKETBOOK_PROJECTION_CONCURRENCY_POLICY_VERSION,
            0,
            (a, b),
            2,
        )

def test_projection_gate_rejects_subclassed_authority_inputs_before_mutation() -> None:
    class RequestId(str):
        pass

    class ObservedInstant(datetime):
        def astimezone(self, tz=None):
            raise AssertionError("subclass clock override must not execute")

    value = gate()

    with pytest.raises(TypeError, match="request_id must be exact str"):
        value.begin(
            RequestId("r-subclass"),
            observed_at=T0,
            has_order_projection=True,
            has_match_projection=False,
        )

    state = value.snapshot()
    assert state.active == ()
    assert state.last_observed_at_utc_us is None

    hostile_time = ObservedInstant(2026, 9, 22, tzinfo=timezone.utc)
    with pytest.raises(TypeError, match="observed_at must be exact datetime"):
        value.begin(
            "r-time-subclass",
            observed_at=hostile_time,
            has_order_projection=True,
            has_match_projection=False,
        )

    state = value.snapshot()
    assert state.active == ()
    assert state.last_observed_at_utc_us is None

    class PolicyVersion(str):
        pass

    with pytest.raises(
        ValueError,
        match="unsupported Betfair MarketBook projection concurrency policy",
    ):
        MarketBookProjectionConcurrencyState(
            PolicyVersion(BETFAIR_MARKETBOOK_PROJECTION_CONCURRENCY_POLICY_VERSION),
            None,
            (),
            1,
        )

