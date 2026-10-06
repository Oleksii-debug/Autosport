from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from threading import Barrier, Thread

import pytest

import autosport.betfair_marketbook_projection_concurrency as _projection_gate_module
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


def test_restart_revalidates_tampered_frozen_projection_state() -> None:
    value = gate()
    assert begin_projected(value, "r0").allowed
    state = value.snapshot()
    lease = state.active[0]

    object.__setattr__(lease, "generation", 0)

    with pytest.raises(ValueError, match="generation must be a positive"):
        BetfairMarketBookProjectionConcurrencyGate(state=state)


def test_restart_revalidates_tampered_projection_policy_identity() -> None:
    state = gate().snapshot()
    object.__setattr__(state, "policy_version", "forged-policy")

    with pytest.raises(
        ValueError,
        match="unsupported Betfair MarketBook projection concurrency policy",
    ):
        BetfairMarketBookProjectionConcurrencyGate(state=state)


def test_instance_shadowed_clock_helper_cannot_bypass_monotonic_fence() -> None:
    acquire_gate = gate()
    assert begin_projected(
        acquire_gate,
        "first",
        at=T0 + timedelta(seconds=1),
    ).allowed

    acquire_gate._require_not_backwards = lambda observed_us: None  # type: ignore[method-assign]

    before_acquire = acquire_gate.snapshot()
    with pytest.raises(ValueError, match="must not move backwards"):
        begin_projected(acquire_gate, "backdated", at=T0)
    assert acquire_gate.snapshot() == before_acquire

    release_gate = gate()
    first = begin_projected(
        release_gate,
        "active",
        at=T0 + timedelta(seconds=1),
    )
    assert first.lease_generation is not None

    release_gate._require_not_backwards = lambda observed_us: None  # type: ignore[method-assign]

    before_release = release_gate.snapshot()
    with pytest.raises(ValueError, match="must not move backwards"):
        release_gate.complete(
            "active",
            lease_generation=first.lease_generation,
            observed_at=T0,
        )
    assert release_gate.snapshot() == before_release


def test_process_control_during_active_insert_burns_lease_generation() -> None:
    value = gate()
    interrupt = KeyboardInterrupt("active insert interrupted")

    class InterruptingActive(dict[str, MarketBookProjectionLease]):
        def __setitem__(self, key: str, lease: MarketBookProjectionLease) -> None:
            super().__setitem__(key, lease)
            raise interrupt

    value._active = InterruptingActive()

    with pytest.raises(KeyboardInterrupt) as exc_info:
        begin_projected(value, "interrupted")

    assert exc_info.value is interrupt
    state = value.snapshot()
    assert state.active == ()
    assert state.next_lease_generation == 2
    assert state.last_observed_at_utc_us is None

    successor = begin_projected(value, "successor")
    assert successor.allowed is True
    assert successor.lease_generation == 2


def test_process_control_during_complete_delete_preserves_causal_time() -> None:
    value = gate()
    first = begin_projected(value, "active")
    assert first.lease_generation is not None
    interrupt = KeyboardInterrupt("complete delete interrupted")

    class InterruptingActive(dict[str, MarketBookProjectionLease]):
        def __delitem__(self, key: str) -> None:
            super().__delitem__(key)
            raise interrupt

    value._active = InterruptingActive(value._active)

    completed_at = T0 + timedelta(seconds=1)
    with pytest.raises(KeyboardInterrupt) as exc_info:
        value.complete(
            "active",
            lease_generation=first.lease_generation,
            observed_at=completed_at,
        )

    assert exc_info.value is interrupt
    state = value.snapshot()
    assert state.active == ()
    assert state.last_observed_at_utc_us == int(completed_at.timestamp() * 1_000_000)
    assert state.next_lease_generation == 2



