from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import autosport.product_paper_decision_cycle as cycle_module
from autosport.continuous_session import SessionState
from autosport.domain import TicketLeg
from autosport.paper import PaperBook
from autosport.product_paper_decision_cycle import (
    ProductDecisionInput,
    ProductPaperDecisionCycle,
)
from autosport.product_runtime import ProductCompositionError


class _Authority:
    def __init__(self) -> None:
        self.contract = SimpleNamespace(max_quote_age_seconds=Decimal("5"))


class _IntentFactory:
    strategy_version_id = "strategy-v1"

    def __call__(self, *_args, **_kwargs):
        return ()


class _Registry:
    def __init__(self, path: Path) -> None:
        self.path = path


class _ExecutionConfig:
    max_quote_age_ms = 5_000


class _Runtime:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.manifest = SimpleNamespace(
            source_id="provider-a",
            initial_bankroll="100",
        )
        self._status = SimpleNamespace(state=SessionState.RUNNING)

    def status(self):
        return self._status

    @contextmanager
    def decision_commit_fence(self):
        if self.status().state is not SessionState.RUNNING:
            raise ProductCompositionError(
                "PAPER decision commit requires the canonical product runtime to remain running"
            )
        yield


class _Ledger:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)


class _Execution:
    def __init__(
        self,
        *,
        book,
        ledger,
        config,
        max_quote_age,
        paper_book_path,
    ) -> None:
        del config, max_quote_age
        self.book = book
        self.ledger = ledger
        self.paper_book_path = Path(paper_book_path)


class _StopWinsThenCommitLoop:
    runtime: _Runtime | None = None

    def __init__(self, workspace, **kwargs) -> None:
        self.workspace = Path(workspace)
        self.kwargs = kwargs
        self.closed = False

    def register_input(self, input_id: str, **selectors) -> None:
        del input_id, selectors

    def run_cycle(self):
        runtime = type(self).runtime
        assert runtime is not None

        paper_book_path = self.kwargs["paper_execution"].paper_book_path

        # Deterministic lifecycle race: STOP has become canonical/durable-visible
        # before any simulated economic commit below.
        runtime._status.state = SessionState.STOPPED
        assert runtime.status().state is SessionState.STOPPED

        # The product composition must wire the runtime-owned fence down to the
        # durable commit boundary. STOP won before entry, so the fence must reject
        # before any simulated DecisionLedger/execution/PaperBook write below.
        with self.kwargs["commit_fence"]():
            self.kwargs["decision_ledger"].path.write_text(
                "decision-committed-after-stop\n",
                encoding="utf-8",
            )
            self.kwargs["paper_execution"].ledger.path.write_text(
                "paper-execution-committed-after-stop\n",
                encoding="utf-8",
            )
            book = self.kwargs["book"]
            book.open_ticket(
                [TicketLeg("event", "market", "selection", Decimal("2.0"))],
                Decimal("10"),
                reason="runtime-stop-race-falsifier",
                placed_at="2026-09-22T06:30:00+00:00",
            )
            book.save(paper_book_path)
        return SimpleNamespace(status="DECIDED")

    def close(self) -> None:
        self.closed = True


def test_runtime_stop_winning_before_commit_prevents_economic_mutation(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(cycle_module, "AutonomousProductRuntime", _Runtime)
    monkeypatch.setattr(cycle_module, "EconomicDecisionAuthority", _Authority)
    monkeypatch.setattr(cycle_module, "ScientificRegistry", _Registry)
    monkeypatch.setattr(cycle_module, "PaperExecutionModelConfig", _ExecutionConfig)
    monkeypatch.setattr(cycle_module, "PaperExecutionLedger", _Ledger)
    monkeypatch.setattr(cycle_module, "JsonlDecisionLedger", _Ledger)
    monkeypatch.setattr(cycle_module, "PaperExecutionAdoptionRuntime", _Execution)
    monkeypatch.setattr(
        cycle_module,
        "PersistentLiveDecisionLoop",
        _StopWinsThenCommitLoop,
    )
    monkeypatch.setattr(
        cycle_module.LiveIntentProvenance,
        "from_registry",
        lambda *_args, **_kwargs: SimpleNamespace(
            strategy_version_id="strategy-v1"
        ),
    )

    PaperBook("100").save(tmp_path / "paper_book.json")
    book_before_stop_race = (tmp_path / "paper_book.json").read_bytes()

    runtime = _Runtime(tmp_path)
    _StopWinsThenCommitLoop.runtime = runtime

    cycle = ProductPaperDecisionCycle(
        runtime,
        loop_id="runtime-stop-race",
        authority=_Authority(),
        intent_factory=_IntentFactory(),
        scientific_registry=_Registry(tmp_path / "scientific_registry.json"),
        execution_config=_ExecutionConfig(),
        max_quote_age=timedelta(seconds=5),
        inputs=(
            ProductDecisionInput(
                "selection-input",
                source_ids=("provider-a",),
                selection_ids=("selection-a",),
            ),
        ),
    )

    with pytest.raises(
        ProductCompositionError,
        match="decision commit requires the canonical product runtime to remain running",
    ):
        cycle._run_decision_cycle()

    # STOP won before the simulated commit boundary.  Post-mutation detection is
    # not enough: no new durable decision/execution/book effect may survive.
    assert not (tmp_path / "decisions.jsonl").exists()
    assert not (tmp_path / "paper-execution.jsonl").exists()
    assert (tmp_path / "paper_book.json").read_bytes() == book_before_stop_race
