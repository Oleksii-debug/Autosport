from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import autosport.continuous_session as session
from autosport.continuous_session import (
    ContinuousSessionCoordinator,
    ContinuousSessionError,
    EventPhase,
    SettlementResolution,
)
from autosport.domain import TicketLeg
from autosport.paper import PaperBook


_AT = "2026-09-22T07:02:00+00:00"


class _State:
    session_id = "session-paperbook-closure"

    def __init__(self) -> None:
        self.failures: list[str] = []

    def snapshot(self):
        return SimpleNamespace(cycles_completed=0, last_success_at=None)

    def validate_settlement_evidence(self, *, settlement_evidence) -> None:
        tuple(settlement_evidence)

    def record_success(self, **_kwargs) -> None:
        return None

    def record_failure(self, *, code: str) -> None:
        self.failures.append(code)


class _Lifecycle:
    def __init__(self, resolution: SettlementResolution) -> None:
        self._resolution = resolution

    def records(self):
        return (
            SimpleNamespace(
                phase=EventPhase.COMPLETED,
                settlement_ref=self._resolution.settlement_ref,
                identity=self._resolution.event_identity,
            ),
        )

    def register_eligible(self, *_args, **_kwargs):
        return ()


class _Collector:
    source_id = "provider-a"

    def run_cycle(self):
        return SimpleNamespace(
            provider_unavailable=False,
            source_id=self.source_id,
            committed_delta_ids=(),
        )


class _DesktopConsumer:
    def drain(self, **_kwargs):
        return ()


class _LearningHandoff:
    def __init__(self) -> None:
        self.prepare_calls = 0
        self.reconcile_calls = 0

    def prepare_settlement(self, **_kwargs) -> None:
        self.prepare_calls += 1

    def reconcile_after_settlement(self, **_kwargs) -> None:
        self.reconcile_calls += 1


def _reachable_closure_cell(function, name: str):
    function_type = type(function)
    pending = [function]
    seen: set[int] = set()
    while pending:
        target = pending.pop()
        identity = id(target)
        if identity in seen:
            continue
        seen.add(identity)
        closure = target.__closure__ or ()
        for freevar, cell in zip(target.__code__.co_freevars, closure):
            if freevar == name:
                return cell
            try:
                value = cell.cell_contents
            except ValueError:
                continue
            if type(value) is function_type:
                pending.append(value)
    raise AssertionError(f"reachable closure graph has no {name!r} cell")


def _hidden_load_book_target():
    coordinator_type = ContinuousSessionCoordinator
    coordinator_dict = type.__getattribute__(coordinator_type, "__dict__")
    descriptor = coordinator_dict["_load_book"]
    descriptor_get = type(descriptor).__dict__["__get__"]
    return descriptor_get(descriptor, None, coordinator_type)


def _paperbook_save_authority_globals() -> dict[str, object]:
    save_target = vars(PaperBook)["save"]
    closure = save_target.__closure__
    if closure is None:
        raise AssertionError("guarded PaperBook.save closure is unavailable")
    for freevar, cell in zip(save_target.__code__.co_freevars, closure):
        if freevar != "trusted_globals":
            continue
        mapping = cell.cell_contents
        if type(mapping) is dict and "_FROZEN_SAVE" in mapping:
            return mapping
    raise AssertionError("guarded PaperBook.save trusted globals are unavailable")


