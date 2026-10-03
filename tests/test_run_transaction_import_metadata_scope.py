from __future__ import annotations

from types import FunctionType

import autosport  # noqa: F401 - package composition installs the guards
import autosport.run_transaction as run_transaction_module
from autosport.run_transaction import RunTransaction


_IMPORT_METADATA = {
    "__cached__",
    "__file__",
    "__loader__",
    "__name__",
    "__package__",
    "__spec__",
}


def _closure_value(function: FunctionType, name: str):
    closure = function.__closure__
    if closure is None or name not in function.__code__.co_freevars:
        raise AssertionError(f"sealed RunTransaction consumer has no {name}")
    return closure[function.__code__.co_freevars.index(name)].cell_contents


def _detached_globals(method: FunctionType) -> dict[str, object]:
    value = _closure_value(method, "inner_globals")
    if type(value) is not dict:
        raise AssertionError("sealed RunTransaction consumer globals are not an exact dict")
    return value


def _original_consumer_code(method: FunctionType):
    detached_wrapper = _closure_value(method, "function")
    if type(detached_wrapper) is not FunctionType:
        raise AssertionError("detached RunTransaction wrapper is not a Python function")
    return _closure_value(detached_wrapper, "inner_code")


def test_detached_run_transaction_globals_are_execution_scoped() -> None:
    """Import metadata must not become PAPER transaction tamper authority."""

    for method in (
        RunTransaction._stage_paper_book_snapshot,
        RunTransaction._promote_paper_book_snapshot,
    ):
        detached = _detached_globals(method)
        original_code = _original_consumer_code(method)

        assert "__builtins__" in detached
        assert _IMPORT_METADATA.isdisjoint(detached)
        assert set(detached).difference({"__builtins__"}).issubset(original_code.co_names)
        assert detached["RunTransaction"] is RunTransaction


def test_live_import_metadata_cannot_enter_detached_transaction_snapshot() -> None:
    """Launcher-owned package metadata can move without changing economic authority."""

    stage_globals = _detached_globals(RunTransaction._stage_paper_book_snapshot)
    promotion_globals = _detached_globals(RunTransaction._promote_paper_book_snapshot)
    original_package = run_transaction_module.__package__
    replacement = object()

    assert "__package__" not in stage_globals
    assert "__package__" not in promotion_globals
    try:
        run_transaction_module.__package__ = replacement
        assert run_transaction_module.__package__ is replacement
        assert "__package__" not in stage_globals
        assert "__package__" not in promotion_globals
    finally:
        run_transaction_module.__package__ = original_package