def test_projection_gate_runtime_authority_is_closure_bound(monkeypatch) -> None:
    gate_type = BetfairMarketBookProjectionConcurrencyGate
    state_type = MarketBookProjectionConcurrencyState
    lease_type = MarketBookProjectionLease
    decision_type = _projection_gate_module.MarketBookProjectionConcurrencyDecision
    original_policy = BETFAIR_MARKETBOOK_PROJECTION_CONCURRENCY_POLICY_VERSION

    monkeypatch.setattr(
        _projection_gate_module,
        "_validate_request_id",
        lambda value: "forged-request",
    )
    monkeypatch.setattr(
        _projection_gate_module,
        "_utc_microseconds",
        lambda *args, **kwargs: 0,
    )
    monkeypatch.setattr(
        _projection_gate_module,
        "_MAX_LOCAL_PROJECTION_REQUESTS_UNRESOLVED",
        999,
    )
    monkeypatch.setattr(
        _projection_gate_module,
        "BETFAIR_MARKETBOOK_PROJECTION_CONCURRENCY_POLICY_VERSION",
        "forged",
    )
    monkeypatch.setattr(
        _projection_gate_module,
        "MarketBookProjectionConcurrencyDecision",
        object,
    )
    monkeypatch.setattr(
        _projection_gate_module,
        "MarketBookProjectionLease",
        object,
    )
    monkeypatch.setattr(
        _projection_gate_module,
        "MarketBookProjectionConcurrencyState",
        object,
    )
    monkeypatch.setattr(
        decision_type,
        "__init__",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("mutable decision constructor must not run")
        ),
    )
    monkeypatch.setattr(
        decision_type,
        "__post_init__",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("mutable decision validator must not run")
        ),
    )
    monkeypatch.setattr(
        lease_type,
        "__init__",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("mutable lease constructor must not run")
        ),
    )
    monkeypatch.setattr(
        lease_type,
        "__post_init__",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("mutable lease validator must not run")
        ),
    )

    value = gate_type()
    first = value.begin(
        "r0",
        observed_at=T0,
        has_order_projection=True,
        has_match_projection=False,
    )
    second = value.begin(
        "r1",
        observed_at=T0,
        has_order_projection=True,
        has_match_projection=False,
    )
    third = value.begin(
        "r2",
        observed_at=T0,
        has_order_projection=True,
        has_match_projection=False,
    )
    denied = value.begin(
        "r3",
        observed_at=T0,
        has_order_projection=True,
        has_match_projection=False,
    )
    state = value.snapshot()

    assert all(
        type(decision) is decision_type and decision.allowed
        for decision in (first, second, third)
    )
    assert type(denied) is decision_type
    assert denied.allowed is False
    assert denied.active_projection_requests == 3
    assert type(state) is state_type
    assert all(type(lease) is lease_type for lease in state.active)
    assert [lease.request_id for lease in state.active] == ["r0", "r1", "r2"]
    assert state.policy_version == original_policy
    assert value.policy_version == original_policy

    assert first.lease_generation is not None
    value.complete(
        "r0",
        lease_generation=first.lease_generation,
        observed_at=T0 + timedelta(seconds=1),
    )
    assert [lease.request_id for lease in value.snapshot().active] == ["r1", "r2"]


def test_projection_restart_validation_ignores_rebound_dto_validators(monkeypatch) -> None:
    gate_type = BetfairMarketBookProjectionConcurrencyGate
    original = gate_type()
    assert begin_projected(original, "r0").allowed
    state = original.snapshot()
    lease = state.active[0]

    monkeypatch.setattr(
        MarketBookProjectionConcurrencyState,
        "__post_init__",
        lambda self: None,
    )
    monkeypatch.setattr(
        MarketBookProjectionLease,
        "__post_init__",
        lambda self: None,
    )
    object.__setattr__(lease, "generation", 0)

    with pytest.raises(ValueError, match="generation must be a positive"):
        gate_type(state=state)