def test_tick_rejects_paperbook_load_closure_retarget_before_learning_prepare(
    tmp_path: Path,
) -> None:
    canonical_paper_book = session.PaperBook
    book = PaperBook("100")
    leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
    book.open_ticket(
        [leg],
        "10",
        placed_at="2026-09-22T07:00:00+00:00",
        provider_source_ids=("provider-a",),
    )
    paper_book_path = tmp_path / "paper_book.json"
    book.save(paper_book_path)

    resolution = SettlementResolution(
        event_identity="provider-a:event-1",
        settlement_ref="provider-result:1",
        quote_outcomes={leg.quote_key: "win"},
        evidence_id="provider-a-outcome-1",
        evidence_sha256="0" * 64,
        available_at="2026-09-22T07:01:00+00:00",
    )

    hidden_load_book = _hidden_load_book_target()
    canonical_class_cell = _reachable_closure_cell(
        hidden_load_book,
        "canonical_paper_book",
    )
    assert canonical_class_cell.cell_contents is canonical_paper_book
    hostile_load_calls: list[Path] = []

    class HostilePaperBook:
        @classmethod
        def load(cls, path):
            hostile_load_calls.append(Path(path))
            session.PaperBook = canonical_paper_book
            canonical_class_cell.cell_contents = canonical_paper_book
            return canonical_paper_book("100")

    class OutcomeAuthority:
        def resolve(self, _record, *, as_of: str):
            del as_of
            canonical_class_cell.cell_contents = HostilePaperBook
            session.PaperBook = HostilePaperBook
            return resolution

    handoff = _LearningHandoff()
    coordinator = ContinuousSessionCoordinator.__new__(ContinuousSessionCoordinator)
    coordinator._state = _State()
    coordinator._require_running = lambda: None
    coordinator.clock = lambda: _AT
    coordinator.collector = _Collector()
    coordinator._refresh_source_state_projection = lambda: SimpleNamespace(
        source_gap_state=None,
        source_sync_state=None,
    )
    coordinator.desktop_consumer = _DesktopConsumer()
    coordinator.causal_view = object()
    coordinator._drain_invalidations = lambda: ((), False, False)
    coordinator.dependency_index = SimpleNamespace(input_ids=set())
    coordinator.lifecycle = _Lifecycle(resolution)
    coordinator.market_store = object()
    coordinator.required_history = timedelta(0)
    coordinator.outcome_authority = OutcomeAuthority()
    coordinator.settlement_learning_handoff = handoff
    coordinator.workspace = tmp_path
    coordinator.paper_book_path = paper_book_path
    coordinator.initial_bankroll = "100"

    try:
        with pytest.raises(
            ContinuousSessionError,
            match="pre-learning settlement authority changed|PaperBook load closure authority changed|settlement coordinator dispatch changed",
        ):
            coordinator.tick()
        assert hostile_load_calls == []
        assert handoff.prepare_calls == 0
        assert handoff.reconcile_calls == 0
    finally:
        session.PaperBook = canonical_paper_book
        canonical_class_cell.cell_contents = canonical_paper_book

def test_tick_rejects_paperbook_save_closure_retarget_before_learning_prepare(
    tmp_path: Path,
) -> None:
    book = PaperBook("100")
    leg = TicketLeg("event-save-1", "winner", "alice", Decimal("2"))
    book.open_ticket(
        [leg],
        "10",
        placed_at="2026-09-22T07:00:00+00:00",
        provider_source_ids=("provider-a",),
    )
    paper_book_path = tmp_path / "paper_book.json"
    book.save(paper_book_path)

    resolution = SettlementResolution(
        event_identity="provider-a:event-save-1",
        settlement_ref="provider-result:save-1",
        quote_outcomes={leg.quote_key: "win"},
        evidence_id="provider-a-outcome-save-1",
        evidence_sha256="1" * 64,
        available_at="2026-09-22T07:01:00+00:00",
    )

    save_globals = _paperbook_save_authority_globals()
    canonical_frozen_save = save_globals["_FROZEN_SAVE"]
    hostile_save_calls: list[Path] = []

    def hostile_frozen_save(_book, path) -> None:
        hostile_save_calls.append(Path(path))
        raise AssertionError("hostile frozen PaperBook save executed")

    class OutcomeAuthority:
        def resolve(self, _record, *, as_of: str):
            del as_of
            save_globals["_FROZEN_SAVE"] = hostile_frozen_save
            return resolution

    handoff = _LearningHandoff()
    coordinator = ContinuousSessionCoordinator.__new__(ContinuousSessionCoordinator)
    coordinator._state = _State()
    coordinator._require_running = lambda: None
    coordinator.clock = lambda: _AT
    coordinator.collector = _Collector()
    coordinator._refresh_source_state_projection = lambda: SimpleNamespace(
        source_gap_state=None,
        source_sync_state=None,
    )
    coordinator.desktop_consumer = _DesktopConsumer()
    coordinator.causal_view = object()
    coordinator._drain_invalidations = lambda: ((), False, False)
    coordinator.dependency_index = SimpleNamespace(input_ids=set())
    coordinator.lifecycle = _Lifecycle(resolution)
    coordinator.market_store = object()
    coordinator.required_history = timedelta(0)
    coordinator.outcome_authority = OutcomeAuthority()
    coordinator.settlement_learning_handoff = handoff
    coordinator.workspace = tmp_path
    coordinator.paper_book_path = paper_book_path
    coordinator.initial_bankroll = "100"

    try:
        with pytest.raises(
            ContinuousSessionError,
            match="PaperBook|paper book|save|pre-learning settlement authority changed|settlement coordinator dispatch changed",
        ):
            coordinator.tick()
        assert hostile_save_calls == []
        assert handoff.prepare_calls == 0
        assert handoff.reconcile_calls == 0
    finally:
        save_globals["_FROZEN_SAVE"] = canonical_frozen_save



