from __future__ import annotations

import tempfile
from datetime import timedelta
from pathlib import Path

import pytest

from autosport import continuous_session
from autosport.paper import PaperBook


_AT = "2026-10-06T00:00:00+00:00"


class _DeltaStore:
    def deltas_after_commit(self, **_kwargs):
        return ()


class _Collector:
    source_id = "provider-a"
    config = type("ConfigStub", (), {"max_items": 1})()

    def __init__(self) -> None:
        self.delta_store = _DeltaStore()
        self.calls = 0

    def run_cycle(self):
        self.calls += 1
        return type(
            "SuccessfulCycle",
            (),
            {
                "provider_unavailable": False,
                "source_id": "provider-a",
                "committed_delta_ids": (),
            },
        )()


class _Desktop:
    def drain(self, **_kwargs):
        return ()


class _Lifecycle:
    def register_eligible(self, *_args, **_kwargs):
        return ()


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


def _coordinator(root: Path) -> continuous_session.ContinuousSessionCoordinator:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    coordinator._state = continuous_session._ContinuousSessionState(
        root / "continuous_session.json",
        session_id="session-convergence",
        source_id="provider-a",
        clock=lambda: _AT,
    )
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
    coordinator.collector = _Collector()
    coordinator.desktop_consumer = _Desktop()
    coordinator.lifecycle = _Lifecycle()
    return coordinator


def test_terminal_invalidation_batch_observes_new_pending_backlog() -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)

    class Buffer:
        full_refresh_required = False

        def __init__(self) -> None:
            self.pending_count = 0

        def drain(self, *, max_items: int):
            assert max_items == 1
            self.pending_count = 1
            return continuous_session.MirrorInvalidationBatch(
                changed_keys=(),
                full_refresh_required=False,
                has_more=False,
            )

    class Index:
        def affected_inputs(self, _batch):
            return ()

    buffer = Buffer()
    affected, full_refresh, backlog = coordinator._drain_invalidations(
        invalidation_buffer=buffer,
        dependency_index=Index(),
        drain_invalidation=buffer.drain,
        affected_inputs=Index().affected_inputs,
        max_batches=1,
        max_items=1,
    )

    assert affected == ()
    assert full_refresh is False
    assert backlog is True


def test_terminal_invalidation_batch_observes_pending_full_refresh() -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)

    class Buffer:
        pending_count = 0

        def __init__(self) -> None:
            self.full_refresh_required = False

        def drain(self, *, max_items: int):
            assert max_items == 1
            self.full_refresh_required = True
            return continuous_session.MirrorInvalidationBatch(
                changed_keys=(),
                full_refresh_required=False,
                has_more=False,
            )

    class Index:
        def affected_inputs(self, _batch):
            return ()

    buffer = Buffer()
    _affected, full_refresh, backlog = coordinator._drain_invalidations(
        invalidation_buffer=buffer,
        dependency_index=Index(),
        drain_invalidation=buffer.drain,
        affected_inputs=Index().affected_inputs,
        max_batches=1,
        max_items=1,
    )

    assert full_refresh is False
    assert backlog is True


def test_last_has_more_remains_conservative_backlog_evidence() -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)

    class Buffer:
        pending_count = 0
        full_refresh_required = False

        def drain(self, *, max_items: int):
            assert max_items == 1
            return continuous_session.MirrorInvalidationBatch(
                changed_keys=(),
                full_refresh_required=False,
                has_more=True,
            )

    class Index:
        def affected_inputs(self, _batch):
            return ()

    buffer = Buffer()
    _affected, _full_refresh, backlog = coordinator._drain_invalidations(
        invalidation_buffer=buffer,
        dependency_index=Index(),
        drain_invalidation=buffer.drain,
        affected_inputs=Index().affected_inputs,
        max_batches=1,
        max_items=1,
    )

    assert backlog is True


