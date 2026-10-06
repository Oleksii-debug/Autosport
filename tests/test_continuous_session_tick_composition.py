from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from autosport import continuous_session


_AT = "2026-10-06T00:00:00+00:00"


def _state(root: Path) -> continuous_session._ContinuousSessionState:
    return continuous_session._ContinuousSessionState(
        root / "continuous_session.json",
        session_id="session-tick-composition",
        source_id="provider-a",
        clock=lambda: _AT,
    )


class _DeltaStore:
    def __init__(self, *, forbidden: bool = False) -> None:
        self.calls = 0
        self.forbidden = forbidden

    def deltas_after_commit(self, **_kwargs):
        if self.forbidden:
            raise AssertionError("rebound collector projection store executed")
        self.calls += 1
        return ()


class _Collector:
    source_id = "provider-a"
    config = type("ConfigStub", (), {"max_items": 1})()

    def __init__(self, callback=None, *, forbidden_store: bool = False) -> None:
        self.callback = callback
        self.delta_store = _DeltaStore(forbidden=forbidden_store)

    def run_cycle(self):
        if self.callback is not None:
            self.callback()
        return type(
            "SuccessfulCycle",
            (),
            {
                "provider_unavailable": False,
                "source_id": "provider-a",
                "committed_delta_ids": (),
            },
        )()


class _InvalidationBuffer:
    pending_count = 0
    full_refresh_required = False

    def drain(self, *, max_items: int):
        assert max_items == 250
        return continuous_session.MirrorInvalidationBatch(
            changed_keys=(),
            full_refresh_required=False,
            has_more=False,
        )


class _DependencyIndex:
    input_ids = ()

    def affected_inputs(self, _batch):
        return ()


def _base_coordinator(root: Path) -> continuous_session.ContinuousSessionCoordinator:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    coordinator._state = _state(root)
    coordinator.clock = lambda: _AT
    coordinator.causal_view = continuous_session.CausalView.AS_KNOWN_AT_DECISION
    coordinator.required_history = None
    coordinator.market_store = object()
    coordinator.paper_book_path = root / "paper_book.json"
    coordinator.initial_bankroll = "100"
    coordinator.workspace = root
    coordinator.max_invalidation_batches_per_tick = 4
    coordinator.max_invalidation_items_per_batch = 250
    coordinator.invalidation_buffer = _InvalidationBuffer()
    coordinator.dependency_index = _DependencyIndex()
    coordinator.outcome_authority = None
    coordinator.settlement_learning_handoff = None
    return coordinator


def test_tick_keeps_collector_for_observation_and_projection() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        replacement = _Collector(forbidden_store=True)
        original = _Collector(callback=lambda: setattr(coordinator, "collector", replacement))
        coordinator.collector = original

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        result = coordinator.tick()

        assert result.cycle_index == 1
        assert original.delta_store.calls == 1
        assert replacement.delta_store.calls == 0


def test_tick_keeps_lifecycle_desktop_and_market_composition() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        original_market = object()
        replacement_market = object()
        coordinator.market_store = original_market

        class Desktop:
            def __init__(self, *, forbidden: bool = False) -> None:
                self.calls = 0
                self.forbidden = forbidden

            def drain(self, **_kwargs):
                if self.forbidden:
                    raise AssertionError("rebound desktop consumer executed")
                self.calls += 1
                return ()

        class Lifecycle:
            def __init__(self, *, forbidden: bool = False) -> None:
                self.calls = 0
                self.forbidden = forbidden

            def register_eligible(self, market_store, **_kwargs):
                if self.forbidden:
                    raise AssertionError("rebound lifecycle executed")
                assert market_store is original_market
                self.calls += 1
                return ()

        original_desktop = Desktop()
        replacement_desktop = Desktop(forbidden=True)
        original_lifecycle = Lifecycle()
        replacement_lifecycle = Lifecycle(forbidden=True)
        coordinator.desktop_consumer = original_desktop
        coordinator.lifecycle = original_lifecycle

        def rebind() -> None:
            coordinator.desktop_consumer = replacement_desktop
            coordinator.lifecycle = replacement_lifecycle
            coordinator.market_store = replacement_market

        coordinator.collector = _Collector(callback=rebind)

        result = coordinator.tick()

        assert result.cycle_index == 1
        assert original_desktop.calls == 1
        assert replacement_desktop.calls == 0
        assert original_lifecycle.calls == 1
        assert replacement_lifecycle.calls == 0