def test_tick_rejects_paperbook_save_verifier_retarget_before_learning_prepare(
    tmp_path: Path,
) -> None:
    book = PaperBook("100")
    leg = TicketLeg("event-save-verifier-1", "winner", "alice", Decimal("2"))
    book.open_ticket(
        [leg],
        "10",
        placed_at="2026-09-22T07:00:00+00:00",
        provider_source_ids=("provider-a",),
    )
    paper_book_path = tmp_path / "paper_book.json"
    book.save(paper_book_path)

    resolution = SettlementResolution(
        event_identity="provider-a:event-save-verifier-1",
        settlement_ref="provider-result:save-verifier-1",
        quote_outcomes={leg.quote_key: "win"},
        evidence_id="provider-a-outcome-save-verifier-1",
        evidence_sha256="2" * 64,
        available_at="2026-09-22T07:01:00+00:00",
    )

    save_globals = _paperbook_save_authority_globals()
    verifier_name = "_require_delegate_graph_witnesses"
    canonical_verifier = save_globals[verifier_name]
    hostile_verifier_calls: list[object] = []

    def hostile_verifier(witnesses) -> None:
        hostile_verifier_calls.append(witnesses)

    class OutcomeAuthority:
        def resolve(self, _record, *, as_of: str):
            del as_of
            save_globals[verifier_name] = hostile_verifier
            return resolution

    handoff = _LearningHandoff()
    coordinator = ContinuousSessionCoordinator.__new__(ContinuousSessionCoordinator)
    coordinator._state = _State()
    coordinator._require_running = lambda: None
    coordinator.clock = lambda: _AT
    coordinator.collector = _Collector()
    coordinator._refresh_source_state_projection = lambda: SimpleNamespace(
        source_gap_state=None,
        source_sync_state=None,
    )
    coordinator.desktop_consumer = _DesktopConsumer()
    coordinator.causal_view = object()
    coordinator._drain_invalidations = lambda: ((), False, False)
    coordinator.dependency_index = SimpleNamespace(input_ids=set())
    coordinator.lifecycle = _Lifecycle(resolution)
    coordinator.market_store = object()
    coordinator.required_history = timedelta(0)
    coordinator.outcome_authority = OutcomeAuthority()
    coordinator.settlement_learning_handoff = handoff
    coordinator.workspace = tmp_path
    coordinator.paper_book_path = paper_book_path
    coordinator.initial_bankroll = "100"

    try:
        with pytest.raises(
            ContinuousSessionError,
            match="PaperBook save wrapper authority changed|PaperBook save verifier graph changed|pre-learning settlement authority changed|settlement coordinator dispatch changed",
        ):
            coordinator.tick()
        assert handoff.prepare_calls == 0
        assert handoff.reconcile_calls == 0
    finally:
        save_globals[verifier_name] = canonical_verifier



