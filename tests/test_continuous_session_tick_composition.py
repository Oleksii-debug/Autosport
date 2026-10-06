from __future__ import annotations

import tempfile
from datetime import timedelta
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


def test_tick_rejects_derived_index_drain_selector_mutation() -> None:
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