def test_tick_keeps_outcome_and_learning_authorities() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        record = continuous_session.EventLifecycleRecord(
            identity="provider-a:event-1",
            source_id="provider-a",
            sport="table_tennis",
            event_id="event-1",
            phase=continuous_session.EventPhase.COMPLETED,
            first_discovered_at=_AT,
            last_available_at=_AT,
            scheduled_start_at=None,
            completion_ref="completion-1",
            settlement_ref="settlement-1",
            completion_discovered_at=_AT,
            settlement_discovered_at=_AT,
            last_discovered_at=_AT,
        )
        resolution = continuous_session.SettlementResolution(
            event_identity=record.identity,
            settlement_ref="settlement-1",
            quote_outcomes={"quote-1": "win"},
            evidence_id="evidence-1",
            evidence_sha256="a" * 64,
            available_at=_AT,
        )

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

            def records(self):
                return (record,)

        class OutcomeAuthority:
            def __init__(self, *, forbidden: bool = False) -> None:
                self.calls = 0
                self.forbidden = forbidden

            def resolve(self, _record, *, as_of: str):
                if self.forbidden:
                    raise AssertionError("rebound outcome authority executed")
                assert as_of == _AT
                self.calls += 1
                return resolution

        class Handoff:
            def __init__(self, *, forbidden: bool = False) -> None:
                self.prepared = 0
                self.reconciled = 0
                self.forbidden = forbidden

            def prepare_settlement(self, **_kwargs):
                if self.forbidden:
                    raise AssertionError("rebound settlement handoff prepare executed")
                self.prepared += 1
                return ()

            def reconcile_after_settlement(self, **_kwargs):
                if self.forbidden:
                    raise AssertionError("rebound settlement handoff reconcile executed")
                self.reconciled += 1

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        original_authority = OutcomeAuthority()
        replacement_authority = OutcomeAuthority(forbidden=True)
        original_handoff = Handoff()
        replacement_handoff = Handoff(forbidden=True)
        coordinator.lifecycle = Lifecycle()
        coordinator.desktop_consumer = Desktop()
        coordinator.outcome_authority = original_authority
        coordinator.settlement_learning_handoff = original_handoff

        def rebind() -> None:
            coordinator.outcome_authority = replacement_authority
            coordinator.settlement_learning_handoff = replacement_handoff

        coordinator.collector = _Collector(callback=rebind)

        result = coordinator.tick()

        assert result.settlement_evidence_ids == ("evidence-1",)
        assert original_authority.calls == 1
        assert replacement_authority.calls == 0
        assert original_handoff.prepared == 1
        assert original_handoff.reconciled == 1
        assert replacement_handoff.prepared == 0
        assert replacement_handoff.reconciled == 0

def test_tick_keeps_invalidation_routing_composition() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class Buffer:
            pending_count = 0
            full_refresh_required = False

            def __init__(self, *, forbidden: bool = False) -> None:
                self.calls = 0
                self.forbidden = forbidden

            def drain(self, *, max_items: int):
                if self.forbidden:
                    raise AssertionError("rebound invalidation buffer executed")
                assert max_items == 250
                self.calls += 1
                return continuous_session.MirrorInvalidationBatch(
                    changed_keys=(),
                    full_refresh_required=False,
                    has_more=False,
                )

        class Index:
            input_ids = ()

            def __init__(self, *, forbidden: bool = False) -> None:
                self.calls = 0
                self.forbidden = forbidden

            def affected_inputs(self, _batch):
                if self.forbidden:
                    raise AssertionError("rebound dependency index executed")
                self.calls += 1
                return ()

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        original_buffer = Buffer()
        replacement_buffer = Buffer(forbidden=True)
        original_index = Index()
        replacement_index = Index(forbidden=True)
        coordinator.invalidation_buffer = original_buffer
        coordinator.dependency_index = original_index
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        def rebind() -> None:
            coordinator.invalidation_buffer = replacement_buffer
            coordinator.dependency_index = replacement_index
            coordinator.max_invalidation_batches_per_tick = 1
            coordinator.max_invalidation_items_per_batch = 1

        coordinator.collector = _Collector(callback=rebind)

        result = coordinator.tick()

        assert result.cycle_index == 1
        assert original_buffer.calls == 1
        assert replacement_buffer.calls == 0
        assert original_index.calls == 1
        assert replacement_index.calls == 0

