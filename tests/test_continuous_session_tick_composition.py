from __future__ import annotations

import tempfile
from datetime import timedelta
from pathlib import Path

import pytest

from autosport import continuous_session
from autosport.market_mirror import MarketMirror


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
    coordinator.required_history = timedelta(0)
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
                raise AssertionError(
                    "desktop side effects ran after economic context changed"
                )

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                raise AssertionError(
                    "lifecycle side effects ran after economic context changed"
                )

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
            source_id = "provider-a"
            config = type("ConfigStub", (), {"max_items": 1})()
            delta_store = _DeltaStore()

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


def test_tick_rejects_incomplete_full_refresh_routing() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class Buffer:
            pending_count = 0
            full_refresh_required = False

            def drain(self, *, max_items: int):
                assert max_items == 250
                return continuous_session.MirrorInvalidationBatch(
                    changed_keys=(),
                    full_refresh_required=True,
                    has_more=False,
                )

        class Index:
            input_ids = ("input-a", "input-b")

            def affected_inputs(self, _batch):
                return ("input-a",)

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        coordinator.collector = _Collector()
        coordinator.invalidation_buffer = Buffer()
        coordinator.dependency_index = Index()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="full refresh routing is incomplete or reordered",
        ):
            coordinator.tick()

        assert (
            coordinator._state.snapshot().last_error_code
            == "ContinuousSessionError"
        )


def test_tick_rejects_unregistered_affected_input_id() -> None:
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
            input_ids = ("registered",)

            def affected_inputs(self, _batch):
                return ("phantom",)

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()
        coordinator.dependency_index = Index()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="dependency index routed an unregistered input",
        ):
            coordinator.tick()

        assert (
            coordinator._state.snapshot().last_error_code
            == "ContinuousSessionError"
        )


def test_tick_rejects_derived_dependency_index_before_invalidation_drain() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class DerivedIndex(continuous_session.FocusedMirrorDependencyIndex):
            pass

        index = DerivedIndex(MarketMirror())
        index.register("input-a", source_ids="provider-a")

        class Buffer:
            pending_count = 0
            full_refresh_required = False

            def drain(self, *, max_items: int):
                raise AssertionError(
                    "invalidation drain ran before dependency subtype rejection"
                )

        class Desktop:
            def drain(self, **_kwargs):
                raise AssertionError(
                    "desktop ran before dependency subtype rejection"
                )

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                raise AssertionError(
                    "lifecycle ran before dependency subtype rejection"
                )

        coordinator.collector = _Collector()
        coordinator.invalidation_buffer = Buffer()
        coordinator.dependency_index = index
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="canonical dependency index subtype is not supported",
        ):
            coordinator.tick()


def test_tick_rejects_invalidation_drain_selector_mutation() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
        index.register("input-a", source_ids="provider-a")

        class Buffer:
            pending_count = 0
            full_refresh_required = False

            def drain(self, *, max_items: int):
                assert max_items == 250
                assert index.unregister("input-a")
                index.register("input-a", source_ids="provider-b")
                return continuous_session.MirrorInvalidationBatch(
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
        coordinator.invalidation_buffer = Buffer()
        coordinator.dependency_index = index
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="routing authority changed during invalidation drain",
        ):
            coordinator.tick()

        assert (
            coordinator._state.snapshot().last_error_code
            == "ContinuousSessionError"
        )


def test_tick_rejects_invalidation_drain_mirror_rebinding() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
        index.register("input-a", source_ids="provider-a")
        replacement_mirror = MarketMirror()

        class Buffer:
            pending_count = 0
            full_refresh_required = False

            def drain(self, *, max_items: int):
                assert max_items == 250
                index._mirror = replacement_mirror
                return continuous_session.MirrorInvalidationBatch(
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
        coordinator.invalidation_buffer = Buffer()
        coordinator.dependency_index = index
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="routing authority changed during invalidation drain",
        ):
            coordinator.tick()

        assert (
            coordinator._state.snapshot().last_error_code
            == "ContinuousSessionError"
        )


def test_tick_rejects_invalidation_drain_identity_mutation() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class Index:
            input_ids = ("input-a",)

            def affected_inputs(self, _batch):
                raise AssertionError("routing must not run after drain mutates index")

        index = Index()

        class Buffer:
            pending_count = 0
            full_refresh_required = False

            def drain(self, *, max_items: int):
                assert max_items == 250
                index.input_ids = ("phantom",)
                return continuous_session.MirrorInvalidationBatch(
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
        coordinator.invalidation_buffer = Buffer()
        coordinator.dependency_index = index
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="invalidation drain mutated dependency index input identity state",
        ):
            coordinator.tick()

        assert (
            coordinator._state.snapshot().last_error_code
            == "ContinuousSessionError"
        )


def test_tick_orders_affected_inputs_by_registration_across_batches() -> None:
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
                    has_more=self.calls == 1,
                )

        class Index:
            input_ids = ("input-a", "input-b", "input-c")

            def __init__(self) -> None:
                self.calls = 0

            def affected_inputs(self, _batch):
                self.calls += 1
                return ("input-c",) if self.calls == 1 else ("input-a",)

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        buffer = Buffer()
        index = Index()
        coordinator.collector = _Collector()
        coordinator.invalidation_buffer = buffer
        coordinator.dependency_index = index
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        result = coordinator.tick()

        assert buffer.calls == 2
        assert index.calls == 2
        assert result.affected_input_ids == ("input-a", "input-c")


def test_tick_rejects_reordered_partial_affected_input_ids() -> None:
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
            input_ids = ("input-a", "input-b", "input-c")

            def affected_inputs(self, _batch):
                return ("input-c", "input-a")

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()
        coordinator.dependency_index = Index()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="dependency index affected input routing is reordered",
        ):
            coordinator.tick()

        assert (
            coordinator._state.snapshot().last_error_code
            == "ContinuousSessionError"
        )


def test_tick_rejects_affected_input_routing_identity_mutation() -> None:
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
            input_ids = ("input-a",)

            def affected_inputs(self, _batch):
                self.input_ids = ("phantom",)
                return ("phantom",)

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()
        coordinator.dependency_index = Index()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="dependency index routing mutated input identity state",
        ):
            coordinator.tick()

        assert (
            coordinator._state.snapshot().last_error_code
            == "ContinuousSessionError"
        )


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
        original_store = original.delta_store
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
        assert original_store.calls == 1
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
                self.ids = ["input-old"]
                self.affected_calls = 0
                self.register_calls = 0
                self.unregister_calls = 0

            @property
            def input_ids(self):
                return tuple(self.ids)

            def affected_inputs(self, _batch):
                self.affected_calls += 1
                return ()

            def register(self, input_id: str, **_selectors):
                self.register_calls += 1
                self.ids.append(input_id)

            def unregister(self, input_id: str):
                self.unregister_calls += 1
                if input_id not in self.ids:
                    return False
                self.ids.remove(input_id)
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

def test_tick_rejects_registration_without_published_index_effect() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Index:
            input_ids = ()

            def affected_inputs(self, _batch):
                return ()

            def register(self, _input_id: str, **_selectors):
                return None

        class Lifecycle:
            def register_eligible(
                self,
                _market_store,
                *,
                register_input,
                **_kwargs,
            ):
                register_input("input-new")
                return ("input-new",)

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.dependency_index = Index()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="dependency index registration did not publish the input",
        ):
            coordinator.tick()


def test_register_rejects_collateral_selector_mutation() -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
    index.register("existing", source_ids="provider-a")

    def malicious_register(input_id: str, **selectors: object) -> None:
        assert index.unregister("existing")
        index.register("existing", source_ids="provider-b")
        index.register(input_id, **selectors)

    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="registration changed unrelated dependency selectors",
    ):
        coordinator._register_input(
            "new",
            dependency_index=index,
            register_input=malicious_register,
            source_ids="provider-a",
        )


def test_register_rejects_canonical_dependency_index_subclass() -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)

    class DerivedIndex(continuous_session.FocusedMirrorDependencyIndex):
        def register(self, *_args, **_kwargs):
            raise AssertionError("derived register authority executed")

    index = DerivedIndex(MarketMirror())
    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="canonical dependency index subtype is not supported",
    ):
        coordinator._register_input(
            "input-a",
            dependency_index=index,
            register_input=index.register,
            source_ids="provider-a",
        )


def test_retire_rejects_canonical_dependency_index_subclass() -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)

    class DerivedIndex(continuous_session.FocusedMirrorDependencyIndex):
        def unregister(self, *_args, **_kwargs):
            raise AssertionError("derived unregister authority executed")

    index = DerivedIndex(MarketMirror())
    continuous_session.FocusedMirrorDependencyIndex.register(
        index,
        "input-a",
        source_ids="provider-a",
    )
    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="canonical dependency index subtype is not supported",
    ):
        coordinator._retire_input(
            "input-a",
            dependency_index=index,
            unregister_input=index.unregister,
        )


def test_invalidation_drain_rejects_canonical_dependency_index_subclass() -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    mirror = MarketMirror()
    buffer = continuous_session.BoundedMirrorInvalidationBuffer(mirror)

    class DerivedIndex(continuous_session.FocusedMirrorDependencyIndex):
        def affected_inputs(self, _batch):
            raise AssertionError("derived routing authority executed")

    index = DerivedIndex(mirror)
    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="canonical dependency index subtype is not supported",
    ):
        coordinator._drain_invalidations(
            invalidation_buffer=buffer,
            dependency_index=index,
            drain_invalidation=buffer.drain,
            affected_inputs=index.affected_inputs,
            max_batches=4,
            max_items=250,
        )


def test_register_rejects_dependency_mirror_rebinding() -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
    index.register("existing", source_ids="provider-a")
    replacement_mirror = MarketMirror()

    def malicious_register(input_id: str, **selectors: object) -> None:
        index._mirror = replacement_mirror
        index.register(input_id, **selectors)

    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="registration changed mirror authority",
    ):
        coordinator._register_input(
            "new",
            dependency_index=index,
            register_input=malicious_register,
            source_ids="provider-a",
        )


def test_retire_rejects_collateral_selector_mutation() -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
    index.register("old", source_ids="provider-a")
    index.register("other", source_ids="provider-a")

    def malicious_unregister(input_id: str) -> bool:
        removed = index.unregister(input_id)
        assert index.unregister("other")
        index.register("other", source_ids="provider-b")
        return removed

    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="retirement changed unrelated dependency selectors",
    ):
        coordinator._retire_input(
            "old",
            dependency_index=index,
            unregister_input=malicious_unregister,
        )


def test_retire_rejects_dependency_mirror_rebinding() -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
    index.register("old", source_ids="provider-a")
    replacement_mirror = MarketMirror()

    def malicious_unregister(input_id: str) -> bool:
        index._mirror = replacement_mirror
        return index.unregister(input_id)

    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="retirement changed mirror authority",
    ):
        coordinator._retire_input(
            "old",
            dependency_index=index,
            unregister_input=malicious_unregister,
        )


def test_register_rejects_preentry_selector_verifier_rebinding(monkeypatch) -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
    callback_called = False

    def hostile_selector(*_args, **_kwargs):
        raise AssertionError("rebound selector verifier executed")

    def register_input(*_args, **_kwargs):
        nonlocal callback_called
        callback_called = True

    monkeypatch.setattr(
        continuous_session.FocusedMirrorDependencyIndex,
        "_selector",
        staticmethod(hostile_selector),
    )
    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="canonical dependency lifecycle verification authority changed",
    ):
        coordinator._register_input(
            "new",
            dependency_index=index,
            register_input=register_input,
            source_ids="provider-a",
        )
    assert not callback_called


def test_register_rejects_preentry_input_ids_descriptor_rebinding(monkeypatch) -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
    callback_called = False

    def register_input(*_args, **_kwargs):
        nonlocal callback_called
        callback_called = True

    monkeypatch.setattr(
        continuous_session.FocusedMirrorDependencyIndex,
        "input_ids",
        property(lambda self: ("forged",)),
    )
    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="canonical dependency lifecycle verification authority changed",
    ):
        coordinator._register_input(
            "new",
            dependency_index=index,
            register_input=register_input,
            source_ids="provider-a",
        )
    assert not callback_called


def test_register_rejects_callback_input_ids_descriptor_rebinding(monkeypatch) -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())

    def malicious_register(input_id: str, **selectors: object) -> None:
        index.register(input_id, **selectors)
        monkeypatch.setattr(
            continuous_session.FocusedMirrorDependencyIndex,
            "input_ids",
            property(lambda self: ("new", "forged")),
        )

    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="canonical dependency lifecycle verification authority changed",
    ):
        coordinator._register_input(
            "new",
            dependency_index=index,
            register_input=malicious_register,
            source_ids="provider-a",
        )


def test_retire_rejects_callback_input_ids_descriptor_rebinding(monkeypatch) -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
    index.register("old", source_ids="provider-a")

    def malicious_unregister(input_id: str) -> bool:
        removed = index.unregister(input_id)
        monkeypatch.setattr(
            continuous_session.FocusedMirrorDependencyIndex,
            "input_ids",
            property(lambda self: ()),
        )
        return removed

    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="canonical dependency lifecycle verification authority changed",
    ):
        coordinator._retire_input(
            "old",
            dependency_index=index,
            unregister_input=malicious_unregister,
        )


def test_invalidation_drain_rejects_input_ids_descriptor_rebinding(monkeypatch) -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    mirror = MarketMirror()
    buffer = continuous_session.BoundedMirrorInvalidationBuffer(mirror)
    index = continuous_session.FocusedMirrorDependencyIndex(mirror)

    monkeypatch.setattr(
        continuous_session.FocusedMirrorDependencyIndex,
        "input_ids",
        property(lambda self: ("forged",)),
    )
    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="canonical dependency routing identity authority changed",
    ):
        coordinator._drain_invalidations(
            invalidation_buffer=buffer,
            dependency_index=index,
            drain_invalidation=buffer.drain,
            affected_inputs=index.affected_inputs,
            max_batches=4,
            max_items=250,
        )


def test_invalidation_routing_rejects_midbatch_input_ids_descriptor_rebinding(
    monkeypatch,
) -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    mirror = MarketMirror()
    buffer = continuous_session.BoundedMirrorInvalidationBuffer(mirror)
    buffer._dirty[("provider-a", "quote-a")] = None
    index = continuous_session.FocusedMirrorDependencyIndex(mirror)
    index.register("input-a", source_ids="provider-a")

    def hostile_input_ids(_self):
        raise AssertionError("rebound input_ids getter executed")

    def malicious_routing(_batch):
        monkeypatch.setattr(
            continuous_session.FocusedMirrorDependencyIndex,
            "input_ids",
            property(hostile_input_ids),
        )
        return ("input-a",)

    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="canonical dependency routing identity authority changed",
    ):
        coordinator._drain_invalidations(
            invalidation_buffer=buffer,
            dependency_index=index,
            drain_invalidation=buffer.drain,
            affected_inputs=malicious_routing,
            max_batches=4,
            max_items=250,
        )

    assert buffer.full_refresh_required is True


def test_register_rejects_callback_dependency_verifier_rebinding(monkeypatch) -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())

    def hostile_dependency(*_args, **_kwargs):
        raise AssertionError("rebound dependency verifier executed")

    def malicious_register(input_id: str, **selectors: object) -> None:
        index.register(input_id, **selectors)
        monkeypatch.setattr(
            continuous_session.FocusedMirrorDependencyIndex,
            "_dependency",
            hostile_dependency,
        )

    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="canonical dependency lifecycle verification authority changed",
    ):
        coordinator._register_input(
            "new",
            dependency_index=index,
            register_input=malicious_register,
            source_ids="provider-a",
        )


