from __future__ import annotations

from types import FunctionType

import autosport.run_transaction as run_transaction


_HOSTILE_RESULT = "__AUTOSPORT_HOSTILE_DETACHED_CODE_EXECUTED__"


def _closure_cell(function: FunctionType, name: str):
    freevars = function.__code__.co_freevars
    assert function.__closure__ is not None
    assert name in freevars
    return function.__closure__[freevars.index(name)]


def _closure_value(function: FunctionType, name: str):
    return _closure_cell(function, name).cell_contents


def _hostile_code_with_same_freevars(freevars: tuple[str, ...]):
    """Compile arbitrary code with the exact freevar schema required by FunctionType."""

    assignments = "\n".join(f"    {name} = None" for name in freevars)
    references = ", ".join(freevars)
    if len(freevars) == 1:
        references += ","
    source = (
        "def factory():\n"
        f"{assignments}\n"
        "    def hostile(*args, **kwargs):\n"
        "        if kwargs.get('__autosport_never__'):\n"
        f"            return ({references})\n"
        f"        return {_HOSTILE_RESULT!r}\n"
        "    return hostile\n"
    )
    namespace: dict[str, object] = {}
    exec(compile(source, "<detached-hostile-code>", "exec"), namespace)
    factory = namespace["factory"]
    assert isinstance(factory, FunctionType)
    hostile = factory()
    assert isinstance(hostile, FunctionType)
    assert hostile.__code__.co_freevars == freevars
    return hostile.__code__


def test_guard_rejects_coordinated_delegate_function_and_code_retarget() -> None:
    """The detached delegate code root must not be movable with its verifier cells.

    The public guard currently compares ``function.__code__`` to the writable
    ``inner_code`` closure cell and ``function.__closure__`` to the writable
    ``inner_closure`` cell.  Moving those cells together can otherwise substitute an
    arbitrary code object with the same freevar schema while every snapshot verifier
    still observes the original authenticated ``inner_globals`` object.
    """

    guarded = run_transaction._promote_paper_book_snapshot
    assert isinstance(guarded, FunctionType)

    function_cell = _closure_cell(guarded, "function")
    inner_code_cell = _closure_cell(guarded, "inner_code")
    inner_closure_cell = _closure_cell(guarded, "inner_closure")

    original_function = function_cell.cell_contents
    original_code = inner_code_cell.cell_contents
    original_closure = inner_closure_cell.cell_contents
    assert isinstance(original_function, FunctionType)
    assert original_function.__code__ is original_code
    assert original_function.__closure__ is original_closure
    assert original_closure is not None

    hostile_code = _hostile_code_with_same_freevars(original_code.co_freevars)
    hostile_function = FunctionType(
        hostile_code,
        original_function.__globals__,
        name=original_function.__name__,
        argdefs=original_function.__defaults__,
        closure=original_closure,
    )
    assert hostile_function.__closure__ is original_closure

    function_cell.cell_contents = hostile_function
    inner_code_cell.cell_contents = hostile_code
    inner_closure_cell.cell_contents = hostile_function.__closure__
    try:
        try:
            result = guarded()
        except Exception:  # noqa: BLE001 - any fail-closed rejection satisfies the oracle.
            pass
        else:
            raise AssertionError(
                "detached guard executed a coordinated replacement delegate code object: "
                f"{result!r}"
            )
    finally:
        inner_closure_cell.cell_contents = original_closure
        inner_code_cell.cell_contents = original_code
        function_cell.cell_contents = original_function

    assert function_cell.cell_contents is original_function
    assert inner_code_cell.cell_contents is original_code
    assert inner_closure_cell.cell_contents is original_closure