def test_tick_rejects_provider_time_economic_context_rebinding() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        original_book = coordinator.paper_book_path
        replacement_book = root / "attacker-paper-book.json"

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        def rebind() -> None:
            coordinator.workspace = root / "attacker-workspace"
            coordinator.paper_book_path = replacement_book
            coordinator.initial_bankroll = "999999"

        coordinator.collector = _Collector(callback=rebind)

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="settlement economic configuration changed during tick",
        ):
            coordinator.tick()

        assert not original_book.exists()
        assert not replacement_book.exists()


def test_tick_rejects_prepare_time_economic_path_rebinding_before_settlement() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        original_book = coordinator.paper_book_path
        replacement_book = root / "attacker-paper-book.json"
        record = continuous_session.EventLifecycleRecord(
            identity="provider-a:event-1",
            source_id="provider-a",
            sport="table_tennis",
            event_id="event-1",
            phase=continuous_session.EventPhase.COMPLETED,
            first_discovered_at=_AT,
            last_available_at=_AT,
            scheduled_start_at=None,
            completion_ref="completion-1",
            settlement_ref="settlement-1",
            completion_discovered_at=_AT,
            settlement_discovered_at=_AT,
            last_discovered_at=_AT,
        )
        resolution = continuous_session.SettlementResolution(
            event_identity=record.identity,
            settlement_ref="settlement-1",
            quote_outcomes={"quote-1": "win"},
            evidence_id="evidence-prepare-rebind",
            evidence_sha256="b" * 64,
            available_at=_AT,
        )

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

            def records(self):
                return (record,)

        class OutcomeAuthority:
            def resolve(self, _record, *, as_of: str):
                assert as_of == _AT
                return resolution

        class Handoff:
            def prepare_settlement(self, **_kwargs):
                coordinator.paper_book_path = replacement_book
                return ()

            def reconcile_after_settlement(self, **_kwargs):
                raise AssertionError("reconcile ran after economic context rebinding")

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()
        coordinator.outcome_authority = OutcomeAuthority()
        coordinator.settlement_learning_handoff = Handoff()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="settlement economic configuration changed during tick",
        ):
            coordinator.tick()

        assert not original_book.exists()
        assert not replacement_book.exists()

def test_tick_snapshots_cycle_metadata_before_callbacks() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class Cycle:
            provider_unavailable = False
            source_id = "provider-a"
            committed_delta_ids = ("delta-original",)

        cycle = Cycle()

        class Collector:
            source_id = "provider-a"
            config = type("ConfigStub", (), {"max_items": 1})()
            delta_store = _DeltaStore()

            def run_cycle(self):
                return cycle

        class Desktop:
            def drain(self, **_kwargs):
                cycle.source_id = "provider-a:attacker"
                cycle.committed_delta_ids = ("delta-attacker",)
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        coordinator.collector = Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        result = coordinator.tick()

        assert result.source_id == "provider-a"
        assert result.committed_delta_ids == ("delta-original",)


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    (
        ("source_id", " provider-a"),
        ("provider_unavailable", 1),
        ("committed_delta_ids", ["delta-1"]),
        ("committed_delta_ids", ("",)),
    ),
)
def test_tick_rejects_invalid_collector_cycle_metadata(
    field_name: str,
    invalid_value: object,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class Cycle:
            provider_unavailable = False
            source_id = "provider-a"
            committed_delta_ids = ()

        cycle = Cycle()
        setattr(cycle, field_name, invalid_value)

        class Collector:
            def run_cycle(self):
                return cycle

        coordinator.collector = Collector()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="collector returned invalid continuous-session cycle metadata",
        ):
            coordinator.tick()

        assert coordinator._state.snapshot().last_error_code == "ContinuousSessionError"


def test_tick_rejects_reconcile_time_economic_path_rebinding_before_success() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        replacement_book = root / "attacker-paper-book.json"
        record = continuous_session.EventLifecycleRecord(
            identity="provider-a:event-1",
            source_id="provider-a",
            sport="table_tennis",
            event_id="event-1",
            phase=continuous_session.EventPhase.COMPLETED,
            first_discovered_at=_AT,
            last_available_at=_AT,
            scheduled_start_at=None,
            completion_ref="completion-1",
            settlement_ref="settlement-1",
            completion_discovered_at=_AT,
            settlement_discovered_at=_AT,
            last_discovered_at=_AT,
        )
        resolution = continuous_session.SettlementResolution(
            event_identity=record.identity,
            settlement_ref="settlement-1",
            quote_outcomes={"quote-1": "win"},
            evidence_id="evidence-reconcile-rebind",
            evidence_sha256="c" * 64,
            available_at=_AT,
        )

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

            def records(self):
                return (record,)

        class OutcomeAuthority:
            def resolve(self, _record, *, as_of: str):
                assert as_of == _AT
                return resolution

        class Handoff:
            def prepare_settlement(self, **_kwargs):
                return ()

            def reconcile_after_settlement(self, **_kwargs):
                coordinator.paper_book_path = replacement_book

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()
        coordinator.outcome_authority = OutcomeAuthority()
        coordinator.settlement_learning_handoff = Handoff()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="settlement economic configuration changed during tick",
        ):
            coordinator.tick()

        snapshot = coordinator._state.snapshot()
        assert snapshot.cycles_completed == 0
        assert snapshot.last_error_code == "ContinuousSessionError"