def test_register_rejects_callback_input_id_verifier_rebinding(monkeypatch) -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())

    def hostile_input_id(*_args, **_kwargs):
        raise AssertionError("rebound input-id verifier executed")

    def malicious_register(input_id: str, **selectors: object) -> None:
        index.register(input_id, **selectors)
        monkeypatch.setattr(
            continuous_session.FocusedMirrorDependencyIndex,
            "_input_id",
            staticmethod(hostile_input_id),
        )

    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="canonical dependency lifecycle verification authority changed",
    ):
        coordinator._register_input(
            "new",
            dependency_index=index,
            register_input=malicious_register,
            source_ids="provider-a",
        )


def test_retire_rejects_callback_dependency_verifier_rebinding(monkeypatch) -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
    index.register("old", source_ids="provider-a")

    def hostile_dependency(*_args, **_kwargs):
        raise AssertionError("rebound dependency verifier executed")

    def malicious_unregister(input_id: str) -> bool:
        removed = index.unregister(input_id)
        monkeypatch.setattr(
            continuous_session.FocusedMirrorDependencyIndex,
            "_dependency",
            hostile_dependency,
        )
        return removed

    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="canonical dependency lifecycle verification authority changed",
    ):
        coordinator._retire_input(
            "old",
            dependency_index=index,
            unregister_input=malicious_unregister,
        )


def test_retire_rejects_callback_input_id_verifier_rebinding(monkeypatch) -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
    index.register("old", source_ids="provider-a")

    def hostile_input_id(*_args, **_kwargs):
        raise AssertionError("rebound input-id verifier executed")

    def malicious_unregister(input_id: str) -> bool:
        removed = index.unregister(input_id)
        monkeypatch.setattr(
            continuous_session.FocusedMirrorDependencyIndex,
            "_input_id",
            staticmethod(hostile_input_id),
        )
        return removed

    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="canonical dependency lifecycle verification authority changed",
    ):
        coordinator._retire_input(
            "old",
            dependency_index=index,
            unregister_input=malicious_unregister,
        )


def test_register_rejects_preentry_dependency_index_class_rebinding(monkeypatch) -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    canonical_index_type = continuous_session.FocusedMirrorDependencyIndex
    index = canonical_index_type(MarketMirror())
    callback_called = False

    class AliasIndex:
        _selector = staticmethod(canonical_index_type._selector)
        _input_id = staticmethod(canonical_index_type._input_id)
        _dependency = canonical_index_type._dependency

    def register_input(*_args, **_kwargs):
        nonlocal callback_called
        callback_called = True

    monkeypatch.setattr(
        continuous_session,
        "FocusedMirrorDependencyIndex",
        AliasIndex,
    )
    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="canonical dependency lifecycle verification authority changed",
    ):
        coordinator._register_input(
            "new",
            dependency_index=index,
            register_input=register_input,
            source_ids="provider-a",
        )
    assert not callback_called


def test_register_rejects_callback_dependency_model_rebinding(monkeypatch) -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())

    class AliasDependency:
        pass

    def malicious_register(input_id: str, **selectors: object) -> None:
        index.register(input_id, **selectors)
        monkeypatch.setattr(
            continuous_session,
            "FocusedMirrorDependency",
            AliasDependency,
        )

    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="canonical dependency lifecycle verification authority changed",
    ):
        coordinator._register_input(
            "new",
            dependency_index=index,
            register_input=malicious_register,
            source_ids="provider-a",
        )


def test_register_rejects_callback_dependency_code_mutation(monkeypatch) -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
    dependency_reader = continuous_session.FocusedMirrorDependencyIndex._dependency

    def hostile_dependency(self, input_id):
        raise AssertionError("mutated dependency verifier executed")

    def malicious_register(input_id: str, **selectors: object) -> None:
        index.register(input_id, **selectors)
        monkeypatch.setattr(
            dependency_reader,
            "__code__",
            hostile_dependency.__code__,
        )

    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="canonical dependency lifecycle verification authority changed",
    ):
        coordinator._register_input(
            "new",
            dependency_index=index,
            register_input=malicious_register,
            source_ids="provider-a",
        )


def test_register_rejects_preentry_selector_code_mutation(monkeypatch) -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
    selector = continuous_session.FocusedMirrorDependencyIndex._selector
    callback_called = False

    def hostile_selector(values, *, name):
        raise AssertionError("mutated selector verifier executed")

    def register_input(*_args, **_kwargs):
        nonlocal callback_called
        callback_called = True

    monkeypatch.setattr(selector, "__code__", hostile_selector.__code__)
    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="canonical dependency lifecycle verification authority changed",
    ):
        coordinator._register_input(
            "new",
            dependency_index=index,
            register_input=register_input,
            source_ids="provider-a",
        )
    assert not callback_called


def test_register_rejects_unrelated_matched_key_mutation() -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
    index.register("existing", source_ids="provider-a")

    def malicious_register(input_id: str, **selectors: object) -> None:
        index.register(input_id, **selectors)
        index._matched_keys["existing"].add(("provider-a", "quote-attacker"))

    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="registration changed unrelated matched-key routing",
    ):
        coordinator._register_input(
            "new",
            dependency_index=index,
            register_input=malicious_register,
            source_ids="provider-a",
        )


def test_retire_rejects_unrelated_matched_key_mutation() -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
    index.register("old", source_ids="provider-a")
    index.register("other", source_ids="provider-a")

    def malicious_unregister(input_id: str) -> bool:
        removed = index.unregister(input_id)
        index._matched_keys["other"].add(("provider-a", "quote-attacker"))
        return removed

    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="retirement changed unrelated matched-key routing",
    ):
        coordinator._retire_input(
            "old",
            dependency_index=index,
            unregister_input=malicious_unregister,
        )


def test_register_rejects_matched_key_store_rebinding() -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())

    def malicious_register(input_id: str, **selectors: object) -> None:
        index.register(input_id, **selectors)
        index._matched_keys = dict(index._matched_keys)

    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="registration changed state authority",
    ):
        coordinator._register_input(
            "new",
            dependency_index=index,
            register_input=malicious_register,
            source_ids="provider-a",
        )


def test_retire_rejects_dependency_store_rebinding() -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
    index.register("old", source_ids="provider-a")
    index.register("other", source_ids="provider-a")

    def malicious_unregister(input_id: str) -> bool:
        removed = index.unregister(input_id)
        index._dependencies = dict(index._dependencies)
        return removed

    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="retirement changed state authority",
    ):
        coordinator._retire_input(
            "old",
            dependency_index=index,
            unregister_input=malicious_unregister,
        )


def test_register_rejects_dependency_lock_rebinding() -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())

    def malicious_register(input_id: str, **selectors: object) -> None:
        index.register(input_id, **selectors)
        index._lock = object()

    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="registration changed state authority",
    ):
        coordinator._register_input(
            "new",
            dependency_index=index,
            register_input=malicious_register,
            source_ids="provider-a",
        )


def test_register_rejects_callback_matching_keys_verifier_rebinding(monkeypatch) -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())

    def hostile_matching_keys(*_args, **_kwargs):
        raise AssertionError("rebound matching-keys verifier executed")

    def malicious_register(input_id: str, **selectors: object) -> None:
        index.register(input_id, **selectors)
        monkeypatch.setattr(
            continuous_session.FocusedMirrorDependencyIndex,
            "matching_keys",
            hostile_matching_keys,
        )

    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="canonical dependency lifecycle verification authority changed",
    ):
        coordinator._register_input(
            "new",
            dependency_index=index,
            register_input=malicious_register,
            source_ids="provider-a",
        )


def test_tick_rejects_dependency_register_code_mutation_after_provider_io(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
        coordinator.dependency_index = index

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            calls = 0

            def register_eligible(self, *_args, **_kwargs):
                self.calls += 1
                return ()

        lifecycle = Lifecycle()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = lifecycle
        register_method = continuous_session.FocusedMirrorDependencyIndex.register

        def hostile_register(self, input_id, **selectors):
            raise AssertionError("mutated dependency register executed")

        def mutate() -> None:
            monkeypatch.setattr(
                register_method,
                "__code__",
                hostile_register.__code__,
            )

        coordinator.collector = _Collector(callback=mutate)

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="canonical dependency lifecycle dispatch authority changed",
        ):
            coordinator.tick()

        assert lifecycle.calls == 0


def test_tick_rejects_dependency_unregister_code_mutation_before_retire(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
        index.register("old", source_ids="provider-a")
        coordinator.dependency_index = index

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        unregister_method = continuous_session.FocusedMirrorDependencyIndex.unregister

        def hostile_unregister(self, input_id):
            raise AssertionError("mutated dependency unregister executed")

        class Lifecycle:
            def register_eligible(self, *_args, retire_input, **_kwargs):
                monkeypatch.setattr(
                    unregister_method,
                    "__code__",
                    hostile_unregister.__code__,
                )
                retire_input("old")
                return ()

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="canonical dependency lifecycle dispatch authority changed",
        ):
            coordinator.tick()

        assert "old" in index.input_ids


def test_tick_rejects_affected_inputs_code_mutation_before_drain(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
        coordinator.dependency_index = index
        affected_method = continuous_session.FocusedMirrorDependencyIndex.affected_inputs

        def hostile_affected(self, batch):
            raise AssertionError("mutated affected-input routing executed")

        class Desktop:
            def drain(self, **_kwargs):
                monkeypatch.setattr(
                    affected_method,
                    "__code__",
                    hostile_affected.__code__,
                )
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="canonical dependency lifecycle dispatch authority changed",
        ):
            coordinator.tick()


def test_tick_rejects_input_ids_descriptor_rebinding_after_provider_io(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
        coordinator.dependency_index = index

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            calls = 0

            def register_eligible(self, *_args, **_kwargs):
                self.calls += 1
                return ()

        lifecycle = Lifecycle()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = lifecycle

        def mutate() -> None:
            monkeypatch.setattr(
                continuous_session.FocusedMirrorDependencyIndex,
                "input_ids",
                property(lambda _self: ()),
            )

        coordinator.collector = _Collector(callback=mutate)

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="canonical dependency lifecycle dispatch authority changed",
        ):
            coordinator.tick()

        assert lifecycle.calls == 0


def test_tick_rejects_canonical_lifecycle_subclass_before_provider_io() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class DerivedLifecycle(continuous_session.ContinuousEventLifecycle):
            def register_eligible(self, *_args, **_kwargs):
                raise AssertionError("derived lifecycle authority executed")

        coordinator.lifecycle = DerivedLifecycle(root / "derived_lifecycle.json")
        coordinator.collector = _Collector(
            callback=lambda: (_ for _ in ()).throw(
                AssertionError("collector ran before lifecycle subtype rejection")
            )
        )

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="canonical event lifecycle subtype is not supported",
        ):
            coordinator.tick()


def test_tick_rejects_canonical_lifecycle_code_mutation_after_provider_io(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        coordinator.dependency_index = (
            continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
        )
        lifecycle = continuous_session.ContinuousEventLifecycle(
            root / "event_lifecycle.json"
        )
        coordinator.lifecycle = lifecycle

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        coordinator.desktop_consumer = Desktop()
        register_eligible = continuous_session.ContinuousEventLifecycle.register_eligible

        def hostile_register_eligible(self, *args, **kwargs):
            raise AssertionError("mutated canonical lifecycle executed")

        def mutate() -> None:
            monkeypatch.setattr(
                register_eligible,
                "__code__",
                hostile_register_eligible.__code__,
            )

        coordinator.collector = _Collector(callback=mutate)

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="canonical event lifecycle dispatch authority changed",
        ):
            coordinator.tick()


@pytest.mark.parametrize("input_id", ("", " input-old", "input-old ", 1, True))
def test_retire_rejects_malformed_input_ids(input_id: object) -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)

    class Index:
        input_ids = ("input-old",)

        def unregister(self, _input_id):
            return False

    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="dependency index retirement input id is invalid",
    ):
        coordinator._retire_input(
            input_id,  # type: ignore[arg-type]
            dependency_index=Index(),
        )


@pytest.mark.parametrize("input_id", ("", " input-old", "input-old ", 1))
def test_tick_rejects_malformed_lifecycle_retirement_id(input_id: object) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Index:
            input_ids = ()

            def affected_inputs(self, _batch):
                return ()

            def unregister(self, _input_id):
                raise AssertionError("malformed retirement reached index callback")

        class Lifecycle:
            def register_eligible(
                self,
                _market_store,
                *,
                retire_input,
                **_kwargs,
            ):
                retire_input(input_id)
                return ()

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.dependency_index = Index()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="dependency index retirement input id is invalid",
        ):
            coordinator.tick()

        assert (
            coordinator._state.snapshot().last_error_code
            == "ContinuousSessionError"
        )


def test_tick_rejects_false_retirement_receipt_that_keeps_input() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Index:
            input_ids = ("input-old",)

            def affected_inputs(self, _batch):
                return ()

            def unregister(self, _input_id: str):
                return True

        class Lifecycle:
            def register_eligible(
                self,
                _market_store,
                *,
                retire_input,
                **_kwargs,
            ):
                retire_input("input-old")
                return ()

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.dependency_index = Index()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="dependency index retirement did not remove the input",
        ):
            coordinator.tick()


def test_tick_does_not_report_retirement_when_index_reports_no_effect() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Index:
            input_ids = ()

            def affected_inputs(self, _batch):
                return ()

            def unregister(self, _input_id: str):
                return False

        class Lifecycle:
            def register_eligible(
                self,
                _market_store,
                *,
                retire_input,
                **_kwargs,
            ):
                retire_input("input-old")
                return ()

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.dependency_index = Index()
        coordinator.lifecycle = Lifecycle()

        result = coordinator.tick()

        assert result.retired_input_ids == ()

def test_tick_keeps_collector_projection_reader_after_provider_rebinding() -> None:
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
        original_store = collector.delta_store

        def attacker_reader(**_kwargs):
            raise AssertionError("provider-time rebound projection reader executed")

        def rebind() -> None:
            original_store.deltas_after_commit = attacker_reader  # type: ignore[method-assign]

        collector.callback = rebind
        coordinator.collector = collector
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        result = coordinator.tick()

        assert result.cycle_index == 1
        assert original_store.calls == 1


@pytest.mark.parametrize(
    "invalid_deltas",
    (
        [],
        (object(),),
    ),
)
def test_source_projection_rejects_noncanonical_delta_collection(
    invalid_deltas: object,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        collector = _Collector()

        def invalid_reader(**_kwargs):
            return invalid_deltas

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="collector projection returned invalid deltas",
        ):
            coordinator._refresh_source_state_projection(
                collector=collector,
                source_id="provider-a",
                delta_store=collector.delta_store,
                read_deltas=invalid_reader,
                max_items=1,
            )

def test_tick_rejects_collector_source_drift_before_provider_io() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class Collector:
            source_id = "provider-attacker"
            delta_store = _DeltaStore()
            config = type("ConfigStub", (), {"max_items": 1})()

            def run_cycle(self):
                raise AssertionError("provider I/O ran with mismatched session source")

        coordinator.collector = Collector()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="collector source identity does not match continuous session",
        ):
            coordinator.tick()

        assert coordinator._state.snapshot().cycles_completed == 0

def test_tick_rejects_duplicate_collector_commit_ids() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class Cycle:
            source_id = "provider-a"
            provider_unavailable = False
            committed_delta_ids = ("delta-1", "delta-1")

        class Collector:
            source_id = "provider-a"
            delta_store = _DeltaStore()
            config = type("ConfigStub", (), {"max_items": 1})()

            def run_cycle(self):
                return Cycle()

        coordinator.collector = Collector()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="collector returned invalid continuous-session cycle metadata",
        ):
            coordinator.tick()


