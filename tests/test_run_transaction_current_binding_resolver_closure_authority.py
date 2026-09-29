from __future__ import annotations

from types import FunctionType

from autosport.run_transaction import RunTransaction


def _closure_cell(function: FunctionType, name: str):
    closure = function.__closure__
    freevars = function.__code__.co_freevars
    assert closure is not None
    assert name in freevars
    return closure[freevars.index(name)]


def _closure_value(function: FunctionType, name: str):
    return _closure_cell(function, name).cell_contents


def test_current_binding_resolver_rejects_coordinated_closure_graph_retarget() -> None:
    """Resolver identity/code alone must not bless a replacement binding authority graph."""

    guarded = RunTransaction._stage_paper_book_snapshot
    assert isinstance(guarded, FunctionType)

    inner_consumer = _closure_value(guarded, "function")
    assert isinstance(inner_consumer, FunctionType)
    resolver = _closure_value(inner_consumer, "resolver")
    assert isinstance(resolver, FunctionType)

    names = (
        "trusted_globals",
        "frozen_load",
        "frozen_load_code",
        "frozen_load_globals",
        "frozen_globals",
        "verifier",
        "graph",
    )
    cells = {name: _closure_cell(resolver, name) for name in names}
    originals = {name: cell.cell_contents for name, cell in cells.items()}

    def hostile_verifier(*_args, **_kwargs) -> None:
        return None

    def load_template() -> None:
        return None

    fake_globals: dict[str, object] = {
        "__builtins__": __builtins__,
        "_require_bound_book": hostile_verifier,
    }
    fake_load = FunctionType(load_template.__code__, fake_globals, name="fake_load")

    cells["trusted_globals"].cell_contents = {"_FROZEN_LOAD": fake_load}
    cells["frozen_load"].cell_contents = fake_load
    cells["frozen_load_code"].cell_contents = fake_load.__code__
    cells["frozen_load_globals"].cell_contents = fake_globals
    cells["frozen_globals"].cell_contents = fake_globals
    cells["verifier"].cell_contents = hostile_verifier
    cells["graph"].cell_contents = ()
    try:
        try:
            resolved = resolver()
        except Exception:  # noqa: BLE001 - any fail-closed rejection satisfies the oracle.
            pass
        else:
            raise AssertionError(
                "current-binding resolver accepted a coordinated replacement closure graph "
                f"and returned {resolved!r}"
            )
    finally:
        for name, value in originals.items():
            cells[name].cell_contents = value

    for name, value in originals.items():
        assert cells[name].cell_contents is value