def test_tick_rejects_paperbook_save_verifier_code_retarget_before_learning_prepare(
    tmp_path: Path,
) -> None:
    book = PaperBook("100")
    leg = TicketLeg("event-save-code-1", "winner", "alice", Decimal("2"))
    book.open_ticket(
        [leg],
        "10",
        placed_at="2026-09-22T07:00:00+00:00",
        provider_source_ids=("provider-a",),
    )
    paper_book_path = tmp_path / "paper_book.json"
    book.save(paper_book_path)

    resolution = SettlementResolution(
        event_identity="provider-a:event-save-code-1",
        settlement_ref="provider-result:save-code-1",
        quote_outcomes={leg.quote_key: "win"},
        evidence_id="provider-a-outcome-save-code-1",
        evidence_sha256="3" * 64,
        available_at="2026-09-22T07:01:00+00:00",
    )

    save_globals = _paperbook_save_authority_globals()
    canonical_verifier = save_globals["_require_delegate_graph_witnesses"]
    canonical_code = canonical_verifier.__code__

    def hostile_verifier(_witnesses) -> None:
        raise AssertionError("mutated PaperBook save verifier reached")

    assert hostile_verifier.__code__.co_freevars == canonical_code.co_freevars

    class OutcomeAuthority:
        def resolve(self, _record, *, as_of: str):
            del as_of
            canonical_verifier.__code__ = hostile_verifier.__code__
            return resolution

    handoff = _LearningHandoff()
    coordinator = ContinuousSessionCoordinator.__new__(ContinuousSessionCoordinator)
    coordinator._state = _State()
    coordinator._require_running = lambda: None
    coordinator.clock = lambda: _AT
    coordinator.collector = _Collector()
    coordinator._refresh_source_state_projection = lambda: SimpleNamespace(
        source_gap_state=None,
        source_sync_state=None,
    )
    coordinator.desktop_consumer = _DesktopConsumer()
    coordinator.causal_view = object()
    coordinator._drain_invalidations = lambda: ((), False, False)
    coordinator.dependency_index = SimpleNamespace(input_ids=set())
    coordinator.lifecycle = _Lifecycle(resolution)
    coordinator.market_store = object()
    coordinator.required_history = timedelta(0)
    coordinator.outcome_authority = OutcomeAuthority()
    coordinator.settlement_learning_handoff = handoff
    coordinator.workspace = tmp_path
    coordinator.paper_book_path = paper_book_path
    coordinator.initial_bankroll = "100"

    try:
        with pytest.raises(
            ContinuousSessionError,
            match="PaperBook save verifier executable changed|pre-learning settlement authority changed|settlement coordinator dispatch changed",
        ):
            coordinator.tick()
        assert handoff.prepare_calls == 0
        assert handoff.reconcile_calls == 0
    finally:
        canonical_verifier.__code__ = canonical_code



def test_tick_rejects_paperbook_save_verifier_private_global_retarget_before_learning_prepare(
    tmp_path: Path,
) -> None:
    book = PaperBook("100")
    leg = TicketLeg("event-save-verifier-global-1", "winner", "alice", Decimal("2"))
    book.open_ticket(
        [leg],
        "10",
        placed_at="2026-09-22T07:00:00+00:00",
        provider_source_ids=("provider-a",),
    )
    paper_book_path = tmp_path / "paper_book.json"
    book.save(paper_book_path)

    resolution = SettlementResolution(
        event_identity="provider-a:event-save-verifier-global-1",
        settlement_ref="provider-result:save-verifier-global-1",
        quote_outcomes={leg.quote_key: "win"},
        evidence_id="provider-a-outcome-save-verifier-global-1",
        evidence_sha256="9" * 64,
        available_at="2026-09-22T07:01:00+00:00",
    )

    save_globals = _paperbook_save_authority_globals()
    canonical_verifier = save_globals["_require_delegate_graph_witnesses"]
    verifier_globals = canonical_verifier.__globals__
    helper_name = "FunctionType"
    canonical_helper = verifier_globals[helper_name]
    hostile_helper = object()

    class OutcomeAuthority:
        def resolve(self, _record, *, as_of: str):
            del as_of
            verifier_globals[helper_name] = hostile_helper
            return resolution

    handoff = _LearningHandoff()
    coordinator = ContinuousSessionCoordinator.__new__(ContinuousSessionCoordinator)
    coordinator._state = _State()
    coordinator._require_running = lambda: None
    coordinator.clock = lambda: _AT
    coordinator.collector = _Collector()
    coordinator._refresh_source_state_projection = lambda: SimpleNamespace(
        source_gap_state=None,
        source_sync_state=None,
    )
    coordinator.desktop_consumer = _DesktopConsumer()
    coordinator.causal_view = object()
    coordinator._drain_invalidations = lambda: ((), False, False)
    coordinator.dependency_index = SimpleNamespace(input_ids=set())
    coordinator.lifecycle = _Lifecycle(resolution)
    coordinator.market_store = object()
    coordinator.required_history = timedelta(0)
    coordinator.outcome_authority = OutcomeAuthority()
    coordinator.settlement_learning_handoff = handoff
    coordinator.workspace = tmp_path
    coordinator.paper_book_path = paper_book_path
    coordinator.initial_bankroll = "100"

    try:
        with pytest.raises(
            ContinuousSessionError,
            match=(
                "PaperBook save verifier dependency global changed"
                "|pre-learning settlement authority changed"
                "|settlement coordinator dispatch changed"
            ),
        ):
            coordinator.tick()
        assert verifier_globals[helper_name] is hostile_helper
        assert handoff.prepare_calls == 0
        assert handoff.reconcile_calls == 0
    finally:
        verifier_globals[helper_name] = canonical_helper