def test_tick_rejects_provider_unavailable_cycle_with_committed_effects() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class Cycle:
            source_id = "provider-a"
            provider_unavailable = True
            committed_delta_ids = ("delta-1",)

        class Collector:
            source_id = "provider-a"
            delta_store = _DeltaStore()
            config = type("ConfigStub", (), {"max_items": 1})()

            def run_cycle(self):
                return Cycle()

        coordinator.collector = Collector()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="collector returned invalid continuous-session cycle metadata",
        ):
            coordinator.tick()


def test_tick_rejects_duplicate_desktop_delivery_ids() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class Desktop:
            def drain(self, **_kwargs):
                return ("delta-1", "delta-1")

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


def test_tick_rejects_duplicate_lifecycle_registration_ids() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Index:
            def __init__(self) -> None:
                self.ids = set()

            @property
            def input_ids(self):
                return tuple(sorted(self.ids))

            def affected_inputs(self, _batch):
                return ()

            def register(self, input_id: str, **_selectors):
                self.ids.add(input_id)

        class Lifecycle:
            def register_eligible(
                self,
                _market_store,
                *,
                register_input,
                **_kwargs,
            ):
                register_input("input-new")
                return ("input-new", "input-new")

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.dependency_index = Index()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="lifecycle returned invalid registered input ids",
        ):
            coordinator.tick()

def test_tick_reads_provider_unavailable_once_from_cycle_snapshot() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class Cycle:
            source_id = "provider-a"
            committed_delta_ids = ()

            def __init__(self) -> None:
                self.provider_reads = 0

            @property
            def provider_unavailable(self):
                self.provider_reads += 1
                return self.provider_reads > 1

        cycle = Cycle()

        class Collector:
            source_id = "provider-a"
            config = type("ConfigStub", (), {"max_items": 1})()
            delta_store = _DeltaStore()

            def run_cycle(self):
                return cycle

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        coordinator.collector = Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        result = coordinator.tick()

        assert result.source_provider_unavailable is False
        assert cycle.provider_reads == 1


def test_tick_allows_missing_lifecycle_records_without_outcome_authority() -> None:
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

        result = coordinator.tick()

        assert result.cycle_index == 1
        assert result.settlement_evidence_ids == ()


def test_tick_rejects_missing_lifecycle_records_with_outcome_authority_before_provider_io() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class Collector(_Collector):
            def run_cycle(self):
                raise AssertionError("provider I/O ran before settlement preflight")

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        class Authority:
            def resolve(self, *_args, **_kwargs):
                return None

        coordinator.collector = Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()
        coordinator.outcome_authority = Authority()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="lifecycle settlement records authority is unavailable",
        ):
            coordinator.tick()


@pytest.mark.parametrize("max_items", (0, True, -1))
def test_tick_rejects_invalid_projection_bound_before_provider_io(
    max_items: object,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class Collector:
            source_id = "provider-a"
            config = type("ConfigStub", (), {"max_items": max_items})()
            delta_store = _DeltaStore()

            def run_cycle(self):
                raise AssertionError("provider I/O ran before projection preflight")

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        coordinator.collector = Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="collector projection configuration is invalid",
        ):
            coordinator.tick()


def test_tick_rejects_noncallable_projection_reader_before_provider_io() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class Store:
            deltas_after_commit = None

        class Collector:
            source_id = "provider-a"
            config = type("ConfigStub", (), {"max_items": 1})()
            delta_store = Store()

            def run_cycle(self):
                raise AssertionError("provider I/O ran before projection preflight")

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        coordinator.collector = Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="collector projection authority is unavailable",
        ):
            coordinator.tick()


def test_tick_rejects_noncallable_lifecycle_registration_before_provider_io() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class Collector(_Collector):
            def run_cycle(self):
                raise AssertionError("provider I/O ran before lifecycle preflight")

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            register_eligible = None

        coordinator.collector = Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="lifecycle registration authority is unavailable",
        ):
            coordinator.tick()


def test_tick_rejects_noncallable_desktop_drain_before_provider_io() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class Collector(_Collector):
            def run_cycle(self):
                raise AssertionError("provider I/O ran before desktop preflight")

        class Desktop:
            drain = None

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        coordinator.collector = Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="desktop delivery authority is unavailable",
        ):
            coordinator.tick()


def test_tick_rejects_noncallable_invalidation_drain_before_provider_io() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class Collector(_Collector):
            def run_cycle(self):
                raise AssertionError("provider I/O ran before invalidation preflight")

        class Buffer:
            pending_count = 0
            full_refresh_required = False
            drain = None

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        coordinator.collector = Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()
        coordinator.invalidation_buffer = Buffer()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="continuous-session invalidation routing authority is unavailable",
        ):
            coordinator.tick()


def test_tick_rejects_noncallable_affected_inputs_before_provider_io() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class Collector(_Collector):
            def run_cycle(self):
                raise AssertionError("provider I/O ran before invalidation preflight")

        class Index:
            input_ids = ()
            affected_inputs = None

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        coordinator.collector = Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()
        coordinator.dependency_index = Index()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="continuous-session invalidation routing authority is unavailable",
        ):
            coordinator.tick()


def test_tick_rejects_invalid_invalidation_bounds_before_provider_io() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class Collector(_Collector):
            def run_cycle(self):
                raise AssertionError("provider I/O ran before invalidation-bound preflight")

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        coordinator.collector = Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()
        coordinator.max_invalidation_batches_per_tick = 0

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="continuous-session invalidation bounds are invalid",
        ):
            coordinator.tick()


def test_tick_rejects_invalid_causal_view_before_provider_io() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class Collector(_Collector):
            def run_cycle(self):
                raise AssertionError("provider I/O ran before causal-view preflight")

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        coordinator.collector = Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()
        coordinator.causal_view = "as_known_at_decision"

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="continuous-session causal view is invalid",
        ):
            coordinator.tick()


@pytest.mark.parametrize(
    "required_history",
    (None, timedelta(seconds=-1)),
)
def test_tick_rejects_invalid_required_history_before_provider_io(
    required_history: object,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class Collector(_Collector):
            def run_cycle(self):
                raise AssertionError("provider I/O ran before history preflight")

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        coordinator.collector = Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()
        coordinator.required_history = required_history

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="continuous-session required history is invalid",
        ):
            coordinator.tick()


def test_tick_rejects_noncallable_clock_before_provider_io() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class Collector(_Collector):
            def run_cycle(self):
                raise AssertionError("provider I/O ran before clock preflight")

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        coordinator.collector = Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()
        coordinator.clock = None

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="continuous-session clock authority is unavailable",
        ):
            coordinator.tick()


@pytest.mark.parametrize(
    ("changed_keys", "full_refresh_required", "has_more"),
    (
        ([], False, False),
        ((("provider-a", "quote-1"),), 1, False),
        ((("provider-a", "quote-1"),), False, 0),
        ((("provider-a",),), False, False),
        (((" provider-a", "quote-1"),), False, False),
        ((("provider-a", "quote-1"),), True, False),
        ((), True, True),
    ),
)
def test_tick_rejects_noncanonical_invalidation_batch_fields(
    changed_keys: object,
    full_refresh_required: object,
    has_more: object,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class Buffer:
            pending_count = 0
            full_refresh_required = False

            def drain(self, *, max_items: int):
                assert max_items == 250
                return continuous_session.MirrorInvalidationBatch(
                    changed_keys=changed_keys,
                    full_refresh_required=full_refresh_required,
                    has_more=has_more,
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


def test_tick_rejects_duplicate_affected_input_ids() -> None:
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
                return ("input-1", "input-1")

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()
        coordinator.dependency_index = Index()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="dependency index returned invalid affected inputs",
        ):
            coordinator.tick()


@pytest.mark.parametrize(
    ("pending_count", "pending_full_refresh"),
    (
        (True, False),
        (-1, False),
        (1, 1),
    ),
)
def test_tick_rejects_noncanonical_invalidation_backlog_state(
    pending_count: object,
    pending_full_refresh: object,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class Buffer:
            full_refresh_required = pending_full_refresh

            @property
            def pending_count(self):
                return pending_count

            def drain(self, *, max_items: int):
                assert max_items == 250
                return continuous_session.MirrorInvalidationBatch(
                    changed_keys=(),
                    full_refresh_required=False,
                    has_more=True,
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
        coordinator.max_invalidation_batches_per_tick = 1

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="invalidation buffer backlog state is invalid",
        ):
            coordinator.tick()


def test_provider_unavailable_tick_rejects_noncanonical_invalidation_backlog_state() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class Cycle:
            source_id = "provider-a"
            provider_unavailable = True
            committed_delta_ids = ()

        class Collector:
            source_id = "provider-a"
            config = type("ConfigStub", (), {"max_items": 1})()
            delta_store = _DeltaStore()

            def run_cycle(self):
                return Cycle()

        class Buffer:
            pending_count = True
            full_refresh_required = False

            def drain(self, **_kwargs):
                raise AssertionError("provider-unavailable tick must not drain invalidations")

        class Desktop:
            def drain(self, **_kwargs):
                raise AssertionError("provider-unavailable tick must not deliver desktop deltas")

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                raise AssertionError("provider-unavailable tick must not register lifecycle inputs")

        coordinator.collector = Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()
        coordinator.invalidation_buffer = Buffer()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="invalidation buffer backlog state is invalid",
        ):
            coordinator.tick()

        assert coordinator._state.snapshot().last_error_code is None

def test_tick_restores_state_after_provider_rebinding() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        canonical_state = coordinator._state
        replacement_state = continuous_session._ContinuousSessionState(
            root / "replacement_session.json",
            session_id="replacement-session",
            source_id="provider-a",
            clock=lambda: _AT,
        )

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()
        coordinator.collector = _Collector(
            callback=lambda: setattr(coordinator, "_state", replacement_state)
        )

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="continuous session state authority changed during tick",
        ):
            coordinator.tick()

        assert coordinator._state is canonical_state
        assert canonical_state.snapshot().last_error_code == "ContinuousSessionError"
        assert replacement_state.snapshot().cycles_completed == 0
        assert replacement_state.snapshot().last_error_code is None


def test_tick_restores_state_when_provider_rebinds_then_raises() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        canonical_state = coordinator._state
        replacement_state = continuous_session._ContinuousSessionState(
            root / "replacement_session.json",
            session_id="replacement-session",
            source_id="provider-a",
            clock=lambda: _AT,
        )

        class Collector(_Collector):
            def run_cycle(self):
                coordinator._state = replacement_state
                raise RuntimeError("provider boom")

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        coordinator.collector = Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(RuntimeError, match="provider boom"):
            coordinator.tick()

        assert coordinator._state is canonical_state
        assert canonical_state.snapshot().last_error_code == "RuntimeError"
        assert replacement_state.snapshot().last_error_code is None


def test_tick_restores_state_after_desktop_rebinding() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        canonical_state = coordinator._state
        replacement_state = continuous_session._ContinuousSessionState(
            root / "replacement_session.json",
            session_id="replacement-session",
            source_id="provider-a",
            clock=lambda: _AT,
        )

        class Desktop:
            def drain(self, **_kwargs):
                coordinator._state = replacement_state
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="continuous session state authority changed during tick",
        ):
            coordinator.tick()

        assert coordinator._state is canonical_state
        assert canonical_state.snapshot().last_error_code == "ContinuousSessionError"
        assert replacement_state.snapshot().last_error_code is None


def test_tick_restores_state_after_outcome_callback_rebinding() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        canonical_state = coordinator._state
        replacement_state = continuous_session._ContinuousSessionState(
            root / "replacement_session.json",
            session_id="replacement-session",
            source_id="provider-a",
            clock=lambda: _AT,
        )
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

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

            def records(self):
                return (record,)

        class Authority:
            def resolve(self, _record, *, as_of: str):
                assert as_of == _AT
                coordinator._state = replacement_state
                return None

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()
        coordinator.outcome_authority = Authority()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="continuous session state authority changed during tick",
        ):
            coordinator.tick()

        assert coordinator._state is canonical_state
        assert canonical_state.snapshot().last_error_code == "ContinuousSessionError"
        assert replacement_state.snapshot().last_error_code is None


def test_tick_restores_state_after_learning_prepare_rebinding() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        canonical_state = coordinator._state
        replacement_state = continuous_session._ContinuousSessionState(
            root / "replacement_session.json",
            session_id="replacement-session",
            source_id="provider-a",
            clock=lambda: _AT,
        )
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
            evidence_id="evidence-state-rebind",
            evidence_sha256="e" * 64,
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

        class Authority:
            def resolve(self, _record, *, as_of: str):
                assert as_of == _AT
                return resolution

        class Handoff:
            def prepare_settlement(self, **_kwargs):
                coordinator._state = replacement_state
                return ()

            def reconcile_after_settlement(self, **_kwargs):
                raise AssertionError("reconcile ran after state authority rebinding")

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()
        coordinator.outcome_authority = Authority()
        coordinator.settlement_learning_handoff = Handoff()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="continuous session state authority changed during tick",
        ):
            coordinator.tick()

        assert coordinator._state is canonical_state
        assert canonical_state.snapshot().last_error_code == "ContinuousSessionError"
        assert replacement_state.snapshot().last_error_code is None

@pytest.mark.parametrize(
    "mutation",
    (
        "selectors",
        "matched_keys",
        "mirror",
        "dependency_store",
        "matched_key_store",
        "lock",
    ),
)
def test_tick_rejects_lifecycle_routing_tamper_outside_callbacks(
    mutation: str,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
        index.register("existing", source_ids="provider-a")

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                if mutation == "selectors":
                    assert index.unregister("existing")
                    index.register("existing", source_ids="provider-b")
                elif mutation == "matched_keys":
                    index._matched_keys["existing"].add(
                        ("provider-a", "quote-attacker")
                    )
                elif mutation == "mirror":
                    index._mirror = MarketMirror()
                elif mutation == "dependency_store":
                    index._dependencies = dict(index._dependencies)
                elif mutation == "matched_key_store":
                    index._matched_keys = dict(index._matched_keys)
                else:
                    index._lock = object()
                return ()

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()
        coordinator.dependency_index = index

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="lifecycle changed dependency routing authority outside "
            "coordinator callbacks",
        ):
            coordinator.tick()

        assert coordinator.dependency_index is index
        assert (
            coordinator._state.snapshot().last_error_code
            == "ContinuousSessionError"
        )


@pytest.mark.parametrize("mutation", ("selectors", "matched_keys"))
def test_tick_rejects_post_registration_lifecycle_routing_tamper(
    mutation: str,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(
                self,
                *_args,
                register_input,
                **_kwargs,
            ):
                register_input("new", source_ids="provider-a")
                if mutation == "selectors":
                    assert index.unregister("new")
                    index.register("new", source_ids="provider-b")
                else:
                    index._matched_keys["new"].add(
                        ("provider-a", "quote-attacker")
                    )
                return ("new",)

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()
        coordinator.dependency_index = index

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="lifecycle changed dependency routing authority outside "
            "coordinator callbacks",
        ):
            coordinator.tick()

        assert (
            coordinator._state.snapshot().last_error_code
            == "ContinuousSessionError"
        )


def test_tick_rejects_dependency_index_object_rebinding_and_restores_it() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        original = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
        replacement = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
        coordinator.dependency_index = original

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()
        coordinator.collector = _Collector(
            callback=lambda: setattr(
                coordinator,
                "dependency_index",
                replacement,
            )
        )

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="dependency index authority changed during tick",
        ):
            coordinator.tick()

        assert coordinator.dependency_index is original
        assert (
            coordinator._state.snapshot().last_error_code
            == "ContinuousSessionError"
        )


