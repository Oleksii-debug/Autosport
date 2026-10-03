from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace

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
    session_id = "session-paperbook-verifier-builtins"

    def snapshot(self):
        return SimpleNamespace(cycles_completed=0, last_success_at=None)

    def validate_settlement_evidence(self, *, settlement_evidence) -> None:
        tuple(settlement_evidence)

    def record_success(self, **_kwargs) -> None:
        return None

    def record_failure(self, **_kwargs) -> None:
        return None


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


def _run_child(workspace: Path) -> None:
    book = PaperBook("100")
    leg = TicketLeg("event-verifier-builtins-1", "winner", "alice", Decimal("2"))
    book.open_ticket(
        [leg],
        "10",
        placed_at="2026-09-22T07:00:00+00:00",
        provider_source_ids=("provider-a",),
    )
    paper_book_path = workspace / "paper_book.json"
    book.save(paper_book_path)

    resolution = SettlementResolution(
        event_identity="provider-a:event-verifier-builtins-1",
        settlement_ref="provider-result:verifier-builtins-1",
        quote_outcomes={leg.quote_key: "win"},
        evidence_id="provider-a-outcome-verifier-builtins-1",
        evidence_sha256="b" * 64,
        available_at="2026-09-22T07:01:00+00:00",
    )

    save_globals = _paperbook_save_authority_globals()
    verifier = save_globals["_require_delegate_graph_witnesses"]
    verifier_builtins = verifier.__builtins__
    if type(verifier_builtins) is not dict:
        raise AssertionError("save verifier builtins mapping is not an exact dict")
    if "type" in verifier.__globals__:
        raise AssertionError("save verifier unexpectedly has a global type shadow")

    canonical_type = verifier_builtins["type"]
    delegate_witnesses = save_globals["_SAVE_DELEGATE_GRAPH_WITNESSES"]
    target_delegate = delegate_witnesses[0][0]
    hostile_calls: list[object] = []

    def hostile_type(*args):
        if len(args) == 1 and args[0] is target_delegate:
            hostile_calls.append(args[0])
            verifier_builtins["type"] = canonical_type
        return canonical_type(*args)

    class OutcomeAuthority:
        def resolve(self, _record, *, as_of: str):
            del as_of
            verifier_builtins["type"] = hostile_type
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
    coordinator.workspace = workspace
    coordinator.paper_book_path = paper_book_path
    coordinator.initial_bankroll = "100"

    try:
        try:
            coordinator.tick()
        except ContinuousSessionError as exc:
            if re.search(
                r"PaperBook|verifier|authority|dependency|pre-learning",
                str(exc),
                flags=re.IGNORECASE,
            ) is None:
                raise AssertionError(
                    f"unexpected fail-closed reason: {exc}"
                ) from exc
        else:
            raise AssertionError(
                "transient save-verifier builtin retarget was not rejected"
            )
    finally:
        verifier_builtins["type"] = canonical_type

    if hostile_calls:
        raise AssertionError(
            f"hostile save-verifier builtin executed: {hostile_calls!r}"
        )
    if handoff.prepare_calls or handoff.reconcile_calls:
        raise AssertionError(
            "settlement learning ran after save-verifier builtin retarget"
        )


def test_tick_rejects_transient_save_verifier_builtin_retarget_before_dispatch(
    tmp_path: Path,
) -> None:
    # Function.__builtins__ can share the interpreter builtins dictionary. Keep
    # this adversary in a child process so even a deliberately RED pre-repair run
    # cannot leak a temporary builtin replacement into later pytest tests.
    completed = subprocess.run(
        [sys.executable, str(Path(__file__)), str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, (
        "isolated save-verifier builtin authority falsifier failed\n"
        f"stdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(
            "usage: test_settlement_paperbook_verifier_builtins_authority.py WORKSPACE"
        )
    _run_child(Path(sys.argv[1]))