def test_tick_rejects_paperbook_save_verifier_transient_global_shadow_before_learning_prepare(
    tmp_path: Path,
) -> None:
    book = PaperBook("100")
    leg = TicketLeg("event-save-verifier-shadow-1", "winner", "alice", Decimal("2"))
    book.open_ticket(
        [leg],
        "10",
        placed_at="2026-09-22T07:00:00+00:00",
        provider_source_ids=("provider-a",),
    )
    paper_book_path = tmp_path / "paper_book.json"
    book.save(paper_book_path)

    resolution = SettlementResolution(
        event_identity="provider-a:event-save-verifier-shadow-1",
        settlement_ref="provider-result:save-verifier-shadow-1",
        quote_outcomes={leg.quote_key: "win"},
        evidence_id="provider-a-outcome-save-verifier-shadow-1",
        evidence_sha256="a" * 64,
        available_at="2026-09-22T07:01:00+00:00",
    )

    save_globals = _paperbook_save_authority_globals()
    canonical_delegate_verifier = save_globals[
        "_require_delegate_graph_witnesses"
    ]
    verifier_globals = canonical_delegate_verifier.__globals__
    assert "type" not in verifier_globals
    hostile_type_calls: list[object] = []

    def hostile_type(value):
        hostile_type_calls.append(value)
        # Self-remove so every later #1789 wrapper sees the canonical no-shadow
        # mapping if Wave M were to execute this transient replacement.
        verifier_globals.pop("type", None)
        return type(value)

    class OutcomeAuthority:
        def resolve(self, _record, *, as_of: str):
            del as_of
            verifier_globals["type"] = hostile_type
            return resolution

    handoff = _LearningHandoff()
    coordinator = ContinuousSessionCoordinator.__new__(ContinuousSessionCoordinator)
    coordinator._state = _State()
    coordinator._require_running = lambda: None
    coordinator.clock = lambda: _AT
    coordinator.collector = _Collector()
    coordinator._refresh_source_state_projection = lambda: SimpleNamespace(
        source_gap_state=None,
        source_sync_state=None,
    )
    coordinator.desktop_consumer = _DesktopConsumer()
    coordinator.causal_view = object()
    coordinator._drain_invalidations = lambda: ((), False, False)
    coordinator.dependency_index = SimpleNamespace(input_ids=set())
    coordinator.lifecycle = _Lifecycle(resolution)
    coordinator.market_store = object()
    coordinator.required_history = timedelta(0)
    coordinator.outcome_authority = OutcomeAuthority()
    coordinator.settlement_learning_handoff = handoff
    coordinator.workspace = tmp_path
    coordinator.paper_book_path = paper_book_path
    coordinator.initial_bankroll = "100"

    try:
        with pytest.raises(
            ContinuousSessionError,
            match=(
                "PaperBook save verifier dependency global changed"
                "|pre-learning settlement authority changed"
                "|settlement coordinator dispatch changed"
            ),
        ):
            coordinator.tick()
        assert hostile_type_calls == []
        assert handoff.prepare_calls == 0
        assert handoff.reconcile_calls == 0
    finally:
        verifier_globals.pop("type", None)