def test_tick_restores_dependency_index_when_collector_rebinds_then_fails() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        original = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
        replacement = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
        coordinator.dependency_index = original

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        def rebind_then_fail() -> None:
            coordinator.dependency_index = replacement
            raise RuntimeError("collector exploded after dependency rebind")

        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()
        coordinator.collector = _Collector(callback=rebind_then_fail)

        with pytest.raises(
            RuntimeError,
            match="collector exploded after dependency rebind",
        ):
            coordinator.tick()

        assert coordinator.dependency_index is original
        assert coordinator._state.snapshot().last_error_code == "RuntimeError"


def test_tick_restores_dependency_index_when_lifecycle_rebinds_then_fails() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        original = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
        replacement = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
        coordinator.dependency_index = original

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                coordinator.dependency_index = replacement
                raise RuntimeError("lifecycle exploded after dependency rebind")

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            RuntimeError,
            match="lifecycle exploded after dependency rebind",
        ):
            coordinator.tick()

        assert coordinator.dependency_index is original
        assert coordinator._state.snapshot().last_error_code == "RuntimeError"


def test_tick_accepts_exact_lifecycle_dependency_transition_with_routing_state() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
        index.register("old", source_ids="provider-a")
        index.register("stay", source_ids="provider-a")
        index._matched_keys["stay"].add(("provider-a", "quote-stay"))

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(
                self,
                *_args,
                register_input,
                retire_input,
                **_kwargs,
            ):
                retire_input("old")
                register_input("new", source_ids="provider-a")
                return ("new",)

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()
        coordinator.dependency_index = index

        result = coordinator.tick()

        assert result.registered_input_ids == ("new",)
        assert result.retired_input_ids == ("old",)
        assert index.input_ids == ("stay", "new")
        assert index.matching_keys("stay") == (("provider-a", "quote-stay"),)


@pytest.mark.parametrize("helper", ("dependency", "matching_keys"))
def test_tick_rejects_lifecycle_verifier_rebinding_after_callback(
    monkeypatch,
    helper: str,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
        index.register("existing", source_ids="provider-a")

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                if helper == "dependency":
                    def hostile_dependency(*_args, **_kwargs):
                        raise AssertionError("rebound dependency reader executed")

                    monkeypatch.setattr(
                        continuous_session.FocusedMirrorDependencyIndex,
                        "_dependency",
                        hostile_dependency,
                    )
                else:
                    def hostile_matching_keys(*_args, **_kwargs):
                        raise AssertionError("rebound matching-keys reader executed")

                    monkeypatch.setattr(
                        continuous_session.FocusedMirrorDependencyIndex,
                        "matching_keys",
                        hostile_matching_keys,
                    )
                return ()

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()
        coordinator.dependency_index = index

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="lifecycle changed dependency routing authority outside "
            "coordinator callbacks",
        ):
            coordinator.tick()

        assert (
            coordinator._state.snapshot().last_error_code
            == "ContinuousSessionError"
        )

@pytest.mark.parametrize(
    "error_type",
    (
        continuous_session.SessionPausedError,
        continuous_session.SessionStoppedError,
    ),
)
def test_tick_records_callback_forged_control_exception_as_operational_failure(
    error_type: type[Exception],
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        state = coordinator._state

        class Desktop:
            def drain(self, **_kwargs):
                raise error_type("forged callback control exception")

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(error_type, match="forged callback control exception"):
            coordinator.tick()

        snapshot = state.snapshot()
        assert snapshot.state is continuous_session.SessionState.RUNNING
        assert snapshot.last_error_code == error_type.__name__


@pytest.mark.parametrize(
    "error_type",
    (
        continuous_session.SessionPausedError,
        continuous_session.SessionStoppedError,
    ),
)
def test_tick_restores_state_when_callback_rebinds_then_forges_control_exception(
    error_type: type[Exception],
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        canonical_state = coordinator._state
        replacement_state = continuous_session._ContinuousSessionState(
            root / "replacement_control_exception_session.json",
            session_id="replacement-control-exception-session",
            source_id="provider-a",
            clock=lambda: _AT,
        )

        class Desktop:
            def drain(self, **_kwargs):
                coordinator._state = replacement_state
                raise error_type("forged callback control exception")

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(error_type, match="forged callback control exception"):
            coordinator.tick()

        assert coordinator._state is canonical_state
        assert canonical_state.snapshot().last_error_code == error_type.__name__
        assert replacement_state.snapshot().last_error_code is None


@pytest.mark.parametrize(
    "error_type",
    (
        continuous_session.SessionPausedError,
        continuous_session.SessionStoppedError,
    ),
)
def test_tick_restores_dependency_index_when_callback_forges_control_exception(
    error_type: type[Exception],
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        canonical_index = coordinator.dependency_index
        replacement_index = _DependencyIndex()

        class Desktop:
            def drain(self, **_kwargs):
                coordinator.dependency_index = replacement_index
                raise error_type("forged callback control exception")

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(error_type, match="forged callback control exception"):
            coordinator.tick()

        assert coordinator.dependency_index is canonical_index
        assert coordinator._state.snapshot().last_error_code == error_type.__name__

@pytest.mark.parametrize("authority_target", ("state", "dependency_index"))
@pytest.mark.parametrize("accessor_failure", ("malformed", "raises"))
def test_provider_unavailable_backlog_failure_restores_coordinator_authority(
    authority_target: str,
    accessor_failure: str,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        canonical_state = coordinator._state
        canonical_index = coordinator.dependency_index
        replacement_state = continuous_session._ContinuousSessionState(
            root / "replacement_provider_unavailable_session.json",
            session_id="replacement-provider-unavailable-session",
            source_id="provider-a",
            clock=lambda: _AT,
        )
        replacement_index = _DependencyIndex()

        class Cycle:
            source_id = "provider-a"
            provider_unavailable = True
            committed_delta_ids = ()

        class Collector:
            source_id = "provider-a"
            config = type("ConfigStub", (), {"max_items": 1})()
            delta_store = _DeltaStore()

            def run_cycle(self):
                return Cycle()

        class Buffer:
            full_refresh_required = False

            @property
            def pending_count(self):
                if authority_target == "state":
                    coordinator._state = replacement_state
                else:
                    coordinator.dependency_index = replacement_index
                if accessor_failure == "raises":
                    raise RuntimeError("provider backlog accessor failed")
                return True

            def drain(self, **_kwargs):
                raise AssertionError(
                    "provider-unavailable tick must not drain invalidations"
                )

        coordinator.collector = Collector()
        coordinator.invalidation_buffer = Buffer()

        if accessor_failure == "raises":
            expected_error = RuntimeError
            expected_match = "provider backlog accessor failed"
        else:
            expected_error = continuous_session.ContinuousSessionError
            expected_match = "invalidation buffer backlog state is invalid"

        with pytest.raises(expected_error, match=expected_match):
            coordinator.tick()

        assert coordinator._state is canonical_state
        assert coordinator.dependency_index is canonical_index
        assert canonical_state.snapshot().last_error_code == expected_error.__name__
        assert replacement_state.snapshot().last_error_code is None

@pytest.mark.parametrize(
    "storage_field",
    ("_dependencies", "_matched_keys", "_lock"),
)
@pytest.mark.parametrize("mutation_phase", ("drain", "backlog"))
def test_tick_rejects_invalidation_routing_storage_rebinding(
    storage_field: str,
    mutation_phase: str,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
        index.register("input-a", source_ids="provider-a")
        coordinator.dependency_index = index

        def rebind_storage() -> None:
            if storage_field == "_dependencies":
                index._dependencies = dict(index._dependencies)
            elif storage_field == "_matched_keys":
                index._matched_keys = {
                    input_id: set(keys)
                    for input_id, keys in index._matched_keys.items()
                }
            else:
                index._lock = type(index._lock)()

        class Buffer:
            full_refresh_required = False

            @property
            def pending_count(self):
                if mutation_phase == "backlog":
                    rebind_storage()
                return 0

            def drain(self, *, max_items: int):
                assert max_items == 250
                if mutation_phase == "drain":
                    rebind_storage()
                return continuous_session.MirrorInvalidationBatch(
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
        coordinator.invalidation_buffer = Buffer()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        expected = (
            "routing authority changed during invalidation drain"
            if mutation_phase == "drain"
            else "routing authority changed during invalidation backlog inspection"
        )
        with pytest.raises(continuous_session.ContinuousSessionError, match=expected):
            coordinator.tick()

        assert coordinator._state.snapshot().last_error_code == "ContinuousSessionError"


def test_tick_rejects_backlog_accessor_selector_mutation() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
        index.register("input-a", source_ids="provider-a")
        coordinator.dependency_index = index

        class Buffer:
            full_refresh_required = False

            @property
            def pending_count(self):
                assert index.unregister("input-a")
                index.register("input-a", source_ids="provider-b")
                return 0

            def drain(self, *, max_items: int):
                assert max_items == 250
                return continuous_session.MirrorInvalidationBatch(
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
        coordinator.invalidation_buffer = Buffer()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="routing authority changed during invalidation backlog inspection",
        ):
            coordinator.tick()

        assert coordinator._state.snapshot().last_error_code == "ContinuousSessionError"

@pytest.mark.parametrize("method_name", ("matches", "__eq__"))
def test_tick_rejects_dependency_model_dispatch_rebinding_during_invalidation_drain(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
        index.register("input-a", source_ids="provider-a")
        coordinator.dependency_index = index
        hostile_called = False

        def hostile(*_args, **_kwargs):
            nonlocal hostile_called
            hostile_called = True
            raise AssertionError("hostile dependency model dispatch executed")

        class Buffer:
            pending_count = 0
            full_refresh_required = False

            def drain(self, *, max_items: int):
                assert max_items == 250
                monkeypatch.setattr(
                    continuous_session.FocusedMirrorDependency,
                    method_name,
                    hostile,
                )
                return continuous_session.MirrorInvalidationBatch(
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
        coordinator.invalidation_buffer = Buffer()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="routing authority changed during invalidation drain",
        ):
            coordinator.tick()

        assert not hostile_called
        assert coordinator._state.snapshot().last_error_code == "ContinuousSessionError"


def test_tick_rejects_dependency_matches_code_mutation_during_invalidation_drain() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
        index.register("input-a", source_ids="provider-a")
        coordinator.dependency_index = index
        canonical_matches = continuous_session.FocusedMirrorDependency.matches
        canonical_code = canonical_matches.__code__

        def hostile_matches(self, event):
            raise AssertionError(
                f"hostile dependency matches executed for {self!r} / {event!r}"
            )

        class Buffer:
            pending_count = 0
            full_refresh_required = False

            def drain(self, *, max_items: int):
                assert max_items == 250
                canonical_matches.__code__ = hostile_matches.__code__
                return continuous_session.MirrorInvalidationBatch(
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
        coordinator.invalidation_buffer = Buffer()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        try:
            with pytest.raises(
                continuous_session.ContinuousSessionError,
                match="routing authority changed during invalidation drain",
            ):
                coordinator.tick()
        finally:
            canonical_matches.__code__ = canonical_code

        assert coordinator._state.snapshot().last_error_code == "ContinuousSessionError"

@pytest.mark.parametrize(
    ("state_value", "error_type"),
    (
        (continuous_session.SessionState.PAUSED, continuous_session.SessionPausedError),
        (continuous_session.SessionState.STOPPED, continuous_session.SessionStoppedError),
    ),
)
def test_tick_preserves_genuine_durable_operator_control_before_callbacks(
    state_value: continuous_session.SessionState,
    error_type: type[Exception],
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        coordinator._state.set_state(state_value, reason="OPERATOR_CONTROL")
        callback_called = False

        class Collector(_Collector):
            def run_cycle(self):
                nonlocal callback_called
                callback_called = True
                return super().run_cycle()

        coordinator.collector = Collector()

        with pytest.raises(error_type):
            coordinator.tick()

        assert not callback_called
        snapshot = coordinator._state.snapshot()
        assert snapshot.state is state_value
        assert snapshot.last_error_code == "OPERATOR_CONTROL"



def test_register_rejects_in_place_collateral_selector_mutation() -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
    existing = index.register("existing", source_ids="provider-a")

    def malicious_register(input_id: str, **selectors: object) -> None:
        object.__setattr__(
            existing,
            "source_ids",
            frozenset({"provider-b"}),
        )
        index.register(input_id, **selectors)

    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="registration changed unrelated dependency selectors",
    ):
        coordinator._register_input(
            "new",
            dependency_index=index,
            register_input=malicious_register,
            source_ids="provider-a",
        )


def test_retire_rejects_in_place_collateral_selector_mutation() -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
    index.register("old", source_ids="provider-a")
    remaining = index.register("remaining", source_ids="provider-a")

    def malicious_unregister(input_id: str) -> bool:
        removed = index.unregister(input_id)
        object.__setattr__(
            remaining,
            "source_ids",
            frozenset({"provider-b"}),
        )
        return removed

    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="retirement changed unrelated dependency selectors",
    ):
        coordinator._retire_input(
            "old",
            dependency_index=index,
            unregister_input=malicious_unregister,
        )


@pytest.mark.parametrize(
    ("mutation_phase", "expected_message"),
    (
        (
            "drain",
            "routing authority changed during invalidation drain",
        ),
        (
            "routing",
            "routing authority changed during affected-input routing",
        ),
        (
            "backlog",
            "routing authority changed during invalidation backlog inspection",
        ),
    ),
)
def test_invalidation_rejects_in_place_dependency_selector_mutation(
    mutation_phase: str,
    expected_message: str,
) -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
    dependency = index.register("input-a", source_ids="provider-a")

    def mutate() -> None:
        object.__setattr__(
            dependency,
            "source_ids",
            frozenset({"provider-b"}),
        )

    class Buffer:
        full_refresh_required = False

        @property
        def pending_count(self):
            if mutation_phase == "backlog":
                mutate()
            return 0

        def drain(self, *, max_items: int):
            assert max_items == 250
            if mutation_phase == "drain":
                mutate()
            return continuous_session.MirrorInvalidationBatch(
                changed_keys=(),
                full_refresh_required=False,
                has_more=False,
            )

    def affected_inputs(_batch):
        if mutation_phase == "routing":
            mutate()
        return ()

    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match=expected_message,
    ):
        coordinator._drain_invalidations(
            invalidation_buffer=Buffer(),
            dependency_index=index,
            drain_invalidation=Buffer().drain,
            affected_inputs=affected_inputs,
            max_batches=4,
            max_items=250,
        )


def test_tick_rejects_in_place_lifecycle_selector_mutation() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
        dependency = index.register("input-a", source_ids="provider-a")
        coordinator.dependency_index = index

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                object.__setattr__(
                    dependency,
                    "source_ids",
                    frozenset({"provider-b"}),
                )
                return ()

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="lifecycle changed dependency routing authority outside coordinator callbacks",
        ):
            coordinator.tick()

        assert coordinator._state.snapshot().last_error_code == "ContinuousSessionError"


def test_tick_rejects_in_place_mutation_after_lifecycle_registration_callback() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
        coordinator.dependency_index = index

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(
                self,
                *_args,
                register_input,
                **_kwargs,
            ):
                register_input("input-new", source_ids="provider-a")
                dependency = index._dependency("input-new")
                object.__setattr__(
                    dependency,
                    "source_ids",
                    frozenset({"provider-b"}),
                )
                return ("input-new",)

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="lifecycle changed dependency routing authority outside coordinator callbacks",
        ):
            coordinator.tick()

        assert coordinator._state.snapshot().last_error_code == "ContinuousSessionError"


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    (
        ("changed_keys", (("provider-a", "quote-mutated"),)),
        ("full_refresh_required", True),
        ("has_more", True),
    ),
)
def test_invalidation_rejects_post_validation_batch_truth_mutation(
    field_name: str,
    replacement: object,
) -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
    index.register("input-a", source_ids="provider-a")
    batch = continuous_session.MirrorInvalidationBatch(
        changed_keys=(),
        full_refresh_required=False,
        has_more=False,
    )

    class Buffer:
        pending_count = 0
        full_refresh_required = False

        def drain(self, *, max_items: int):
            assert max_items == 250
            return batch

    def affected_inputs(received):
        assert received is batch
        object.__setattr__(received, field_name, replacement)
        return ()

    buffer = Buffer()
    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="routing mutated invalidation batch truth",
    ):
        coordinator._drain_invalidations(
            invalidation_buffer=buffer,
            dependency_index=index,
            drain_invalidation=buffer.drain,
            affected_inputs=affected_inputs,
            max_batches=4,
            max_items=250,
        )


def test_tick_rejects_in_place_dependency_mutation_during_collector_observation() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
        dependency = index.register("input-a", source_ids="provider-a")
        coordinator.dependency_index = index

        class Desktop:
            def drain(self, **_kwargs):
                raise AssertionError("desktop delivery ran after collector routing mutation")

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                raise AssertionError("lifecycle ran after collector routing mutation")

        def mutate() -> None:
            object.__setattr__(
                dependency,
                "source_ids",
                frozenset({"provider-b"}),
            )

        coordinator.collector = _Collector(callback=mutate)
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="dependency routing authority changed during collector observation",
        ):
            coordinator.tick()

        assert coordinator._state.snapshot().last_error_code == "ContinuousSessionError"


def test_tick_rejects_in_place_dependency_mutation_during_desktop_delivery() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
        dependency = index.register("input-a", source_ids="provider-a")
        coordinator.dependency_index = index

        class Desktop:
            def drain(self, **_kwargs):
                object.__setattr__(
                    dependency,
                    "source_ids",
                    frozenset({"provider-b"}),
                )
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                raise AssertionError("lifecycle ran after desktop routing mutation")

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="dependency routing authority changed during desktop delivery",
        ):
            coordinator.tick()

        assert coordinator._state.snapshot().last_error_code == "ContinuousSessionError"