@pytest.mark.parametrize(
    "delivered",
    (
        ["delta-1"],
        ("",),
        (" delta-1",),
        (1,),
    ),
)
def test_tick_rejects_invalid_desktop_delivery_ids(delivered: object) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class Desktop:
            def drain(self, **_kwargs):
                return delivered

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="desktop consumer returned invalid delivered delta ids",
        ):
            coordinator.tick()


@pytest.mark.parametrize(
    ("max_batches", "max_items"),
    (
        (0, 250),
        (True, 250),
        (4, 0),
        (4, True),
    ),
)
def test_tick_rejects_invalid_invalidation_bounds(
    max_batches: object,
    max_items: object,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()
        coordinator.max_invalidation_batches_per_tick = max_batches
        coordinator.max_invalidation_items_per_batch = max_items

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="continuous-session invalidation bounds are invalid",
        ):
            coordinator.tick()


def test_tick_rejects_noncanonical_invalidation_batch() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class DerivedBatch(continuous_session.MirrorInvalidationBatch):
            pass

        class Buffer:
            pending_count = 0
            full_refresh_required = False

            def drain(self, *, max_items: int):
                assert max_items == 250
                return DerivedBatch(
                    changed_keys=(),
                    full_refresh_required=False,
                    has_more=False,
                )

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()
        coordinator.invalidation_buffer = Buffer()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="invalidation buffer returned an invalid batch",
        ):
            coordinator.tick()


@pytest.mark.parametrize(
    "routed",
    (
        ["input-1"],
        ("",),
        (" input-1",),
        (1,),
    ),
)
def test_tick_rejects_invalid_affected_input_ids(routed: object) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        class Index:
            input_ids = ()

            def affected_inputs(self, _batch):
                return routed

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()
        coordinator.dependency_index = Index()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="dependency index returned invalid affected inputs",
        ):
            coordinator.tick()


def test_tick_rejects_phantom_lifecycle_registration() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ("catalog:provider-a:event-1",)

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="lifecycle reported an input absent from dependency index",
        ):
            coordinator.tick()

def test_tick_keeps_bound_callback_handles_after_provider_rebinding() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        record = continuous_session.EventLifecycleRecord(
            identity="provider-a:event-1",
            source_id="provider-a",
            sport="table_tennis",
            event_id="event-1",
            phase=continuous_session.EventPhase.COMPLETED,
            first_discovered_at=_AT,
            last_available_at=_AT,
            scheduled_start_at=None,
            completion_ref="completion-1",
            settlement_ref="settlement-1",
            completion_discovered_at=_AT,
            settlement_discovered_at=_AT,
            last_discovered_at=_AT,
        )
        resolution = continuous_session.SettlementResolution(
            event_identity=record.identity,
            settlement_ref="settlement-1",
            quote_outcomes={"quote-1": "win"},
            evidence_id="evidence-bound-handles",
            evidence_sha256="d" * 64,
            available_at=_AT,
        )

        class Desktop:
            def __init__(self) -> None:
                self.calls = 0

            def drain(self, **_kwargs):
                self.calls += 1
                return ()

        class Lifecycle:
            def __init__(self) -> None:
                self.register_calls = 0
                self.records_calls = 0

            def register_eligible(self, *_args, **_kwargs):
                self.register_calls += 1
                return ()

            def records(self):
                self.records_calls += 1
                return (record,)

        class Authority:
            def __init__(self) -> None:
                self.calls = 0

            def resolve(self, _record, *, as_of: str):
                assert as_of == _AT
                self.calls += 1
                return resolution

        class Handoff:
            def __init__(self) -> None:
                self.prepared = 0
                self.reconciled = 0

            def prepare_settlement(self, **_kwargs):
                self.prepared += 1
                return ()

            def reconcile_after_settlement(self, **_kwargs):
                self.reconciled += 1

        desktop = Desktop()
        lifecycle = Lifecycle()
        authority = Authority()
        handoff = Handoff()
        coordinator.desktop_consumer = desktop
        coordinator.lifecycle = lifecycle
        coordinator.outcome_authority = authority
        coordinator.settlement_learning_handoff = handoff

        def attacker(*_args, **_kwargs):
            raise AssertionError("provider-time rebound callback executed")

        def rebind() -> None:
            desktop.drain = attacker  # type: ignore[method-assign]
            lifecycle.register_eligible = attacker  # type: ignore[method-assign]
            lifecycle.records = attacker  # type: ignore[method-assign]
            authority.resolve = attacker  # type: ignore[method-assign]
            handoff.prepare_settlement = attacker  # type: ignore[method-assign]
            handoff.reconcile_after_settlement = attacker  # type: ignore[method-assign]

        coordinator.collector = _Collector(callback=rebind)

        result = coordinator.tick()

        assert result.settlement_evidence_ids == ("evidence-bound-handles",)
        assert desktop.calls == 1
        assert lifecycle.register_calls == 1
        assert lifecycle.records_calls == 1
        assert authority.calls == 1
        assert handoff.prepared == 1
        assert handoff.reconciled == 1