@pytest.mark.parametrize(
    ("pending_count", "pending_full_refresh"),
    ((True, False), (-1, False), (0, 1)),
)
def test_terminal_batch_rejects_noncanonical_backlog_state(
    pending_count: object,
    pending_full_refresh: object,
) -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)

    class Buffer:
        full_refresh_required = pending_full_refresh

        @property
        def pending_count(self):
            return pending_count

        def drain(self, *, max_items: int):
            assert max_items == 1
            return continuous_session.MirrorInvalidationBatch(
                changed_keys=(),
                full_refresh_required=False,
                has_more=False,
            )

    class Index:
        def affected_inputs(self, _batch):
            return ()

    buffer = Buffer()
    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="invalidation buffer backlog state is invalid",
    ):
        coordinator._drain_invalidations(
            invalidation_buffer=buffer,
            dependency_index=Index(),
            drain_invalidation=buffer.drain,
            affected_inputs=Index().affected_inputs,
            max_batches=1,
            max_items=1,
        )


def test_invalidation_batch_rejects_duplicate_changed_keys() -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)

    class Buffer:
        pending_count = 0
        full_refresh_required = False

        def drain(self, *, max_items: int):
            assert max_items == 1
            return continuous_session.MirrorInvalidationBatch(
                changed_keys=(
                    ("provider-a", "quote-1"),
                    ("provider-a", "quote-1"),
                ),
                full_refresh_required=False,
                has_more=False,
            )

    class Index:
        def affected_inputs(self, _batch):
            return ()

    buffer = Buffer()
    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="invalidation buffer returned an invalid batch",
    ):
        coordinator._drain_invalidations(
            invalidation_buffer=buffer,
            dependency_index=Index(),
            drain_invalidation=buffer.drain,
            affected_inputs=Index().affected_inputs,
            max_batches=1,
            max_items=1,
        )


def test_missing_collector_run_cycle_fails_before_provider_io() -> None:
    with tempfile.TemporaryDirectory() as directory:
        coordinator = _coordinator(Path(directory))

        class Collector:
            source_id = "provider-a"
            config = type("ConfigStub", (), {"max_items": 1})()
            delta_store = _DeltaStore()

        coordinator.collector = Collector()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="collector cycle authority is unavailable",
        ):
            coordinator.tick()


def test_missing_projection_store_fails_before_provider_io() -> None:
    with tempfile.TemporaryDirectory() as directory:
        coordinator = _coordinator(Path(directory))

        class Collector:
            source_id = "provider-a"
            config = type("ConfigStub", (), {"max_items": 1})()

            def run_cycle(self):
                raise AssertionError("provider I/O ran before projection preflight")

        coordinator.collector = Collector()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="collector projection authority is unavailable",
        ):
            coordinator.tick()


def test_missing_collector_config_fails_before_provider_io() -> None:
    with tempfile.TemporaryDirectory() as directory:
        coordinator = _coordinator(Path(directory))

        class Collector:
            source_id = "provider-a"
            delta_store = _DeltaStore()

            def run_cycle(self):
                raise AssertionError("provider I/O ran before projection preflight")

        coordinator.collector = Collector()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="collector projection configuration is invalid",
        ):
            coordinator.tick()


def test_missing_desktop_drain_fails_before_provider_io() -> None:
    with tempfile.TemporaryDirectory() as directory:
        coordinator = _coordinator(Path(directory))
        collector = coordinator.collector

        class Desktop:
            pass

        coordinator.desktop_consumer = Desktop()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="desktop delivery authority is unavailable",
        ):
            coordinator.tick()

        assert collector.calls == 0


def test_missing_invalidation_drain_fails_before_provider_io() -> None:
    with tempfile.TemporaryDirectory() as directory:
        coordinator = _coordinator(Path(directory))
        collector = coordinator.collector

        class Buffer:
            pending_count = 0
            full_refresh_required = False

        coordinator.invalidation_buffer = Buffer()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="continuous-session invalidation routing authority is unavailable",
        ):
            coordinator.tick()

        assert collector.calls == 0


def test_missing_affected_inputs_fails_before_provider_io() -> None:
    with tempfile.TemporaryDirectory() as directory:
        coordinator = _coordinator(Path(directory))
        collector = coordinator.collector

        class Index:
            input_ids = ()

        coordinator.dependency_index = Index()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="continuous-session invalidation routing authority is unavailable",
        ):
            coordinator.tick()

        assert collector.calls == 0


def test_missing_lifecycle_registration_fails_before_provider_io() -> None:
    with tempfile.TemporaryDirectory() as directory:
        coordinator = _coordinator(Path(directory))
        collector = coordinator.collector

        class Lifecycle:
            pass

        coordinator.lifecycle = Lifecycle()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="lifecycle registration authority is unavailable",
        ):
            coordinator.tick()

        assert collector.calls == 0


