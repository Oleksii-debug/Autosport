from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from threading import Barrier, Thread

import pytest

import autosport.betfair_marketbook_rate_gate as _rate_gate_module
from autosport.betfair_marketbook_rate_gate import (
    BETFAIR_MARKETBOOK_RATE_POLICY_VERSION,
    BetfairMarketBookPerMarketRateGate,
    MarketBookRateGateState,
    MarketBookRateWindowState,
)


T0 = datetime(2026, 9, 22, 0, 0, 0, tzinfo=timezone.utc)


def test_five_calls_allowed_and_sixth_is_denied_with_exact_next_time() -> None:
    gate = BetfairMarketBookPerMarketRateGate()
    for index in range(5):
        decision = gate.reserve(("1.234",), scheduled_at=T0 + timedelta(milliseconds=100 * index))
        assert decision.allowed is True

    denied = gate.reserve(("1.234",), scheduled_at=T0 + timedelta(milliseconds=500))
    assert denied.allowed is False
    assert denied.blocked_market_ids == ("1.234",)
    assert denied.next_eligible_at == T0 + timedelta(seconds=1)


def test_exact_one_second_boundary_expires_oldest_reservation() -> None:
    gate = BetfairMarketBookPerMarketRateGate()
    for index in range(5):
        assert gate.reserve(("1.234",), scheduled_at=T0 + timedelta(milliseconds=100 * index)).allowed

    assert gate.reserve(("1.234",), scheduled_at=T0 + timedelta(seconds=1)).allowed


def test_batch_consumes_one_call_for_every_market() -> None:
    gate = BetfairMarketBookPerMarketRateGate()
    for index in range(5):
        assert gate.reserve(("1.1", "1.2"), scheduled_at=T0 + timedelta(milliseconds=index)).allowed

    denied = gate.reserve(("1.1", "1.2"), scheduled_at=T0 + timedelta(milliseconds=10))
    assert denied.allowed is False
    assert denied.blocked_market_ids == ("1.1", "1.2")


def test_mixed_batch_denial_is_atomic() -> None:
    gate = BetfairMarketBookPerMarketRateGate()
    for index in range(5):
        assert gate.reserve(("hot",), scheduled_at=T0 + timedelta(milliseconds=index)).allowed

    before = gate.snapshot()
    denied = gate.reserve(("cold", "hot"), scheduled_at=T0 + timedelta(milliseconds=10))
    after = gate.snapshot()

    assert denied.allowed is False
    assert denied.blocked_market_ids == ("hot",)
    assert all(window.market_id != "cold" for window in after.markets)
    assert tuple((m.market_id, m.accepted_at_utc_us) for m in after.markets) == tuple(
        (m.market_id, m.accepted_at_utc_us) for m in before.markets
    )


def test_hot_market_does_not_block_unrelated_market() -> None:
    gate = BetfairMarketBookPerMarketRateGate()
    for index in range(5):
        assert gate.reserve(("hot",), scheduled_at=T0 + timedelta(milliseconds=index)).allowed

    assert gate.reserve(("cold",), scheduled_at=T0 + timedelta(milliseconds=10)).allowed


def test_denial_reports_latest_next_eligible_for_multiple_blockers() -> None:
    gate = BetfairMarketBookPerMarketRateGate()
    for index in range(5):
        assert gate.reserve(("a",), scheduled_at=T0 + timedelta(milliseconds=10 * index)).allowed
    for index in range(5):
        assert gate.reserve(("b",), scheduled_at=T0 + timedelta(milliseconds=100 + 10 * index)).allowed

    denied = gate.reserve(("a", "b"), scheduled_at=T0 + timedelta(milliseconds=200))
    assert denied.allowed is False
    assert denied.blocked_market_ids == ("a", "b")
    assert denied.next_eligible_at == T0 + timedelta(milliseconds=1100)