def test_tick_rejects_outcome_callback_learning_handoff_retarget_before_prepare(
    tmp_path: Path,
) -> None:
    book = PaperBook("100")
    leg = TicketLeg("event-handoff-1", "winner", "alice", Decimal("2"))
    book.open_ticket(
        [leg],
        "10",
        placed_at="2026-09-22T07:00:00+00:00",
        provider_source_ids=("provider-a",),
    )
    paper_book_path = tmp_path / "paper_book.json"
    book.save(paper_book_path)

    resolution = SettlementResolution(
        event_identity="provider-a:event-handoff-1",
        settlement_ref="provider-result:handoff-1",
        quote_outcomes={leg.quote_key: "win"},
        evidence_id="provider-a-outcome-handoff-1",
        evidence_sha256="4" * 64,
        available_at="2026-09-22T07:01:00+00:00",
    )

    canonical_handoff = _LearningHandoff()
    hostile_prepare_calls: list[tuple[object, ...]] = []
    hostile_reconcile_calls: list[tuple[object, ...]] = []

    class HostileHandoff:
        def prepare_settlement(self, **kwargs):
            hostile_prepare_calls.append(tuple(sorted(kwargs)))
            return ()

        def reconcile_after_settlement(self, **kwargs):
            hostile_reconcile_calls.append(tuple(sorted(kwargs)))
            return ()

    hostile_handoff = HostileHandoff()

    class OutcomeAuthority:
        def resolve(self, _record, *, as_of: str):
            del as_of
            coordinator.settlement_learning_handoff = hostile_handoff
            return resolution

    coordinator = ContinuousSessionCoordinator.__new__(ContinuousSessionCoordinator)
    coordinator._state = _State()
    coordinator._require_running = lambda: None
    coordinator.clock = lambda: _AT
    coordinator.collector = _Collector()
    coordinator._refresh_source_state_projection = lambda: SimpleNamespace(
        source_gap_state=None,
        source_sync_state=None,
    )
    coordinator.desktop_consumer = _DesktopConsumer()
    coordinator.causal_view = object()
    coordinator._drain_invalidations = lambda: ((), False, False)
    coordinator.dependency_index = SimpleNamespace(input_ids=set())
    coordinator.lifecycle = _Lifecycle(resolution)
    coordinator.market_store = object()
    coordinator.required_history = timedelta(0)
    coordinator.outcome_authority = OutcomeAuthority()
    coordinator.settlement_learning_handoff = canonical_handoff
    coordinator.workspace = tmp_path
    coordinator.paper_book_path = paper_book_path
    coordinator.initial_bankroll = "100"

    try:
        with pytest.raises(
            ContinuousSessionError,
            match="settlement learning handoff changed during tick",
        ):
            coordinator.tick()
        assert canonical_handoff.prepare_calls == 0
        assert canonical_handoff.reconcile_calls == 0
        assert hostile_prepare_calls == []
        assert hostile_reconcile_calls == []
    finally:
        coordinator.settlement_learning_handoff = canonical_handoff



def test_tick_rejects_outcome_authority_self_retarget_before_learning_prepare(
    tmp_path: Path,
) -> None:
    book = PaperBook("100")
    leg = TicketLeg("event-outcome-authority-1", "winner", "alice", Decimal("2"))
    book.open_ticket(
        [leg],
        "10",
        placed_at="2026-09-22T07:00:00+00:00",
        provider_source_ids=("provider-a",),
    )
    paper_book_path = tmp_path / "paper_book.json"
    book.save(paper_book_path)

    resolution = SettlementResolution(
        event_identity="provider-a:event-outcome-authority-1",
        settlement_ref="provider-result:outcome-authority-1",
        quote_outcomes={leg.quote_key: "win"},
        evidence_id="provider-a-outcome-authority-1",
        evidence_sha256="5" * 64,
        available_at="2026-09-22T07:01:00+00:00",
    )

    hostile_resolve_calls: list[str] = []

    class HostileOutcomeAuthority:
        def resolve(self, _record, *, as_of: str):
            hostile_resolve_calls.append(as_of)
            return resolution

    hostile_authority = HostileOutcomeAuthority()

    class CanonicalOutcomeAuthority:
        def resolve(self, _record, *, as_of: str):
            del as_of
            coordinator.outcome_authority = hostile_authority
            return resolution

    canonical_authority = CanonicalOutcomeAuthority()
    handoff = _LearningHandoff()
    coordinator = ContinuousSessionCoordinator.__new__(ContinuousSessionCoordinator)
    coordinator._state = _State()
    coordinator._require_running = lambda: None
    coordinator.clock = lambda: _AT
    coordinator.collector = _Collector()
    coordinator._refresh_source_state_projection = lambda: SimpleNamespace(
        source_gap_state=None,
        source_sync_state=None,
    )
    coordinator.desktop_consumer = _DesktopConsumer()
    coordinator.causal_view = object()
    coordinator._drain_invalidations = lambda: ((), False, False)
    coordinator.dependency_index = SimpleNamespace(input_ids=set())
    coordinator.lifecycle = _Lifecycle(resolution)
    coordinator.market_store = object()
    coordinator.required_history = timedelta(0)
    coordinator.outcome_authority = canonical_authority
    coordinator.settlement_learning_handoff = handoff
    coordinator.workspace = tmp_path
    coordinator.paper_book_path = paper_book_path
    coordinator.initial_bankroll = "100"

    try:
        with pytest.raises(
            ContinuousSessionError,
            match="settlement outcome authority changed during resolution|settlement outcome authority changed during tick",
        ):
            coordinator.tick()
        assert hostile_resolve_calls == []
        assert handoff.prepare_calls == 0
        assert handoff.reconcile_calls == 0
    finally:
        coordinator.outcome_authority = canonical_authority



