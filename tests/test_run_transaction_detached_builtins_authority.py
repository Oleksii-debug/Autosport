from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path
from types import FunctionType

import autosport.run_transaction as run_transaction
from autosport.decision_ledger import JsonlDecisionLedger
from autosport.domain import TicketLeg
from autosport.integrity import sha256_file
from autosport.paper import PaperBook
from autosport.run_registry import RunRegistry
from autosport.run_transaction import RunTransaction


def _closure_value(function: FunctionType, name: str):
    freevars = function.__code__.co_freevars
    assert function.__closure__ is not None
    assert name in freevars
    return function.__closure__[freevars.index(name)].cell_contents


def _prepare_precommitted_transaction(root: Path) -> tuple[RunTransaction, Path, str]:
    registry = RunRegistry.initialize_pristine(root / "run_registry.json")
    book_path = root / "paper_book.json"
    PaperBook("10000").save(book_path)
    ledger = JsonlDecisionLedger(root / "decisions.jsonl")
    ledger.path.touch()

    market_sha256 = "a" * 64
    results_sha256 = "b" * 64
    strategy_id = "baseline-v1"
    run_id = "detached-builtins-authority"
    base_book_hash = sha256_file(book_path)
    base_ledger_hash = sha256_file(ledger.path)
    experiment_key = registry.begin(
        market_sha256,
        results_sha256,
        strategy_id,
        run_id,
        base_paper_book_sha256=base_book_hash,
        base_decision_ledger_sha256=base_ledger_hash,
    )
    tx = RunTransaction.start(
        root,
        run_id=run_id,
        experiment_key=experiment_key,
        market_sha256=market_sha256,
        results_sha256=results_sha256,
        strategy_id=strategy_id,
        base_paper_book_sha256=base_book_hash,
        base_decision_ledger_sha256=base_ledger_hash,
    )
    staged_book = PaperBook.load(book_path)
    staged_book.open_ticket(
        [
            TicketLeg(
                event_id="detached-builtins:event",
                market_id="detached-builtins:winner",
                selection_id="detached-builtins:home",
                locked_odds=Decimal("2"),
                sport="motorsport",
                exchange_side="back",
            )
        ],
        Decimal("1"),
        reason="detached-builtins-authority",
        placed_at="2000-01-01T00:00:00+00:00",
    )
    tx.stage_outputs(staged_book, ledger.path)
    tx.precommit(
        {
            "schema_version": 2,
            "run_id": run_id,
            "experiment_key": experiment_key,
            "market_sha256": market_sha256,
            "sealed_results_sha256": results_sha256,
            "strategy_id": strategy_id,
            "real_money_execution": False,
        }
    )
    return tx, book_path, sha256_file(tx.staged_book_path)


def test_detached_consumer_does_not_execute_mutated_live_builtins_dict() -> None:
    """A valid promotion executes only the composition-time private builtins snapshot."""

    guarded = run_transaction.RunTransaction._promote_paper_book_snapshot
    assert isinstance(guarded, FunctionType)
    clone = _closure_value(guarded, "function")
    assert isinstance(clone, FunctionType)

    builtins_map = clone.__globals__.get("__builtins__")
    assert type(builtins_map) is dict
    original_dict = builtins_map.get("dict")
    assert original_dict is dict

    hostile_calls = 0

    def hostile_dict(*args, **kwargs):
        nonlocal hostile_calls
        hostile_calls += 1
        raise AssertionError("reachable live builtins dict controlled detached execution")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        tx, book_path, expected_new = _prepare_precommitted_transaction(root)
        expected_base = sha256_file(book_path)
        assert expected_base != expected_new

        builtins_map["dict"] = hostile_dict
        try:
            # This is deliberately a semantically valid direct promotion call, not an
            # argument-binding oracle. It traverses durable identity/precommit checks,
            # current-binding reconstruction, staged semantic validation and canonical
            # PaperBook publication. On the vulnerable predecessor the reconstructed
            # prior consumer executes builtin dict(inner_globals) from this reachable
            # mapping and therefore invokes hostile_dict.
            tx._promote_paper_book_snapshot()
        finally:
            builtins_map["dict"] = original_dict

        assert hostile_calls == 0, "mutated live builtins dict was used for execution"
        assert sha256_file(book_path) == expected_new
        assert sha256_file(book_path) != expected_base

    assert builtins_map.get("dict") is original_dict