def test_restart_snapshot_preserves_admission_decision() -> None:
    gate = BetfairMarketBookPerMarketRateGate()
    for index in range(5):
        assert gate.reserve(("1.234",), scheduled_at=T0 + timedelta(milliseconds=index)).allowed

    restored = BetfairMarketBookPerMarketRateGate(gate.snapshot())
    when = T0 + timedelta(milliseconds=20)
    assert gate.reserve(("1.234",), scheduled_at=when) == restored.reserve(
        ("1.234",), scheduled_at=when
    )
    assert gate.snapshot() == restored.snapshot()


def test_timezone_equivalent_instants_have_same_decision_time() -> None:
    gate = BetfairMarketBookPerMarketRateGate()
    plus_two = timezone(timedelta(hours=2))
    first = gate.reserve(("1.1",), scheduled_at=T0)
    second = gate.reserve(("1.2",), scheduled_at=T0.astimezone(plus_two))
    assert first.scheduled_at_utc_us == second.scheduled_at_utc_us


def test_scheduled_time_cannot_move_backwards() -> None:
    gate = BetfairMarketBookPerMarketRateGate()
    assert gate.reserve(("1.1",), scheduled_at=T0 + timedelta(seconds=1)).allowed
    with pytest.raises(ValueError, match="must not move backwards"):
        gate.reserve(("1.2",), scheduled_at=T0)


@pytest.mark.parametrize("bad", ["1.1", b"1.1"])
def test_market_id_collection_cannot_be_scalar_text(bad: object) -> None:
    gate = BetfairMarketBookPerMarketRateGate()
    with pytest.raises(TypeError):
        gate.reserve(bad, scheduled_at=T0)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [(), ("",), (" 1.1",), ("1.1 ",), (1,)])
def test_invalid_market_ids_fail_closed(bad: object) -> None:
    gate = BetfairMarketBookPerMarketRateGate()
    with pytest.raises((TypeError, ValueError)):
        gate.reserve(bad, scheduled_at=T0)  # type: ignore[arg-type]


def test_duplicate_market_in_one_batch_is_rejected_without_state_change() -> None:
    gate = BetfairMarketBookPerMarketRateGate()
    with pytest.raises(ValueError, match="duplicate"):
        gate.reserve(("1.1", "1.1"), scheduled_at=T0)
    assert gate.snapshot().markets == ()


def test_naive_time_is_rejected() -> None:
    gate = BetfairMarketBookPerMarketRateGate()
    with pytest.raises(ValueError, match="timezone-aware"):
        gate.reserve(("1.1",), scheduled_at=datetime(2026, 9, 22))


def test_snapshot_is_canonical_sorted_and_prunes_expired_markets() -> None:
    gate = BetfairMarketBookPerMarketRateGate()
    assert gate.reserve(("z", "a"), scheduled_at=T0).allowed
    assert [item.market_id for item in gate.snapshot().markets] == ["a", "z"]

    assert gate.reserve(("m",), scheduled_at=T0 + timedelta(seconds=2)).allowed
    assert [item.market_id for item in gate.snapshot().markets] == ["m"]


def test_snapshot_has_bounded_per_market_history() -> None:
    gate = BetfairMarketBookPerMarketRateGate()
    for index in range(5):
        assert gate.reserve(("1.1",), scheduled_at=T0 + timedelta(microseconds=index)).allowed
    state = gate.snapshot()
    assert len(state.markets[0].accepted_at_utc_us) == 5


def test_state_rejects_wrong_policy_version() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        MarketBookRateGateState("wrong", None, ())


def test_state_rejects_more_than_five_recent_reservations() -> None:
    with pytest.raises(ValueError, match="exceeds"):
        MarketBookRateWindowState("1.1", (1, 2, 3, 4, 5, 6))


def test_state_rejects_unsorted_or_duplicate_market_windows() -> None:
    a = MarketBookRateWindowState("a", (1,))
    b = MarketBookRateWindowState("b", (1,))
    with pytest.raises(ValueError, match="sorted"):
        MarketBookRateGateState(BETFAIR_MARKETBOOK_RATE_POLICY_VERSION, 1, (b, a))
    with pytest.raises(ValueError, match="duplicate"):
        MarketBookRateGateState(BETFAIR_MARKETBOOK_RATE_POLICY_VERSION, 1, (a, a))