def test_provider_unavailable_tick_rejects_collector_learning_handoff_retarget(
    tmp_path: Path,
) -> None:
    canonical_handoff = _LearningHandoff()

    class HostileHandoff:
        def prepare_settlement(self, **_kwargs):
            raise AssertionError("hostile prepare executed")

        def reconcile_after_settlement(self, **_kwargs):
            raise AssertionError("hostile reconcile executed")

    hostile_handoff = HostileHandoff()

    class MutatingUnavailableCollector:
        source_id = "provider-a"

        def run_cycle(self):
            coordinator.settlement_learning_handoff = hostile_handoff
            return SimpleNamespace(
                provider_unavailable=True,
                source_id=self.source_id,
                committed_delta_ids=(),
            )

    coordinator = ContinuousSessionCoordinator.__new__(ContinuousSessionCoordinator)
    coordinator._state = _State()
    coordinator._require_running = lambda: None
    coordinator.clock = lambda: _AT
    coordinator.collector = MutatingUnavailableCollector()
    coordinator._refresh_source_state_projection = lambda: SimpleNamespace(
        source_gap_state=None,
        source_sync_state=None,
    )
    coordinator.invalidation_buffer = SimpleNamespace(
        pending_count=0,
        full_refresh_required=False,
    )
    coordinator.outcome_authority = None
    coordinator.settlement_learning_handoff = canonical_handoff
    coordinator.workspace = tmp_path
    coordinator.paper_book_path = tmp_path / "paper_book.json"
    coordinator.initial_bankroll = "100"

    try:
        with pytest.raises(
            ContinuousSessionError,
            match="settlement learning handoff changed during tick",
        ):
            coordinator.tick()
        assert canonical_handoff.prepare_calls == 0
        assert canonical_handoff.reconcile_calls == 0
    finally:
        coordinator.settlement_learning_handoff = canonical_handoff


def _paperbook_wrapper_closure_cell(*, operation: str, name: str):
    descriptor = vars(PaperBook)[operation]
    target = descriptor.__func__ if operation == "load" else descriptor
    closure = target.__closure__ or ()
    for freevar, cell in zip(target.__code__.co_freevars, closure):
        if freevar == name:
            return cell
    raise AssertionError(
        f"guarded PaperBook.{operation} closure has no {name!r} cell"
    )


def _hostile_save_inner_code(_book, _path) -> None:
    raise AssertionError("hostile final PaperBook.save inner code executed")


def _hostile_load_inner_code(_cls, _path):
    raise AssertionError("hostile final PaperBook.load inner code executed")