def test_tick_rejects_collector_in_place_matched_key_mutation() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
        index.register("input-a", source_ids="provider-a")
        coordinator.dependency_index = index

        class Desktop:
            def drain(self, **_kwargs):
                raise AssertionError("desktop delivery ran after collector routing mutation")

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                raise AssertionError("lifecycle ran after collector routing mutation")

        def mutate() -> None:
            index._matched_keys["input-a"].add(("provider-a", "forged-quote"))

        coordinator.collector = _Collector(callback=mutate)
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="dependency routing authority changed during collector observation",
        ):
            coordinator.tick()

        assert coordinator._state.snapshot().last_error_code == "ContinuousSessionError"


def test_tick_rejects_desktop_in_place_matched_key_mutation() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
        index.register("input-a", source_ids="provider-a")
        coordinator.dependency_index = index

        class Desktop:
            def drain(self, **_kwargs):
                index._matched_keys["input-a"].add(("provider-a", "forged-quote"))
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                raise AssertionError("lifecycle ran after desktop routing mutation")

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="dependency routing authority changed during desktop delivery",
        ):
            coordinator.tick()

        assert coordinator._state.snapshot().last_error_code == "ContinuousSessionError"


def test_tick_rejects_settlement_prepare_dependency_routing_mutation() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
        dependency = index.register("input-a", source_ids="provider-a")
        coordinator.dependency_index = index

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        class Handoff:
            def prepare_settlement(self, **_kwargs):
                object.__setattr__(
                    dependency,
                    "source_ids",
                    frozenset({"provider-b"}),
                )

            def reconcile_after_settlement(self, **_kwargs):
                return None

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()
        coordinator.settlement_learning_handoff = Handoff()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="dependency routing authority changed during settlement preparation",
        ):
            coordinator.tick()

        assert coordinator._state.snapshot().last_error_code == "ContinuousSessionError"


def test_tick_rejects_settlement_reconcile_matched_key_mutation() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
        index.register("input-a", source_ids="provider-a")
        coordinator.dependency_index = index

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        class Handoff:
            def prepare_settlement(self, **_kwargs):
                return None

            def reconcile_after_settlement(self, **_kwargs):
                index._matched_keys["input-a"].add(("provider-a", "forged-quote"))

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()
        coordinator.settlement_learning_handoff = Handoff()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="dependency routing authority changed during settlement reconciliation",
        ):
            coordinator.tick()

        assert coordinator._state.snapshot().last_error_code == "ContinuousSessionError"

def test_tick_rejects_dependency_matches_dispatch_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
        index.register("input-a", source_ids="provider-a")
        coordinator.dependency_index = index

        class Desktop:
            def drain(self, **_kwargs):
                raise AssertionError("desktop delivery ran after dependency dispatch mutation")

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                raise AssertionError("lifecycle ran after dependency dispatch mutation")

        def mutate() -> None:
            monkeypatch.setattr(
                continuous_session.FocusedMirrorDependency,
                "matches",
                lambda self, event: False,
            )

        coordinator.collector = _Collector(callback=mutate)
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="canonical dependency lifecycle dispatch authority changed",
        ):
            coordinator.tick()

        assert coordinator._state.snapshot().last_error_code == "ContinuousSessionError"


def test_tick_rejects_dependency_equality_dispatch_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
        index.register("input-a", source_ids="provider-a")
        coordinator.dependency_index = index

        class Desktop:
            def drain(self, **_kwargs):
                raise AssertionError("desktop delivery ran after dependency dispatch mutation")

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                raise AssertionError("lifecycle ran after dependency dispatch mutation")

        def mutate() -> None:
            monkeypatch.setattr(
                continuous_session.FocusedMirrorDependency,
                "__eq__",
                lambda self, other: True,
            )

        coordinator.collector = _Collector(callback=mutate)
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="canonical dependency lifecycle dispatch authority changed",
        ):
            coordinator.tick()

        assert coordinator._state.snapshot().last_error_code == "ContinuousSessionError"


def test_tick_rejects_matching_keys_dispatch_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
        index.register("input-a", source_ids="provider-a")
        coordinator.dependency_index = index

        class Desktop:
            def drain(self, **_kwargs):
                raise AssertionError("desktop delivery ran after routing reader mutation")

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                raise AssertionError("lifecycle ran after routing reader mutation")

        def mutate() -> None:
            monkeypatch.setattr(
                continuous_session.FocusedMirrorDependencyIndex,
                "matching_keys",
                lambda self, input_id: (),
            )

        coordinator.collector = _Collector(callback=mutate)
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="canonical dependency lifecycle dispatch authority changed",
        ):
            coordinator.tick()

        assert coordinator._state.snapshot().last_error_code == "ContinuousSessionError"


def test_provider_unavailable_tick_rejects_dependency_reader_dispatch_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
        index.register("input-a", source_ids="provider-a")
        coordinator.dependency_index = index

        class ProviderUnavailableCollector(_Collector):
            def run_cycle(self):
                if self.callback is not None:
                    self.callback()
                return type(
                    "UnavailableCycle",
                    (),
                    {
                        "provider_unavailable": True,
                        "source_id": "provider-a",
                        "committed_delta_ids": (),
                    },
                )()

        def mutate() -> None:
            monkeypatch.setattr(
                continuous_session.FocusedMirrorDependencyIndex,
                "_dependency",
                lambda self, input_id: self._dependencies[input_id],
            )

        coordinator.collector = ProviderUnavailableCollector(callback=mutate)

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="canonical dependency lifecycle dispatch authority changed",
        ):
            coordinator.tick()

        assert coordinator._state.snapshot().last_error_code == "ContinuousSessionError"

def test_invalidation_rejects_matched_key_mutation_during_drain() -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
    index.register("input-a", source_ids="provider-a")

    class Buffer:
        pending_count = 0
        full_refresh_required = False

        def drain(self, *, max_items: int):
            assert max_items == 250
            index._matched_keys["input-a"].add(("provider-a", "forged-quote"))
            return continuous_session.MirrorInvalidationBatch(
                changed_keys=(),
                full_refresh_required=False,
                has_more=False,
            )

    buffer = Buffer()
    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="dependency index matched-key routing changed during invalidation drain",
    ):
        coordinator._drain_invalidations(
            invalidation_buffer=buffer,
            dependency_index=index,
            drain_invalidation=buffer.drain,
            affected_inputs=index.affected_inputs,
            max_batches=4,
            max_items=250,
        )


def test_invalidation_rejects_matched_key_mutation_during_backlog_inspection() -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
    index.register("input-a", source_ids="provider-a")

    class Buffer:
        full_refresh_required = False

        @property
        def pending_count(self):
            index._matched_keys["input-a"].add(("provider-a", "forged-quote"))
            return 0

        def drain(self, *, max_items: int):
            assert max_items == 250
            return continuous_session.MirrorInvalidationBatch(
                changed_keys=(),
                full_refresh_required=False,
                has_more=False,
            )

    buffer = Buffer()
    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="dependency index matched-key routing changed during invalidation backlog inspection",
    ):
        coordinator._drain_invalidations(
            invalidation_buffer=buffer,
            dependency_index=index,
            drain_invalidation=buffer.drain,
            affected_inputs=index.affected_inputs,
            max_batches=4,
            max_items=250,
        )


def test_provider_unavailable_rejects_matched_key_mutation_during_backlog_inspection() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
        index.register("input-a", source_ids="provider-a")
        coordinator.dependency_index = index

        class ProviderUnavailableCollector(_Collector):
            def run_cycle(self):
                return type(
                    "UnavailableCycle",
                    (),
                    {
                        "provider_unavailable": True,
                        "source_id": "provider-a",
                        "committed_delta_ids": (),
                    },
                )()

        class Buffer:
            full_refresh_required = False

            @property
            def pending_count(self):
                index._matched_keys["input-a"].add(("provider-a", "forged-quote"))
                return 0

        coordinator.collector = ProviderUnavailableCollector()
        coordinator.invalidation_buffer = Buffer()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="dependency routing authority changed during provider-unavailable backlog inspection",
        ):
            coordinator.tick()

        assert coordinator._state.snapshot().last_error_code == "ContinuousSessionError"

def test_collector_routing_selector_tamper_is_restored_before_failure_publication() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
        dependency = index.register("input-a", source_ids="provider-a")
        coordinator.dependency_index = index

        class Desktop:
            def drain(self, **_kwargs):
                raise AssertionError("desktop delivery ran after collector routing mutation")

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                raise AssertionError("lifecycle ran after collector routing mutation")

        def mutate() -> None:
            object.__setattr__(
                dependency,
                "source_ids",
                frozenset({"provider-b"}),
            )

        coordinator.collector = _Collector(callback=mutate)
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="dependency routing authority changed during collector observation",
        ):
            coordinator.tick()

        assert index._dependency("input-a").source_ids == frozenset({"provider-a"})
        assert coordinator._state.snapshot().last_error_code == "ContinuousSessionError"


def test_collector_matched_key_tamper_is_restored_before_failure_publication() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
        index.register("input-a", source_ids="provider-a")
        coordinator.dependency_index = index

        class Desktop:
            def drain(self, **_kwargs):
                raise AssertionError("desktop delivery ran after collector routing mutation")

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                raise AssertionError("lifecycle ran after collector routing mutation")

        def mutate() -> None:
            index._matched_keys["input-a"].add(("provider-a", "forged-quote"))

        coordinator.collector = _Collector(callback=mutate)
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="dependency routing authority changed during collector observation",
        ):
            coordinator.tick()

        assert index.matching_keys("input-a") == ()
        assert coordinator._state.snapshot().last_error_code == "ContinuousSessionError"


def test_desktop_dependency_storage_rebind_is_restored_before_failure_publication() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
        dependency = index.register("input-a", source_ids="provider-a")
        canonical_storage = index._dependencies
        coordinator.dependency_index = index

        class Desktop:
            def drain(self, **_kwargs):
                index._dependencies = {}
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                raise AssertionError("lifecycle ran after dependency storage rebind")

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="dependency routing authority changed during desktop delivery",
        ):
            coordinator.tick()

        assert index._dependencies is canonical_storage
        assert index._dependency("input-a") is dependency
        assert coordinator._state.snapshot().last_error_code == "ContinuousSessionError"


def test_settlement_prepare_selector_tamper_restores_latest_accepted_routing_baseline() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
        dependency = index.register("input-a", source_ids="provider-a")
        coordinator.dependency_index = index

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        class Handoff:
            def prepare_settlement(self, **_kwargs):
                object.__setattr__(
                    dependency,
                    "source_ids",
                    frozenset({"provider-b"}),
                )

            def reconcile_after_settlement(self, **_kwargs):
                raise AssertionError("reconcile ran after settlement preparation tamper")

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()
        coordinator.settlement_learning_handoff = Handoff()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="dependency routing authority changed during settlement preparation",
        ):
            coordinator.tick()

        assert index._dependency("input-a").source_ids == frozenset({"provider-a"})
        assert coordinator._state.snapshot().last_error_code == "ContinuousSessionError"

def test_invalidation_restores_selector_tamper_during_drain() -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
    dependency = index.register("input-a", source_ids="provider-a")

    class Buffer:
        pending_count = 0
        full_refresh_required = False

        def drain(self, *, max_items: int):
            assert max_items == 250
            object.__setattr__(
                dependency,
                "source_ids",
                frozenset({"provider-b"}),
            )
            return continuous_session.MirrorInvalidationBatch(
                changed_keys=(),
                full_refresh_required=False,
                has_more=False,
            )

    buffer = Buffer()
    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="routing authority changed during invalidation drain",
    ):
        coordinator._drain_invalidations(
            invalidation_buffer=buffer,
            dependency_index=index,
            drain_invalidation=buffer.drain,
            affected_inputs=index.affected_inputs,
            max_batches=4,
            max_items=250,
        )

    assert index._dependency("input-a").source_ids == frozenset({"provider-a"})


def test_invalidation_restores_matched_keys_tamper_during_drain() -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
    index.register("input-a", source_ids="provider-a")

    class Buffer:
        pending_count = 0
        full_refresh_required = False

        def drain(self, *, max_items: int):
            assert max_items == 250
            index._matched_keys["input-a"].add(("provider-a", "forged-quote"))
            return continuous_session.MirrorInvalidationBatch(
                changed_keys=(),
                full_refresh_required=False,
                has_more=False,
            )

    buffer = Buffer()
    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="matched-key routing changed during invalidation drain",
    ):
        coordinator._drain_invalidations(
            invalidation_buffer=buffer,
            dependency_index=index,
            drain_invalidation=buffer.drain,
            affected_inputs=index.affected_inputs,
            max_batches=4,
            max_items=250,
        )

    assert index.matching_keys("input-a") == ()


def test_invalidation_restores_matched_keys_tamper_during_backlog_inspection() -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
    index.register("input-a", source_ids="provider-a")

    class Buffer:
        full_refresh_required = False

        @property
        def pending_count(self):
            index._matched_keys["input-a"].add(("provider-a", "forged-quote"))
            return 0

        def drain(self, *, max_items: int):
            assert max_items == 250
            return continuous_session.MirrorInvalidationBatch(
                changed_keys=(),
                full_refresh_required=False,
                has_more=False,
            )

    buffer = Buffer()
    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="matched-key routing changed during invalidation backlog inspection",
    ):
        coordinator._drain_invalidations(
            invalidation_buffer=buffer,
            dependency_index=index,
            drain_invalidation=buffer.drain,
            affected_inputs=index.affected_inputs,
            max_batches=4,
            max_items=250,
        )

    assert index.matching_keys("input-a") == ()