def test_state_rejects_reservation_after_last_scheduled_time() -> None:
    window = MarketBookRateWindowState("a", (2,))
    with pytest.raises(ValueError, match="after last"):
        MarketBookRateGateState(BETFAIR_MARKETBOOK_RATE_POLICY_VERSION, 1, (window,))


def test_denied_reservation_advances_causal_time_but_adds_no_call() -> None:
    gate = BetfairMarketBookPerMarketRateGate()
    for index in range(5):
        assert gate.reserve(("hot",), scheduled_at=T0 + timedelta(milliseconds=index)).allowed
    assert not gate.reserve(("hot",), scheduled_at=T0 + timedelta(milliseconds=10)).allowed
    state = gate.snapshot()
    hot = next(item for item in state.markets if item.market_id == "hot")
    assert len(hot.accepted_at_utc_us) == 5
    assert state.last_scheduled_at_utc_us == int(
        (T0 + timedelta(milliseconds=10) - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds()
        * 1_000_000
    )


def test_blocked_batch_does_not_consume_unblocked_market_capacity() -> None:
    gate = BetfairMarketBookPerMarketRateGate()
    for index in range(5):
        assert gate.reserve(("hot",), scheduled_at=T0 + timedelta(milliseconds=index)).allowed
    assert not gate.reserve(("cold", "hot"), scheduled_at=T0 + timedelta(milliseconds=10)).allowed

    for offset in range(5):
        assert gate.reserve(("cold",), scheduled_at=T0 + timedelta(milliseconds=20 + offset)).allowed
    assert not gate.reserve(("cold",), scheduled_at=T0 + timedelta(milliseconds=30)).allowed


def test_mutable_rate_state_reads_are_serialized_by_one_gate_lock() -> None:
    gate = BetfairMarketBookPerMarketRateGate()
    assert gate.reserve(("1.1",), scheduled_at=T0).allowed

    class LockProbe:
        def __init__(self) -> None:
            self.depth = 0
            self.entries = 0

        def __enter__(self) -> "LockProbe":
            assert self.depth == 0
            self.depth = 1
            self.entries += 1
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            assert self.depth == 1
            self.depth = 0

    probe = LockProbe()

    class GuardedAccepted(dict[str, list[tuple[int, int]]]):
        def _assert_guarded(self) -> None:
            assert probe.depth == 1

        def __iter__(self):  # type: ignore[no-untyped-def]
            self._assert_guarded()
            return super().__iter__()

        def __getitem__(self, key: str) -> list[tuple[int, int]]:
            self._assert_guarded()
            return super().__getitem__(key)

        def items(self):  # type: ignore[no-untyped-def]
            self._assert_guarded()
            return super().items()

    seed = {
        market_id: list(accepted)
        for market_id, accepted in gate._accepted.items()
    }

    gate._accepted = GuardedAccepted(
        {market_id: list(accepted) for market_id, accepted in seed.items()}
    )
    gate._lock = probe  # type: ignore[assignment]
    snapshot = gate.snapshot()
    assert snapshot.markets[0].market_id == "1.1"

    # Re-arm the guarded mapping from the plain pre-probe seed. Reading the
    # already-guarded mapping here would itself be an out-of-lock test bug.
    gate._accepted = GuardedAccepted(
        {market_id: list(accepted) for market_id, accepted in seed.items()}
    )
    decision = gate.reserve(("1.2",), scheduled_at=T0 + timedelta(microseconds=1))
    assert decision.allowed
    assert probe.entries == 2
    assert probe.depth == 0


def test_process_control_after_rate_mutation_rolls_back_only_own_capacity() -> None:
    gate = BetfairMarketBookPerMarketRateGate()
    interrupt = KeyboardInterrupt("rate reserve interrupted after mutation")
    original_lock = gate._lock

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

    gate._lock = InterruptAfterMutationLock()

    with pytest.raises(KeyboardInterrupt) as exc_info:
        gate.reserve(("1.234",), scheduled_at=T0)

    assert exc_info.value is interrupt
    state = gate.snapshot()
    assert state.markets == ()
    assert state.last_scheduled_at_utc_us == int(T0.timestamp() * 1_000_000)
    assert gate._next_reservation_generation == 2

    successor = gate.reserve(("1.234",), scheduled_at=T0)
    assert successor.allowed is True
    assert gate.snapshot().markets[0].accepted_at_utc_us == (
        int(T0.timestamp() * 1_000_000),
    )


def test_interrupted_rate_cleanup_preserves_concurrent_successor_same_timestamp() -> None:
    gate = BetfairMarketBookPerMarketRateGate()
    interrupt = KeyboardInterrupt("rate reserve interrupted after successor")
    original_lock = gate._lock
    successor_decisions = []

    class SuccessorThenInterruptLock:
        def __init__(self) -> None:
            self.raise_once = True

        def __enter__(self):
            return original_lock.__enter__()

        def __exit__(self, exc_type, exc, tb):
            result = original_lock.__exit__(exc_type, exc, tb)
            if self.raise_once and exc_type is None:
                self.raise_once = False
                successor_decisions.append(
                    BetfairMarketBookPerMarketRateGate.reserve(
                        gate,
                        ("1.234",),
                        scheduled_at=T0,
                    )
                )
                raise interrupt
            return result

    gate._lock = SuccessorThenInterruptLock()

    with pytest.raises(KeyboardInterrupt) as exc_info:
        gate.reserve(("1.234",), scheduled_at=T0)

    assert exc_info.value is interrupt
    assert len(successor_decisions) == 1
    assert successor_decisions[0].allowed is True
    state = gate.snapshot()
    assert state.markets[0].accepted_at_utc_us == (
        int(T0.timestamp() * 1_000_000),
    )
    assert gate._next_reservation_generation == 3


def test_competing_fifth_call_reservations_are_serialized() -> None:
    gate = BetfairMarketBookPerMarketRateGate()
    for index in range(4):
        assert gate.reserve(
            ("1.234",),
            scheduled_at=T0 + timedelta(microseconds=index),
        ).allowed

    start = Barrier(3)
    decisions = []
    errors: list[BaseException] = []

    def reserve_fifth() -> None:
        try:
            start.wait()
            decisions.append(
                gate.reserve(
                    ("1.234",),
                    scheduled_at=T0 + timedelta(microseconds=10),
                )
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    workers = [Thread(target=reserve_fifth) for _ in range(2)]
    for worker in workers:
        worker.start()
    start.wait()
    for worker in workers:
        worker.join()

    assert errors == []
    assert len(decisions) == 2
    assert sum(decision.allowed for decision in decisions) == 1
    assert sum(not decision.allowed for decision in decisions) == 1

    state = gate.snapshot()
    window = next(item for item in state.markets if item.market_id == "1.234")
    assert len(window.accepted_at_utc_us) == 5



def test_local_rate_admission_never_claims_complete_provider_dispatch_authority() -> None:
    gate = BetfairMarketBookPerMarketRateGate()
    allowed = gate.reserve(("1.234",), scheduled_at=T0)
    for index in range(1, 5):
        assert gate.reserve(
            ("1.234",),
            scheduled_at=T0 + timedelta(microseconds=index),
        ).allowed
    denied = gate.reserve(
        ("1.234",),
        scheduled_at=T0 + timedelta(microseconds=10),
    )

    assert allowed.allowed is True
    assert denied.allowed is False
    for decision in (allowed, denied):
        assert decision.provider_limit_coverage_complete is False
        assert decision.provider_dispatch_authorized is False



def test_restart_state_rejects_noncanonical_window_shapes() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        MarketBookRateWindowState("1.1", ())

    window = MarketBookRateWindowState("1.1", (1,))
    with pytest.raises(ValueError, match="requires last scheduled"):
        MarketBookRateGateState(
            BETFAIR_MARKETBOOK_RATE_POLICY_VERSION,
            None,
            (window,),
        )

    with pytest.raises(ValueError, match="outside active window"):
        MarketBookRateGateState(
            BETFAIR_MARKETBOOK_RATE_POLICY_VERSION,
            1_000_000,
            (MarketBookRateWindowState("1.1", (0,)),),
        )


def test_rate_decision_rejects_inconsistent_or_widened_authority() -> None:
    from autosport.betfair_marketbook_rate_gate import MarketBookRateDecision

    with pytest.raises(ValueError, match="allowed decision"):
        MarketBookRateDecision(
            market_ids=("1.1",),
            scheduled_at_utc_us=1,
            allowed=True,
            blocked_market_ids=("1.1",),
            next_eligible_at_utc_us=2,
        )
    with pytest.raises(ValueError, match="denied decision requires"):
        MarketBookRateDecision(
            market_ids=("1.1",),
            scheduled_at_utc_us=1,
            allowed=False,
        )
    with pytest.raises(ValueError, match="complete provider-limit"):
        MarketBookRateDecision(
            market_ids=("1.1",),
            scheduled_at_utc_us=1,
            allowed=True,
            provider_limit_coverage_complete=True,
        )
    with pytest.raises(ValueError, match="authorize provider dispatch"):
        MarketBookRateDecision(
            market_ids=("1.1",),
            scheduled_at_utc_us=1,
            allowed=True,
            provider_dispatch_authorized=True,
        )


def test_rate_gate_rejects_subclassed_authority_inputs_before_mutation() -> None:
    class MarketId(str):
        pass

    class ScheduledInstant(datetime):
        def astimezone(self, tz=None):
            raise AssertionError("subclass clock override must not execute")

    value = BetfairMarketBookPerMarketRateGate()

    with pytest.raises(TypeError, match="market_id must be exact str"):
        value.reserve((MarketId("1.1"),), scheduled_at=T0)

    state = value.snapshot()
    assert state.markets == ()
    assert state.last_scheduled_at_utc_us is None

    hostile_time = ScheduledInstant(2026, 9, 22, tzinfo=timezone.utc)
    with pytest.raises(TypeError, match="scheduled_at must be exact datetime"):
        value.reserve(("1.1",), scheduled_at=hostile_time)

    state = value.snapshot()
    assert state.markets == ()
    assert state.last_scheduled_at_utc_us is None

    class PolicyVersion(str):
        pass

    with pytest.raises(ValueError, match="unsupported Betfair MarketBook rate policy"):
        MarketBookRateGateState(
            PolicyVersion(BETFAIR_MARKETBOOK_RATE_POLICY_VERSION),
            None,
            (),
        )


def test_rate_inputs_are_normalized_before_gate_lock() -> None:
    gate = BetfairMarketBookPerMarketRateGate()

    class LockProbe:
        def __init__(self) -> None:
            self.depth = 0
            self.entries = 0

        def __enter__(self) -> "LockProbe":
            assert self.depth == 0
            self.depth = 1
            self.entries += 1
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            assert self.depth == 1
            self.depth = 0

    probe = LockProbe()
    gate._lock = probe  # type: ignore[assignment]

    class MarketIds:
        def __iter__(self):  # type: ignore[no-untyped-def]
            assert probe.depth == 0
            return iter(("1.1",))

    class GuardedTimezone(tzinfo):
        def utcoffset(self, dt: datetime | None) -> timedelta:
            assert probe.depth == 0
            return timedelta(0)

        def dst(self, dt: datetime | None) -> timedelta:
            assert probe.depth == 0
            return timedelta(0)

        def tzname(self, dt: datetime | None) -> str:
            assert probe.depth == 0
            return "GUARDED"

    decision = gate.reserve(
        MarketIds(),  # type: ignore[arg-type]
        scheduled_at=datetime(2026, 9, 22, tzinfo=GuardedTimezone()),
    )

    assert decision.allowed is True
    assert decision.market_ids == ("1.1",)
    assert probe.entries == 1
    assert probe.depth == 0


def test_restart_revalidates_tampered_frozen_rate_state() -> None:
    gate = BetfairMarketBookPerMarketRateGate()
    assert gate.reserve(("1.1",), scheduled_at=T0).allowed
    state = gate.snapshot()
    window = state.markets[0]

    object.__setattr__(window, "accepted_at_utc_us", ("forged",))

    with pytest.raises(TypeError, match="accepted rate timestamp"):
        BetfairMarketBookPerMarketRateGate(state=state)


def test_restart_revalidates_tampered_rate_policy_identity() -> None:
    state = BetfairMarketBookPerMarketRateGate().snapshot()
    object.__setattr__(state, "policy_version", "forged-policy")

    with pytest.raises(ValueError, match="unsupported Betfair MarketBook rate policy"):
        BetfairMarketBookPerMarketRateGate(state=state)



def test_rate_gate_runtime_authority_is_closure_bound(monkeypatch) -> None:
    gate_type = BetfairMarketBookPerMarketRateGate
    state_type = MarketBookRateGateState
    window_type = MarketBookRateWindowState
    decision_type = _rate_gate_module.MarketBookRateDecision
    original_policy = BETFAIR_MARKETBOOK_RATE_POLICY_VERSION

    monkeypatch.setattr(
        _rate_gate_module,
        "_validate_market_id",
        lambda value: "forged-market",
    )
    monkeypatch.setattr(
        _rate_gate_module,
        "_normalize_market_ids",
        lambda value: ("forged-market",),
    )
    monkeypatch.setattr(_rate_gate_module, "_utc_microseconds", lambda value: 0)
    monkeypatch.setattr(
        _rate_gate_module,
        "_datetime_from_utc_microseconds",
        lambda value: datetime(1999, 1, 1, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(_rate_gate_module, "_MAX_CALLS_PER_WINDOW", 999)
    monkeypatch.setattr(_rate_gate_module, "_WINDOW_MICROSECONDS", 1)
    monkeypatch.setattr(_rate_gate_module, "BETFAIR_MARKETBOOK_RATE_POLICY_VERSION", "forged")
    monkeypatch.setattr(_rate_gate_module, "MarketBookRateDecision", object)
    monkeypatch.setattr(_rate_gate_module, "MarketBookRateWindowState", object)
    monkeypatch.setattr(_rate_gate_module, "MarketBookRateGateState", object)
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

    value = gate_type()
    decisions = [
        value.reserve(
            ("1.234",),
            scheduled_at=T0 + timedelta(milliseconds=100 * index),
        )
        for index in range(5)
    ]
    denied = value.reserve(
        ("1.234",),
        scheduled_at=T0 + timedelta(milliseconds=500),
    )
    state = value.snapshot()

    assert all(type(decision) is decision_type and decision.allowed for decision in decisions)
    assert decisions[0].scheduled_at == T0
    assert type(denied) is decision_type
    assert denied.allowed is False
    assert denied.blocked_market_ids == ("1.234",)
    assert denied.next_eligible_at == T0 + timedelta(seconds=1)
    assert type(state) is state_type
    assert len(state.markets) == 1
    assert type(state.markets[0]) is window_type
    assert state.policy_version == original_policy
    assert value.policy_version == original_policy


def test_rate_gate_restart_validation_ignores_rebound_dto_validators(monkeypatch) -> None:
    gate_type = BetfairMarketBookPerMarketRateGate
    original = gate_type()
    assert original.reserve(("1.234",), scheduled_at=T0).allowed
    state = original.snapshot()
    window = state.markets[0]

    monkeypatch.setattr(
        MarketBookRateGateState,
        "__post_init__",
        lambda self: None,
    )
    monkeypatch.setattr(
        MarketBookRateWindowState,
        "__post_init__",
        lambda self: None,
    )
    object.__setattr__(window, "accepted_at_utc_us", ("forged",))

    with pytest.raises(TypeError, match="accepted rate timestamp"):
        gate_type(state=state)


def test_rate_snapshot_is_detached_from_live_window_authority() -> None:
    value = BetfairMarketBookPerMarketRateGate()
    for index in range(5):
        assert value.reserve(
            ("1.234",),
            scheduled_at=T0 + timedelta(milliseconds=100 * index),
        ).allowed

    snapshot = value.snapshot()
    exposed = snapshot.markets[0]
    object.__setattr__(exposed, "accepted_at_utc_us", ())

    live = value.snapshot()
    assert len(live.markets[0].accepted_at_utc_us) == 5
    denied = value.reserve(
        ("1.234",),
        scheduled_at=T0 + timedelta(milliseconds=500),
    )
    assert denied.allowed is False


def test_rate_restart_detaches_imported_window_authority() -> None:
    source = BetfairMarketBookPerMarketRateGate()
    assert source.reserve(("1.234",), scheduled_at=T0).allowed
    state = source.snapshot()

    restored = BetfairMarketBookPerMarketRateGate(state=state)
    imported = state.markets[0]
    object.__setattr__(imported, "market_id", "forged-after-restart")
    object.__setattr__(imported, "accepted_at_utc_us", ())

    live = restored.snapshot()
    assert len(live.markets) == 1
    assert live.markets[0].market_id == "1.234"
    assert live.markets[0].accepted_at_utc_us == (
        int(T0.timestamp() * 1_000_000),
    )


def test_rate_gate_instance_storage_authority_ignores_rebound_special_methods(
    monkeypatch,
) -> None:
    gate_type = BetfairMarketBookPerMarketRateGate
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

    for index in range(5):
        decision = value.reserve(
            ["1.234"],
            scheduled_at=T0 + timedelta(microseconds=index),
        )
        assert decision.allowed is True

    denied = value.reserve(
        ["1.234"],
        scheduled_at=T0 + timedelta(microseconds=5),
    )
    assert denied.allowed is False
    snapshot = value.snapshot()
    assert len(snapshot.markets) == 1
    assert len(snapshot.markets[0].accepted_at_utc_us) == 5

    restored = gate_type(snapshot)
    assert restored.snapshot() == snapshot


def test_rate_restart_authority_ignores_rebound_state_slot_descriptor(
    monkeypatch,
) -> None:
    gate_type = BetfairMarketBookPerMarketRateGate
    source = gate_type()
    for index in range(5):
        assert source.reserve(
            ["1.234"],
            scheduled_at=T0 + timedelta(microseconds=index),
        ).allowed
    state = source.snapshot()

    monkeypatch.setattr(
        MarketBookRateGateState,
        "markets",
        property(lambda self: ()),
    )

    restored = gate_type(state)
    denied = restored.reserve(
        ["1.234"],
        scheduled_at=T0 + timedelta(microseconds=5),
    )
    assert denied.allowed is False


def test_rate_restart_authority_ignores_rebound_window_slot_descriptor(
    monkeypatch,
) -> None:
    gate_type = BetfairMarketBookPerMarketRateGate
    source = gate_type()
    for index in range(5):
        assert source.reserve(
            ["1.234"],
            scheduled_at=T0 + timedelta(microseconds=index),
        ).allowed
    state = source.snapshot()
    first_timestamp = state.markets[0].accepted_at_utc_us[0]

    monkeypatch.setattr(
        MarketBookRateWindowState,
        "accepted_at_utc_us",
        property(lambda self: (first_timestamp,)),
    )

    restored = gate_type(state)
    denied = restored.reserve(
        ["1.234"],
        scheduled_at=T0 + timedelta(microseconds=5),
    )
    assert denied.allowed is False


def test_rate_decision_construction_ignores_rebound_field_descriptor(
    monkeypatch,
) -> None:
    gate_type = BetfairMarketBookPerMarketRateGate
    value = gate_type()

    with monkeypatch.context() as context:
        context.setattr(
            MarketBookRateDecision,
            "allowed",
            property(lambda self: False),
        )
        decision = value.reserve(("1.234",), scheduled_at=T0)

    assert decision.allowed is True
    assert decision.scheduled_at == T0


def test_rate_decision_time_property_ignores_rebound_timestamp_descriptor(
    monkeypatch,
) -> None:
    value = BetfairMarketBookPerMarketRateGate()
    decision = value.reserve(("1.234",), scheduled_at=T0)

    monkeypatch.setattr(
        MarketBookRateDecision,
        "scheduled_at_utc_us",
        property(lambda self: 0),
    )

    assert decision.scheduled_at == T0


def test_rate_timezone_offset_is_observed_once_before_gate_lock() -> None:
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
    value = BetfairMarketBookPerMarketRateGate()

    decision = value.reserve(("1.234",), scheduled_at=local)

    assert zone.calls == 1
    assert decision.scheduled_at == T0