def test_tick_rejects_final_save_inner_code_cell_retarget_before_learning(
    tmp_path: Path,
) -> None:
    book = PaperBook("100")
    leg = TicketLeg("event-inner-save", "winner", "alice", Decimal("2"))
    book.open_ticket(
        [leg],
        "10",
        placed_at="2026-09-22T07:00:00+00:00",
        provider_source_ids=("provider-a",),
    )
    paper_book_path = tmp_path / "paper_book_inner_save.json"
    book.save(paper_book_path)

    resolution = SettlementResolution(
        event_identity="provider-a:event-inner-save",
        settlement_ref="provider-result:inner-save",
        quote_outcomes={leg.quote_key: "win"},
        evidence_id="provider-a-outcome-inner-save",
        evidence_sha256="7" * 64,
        available_at="2026-09-22T07:01:00+00:00",
    )
    inner_code_cell = _paperbook_wrapper_closure_cell(
        operation="save",
        name="inner_code",
    )
    canonical_inner_code = inner_code_cell.cell_contents

    class OutcomeAuthority:
        def resolve(self, _record, *, as_of: str):
            del as_of
            inner_code_cell.cell_contents = _hostile_save_inner_code.__code__
            return resolution

    handoff = _LearningHandoff()
    coordinator = ContinuousSessionCoordinator.__new__(ContinuousSessionCoordinator)
    coordinator._state = _State()
    coordinator._require_running = lambda: None
    coordinator.clock = lambda: _AT
    coordinator.collector = _Collector()
    coordinator._refresh_source_state_projection = lambda: SimpleNamespace(
        source_gap_state=None,
        source_sync_state=None,
    )
    coordinator.desktop_consumer = _DesktopConsumer()
    coordinator.causal_view = object()
    coordinator._drain_invalidations = lambda: ((), False, False)
    coordinator.dependency_index = SimpleNamespace(input_ids=set())
    coordinator.lifecycle = _Lifecycle(resolution)
    coordinator.market_store = object()
    coordinator.required_history = timedelta(0)
    coordinator.outcome_authority = OutcomeAuthority()
    coordinator.settlement_learning_handoff = handoff
    coordinator.workspace = tmp_path
    coordinator.paper_book_path = paper_book_path
    coordinator.initial_bankroll = "100"

    try:
        with pytest.raises(
            ContinuousSessionError,
            match=(
                "PaperBook save closure contents changed"
                "|pre-learning settlement authority changed"
                "|settlement coordinator dispatch changed"
            ),
        ):
            coordinator.tick()
        assert handoff.prepare_calls == 0
        assert handoff.reconcile_calls == 0
    finally:
        inner_code_cell.cell_contents = canonical_inner_code


def test_tick_rejects_final_load_inner_code_cell_retarget_before_learning(
    tmp_path: Path,
) -> None:
    book = PaperBook("100")
    leg = TicketLeg("event-inner-load", "winner", "alice", Decimal("2"))
    book.open_ticket(
        [leg],
        "10",
        placed_at="2026-09-22T07:00:00+00:00",
        provider_source_ids=("provider-a",),
    )
    paper_book_path = tmp_path / "paper_book_inner_load.json"
    book.save(paper_book_path)

    resolution = SettlementResolution(
        event_identity="provider-a:event-inner-load",
        settlement_ref="provider-result:inner-load",
        quote_outcomes={leg.quote_key: "win"},
        evidence_id="provider-a-outcome-inner-load",
        evidence_sha256="8" * 64,
        available_at="2026-09-22T07:01:00+00:00",
    )
    inner_code_cell = _paperbook_wrapper_closure_cell(
        operation="load",
        name="inner_code",
    )
    canonical_inner_code = inner_code_cell.cell_contents

    class OutcomeAuthority:
        def resolve(self, _record, *, as_of: str):
            del as_of
            inner_code_cell.cell_contents = _hostile_load_inner_code.__code__
            return resolution

    handoff = _LearningHandoff()
    coordinator = ContinuousSessionCoordinator.__new__(ContinuousSessionCoordinator)
    coordinator._state = _State()
    coordinator._require_running = lambda: None
    coordinator.clock = lambda: _AT
    coordinator.collector = _Collector()
    coordinator._refresh_source_state_projection = lambda: SimpleNamespace(
        source_gap_state=None,
        source_sync_state=None,
    )
    coordinator.desktop_consumer = _DesktopConsumer()
    coordinator.causal_view = object()
    coordinator._drain_invalidations = lambda: ((), False, False)
    coordinator.dependency_index = SimpleNamespace(input_ids=set())
    coordinator.lifecycle = _Lifecycle(resolution)
    coordinator.market_store = object()
    coordinator.required_history = timedelta(0)
    coordinator.outcome_authority = OutcomeAuthority()
    coordinator.settlement_learning_handoff = handoff
    coordinator.workspace = tmp_path
    coordinator.paper_book_path = paper_book_path
    coordinator.initial_bankroll = "100"

    try:
        with pytest.raises(
            ContinuousSessionError,
            match=(
                "PaperBook load closure contents changed"
                "|pre-learning settlement authority changed"
                "|settlement coordinator dispatch changed"
            ),
        ):
            coordinator.tick()
        assert handoff.prepare_calls == 0
        assert handoff.reconcile_calls == 0
    finally:
        inner_code_cell.cell_contents = canonical_inner_code