def test_projection_snapshot_is_detached_from_live_lease_authority() -> None:
    value = gate()
    first = begin_projected(value, "r0")
    assert first.lease_generation is not None

    snapshot = value.snapshot()
    exposed = snapshot.active[0]
    object.__setattr__(exposed, "generation", exposed.generation + 100)

    live = value.snapshot()
    assert live.active[0].generation == first.lease_generation
    value.complete(
        "r0",
        lease_generation=first.lease_generation,
        observed_at=T0 + timedelta(seconds=1),
    )
    assert value.snapshot().active == ()


def test_projection_restart_detaches_imported_lease_authority() -> None:
    source = gate()
    first = begin_projected(source, "r0")
    assert first.lease_generation is not None
    state = source.snapshot()

    restored = BetfairMarketBookProjectionConcurrencyGate(state=state)
    imported = state.active[0]
    object.__setattr__(imported, "request_id", "forged-after-restart")
    object.__setattr__(imported, "generation", imported.generation + 100)

    live = restored.snapshot()
    assert len(live.active) == 1
    assert live.active[0].request_id == "r0"
    assert live.active[0].generation == first.lease_generation
    restored.complete(
        "r0",
        lease_generation=first.lease_generation,
        observed_at=T0 + timedelta(seconds=1),
    )
    assert restored.snapshot().active == ()


def test_projection_gate_instance_storage_authority_ignores_rebound_special_methods(
    monkeypatch,
) -> None:
    gate_type = BetfairMarketBookProjectionConcurrencyGate
    value = gate_type()

    def hostile_getattribute(self, name: str):
        if name.startswith("_"):
            raise AssertionError(f"rebound __getattribute__ reached authority field {name}")
        return object.__getattribute__(self, name)

    def hostile_setattr(self, name: str, new_value: object) -> None:
        if name.startswith("_"):
            raise AssertionError(f"rebound __setattr__ reached authority field {name}")
        object.__setattr__(self, name, new_value)

    monkeypatch.setattr(gate_type, "__getattribute__", hostile_getattribute)
    monkeypatch.setattr(gate_type, "__setattr__", hostile_setattr)

    first = value.begin(
        "r0",
        observed_at=T0,
        has_order_projection=True,
        has_match_projection=False,
    )
    assert first.allowed is True
    assert first.lease_generation is not None
    snapshot = value.snapshot()
    assert [lease.request_id for lease in snapshot.active] == ["r0"]

    restored = gate_type(state=snapshot)
    restored.complete(
        "r0",
        lease_generation=first.lease_generation,
        observed_at=T0 + timedelta(seconds=1),
    )
    assert restored.snapshot().active == ()


def test_projection_restart_authority_ignores_rebound_state_slot_descriptor(
    monkeypatch,
) -> None:
    gate_type = BetfairMarketBookProjectionConcurrencyGate
    source = gate_type()
    for index in range(3):
        assert begin_projected(source, f"r{index}").allowed
    state = source.snapshot()

    monkeypatch.setattr(
        MarketBookProjectionConcurrencyState,
        "active",
        property(lambda self: ()),
    )

    restored = gate_type(state=state)
    denied = restored.begin(
        "r3",
        observed_at=T0,
        has_order_projection=True,
        has_match_projection=False,
    )
    assert denied.allowed is False


def test_projection_live_release_ignores_rebound_lease_generation_descriptor(
    monkeypatch,
) -> None:
    gate_type = BetfairMarketBookProjectionConcurrencyGate
    value = gate_type()
    first = begin_projected(value, "r0")
    assert first.lease_generation is not None

    with monkeypatch.context() as context:
        context.setattr(
            MarketBookProjectionLease,
            "generation",
            property(lambda self: first.lease_generation + 100),
        )
        value.complete(
            "r0",
            lease_generation=first.lease_generation,
            observed_at=T0 + timedelta(seconds=1),
        )

    assert value.snapshot().active == ()