def test_consumed_invalidation_routing_failure_promotes_next_drain_to_full_refresh() -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    mirror = MarketMirror()
    buffer = continuous_session.BoundedMirrorInvalidationBuffer(mirror)
    buffer._dirty[("provider-a", "quote-a")] = None
    index = continuous_session.FocusedMirrorDependencyIndex(mirror)
    index.register("input-a", source_ids="provider-a")

    def fail_routing(_batch):
        raise RuntimeError("routing failed after drain")

    with pytest.raises(RuntimeError, match="routing failed after drain"):
        coordinator._drain_invalidations(
            invalidation_buffer=buffer,
            dependency_index=index,
            drain_invalidation=buffer.drain,
            affected_inputs=fail_routing,
            max_batches=4,
            max_items=250,
        )

    assert buffer.pending_count == 0
    assert buffer.full_refresh_required is True
    recovery = buffer.drain()
    assert recovery.changed_keys == ()
    assert recovery.full_refresh_required is True
    assert recovery.has_more is False


def test_consumed_invalidation_receipt_failure_promotes_next_drain_to_full_refresh() -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    mirror = MarketMirror()
    buffer = continuous_session.BoundedMirrorInvalidationBuffer(mirror)
    buffer._dirty[("provider-a", "quote-a")] = None
    index = continuous_session.FocusedMirrorDependencyIndex(mirror)
    index.register("input-a", source_ids="provider-a")

    def invalid_routing(_batch):
        return ("missing-input",)

    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="routed an unregistered input",
    ):
        coordinator._drain_invalidations(
            invalidation_buffer=buffer,
            dependency_index=index,
            drain_invalidation=buffer.drain,
            affected_inputs=invalid_routing,
            max_batches=4,
            max_items=250,
        )

    assert buffer.full_refresh_required is True
    assert buffer.drain().full_refresh_required is True


def test_successful_invalidation_routing_does_not_force_recovery_refresh() -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    mirror = MarketMirror()
    buffer = continuous_session.BoundedMirrorInvalidationBuffer(mirror)
    buffer._dirty[("provider-a", "quote-a")] = None
    index = continuous_session.FocusedMirrorDependencyIndex(mirror)
    index.register("input-a", source_ids="provider-a")

    affected, full_refresh, backlog = coordinator._drain_invalidations(
        invalidation_buffer=buffer,
        dependency_index=index,
        drain_invalidation=buffer.drain,
        affected_inputs=lambda _batch: (),
        max_batches=4,
        max_items=250,
    )

    assert affected == ()
    assert full_refresh is False
    assert backlog is False
    assert buffer.full_refresh_required is False

@pytest.mark.parametrize(
    "mutate",
    (
        lambda buffer: setattr(buffer, "_dirty", []),
        lambda buffer: setattr(
            buffer,
            "_dirty",
            {("provider-a", "quote-a"): object()},
        ),
        lambda buffer: setattr(
            buffer,
            "_dirty",
            {("", "quote-a"): None},
        ),
        lambda buffer: setattr(buffer, "_max_dirty_keys", 0),
        lambda buffer: (
            setattr(buffer, "_dirty", {("provider-a", "quote-a"): None}),
            setattr(buffer, "_full_refresh_required", True),
        ),
    ),
)
def test_direct_invalidation_helper_rejects_malformed_canonical_buffer_state(
    mutate,
) -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    mirror = MarketMirror()
    buffer = continuous_session.BoundedMirrorInvalidationBuffer(mirror)
    index = continuous_session.FocusedMirrorDependencyIndex(mirror)
    mutate(buffer)

    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="canonical invalidation buffer state is invalid",
    ):
        coordinator._drain_invalidations(
            invalidation_buffer=buffer,
            dependency_index=index,
            drain_invalidation=buffer.drain,
            affected_inputs=index.affected_inputs,
            max_batches=4,
            max_items=250,
        )


def test_tick_rejects_malformed_canonical_invalidation_state_before_provider_io() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        mirror = MarketMirror()
        buffer = continuous_session.BoundedMirrorInvalidationBuffer(mirror)
        buffer._dirty = {("provider-a", "quote-a"): None}
        buffer._full_refresh_required = True
        coordinator.invalidation_buffer = buffer
        coordinator.dependency_index = (
            continuous_session.FocusedMirrorDependencyIndex(mirror)
        )
        coordinator.collector = _Collector(
            callback=lambda: (_ for _ in ()).throw(
                AssertionError("collector ran before malformed invalidation rejection")
            )
        )

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="canonical invalidation buffer state is invalid",
        ):
            coordinator.tick()


def test_tick_rejects_invalidation_drain_dispatch_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        mirror = MarketMirror()
        buffer = continuous_session.BoundedMirrorInvalidationBuffer(mirror)
        coordinator.invalidation_buffer = buffer
        coordinator.dependency_index = (
            continuous_session.FocusedMirrorDependencyIndex(mirror)
        )

        class Desktop:
            def drain(self, **_kwargs):
                raise AssertionError("desktop ran after invalidation dispatch mutation")

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                raise AssertionError("lifecycle ran after invalidation dispatch mutation")

        def mutate() -> None:
            monkeypatch.setattr(
                continuous_session.BoundedMirrorInvalidationBuffer,
                "drain",
                lambda self, *, max_items=250: continuous_session.MirrorInvalidationBatch(
                    changed_keys=(),
                    full_refresh_required=False,
                    has_more=False,
                ),
            )

        coordinator.collector = _Collector(callback=mutate)
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="canonical invalidation buffer dispatch authority changed",
        ):
            coordinator.tick()

        assert coordinator._state.snapshot().last_error_code == "ContinuousSessionError"


def test_tick_rejects_invalidation_pending_descriptor_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        mirror = MarketMirror()
        buffer = continuous_session.BoundedMirrorInvalidationBuffer(mirror)
        coordinator.invalidation_buffer = buffer
        coordinator.dependency_index = (
            continuous_session.FocusedMirrorDependencyIndex(mirror)
        )

        class Desktop:
            def drain(self, **_kwargs):
                raise AssertionError("desktop ran after invalidation descriptor mutation")

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                raise AssertionError("lifecycle ran after invalidation descriptor mutation")

        def mutate() -> None:
            monkeypatch.setattr(
                continuous_session.BoundedMirrorInvalidationBuffer,
                "pending_count",
                property(lambda self: 0),
            )

        coordinator.collector = _Collector(callback=mutate)
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="canonical invalidation buffer dispatch authority changed",
        ):
            coordinator.tick()

        assert coordinator._state.snapshot().last_error_code == "ContinuousSessionError"


def test_direct_invalidation_helper_rejects_canonical_buffer_drain_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    mirror = MarketMirror()
    buffer = continuous_session.BoundedMirrorInvalidationBuffer(mirror)
    index = continuous_session.FocusedMirrorDependencyIndex(mirror)

    monkeypatch.setattr(
        continuous_session.BoundedMirrorInvalidationBuffer,
        "drain",
        lambda self, *, max_items=250: continuous_session.MirrorInvalidationBatch(
            changed_keys=(),
            full_refresh_required=False,
            has_more=False,
        ),
    )

    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="canonical invalidation buffer dispatch authority changed",
    ):
        coordinator._drain_invalidations(
            invalidation_buffer=buffer,
            dependency_index=index,
            drain_invalidation=buffer.drain,
            affected_inputs=index.affected_inputs,
            max_batches=4,
            max_items=250,
        )


def test_direct_invalidation_helper_rejects_canonical_buffer_subclass() -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    mirror = MarketMirror()

    class ForgedInvalidationBuffer(
        continuous_session.BoundedMirrorInvalidationBuffer
    ):
        def drain(self, *, max_items=250):
            return continuous_session.MirrorInvalidationBatch(
                changed_keys=(),
                full_refresh_required=False,
                has_more=False,
            )

    buffer = ForgedInvalidationBuffer(mirror)
    index = continuous_session.FocusedMirrorDependencyIndex(mirror)

    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="canonical invalidation buffer subtype is not supported",
    ):
        coordinator._drain_invalidations(
            invalidation_buffer=buffer,
            dependency_index=index,
            drain_invalidation=buffer.drain,
            affected_inputs=index.affected_inputs,
            max_batches=4,
            max_items=250,
        )


def test_tick_rejects_canonical_invalidation_buffer_subclass_before_cycle() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        mirror = MarketMirror()

        class ForgedInvalidationBuffer(
            continuous_session.BoundedMirrorInvalidationBuffer
        ):
            def drain(self, *, max_items=250):
                raise AssertionError("subclass invalidation drain must not run")

        buffer = ForgedInvalidationBuffer(mirror)
        coordinator.invalidation_buffer = buffer
        coordinator.dependency_index = (
            continuous_session.FocusedMirrorDependencyIndex(mirror)
        )
        coordinator.collector = _Collector(
            callback=lambda: (_ for _ in ()).throw(
                AssertionError("collector ran before invalidation subtype rejection")
            )
        )

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="canonical invalidation buffer subtype is not supported",
        ):
            coordinator.tick()

def test_collector_invalidation_buffer_rebind_is_restored_before_next_phase() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        canonical = coordinator.invalidation_buffer
        replacement = _InvalidationBuffer()

        class Desktop:
            def drain(self, **_kwargs):
                raise AssertionError("desktop ran after invalidation buffer rebind")

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                raise AssertionError("lifecycle ran after invalidation buffer rebind")

        def rebind() -> None:
            coordinator.invalidation_buffer = replacement

        coordinator.collector = _Collector(callback=rebind)
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="continuous-session invalidation buffer authority changed during tick",
        ):
            coordinator.tick()

        assert coordinator.invalidation_buffer is canonical
        assert coordinator._state.snapshot().last_error_code == "ContinuousSessionError"


def test_collector_failure_restores_invalidation_buffer_rebind() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        canonical = coordinator.invalidation_buffer
        replacement = _InvalidationBuffer()

        class FailingCollector(_Collector):
            def run_cycle(self):
                coordinator.invalidation_buffer = replacement
                raise RuntimeError("collector failed after rebind")

        coordinator.collector = FailingCollector()

        with pytest.raises(RuntimeError, match="collector failed after rebind"):
            coordinator.tick()

        assert coordinator.invalidation_buffer is canonical
        assert coordinator._state.snapshot().last_error_code == "RuntimeError"


def test_desktop_failure_restores_invalidation_buffer_rebind() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        canonical = coordinator.invalidation_buffer
        replacement = _InvalidationBuffer()

        class Desktop:
            def drain(self, **_kwargs):
                coordinator.invalidation_buffer = replacement
                raise RuntimeError("desktop failed after rebind")

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(RuntimeError, match="desktop failed after rebind"):
            coordinator.tick()

        assert coordinator.invalidation_buffer is canonical
        assert coordinator._state.snapshot().last_error_code == "RuntimeError"

def test_collector_malformed_matched_keys_are_restored_without_sorting_tampered_state() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        index = continuous_session.FocusedMirrorDependencyIndex(MarketMirror())
        index.register("input-a", source_ids="provider-a")
        coordinator.dependency_index = index

        class Desktop:
            def drain(self, **_kwargs):
                raise AssertionError("desktop ran after malformed routing state")

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                raise AssertionError("lifecycle ran after malformed routing state")

        def mutate() -> None:
            index._matched_keys["input-a"].add(("provider-a", "forged-quote"))
            index._matched_keys["input-a"].add(object())

        coordinator.collector = _Collector(callback=mutate)
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="dependency routing authority changed during collector observation",
        ):
            coordinator.tick()

        assert index.matching_keys("input-a") == ()
        assert coordinator._state.snapshot().last_error_code == "ContinuousSessionError"

def test_collector_invalidation_dirty_storage_rebind_is_restored() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        mirror = MarketMirror()
        buffer = continuous_session.BoundedMirrorInvalidationBuffer(mirror)
        canonical_dirty = buffer._dirty
        coordinator.invalidation_buffer = buffer
        coordinator.dependency_index = (
            continuous_session.FocusedMirrorDependencyIndex(mirror)
        )

        class Desktop:
            def drain(self, **_kwargs):
                raise AssertionError("desktop ran after invalidation storage rebind")

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                raise AssertionError("lifecycle ran after invalidation storage rebind")

        def mutate() -> None:
            buffer._dirty = {("provider-a", "forged"): None}

        coordinator.collector = _Collector(callback=mutate)
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="invalidation buffer structure changed during tick",
        ):
            coordinator.tick()

        assert buffer._dirty is canonical_dirty
        assert coordinator._state.snapshot().last_error_code == "ContinuousSessionError"


def test_desktop_invalidation_capacity_mutation_is_restored() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        mirror = MarketMirror()
        buffer = continuous_session.BoundedMirrorInvalidationBuffer(
            mirror,
            max_dirty_keys=8,
        )
        coordinator.invalidation_buffer = buffer
        coordinator.dependency_index = (
            continuous_session.FocusedMirrorDependencyIndex(mirror)
        )

        class Desktop:
            def drain(self, **_kwargs):
                buffer._max_dirty_keys = 1
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                raise AssertionError("lifecycle ran after invalidation capacity mutation")

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="invalidation buffer structure changed during tick",
        ):
            coordinator.tick()

        assert buffer.max_dirty_keys == 8
        assert coordinator._state.snapshot().last_error_code == "ContinuousSessionError"


def test_desktop_failure_restores_invalidation_lock_rebind() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        mirror = MarketMirror()
        buffer = continuous_session.BoundedMirrorInvalidationBuffer(mirror)
        canonical_lock = buffer._lock
        coordinator.invalidation_buffer = buffer
        coordinator.dependency_index = (
            continuous_session.FocusedMirrorDependencyIndex(mirror)
        )

        class Desktop:
            def drain(self, **_kwargs):
                buffer._lock = object()
                raise RuntimeError("desktop failed after invalidation lock rebind")

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            RuntimeError,
            match="desktop failed after invalidation lock rebind",
        ):
            coordinator.tick()

        assert buffer._lock is canonical_lock
        assert coordinator._state.snapshot().last_error_code == "RuntimeError"


def test_register_rejects_callback_dependency_index_class_mutation() -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    mirror = MarketMirror()
    index = continuous_session.FocusedMirrorDependencyIndex(mirror)

    class MutatedIndex(continuous_session.FocusedMirrorDependencyIndex):
        pass

    def register(input_id: str, **selectors: object) -> None:
        index.__class__ = MutatedIndex
        continuous_session.FocusedMirrorDependencyIndex.register(
            index,
            input_id,
            **selectors,
        )

    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="canonical dependency index type changed during lifecycle registration",
    ):
        coordinator._register_input(
            "input-a",
            dependency_index=index,
            register_input=register,
            source_ids="provider-a",
        )

    assert type(index) is continuous_session.FocusedMirrorDependencyIndex


def test_retire_rejects_callback_dependency_index_class_mutation() -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    mirror = MarketMirror()
    index = continuous_session.FocusedMirrorDependencyIndex(mirror)
    index.register("input-a", source_ids="provider-a")

    class MutatedIndex(continuous_session.FocusedMirrorDependencyIndex):
        pass

    def unregister(input_id: str) -> bool:
        index.__class__ = MutatedIndex
        return continuous_session.FocusedMirrorDependencyIndex.unregister(
            index,
            input_id,
        )

    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="canonical dependency index type changed during lifecycle retirement",
    ):
        coordinator._retire_input(
            "input-a",
            dependency_index=index,
            unregister_input=unregister,
        )

    assert type(index) is continuous_session.FocusedMirrorDependencyIndex