def test_tick_keeps_collector_projection_fields_after_provider_rebinding() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        original = _Collector()
        replacement_store = _DeltaStore(forbidden=True)

        def rebind() -> None:
            original.source_id = "provider-attacker"
            original.delta_store = replacement_store
            original.config = type("ConfigStub", (), {"max_items": 999})()

        original.callback = rebind
        coordinator.collector = original
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        result = coordinator.tick()

        assert result.source_id == "provider-a"
        assert original.delta_store is replacement_store
        assert replacement_store.calls == 0


def test_tick_keeps_collector_run_callable_across_clock_callback() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        collector = _Collector()
        coordinator.collector = collector
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        def attacker_run():
            raise AssertionError("clock-time rebound collector run executed")

        def clock() -> str:
            collector.run_cycle = attacker_run  # type: ignore[method-assign]
            return _AT

        coordinator.clock = clock

        result = coordinator.tick()

        assert result.cycle_index == 1

def test_tick_keeps_invalidation_bound_methods_after_provider_rebinding() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class Buffer:
            pending_count = 0
            full_refresh_required = False

            def __init__(self) -> None:
                self.calls = 0

            def drain(self, *, max_items: int):
                assert max_items == 250
                self.calls += 1
                return continuous_session.MirrorInvalidationBatch(
                    changed_keys=(),
                    full_refresh_required=False,
                    has_more=False,
                )

        class Index:
            def __init__(self) -> None:
                self.ids = {"input-old"}
                self.affected_calls = 0
                self.register_calls = 0
                self.unregister_calls = 0

            @property
            def input_ids(self):
                return tuple(sorted(self.ids))

            def affected_inputs(self, _batch):
                self.affected_calls += 1
                return ()

            def register(self, input_id: str, **_selectors):
                self.register_calls += 1
                self.ids.add(input_id)

            def unregister(self, input_id: str):
                self.unregister_calls += 1
                self.ids.discard(input_id)
                return True

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(
                self,
                _market_store,
                *,
                register_input,
                retire_input,
                **_kwargs,
            ):
                register_input("input-new", source_ids="provider-a")
                retire_input("input-old")
                return ("input-new",)

        buffer = Buffer()
        index = Index()
        coordinator.invalidation_buffer = buffer
        coordinator.dependency_index = index
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        def attacker(*_args, **_kwargs):
            raise AssertionError("provider-time rebound invalidation method executed")

        def rebind() -> None:
            buffer.drain = attacker  # type: ignore[method-assign]
            index.affected_inputs = attacker  # type: ignore[method-assign]
            index.register = attacker  # type: ignore[method-assign]
            index.unregister = attacker  # type: ignore[method-assign]

        coordinator.collector = _Collector(callback=rebind)

        result = coordinator.tick()

        assert result.registered_input_ids == ("input-new",)
        assert result.retired_input_ids == ("input-old",)
        assert buffer.calls == 1
        assert index.affected_calls == 1
        assert index.register_calls == 1
        assert index.unregister_calls == 1
        assert index.input_ids == ("input-new",)