@pytest.mark.parametrize(
    "input_ids",
    (["input-1"], ("input-1", "input-1"), (" input-1",)),
)
def test_register_rejects_noncanonical_dependency_identity_state(
    input_ids: object,
) -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)

    class Index:
        pass

    index = Index()
    index.input_ids = input_ids

    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="dependency index input identity state is invalid",
    ):
        coordinator._register_input(
            "input-new",
            dependency_index=index,
            register_input=lambda *_args, **_kwargs: None,
        )


def test_register_rejects_noncanonical_postpublication_identity_state() -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)

    class Index:
        def __init__(self) -> None:
            self.invalid = False

        @property
        def input_ids(self):
            return ["input-new"] if self.invalid else ()

        def register(self, *_args, **_kwargs):
            self.invalid = True

    index = Index()
    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="dependency index input identity state is invalid",
    ):
        coordinator._register_input(
            "input-new",
            dependency_index=index,
            register_input=index.register,
        )


@pytest.mark.parametrize(
    "input_ids",
    (["input-1"], ("input-1", "input-1"), (" input-1",)),
)
def test_retire_rejects_noncanonical_dependency_identity_state(
    input_ids: object,
) -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)

    class Index:
        pass

    index = Index()
    index.input_ids = input_ids

    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="dependency index input identity state is invalid",
    ):
        coordinator._retire_input(
            "input-1",
            dependency_index=index,
            unregister_input=lambda _input_id: True,
        )


def test_retire_rejects_noncanonical_postpublication_identity_state() -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)

    class Index:
        def __init__(self) -> None:
            self.invalid = False

        @property
        def input_ids(self):
            return ["input-1"] if self.invalid else ("input-1",)

        def unregister(self, _input_id: str):
            self.invalid = True
            return True

    index = Index()
    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="dependency index input identity state is invalid",
    ):
        coordinator._retire_input(
            "input-1",
            dependency_index=index,
            unregister_input=index.unregister,
        )


def test_tick_rejects_noncanonical_index_identity_snapshot() -> None:
    with tempfile.TemporaryDirectory() as directory:
        coordinator = _coordinator(Path(directory))

        class Index:
            input_ids = []

            def affected_inputs(self, _batch):
                return ()

        coordinator.dependency_index = Index()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="dependency index input identity state is invalid",
        ):
            coordinator.tick()

        assert (
            coordinator._state.snapshot().last_error_code
            == "ContinuousSessionError"
        )


def test_settlement_uses_explicit_frozen_economic_context() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        original_workspace = root / "original"
        attacker_workspace = root / "attacker"
        original_workspace.mkdir()
        attacker_workspace.mkdir()

        original_book = original_workspace / "paper_book.json"
        attacker_book = attacker_workspace / "paper_book.json"
        PaperBook("100").save(original_book)
        attacker_book.write_text("{not-json", encoding="utf-8")

        coordinator = object.__new__(
            continuous_session.ContinuousSessionCoordinator
        )
        coordinator.workspace = attacker_workspace
        coordinator.paper_book_path = attacker_book
        coordinator.initial_bankroll = "not-a-bankroll"

        resolution = continuous_session.SettlementResolution(
            event_identity="provider-a:event-1",
            settlement_ref="settlement-1",
            quote_outcomes={"quote-1": "win"},
            evidence_id="evidence-frozen-context",
            evidence_sha256="f" * 64,
            available_at=_AT,
        )

        settled, evidence_ids = coordinator._settle(
            resolutions=(resolution,),
            workspace=original_workspace,
            paper_book_path=original_book,
            initial_bankroll="100",
        )

        assert settled == ()
        assert evidence_ids == ("evidence-frozen-context",)


def test_tick_reports_backlog_arriving_after_terminal_drain() -> None:
    with tempfile.TemporaryDirectory() as directory:
        coordinator = _coordinator(Path(directory))

        class Buffer:
            full_refresh_required = False

            def __init__(self) -> None:
                self.pending_count = 0

            def drain(self, *, max_items: int):
                assert max_items == 250
                self.pending_count = 1
                return continuous_session.MirrorInvalidationBatch(
                    changed_keys=(),
                    full_refresh_required=False,
                    has_more=False,
                )

        coordinator.invalidation_buffer = Buffer()

        result = coordinator.tick()

        assert result.invalidation_backlog is True