def test_provider_unavailable_rejects_dependency_index_class_mutation() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        mirror = MarketMirror()
        coordinator.invalidation_buffer = (
            continuous_session.BoundedMirrorInvalidationBuffer(mirror)
        )
        index = continuous_session.FocusedMirrorDependencyIndex(mirror)
        coordinator.dependency_index = index

        class MutatedIndex(continuous_session.FocusedMirrorDependencyIndex):
            pass

        class ProviderUnavailableCollector(_Collector):
            def run_cycle(self):
                index.__class__ = MutatedIndex
                return type(
                    "ProviderUnavailableCycle",
                    (),
                    {
                        "provider_unavailable": True,
                        "source_id": "provider-a",
                        "committed_delta_ids": (),
                    },
                )()

        class Desktop:
            def drain(self, **_kwargs):
                raise AssertionError("desktop ran after dependency type mutation")

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                raise AssertionError("lifecycle ran after dependency type mutation")

        coordinator.collector = ProviderUnavailableCollector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="dependency index authority changed during tick",
        ):
            coordinator.tick()

        assert type(index) is continuous_session.FocusedMirrorDependencyIndex
        assert coordinator._state.snapshot().last_error_code == "ContinuousSessionError"


def test_provider_unavailable_rejects_invalidation_buffer_class_mutation() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        mirror = MarketMirror()
        buffer = continuous_session.BoundedMirrorInvalidationBuffer(mirror)
        coordinator.invalidation_buffer = buffer
        coordinator.dependency_index = (
            continuous_session.FocusedMirrorDependencyIndex(mirror)
        )

        class MutatedBuffer(continuous_session.BoundedMirrorInvalidationBuffer):
            pass

        class ProviderUnavailableCollector(_Collector):
            def run_cycle(self):
                buffer.__class__ = MutatedBuffer
                return type(
                    "ProviderUnavailableCycle",
                    (),
                    {
                        "provider_unavailable": True,
                        "source_id": "provider-a",
                        "committed_delta_ids": (),
                    },
                )()

        class Desktop:
            def drain(self, **_kwargs):
                raise AssertionError("desktop ran after invalidation type mutation")

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                raise AssertionError("lifecycle ran after invalidation type mutation")

        coordinator.collector = ProviderUnavailableCollector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="invalidation buffer structure changed during tick",
        ):
            coordinator.tick()

        assert type(buffer) is continuous_session.BoundedMirrorInvalidationBuffer
        assert coordinator._state.snapshot().last_error_code == "ContinuousSessionError"


def test_tick_rejects_canonical_lifecycle_class_mutation_after_provider_io() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        lifecycle = continuous_session.ContinuousEventLifecycle(
            root / "event_lifecycle.json"
        )

        class MutatedLifecycle(continuous_session.ContinuousEventLifecycle):
            pass

        class Desktop:
            def drain(self, **_kwargs):
                raise AssertionError("desktop ran after lifecycle type mutation")

        def mutate() -> None:
            lifecycle.__class__ = MutatedLifecycle

        coordinator.lifecycle = lifecycle
        coordinator.desktop_consumer = Desktop()
        coordinator.collector = _Collector(callback=mutate)

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="canonical event lifecycle type changed during tick",
        ):
            coordinator.tick()

        assert type(lifecycle) is continuous_session.ContinuousEventLifecycle
        assert coordinator._state.snapshot().last_error_code == "ContinuousSessionError"

def test_invalidation_rejects_dependency_index_class_mutation_during_routing() -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    mirror = MarketMirror()
    buffer = continuous_session.BoundedMirrorInvalidationBuffer(mirror)
    buffer._dirty[("provider-a", "quote-1")] = None
    index = continuous_session.FocusedMirrorDependencyIndex(mirror)

    class MutatedIndex(continuous_session.FocusedMirrorDependencyIndex):
        pass

    def affected_inputs(_batch) -> tuple[str, ...]:
        index.__class__ = MutatedIndex
        return ()

    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="canonical dependency index type changed during invalidation routing",
    ):
        coordinator._drain_invalidations(
            invalidation_buffer=buffer,
            dependency_index=index,
            drain_invalidation=buffer.drain,
            affected_inputs=affected_inputs,
            max_batches=4,
            max_items=250,
        )

    assert type(index) is continuous_session.FocusedMirrorDependencyIndex
    assert buffer.pending_count == 0
    assert buffer.full_refresh_required is True


def test_invalidation_rejects_buffer_class_mutation_during_routing() -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    mirror = MarketMirror()
    buffer = continuous_session.BoundedMirrorInvalidationBuffer(mirror)
    buffer._dirty[("provider-a", "quote-1")] = None

    class MutatedBuffer(continuous_session.BoundedMirrorInvalidationBuffer):
        pass

    class Index:
        input_ids = ()

        def affected_inputs(self, _batch):
            buffer.__class__ = MutatedBuffer
            return ()

    index = Index()

    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="canonical invalidation buffer type changed during invalidation routing",
    ):
        coordinator._drain_invalidations(
            invalidation_buffer=buffer,
            dependency_index=index,
            drain_invalidation=buffer.drain,
            affected_inputs=index.affected_inputs,
            max_batches=4,
            max_items=250,
        )

    assert type(buffer) is continuous_session.BoundedMirrorInvalidationBuffer
    assert buffer.pending_count == 0
    assert buffer.full_refresh_required is True

def test_collector_failure_restores_dependency_index_class_mutation() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        mirror = MarketMirror()
        index = continuous_session.FocusedMirrorDependencyIndex(mirror)
        coordinator.dependency_index = index
        coordinator.invalidation_buffer = (
            continuous_session.BoundedMirrorInvalidationBuffer(mirror)
        )

        class MutatedIndex(continuous_session.FocusedMirrorDependencyIndex):
            pass

        class FailingCollector(_Collector):
            def run_cycle(self):
                index.__class__ = MutatedIndex
                raise RuntimeError("collector failed after dependency type mutation")

        coordinator.collector = FailingCollector()

        with pytest.raises(
            RuntimeError,
            match="collector failed after dependency type mutation",
        ):
            coordinator.tick()

        assert type(index) is continuous_session.FocusedMirrorDependencyIndex
        assert coordinator._state.snapshot().last_error_code == "RuntimeError"


def test_collector_failure_restores_lifecycle_class_mutation() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        lifecycle = continuous_session.ContinuousEventLifecycle(
            root / "event_lifecycle.json"
        )
        coordinator.lifecycle = lifecycle

        class MutatedLifecycle(continuous_session.ContinuousEventLifecycle):
            pass

        class FailingCollector(_Collector):
            def run_cycle(self):
                lifecycle.__class__ = MutatedLifecycle
                raise RuntimeError("collector failed after lifecycle type mutation")

        coordinator.collector = FailingCollector()

        with pytest.raises(
            RuntimeError,
            match="collector failed after lifecycle type mutation",
        ):
            coordinator.tick()

        assert type(lifecycle) is continuous_session.ContinuousEventLifecycle
        assert coordinator._state.snapshot().last_error_code == "RuntimeError"


def test_desktop_failure_restores_lifecycle_class_mutation() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        lifecycle = continuous_session.ContinuousEventLifecycle(
            root / "event_lifecycle.json"
        )
        coordinator.lifecycle = lifecycle

        class MutatedLifecycle(continuous_session.ContinuousEventLifecycle):
            pass

        class Desktop:
            def drain(self, **_kwargs):
                lifecycle.__class__ = MutatedLifecycle
                raise RuntimeError("desktop failed after lifecycle type mutation")

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()

        with pytest.raises(
            RuntimeError,
            match="desktop failed after lifecycle type mutation",
        ):
            coordinator.tick()

        assert type(lifecycle) is continuous_session.ContinuousEventLifecycle
        assert coordinator._state.snapshot().last_error_code == "RuntimeError"

def test_collector_cannot_consume_pending_invalidations() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        mirror = MarketMirror()
        buffer = continuous_session.BoundedMirrorInvalidationBuffer(mirror)
        buffer._dirty[("provider-a", "quote-1")] = None
        coordinator.invalidation_buffer = buffer
        coordinator.dependency_index = (
            continuous_session.FocusedMirrorDependencyIndex(mirror)
        )

        class Desktop:
            def drain(self, **_kwargs):
                raise AssertionError("desktop ran after collector stole invalidation")

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                raise AssertionError("lifecycle ran after collector stole invalidation")

        coordinator.collector = _Collector(
            callback=lambda: buffer.drain(max_items=250)
        )
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="collector observation consumed pending invalidations",
        ):
            coordinator.tick()

        assert buffer.pending_count == 0
        assert buffer.full_refresh_required is True
        assert coordinator._state.snapshot().last_error_code == "ContinuousSessionError"


def test_collector_failure_after_consuming_invalidation_preserves_error_and_recovers() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        mirror = MarketMirror()
        buffer = continuous_session.BoundedMirrorInvalidationBuffer(mirror)
        buffer._dirty[("provider-a", "quote-1")] = None
        coordinator.invalidation_buffer = buffer
        coordinator.dependency_index = (
            continuous_session.FocusedMirrorDependencyIndex(mirror)
        )

        class FailingCollector(_Collector):
            def run_cycle(self):
                buffer.drain(max_items=250)
                raise RuntimeError("collector failed after consuming invalidation")

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        coordinator.collector = FailingCollector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            RuntimeError,
            match="collector failed after consuming invalidation",
        ):
            coordinator.tick()

        assert buffer.pending_count == 0
        assert buffer.full_refresh_required is True
        assert coordinator._state.snapshot().last_error_code == "RuntimeError"


def test_desktop_cannot_consume_pending_invalidations() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        mirror = MarketMirror()
        buffer = continuous_session.BoundedMirrorInvalidationBuffer(mirror)
        buffer._dirty[("provider-a", "quote-1")] = None
        coordinator.invalidation_buffer = buffer
        coordinator.dependency_index = (
            continuous_session.FocusedMirrorDependencyIndex(mirror)
        )

        class Desktop:
            def drain(self, **_kwargs):
                buffer.drain(max_items=250)
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                raise AssertionError("lifecycle ran after desktop stole invalidation")

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="desktop delivery consumed pending invalidations",
        ):
            coordinator.tick()

        assert buffer.pending_count == 0
        assert buffer.full_refresh_required is True
        assert coordinator._state.snapshot().last_error_code == "ContinuousSessionError"


def test_desktop_failure_after_consuming_invalidation_preserves_error_and_recovers() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        mirror = MarketMirror()
        buffer = continuous_session.BoundedMirrorInvalidationBuffer(mirror)
        buffer._dirty[("provider-a", "quote-1")] = None
        coordinator.invalidation_buffer = buffer
        coordinator.dependency_index = (
            continuous_session.FocusedMirrorDependencyIndex(mirror)
        )

        class Desktop:
            def drain(self, **_kwargs):
                buffer.drain(max_items=250)
                raise RuntimeError("desktop failed after consuming invalidation")

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            RuntimeError,
            match="desktop failed after consuming invalidation",
        ):
            coordinator.tick()

        assert buffer.pending_count == 0
        assert buffer.full_refresh_required is True
        assert coordinator._state.snapshot().last_error_code == "RuntimeError"

class _InjectAfterCoordinatorDrainLock:
    def __init__(
        self,
        buffer: continuous_session.BoundedMirrorInvalidationBuffer,
    ) -> None:
        self.buffer = buffer
        self.injected = False
        self._entered_with_dirty = False

    def __enter__(self):
        self._entered_with_dirty = bool(self.buffer._dirty)
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if (
            not self.injected
            and self._entered_with_dirty
            and not self.buffer._dirty
            and not self.buffer._full_refresh_required
        ):
            self.buffer._dirty[("provider-a", "quote-late")] = None
            self.injected = True


def _coordinator_with_late_post_drain_invalidation(
    root: Path,
) -> tuple[
    continuous_session.ContinuousSessionCoordinator,
    continuous_session.BoundedMirrorInvalidationBuffer,
]:
    coordinator = _base_coordinator(root)
    mirror = MarketMirror()
    buffer = continuous_session.BoundedMirrorInvalidationBuffer(mirror)
    buffer._dirty[("provider-a", "quote-initial")] = None
    buffer._lock = _InjectAfterCoordinatorDrainLock(buffer)
    coordinator.invalidation_buffer = buffer
    coordinator.dependency_index = (
        continuous_session.FocusedMirrorDependencyIndex(mirror)
    )
    coordinator.collector = _Collector()

    class Desktop:
        def drain(self, **_kwargs):
            return ()

    coordinator.desktop_consumer = Desktop()
    return coordinator, buffer


def test_event_lifecycle_cannot_consume_post_drain_invalidations() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator, buffer = _coordinator_with_late_post_drain_invalidation(root)

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                assert buffer.drain(max_items=250).changed_keys == (
                    ("provider-a", "quote-late"),
                )
                return ()

        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="event lifecycle consumed pending invalidations",
        ):
            coordinator.tick()

        assert buffer.pending_count == 0
        assert buffer.full_refresh_required is True
        assert coordinator._state.snapshot().last_error_code == "ContinuousSessionError"


def test_event_lifecycle_failure_after_consuming_post_drain_invalidation_recovers() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator, buffer = _coordinator_with_late_post_drain_invalidation(root)

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                buffer.drain(max_items=250)
                raise RuntimeError("lifecycle failed after consuming invalidation")

        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            RuntimeError,
            match="lifecycle failed after consuming invalidation",
        ):
            coordinator.tick()

        assert buffer.pending_count == 0
        assert buffer.full_refresh_required is True
        assert coordinator._state.snapshot().last_error_code == "RuntimeError"


def test_settlement_resolution_cannot_consume_post_drain_invalidations() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator, buffer = _coordinator_with_late_post_drain_invalidation(root)
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

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

            def records(self):
                return (record,)

        class OutcomeAuthority:
            def resolve(self, _record, *, as_of: str):
                assert as_of == _AT
                buffer.drain(max_items=250)
                return None

        coordinator.lifecycle = Lifecycle()
        coordinator.outcome_authority = OutcomeAuthority()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="settlement resolution consumed pending invalidations",
        ):
            coordinator.tick()

        assert buffer.pending_count == 0
        assert buffer.full_refresh_required is True


def test_settlement_prepare_cannot_consume_post_drain_invalidations() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator, buffer = _coordinator_with_late_post_drain_invalidation(root)

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        class Handoff:
            def prepare_settlement(self, **_kwargs):
                buffer.drain(max_items=250)
                return ()

            def reconcile_after_settlement(self, **_kwargs):
                raise AssertionError("reconcile ran after prepare stole invalidation")

        coordinator.lifecycle = Lifecycle()
        coordinator.settlement_learning_handoff = Handoff()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="settlement preparation consumed pending invalidations",
        ):
            coordinator.tick()

        assert buffer.pending_count == 0
        assert buffer.full_refresh_required is True


def test_settlement_reconcile_cannot_consume_post_drain_invalidations() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator, buffer = _coordinator_with_late_post_drain_invalidation(root)

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        class Handoff:
            def prepare_settlement(self, **_kwargs):
                return ()

            def reconcile_after_settlement(self, **_kwargs):
                buffer.drain(max_items=250)

        coordinator.lifecycle = Lifecycle()
        coordinator.settlement_learning_handoff = Handoff()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="settlement reconciliation consumed pending invalidations",
        ):
            coordinator.tick()

        assert buffer.pending_count == 0
        assert buffer.full_refresh_required is True
