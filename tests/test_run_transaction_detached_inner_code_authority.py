from __future__ import annotations

from types import FunctionType

from autosport.run_transaction import RunTransaction


def _closure_cell(function: FunctionType, name: str):
    freevars = function.__code__.co_freevars
    assert function.__closure__ is not None
    assert name in freevars
    return function.__closure__[freevars.index(name)]


def _hostile_code_with_same_freevars(function: FunctionType):
    """Build a no-op code object assignable to the exact captured function closure."""

    names = function.__code__.co_freevars
    assert names
    parameters = ", ".join(names)
    references = ", ".join(names)
    namespace: dict[str, object] = {}
    source = (
        f"def outer({parameters}):\n"
        "    def hostile(*args, **kwargs):\n"
        f"        captured = ({references},)\n"
        "        del captured, args, kwargs\n"
        "        return None\n"
        "    return hostile\n"
    )
    exec(source, namespace)
    outer = namespace["outer"]
    assert isinstance(outer, FunctionType)
    hostile = outer(*([None] * len(names)))
    assert isinstance(hostile, FunctionType)
    assert hostile.__code__.co_freevars == names
    return hostile.__code__


def test_guard_rejects_coordinated_executable_and_inner_code_retarget() -> None:
    """The executed clone code cannot move with its writable expected-code cell."""

    guarded = RunTransaction._promote_paper_book_snapshot
    assert isinstance(guarded, FunctionType)

    function_cell = _closure_cell(guarded, "function")
    inner_code_cell = _closure_cell(guarded, "inner_code")
    function = function_cell.cell_contents
    original_code = inner_code_cell.cell_contents
    assert isinstance(function, FunctionType)
    assert function.__code__ is original_code

    hostile_code = _hostile_code_with_same_freevars(function)
    assert hostile_code is not original_code

    function.__code__ = hostile_code
    inner_code_cell.cell_contents = hostile_code
    try:
        try:
            guarded()
        except Exception as exc:  # noqa: BLE001 - rejection must precede hostile delegate.
            assert not isinstance(exc, TypeError), (
                "guard admitted coordinated executable/code retargeting far enough to "
                "dispatch through a replacement delegate"
            )
        else:
            raise AssertionError(
                "guard executed replacement inner code after coordinated retargeting"
            )
    finally:
        inner_code_cell.cell_contents = original_code
        function.__code__ = original_code

    assert function.__code__ is original_code
    assert inner_code_cell.cell_contents is original_code