def test_projection_decision_construction_ignores_rebound_field_descriptor(
    monkeypatch,
) -> None:
    gate_type = BetfairMarketBookProjectionConcurrencyGate
    value = gate_type()

    with monkeypatch.context() as context:
        context.setattr(
            _projection_gate_module.MarketBookProjectionConcurrencyDecision,
            "allowed",
            property(lambda self: False),
        )
        decision = value.begin(
            "r0",
            observed_at=T0,
            has_order_projection=True,
            has_match_projection=False,
        )

    assert decision.allowed is True
    assert decision.lease_generation == 1


def test_projection_timezone_offset_is_observed_once_before_gate_lock() -> None:
    class ChangingOffset(tzinfo):
        def __init__(self) -> None:
            self.calls = 0

        def utcoffset(self, dt):
            self.calls += 1
            return timedelta(hours=self.calls)

        def dst(self, dt):
            return timedelta(0)

    zone = ChangingOffset()
    local = datetime(2026, 9, 22, 1, 0, 0, tzinfo=zone)
    value = BetfairMarketBookProjectionConcurrencyGate()

    decision = value.begin(
        "r0",
        observed_at=local,
        has_order_projection=True,
        has_match_projection=False,
    )

    assert zone.calls == 1
    assert decision.observed_at_utc_us == int(T0.timestamp() * 1_000_000)


def test_projection_gate_authority_ignores_late_builtin_shadowing(monkeypatch) -> None:
    gate_type = BetfairMarketBookProjectionConcurrencyGate
    shadowed = (
        "object",
        "type",
        "isinstance",
        "tuple",
        "set",
        "str",
        "int",
        "bool",
        "len",
        "sorted",
        "TypeError",
        "ValueError",
    )
    for name in shadowed:
        monkeypatch.setattr(_projection_gate_module, name, None, raising=False)

    value = gate_type()
    with pytest.raises(TypeError, match="request_id must be exact str"):
        value.begin(
            object(),  # type: ignore[arg-type]
            observed_at=T0,
            has_order_projection=True,
            has_match_projection=False,
        )

    first = value.begin(
        "r0",
        observed_at=T0,
        has_order_projection=True,
        has_match_projection=False,
    )
    with pytest.raises(ValueError, match="request_id is already active"):
        value.begin(
            "r0",
            observed_at=T0,
            has_order_projection=True,
            has_match_projection=False,
        )
    state = value.snapshot()
    restored = gate_type(state=state)

    assert first.allowed is True
    assert first.lease_generation == 1
    assert restored.snapshot() == state
    restored.complete(
        "r0",
        lease_generation=first.lease_generation,
        observed_at=T0 + timedelta(microseconds=1),
    )
    assert restored.snapshot().active == ()


def test_projection_cleanup_authority_ignores_late_exception_builtin_shadowing(
    monkeypatch,
) -> None:
    value = BetfairMarketBookProjectionConcurrencyGate()
    original_lock = value._lock
    primary = KeyboardInterrupt("primary process-control failure")

    class InterruptThenCleanupFailureLock:
        def __init__(self) -> None:
            self.cleanup = False

        def __enter__(self):
            if self.cleanup:
                raise RuntimeError("cleanup lock failure")
            return original_lock.__enter__()

        def __exit__(self, exc_type, exc, tb):
            result = original_lock.__exit__(exc_type, exc, tb)
            if exc_type is None and not self.cleanup:
                self.cleanup = True
                raise primary
            return result

    value._lock = InterruptThenCleanupFailureLock()
    for name in ("BaseException", "Exception", "getattr", "callable"):
        monkeypatch.setattr(_projection_gate_module, name, None, raising=False)

    with pytest.raises(KeyboardInterrupt) as exc_info:
        value.begin(
            "r0",
            observed_at=T0,
            has_order_projection=True,
            has_match_projection=False,
        )

    assert exc_info.value is primary
    assert any(
        "projection-begin cleanup also failed: RuntimeError: cleanup lock failure"
        in note
        for note in getattr(primary, "__notes__", ())
    )

    value._lock = original_lock
    state = value.snapshot()
    assert [lease.request_id for lease in state.active] == ["r0"]
    assert state.next_lease_generation == 2