@pytest.mark.parametrize("injection_mode", ("dirty", "full_refresh"))
def test_tick_reports_post_drain_invalidation_backlog(
    injection_mode: str,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        mirror = MarketMirror()
        buffer = continuous_session.BoundedMirrorInvalidationBuffer(mirror)
        coordinator.invalidation_buffer = buffer
        coordinator.dependency_index = (
            continuous_session.FocusedMirrorDependencyIndex(mirror)
        )

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                if injection_mode == "dirty":
                    with buffer._lock:
                        buffer._dirty[("provider-a", "quote-late")] = None
                else:
                    buffer.force_full_refresh()
                return ()

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        result = coordinator.tick()
        status = coordinator.status()

        assert result.invalidation_backlog is True
        assert result.full_refresh_required is (injection_mode == "full_refresh")
        if injection_mode == "dirty":
            assert buffer.pending_count == 1
            assert buffer.full_refresh_required is False
            assert status.invalidation_pending_count == 1
            assert status.invalidation_full_refresh_required is False
        else:
            assert buffer.pending_count == 0
            assert buffer.full_refresh_required is True
            assert status.invalidation_pending_count == 0
            assert status.invalidation_full_refresh_required is True


def test_tick_reports_invalidation_added_during_settlement_reconciliation() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        mirror = MarketMirror()
        buffer = continuous_session.BoundedMirrorInvalidationBuffer(mirror)
        coordinator.invalidation_buffer = buffer
        coordinator.dependency_index = (
            continuous_session.FocusedMirrorDependencyIndex(mirror)
        )

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        class Handoff:
            def prepare_settlement(self, **_kwargs):
                return None

            def reconcile_after_settlement(self, **_kwargs):
                with buffer._lock:
                    buffer._dirty[("provider-a", "quote-after-reconcile")] = None
                return None

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()
        coordinator.settlement_learning_handoff = Handoff()

        result = coordinator.tick()
        status = coordinator.status()

        assert result.invalidation_backlog is True
        assert buffer.pending_count == 1
        assert buffer.full_refresh_required is False
        assert status.invalidation_pending_count == 1
        assert status.invalidation_full_refresh_required is False

@pytest.mark.parametrize("late_state", ("dirty", "full_refresh"))
def test_provider_unavailable_rechecks_backlog_after_failure_publication(
    late_state: str,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class ProviderUnavailableCollector(_Collector):
            def run_cycle(self):
                return type(
                    "ProviderUnavailableCycle",
                    (),
                    {
                        "provider_unavailable": True,
                        "source_id": "provider-a",
                        "committed_delta_ids": (),
                    },
                )()

        class Buffer:
            def __init__(self) -> None:
                self.pending_reads = 0
                self.full_refresh_reads = 0

            @property
            def pending_count(self) -> int:
                self.pending_reads += 1
                if late_state == "dirty" and self.pending_reads >= 2:
                    return 1
                return 0

            @property
            def full_refresh_required(self) -> bool:
                self.full_refresh_reads += 1
                return (
                    late_state == "full_refresh"
                    and self.full_refresh_reads >= 2
                )

            def drain(self, **_kwargs):
                raise AssertionError(
                    "provider-unavailable tick must not drain invalidations"
                )

        buffer = Buffer()
        coordinator.collector = ProviderUnavailableCollector()
        coordinator.invalidation_buffer = buffer

        result = coordinator.tick()

        assert buffer.pending_reads == 2
        assert buffer.full_refresh_reads == 2
        assert result.invalidation_backlog is True
        assert result.full_refresh_required is (late_state == "full_refresh")
        assert (
            coordinator._state.snapshot().last_error_code
            == "ProviderUnavailableError"
        )


@pytest.mark.parametrize("authority_target", ("state", "dependency_index"))
def test_provider_unavailable_rechecks_authority_after_failure_publication(
    authority_target: str,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        canonical_state = coordinator._state
        canonical_index = coordinator.dependency_index
        replacement_state = continuous_session._ContinuousSessionState(
            root / "late_provider_unavailable_session.json",
            session_id="late-provider-unavailable-session",
            source_id="provider-a",
            clock=lambda: _AT,
        )
        replacement_index = _DependencyIndex()

        class ProviderUnavailableCollector(_Collector):
            def run_cycle(self):
                return type(
                    "ProviderUnavailableCycle",
                    (),
                    {
                        "provider_unavailable": True,
                        "source_id": "provider-a",
                        "committed_delta_ids": (),
                    },
                )()

        class Buffer:
            full_refresh_required = False

            def __init__(self) -> None:
                self.pending_reads = 0

            @property
            def pending_count(self) -> int:
                self.pending_reads += 1
                if self.pending_reads == 2:
                    if authority_target == "state":
                        coordinator._state = replacement_state
                    else:
                        coordinator.dependency_index = replacement_index
                return 0

            def drain(self, **_kwargs):
                raise AssertionError(
                    "provider-unavailable tick must not drain invalidations"
                )

        buffer = Buffer()
        coordinator.collector = ProviderUnavailableCollector()
        coordinator.invalidation_buffer = buffer

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match=(
                "continuous session state authority changed during tick"
                if authority_target == "state"
                else "dependency index authority changed during tick"
            ),
        ):
            coordinator.tick()

        assert buffer.pending_reads == 2
        assert coordinator._state is canonical_state
        assert coordinator.dependency_index is canonical_index

@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    (
        (
            "state",
            "continuous session state authority changed during tick",
        ),
        (
            "dependency_storage",
            "dependency routing authority changed during success publication",
        ),
        (
            "economic_context",
            "settlement economic configuration changed during tick",
        ),
    ),
)
def test_tick_rechecks_authority_after_success_publication(
    mutation: str,
    expected_error: str,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        mirror = MarketMirror()
        buffer = continuous_session.BoundedMirrorInvalidationBuffer(mirror)
        index = continuous_session.FocusedMirrorDependencyIndex(mirror)
        coordinator.invalidation_buffer = buffer
        coordinator.dependency_index = index
        canonical_state = coordinator._state
        canonical_matched_keys = index._matched_keys

        replacement_state = continuous_session._ContinuousSessionState(
            root / "post_success_replacement.json",
            session_id="post-success-replacement",
            source_id="provider-a",
            clock=lambda: _AT,
        )

        class HookLock:
            def __init__(self) -> None:
                self.fired = False

            def __enter__(self):
                if canonical_state._cycles_completed == 1 and not self.fired:
                    self.fired = True
                    if mutation == "state":
                        coordinator._state = replacement_state
                    elif mutation == "dependency_storage":
                        index._matched_keys = {}
                    else:
                        coordinator.initial_bankroll = "999"
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        hook_lock = HookLock()
        buffer._lock = hook_lock

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match=expected_error,
        ):
            coordinator.tick()

        assert hook_lock.fired is True
        if mutation == "state":
            assert coordinator._state is canonical_state
        if mutation == "dependency_storage":
            assert index._matched_keys is canonical_matched_keys

@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    (
        (
            "state",
            "continuous session state authority changed during tick",
        ),
        (
            "dependency_storage",
            "dependency routing authority changed during final invalidation publication",
        ),
        (
            "economic_context",
            "settlement economic configuration changed during tick",
        ),
    ),
)
def test_tick_rechecks_authority_after_final_invalidation_snapshot(
    mutation: str,
    expected_error: str,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        mirror = MarketMirror()
        buffer = continuous_session.BoundedMirrorInvalidationBuffer(mirror)
        index = continuous_session.FocusedMirrorDependencyIndex(mirror)
        coordinator.invalidation_buffer = buffer
        coordinator.dependency_index = index
        canonical_state = coordinator._state
        canonical_matched_keys = index._matched_keys

        replacement_state = continuous_session._ContinuousSessionState(
            root / "final_snapshot_replacement.json",
            session_id="final-snapshot-replacement",
            source_id="provider-a",
            clock=lambda: _AT,
        )

        class HookLock:
            def __init__(self) -> None:
                self.post_success_entries = 0
                self.fired = False

            def __enter__(self):
                if canonical_state._cycles_completed == 1:
                    self.post_success_entries += 1
                    if self.post_success_entries == 2 and not self.fired:
                        self.fired = True
                        if mutation == "state":
                            coordinator._state = replacement_state
                        elif mutation == "dependency_storage":
                            index._matched_keys = {}
                        else:
                            coordinator.initial_bankroll = "999"
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        hook_lock = HookLock()
        buffer._lock = hook_lock

        class Desktop:
            def drain(self, **_kwargs):
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        coordinator.collector = _Collector()
        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match=expected_error,
        ):
            coordinator.tick()

        assert hook_lock.fired is True
        assert hook_lock.post_success_entries >= 2
        if mutation == "state":
            assert coordinator._state is canonical_state
        if mutation == "dependency_storage":
            assert index._matched_keys is canonical_matched_keys

@pytest.mark.parametrize(
    "failure_phase",
    ("collector", "provider_backlog", "desktop"),
)
def test_tick_exception_restores_economic_context(
    failure_phase: str,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        original_workspace = coordinator.workspace
        original_book = coordinator.paper_book_path
        original_bankroll = coordinator.initial_bankroll

        def mutate_and_raise() -> None:
            coordinator.workspace = root / "attacker-workspace"
            coordinator.paper_book_path = root / "attacker-paper-book.json"
            coordinator.initial_bankroll = "999999"
            raise RuntimeError("economic mutation failure")

        class Desktop:
            def drain(self, **_kwargs):
                if failure_phase == "desktop":
                    mutate_and_raise()
                return ()

        class Lifecycle:
            def register_eligible(self, *_args, **_kwargs):
                return ()

        if failure_phase == "collector":
            coordinator.collector = _Collector(callback=mutate_and_raise)
        elif failure_phase == "provider_backlog":
            class ProviderUnavailableCollector(_Collector):
                def run_cycle(self):
                    return type(
                        "ProviderUnavailableCycle",
                        (),
                        {
                            "provider_unavailable": True,
                            "source_id": "provider-a",
                            "committed_delta_ids": (),
                        },
                    )()

            class Buffer:
                full_refresh_required = False

                @property
                def pending_count(self):
                    mutate_and_raise()

                def drain(self, **_kwargs):
                    raise AssertionError(
                        "provider-unavailable tick must not drain invalidations"
                    )

            coordinator.collector = ProviderUnavailableCollector()
            coordinator.invalidation_buffer = Buffer()
        else:
            coordinator.collector = _Collector()

        coordinator.desktop_consumer = Desktop()
        coordinator.lifecycle = Lifecycle()

        with pytest.raises(RuntimeError, match="economic mutation failure"):
            coordinator.tick()

        assert coordinator.workspace == original_workspace
        assert coordinator.paper_book_path == original_book
        assert coordinator.initial_bankroll == original_bankroll

@pytest.mark.parametrize(
    ("late_failure", "expected_error", "expected_match"),
    (
        ("raises", RuntimeError, "late provider backlog accessor failed"),
        (
            "malformed",
            continuous_session.ContinuousSessionError,
            "invalidation buffer backlog state is invalid",
        ),
    ),
)
def test_provider_unavailable_late_backlog_failure_replaces_provider_receipt(
    late_failure: str,
    expected_error: type[Exception],
    expected_match: str,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)

        class ProviderUnavailableCollector(_Collector):
            def run_cycle(self):
                return type(
                    "ProviderUnavailableCycle",
                    (),
                    {
                        "provider_unavailable": True,
                        "source_id": "provider-a",
                        "committed_delta_ids": (),
                    },
                )()

        class Buffer:
            full_refresh_required = False

            def __init__(self) -> None:
                self.pending_reads = 0

            @property
            def pending_count(self):
                self.pending_reads += 1
                if self.pending_reads == 1:
                    return 0
                if late_failure == "raises":
                    raise RuntimeError("late provider backlog accessor failed")
                return True

            def drain(self, **_kwargs):
                raise AssertionError(
                    "provider-unavailable tick must not drain invalidations"
                )

        buffer = Buffer()
        coordinator.collector = ProviderUnavailableCollector()
        coordinator.invalidation_buffer = buffer

        with pytest.raises(expected_error, match=expected_match):
            coordinator.tick()

        assert buffer.pending_reads == 2
        assert (
            coordinator._state.snapshot().last_error_code
            == expected_error.__name__
        )

@pytest.mark.parametrize("mutation_style", ("delete", "hostile_equality"))
def test_tick_economic_recovery_does_not_trust_replacement_values(
    mutation_style: str,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        original_workspace = coordinator.workspace
        original_book = coordinator.paper_book_path
        original_bankroll = coordinator.initial_bankroll
        equality_calls = 0

        class HostileValue:
            def __eq__(self, _other):
                nonlocal equality_calls
                equality_calls += 1
                raise AssertionError("replacement equality executed")

            def __ne__(self, _other):
                nonlocal equality_calls
                equality_calls += 1
                raise AssertionError("replacement inequality executed")

        def mutate_and_raise() -> None:
            if mutation_style == "delete":
                del coordinator.workspace
                del coordinator.paper_book_path
                del coordinator.initial_bankroll
            else:
                coordinator.workspace = HostileValue()
                coordinator.paper_book_path = HostileValue()
                coordinator.initial_bankroll = HostileValue()
            raise RuntimeError("economic recovery trigger")

        coordinator.collector = _Collector(callback=mutate_and_raise)

        with pytest.raises(RuntimeError, match="economic recovery trigger"):
            coordinator.tick()

        assert equality_calls == 0
        assert coordinator.workspace == original_workspace
        assert coordinator.paper_book_path == original_book
        assert coordinator.initial_bankroll == original_bankroll

@pytest.mark.parametrize(
    "authority_name",
    ("_state", "dependency_index", "invalidation_buffer"),
)
def test_tick_failure_restores_deleted_coordinator_authority(
    authority_name: str,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        canonical = getattr(coordinator, authority_name)

        def delete_and_raise() -> None:
            delattr(coordinator, authority_name)
            raise RuntimeError("deleted coordinator authority")

        coordinator.collector = _Collector(callback=delete_and_raise)

        with pytest.raises(RuntimeError, match="deleted coordinator authority"):
            coordinator.tick()

        assert getattr(coordinator, authority_name) is canonical
        assert coordinator._state.snapshot().last_error_code == "RuntimeError"


@pytest.mark.parametrize(
    "field_name",
    ("_mirror", "_dirty", "_lock", "_max_dirty_keys"),
)
def test_tick_failure_restores_deleted_canonical_invalidation_structure(
    field_name: str,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        mirror = MarketMirror()
        buffer = continuous_session.BoundedMirrorInvalidationBuffer(mirror)
        coordinator.invalidation_buffer = buffer
        coordinator.dependency_index = (
            continuous_session.FocusedMirrorDependencyIndex(mirror)
        )
        canonical_value = getattr(buffer, field_name)

        def delete_and_raise() -> None:
            delattr(buffer, field_name)
            raise RuntimeError("deleted invalidation structure")

        coordinator.collector = _Collector(callback=delete_and_raise)

        with pytest.raises(RuntimeError, match="deleted invalidation structure"):
            coordinator.tick()

        assert getattr(buffer, field_name) is canonical_value
        assert coordinator._state.snapshot().last_error_code == "RuntimeError"


def test_tick_invalidation_structure_recovery_does_not_trust_foreign_limit_equality() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator = _base_coordinator(root)
        mirror = MarketMirror()
        buffer = continuous_session.BoundedMirrorInvalidationBuffer(mirror)
        coordinator.invalidation_buffer = buffer
        coordinator.dependency_index = (
            continuous_session.FocusedMirrorDependencyIndex(mirror)
        )
        canonical_limit = buffer._max_dirty_keys
        equality_calls = 0

        class HostileLimit:
            def __eq__(self, _other):
                nonlocal equality_calls
                equality_calls += 1
                raise AssertionError("foreign invalidation limit equality executed")

            def __ne__(self, _other):
                nonlocal equality_calls
                equality_calls += 1
                raise AssertionError("foreign invalidation limit inequality executed")

        def mutate_and_raise() -> None:
            buffer._max_dirty_keys = HostileLimit()
            raise RuntimeError("foreign invalidation limit")

        coordinator.collector = _Collector(callback=mutate_and_raise)

        with pytest.raises(RuntimeError, match="foreign invalidation limit"):
            coordinator.tick()

        assert equality_calls == 0
        assert buffer._max_dirty_keys == canonical_limit
        assert coordinator._state.snapshot().last_error_code == "RuntimeError"

