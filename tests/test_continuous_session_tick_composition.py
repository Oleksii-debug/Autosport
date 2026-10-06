from __future__ import annotations

import tempfile
from pathlib import Path

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
