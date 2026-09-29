"""Code-anchor RunTransaction current-binding resolver closure authority.

The canonical current-binding resolver witnesses the sealed PaperBook load/verifier
graph, but its own root objects are Python closure cells. A coordinated retarget of
those cells can otherwise move the graph and its expectations together. This guard
runs after ``run_transaction`` installs the current-binding consumers and before the
later detached-dispatch guard snapshots them.

No path binding or persistence authority is introduced. The exact existing resolver
function identity is retained; only its code is replaced by an equivalent verifier
whose composition-time roots are independently embedded in code constants.
"""

from __future__ import annotations

from types import FunctionType

from . import run_transaction as _run_transaction


def _closure_cell(function: FunctionType, name: str):
    closure = function.__closure__
    freevars = function.__code__.co_freevars
    if closure is None or name not in freevars:
        raise RuntimeError(
            f"RunTransaction current-binding resolver closure is unavailable: {name}"
        )
    return closure[freevars.index(name)]


def _harden_resolver(consumer: FunctionType) -> None:
    resolver_cell = _closure_cell(consumer, "resolver")
    resolver_code_cell = _closure_cell(consumer, "resolver_code")
    resolver = resolver_cell.cell_contents
    resolver_code = resolver_code_cell.cell_contents
    if type(resolver) is not FunctionType or resolver.__code__ is not resolver_code:
        raise RuntimeError("RunTransaction current-binding resolver is invalid")

    cells = {
        name: _closure_cell(resolver, name)
        for name in (
            "empty_cell",
            "exact_type",
            "frozen_globals",
            "frozen_load",
            "frozen_load_code",
            "frozen_load_globals",
            "function_type",
            "graph",
            "trusted_globals",
            "verifier",
        )
    }
    empty_cell = cells["empty_cell"].cell_contents
    exact_type = cells["exact_type"].cell_contents
    frozen_globals = cells["frozen_globals"].cell_contents
    frozen_load = cells["frozen_load"].cell_contents
    frozen_load_code = cells["frozen_load_code"].cell_contents
    frozen_load_globals = cells["frozen_load_globals"].cell_contents
    function_type = cells["function_type"].cell_contents
    graph = cells["graph"].cell_contents
    trusted_globals = cells["trusted_globals"].cell_contents
    verifier = cells["verifier"].cell_contents

    markers = {
        "empty_cell": "__AUTOSPORT_CURRENT_BINDING_EMPTY_CELL_ANCHOR__",
        "exact_type": "__AUTOSPORT_CURRENT_BINDING_EXACT_TYPE_ANCHOR__",
        "frozen_globals": "__AUTOSPORT_CURRENT_BINDING_FROZEN_GLOBALS_ANCHOR__",
        "frozen_load": "__AUTOSPORT_CURRENT_BINDING_FROZEN_LOAD_ANCHOR__",
        "frozen_load_code": "__AUTOSPORT_CURRENT_BINDING_FROZEN_LOAD_CODE_ANCHOR__",
        "frozen_load_globals": "__AUTOSPORT_CURRENT_BINDING_FROZEN_LOAD_GLOBALS_ANCHOR__",
        "function_type": "__AUTOSPORT_CURRENT_BINDING_FUNCTION_TYPE_ANCHOR__",
        "graph": "__AUTOSPORT_CURRENT_BINDING_GRAPH_ANCHOR__",
        "trusted_globals": "__AUTOSPORT_CURRENT_BINDING_TRUSTED_GLOBALS_ANCHOR__",
        "verifier": "__AUTOSPORT_CURRENT_BINDING_VERIFIER_ANCHOR__",
    }

    def hardened_resolver() -> FunctionType:
        anchored_empty_cell = "__AUTOSPORT_CURRENT_BINDING_EMPTY_CELL_ANCHOR__"
        anchored_exact_type = "__AUTOSPORT_CURRENT_BINDING_EXACT_TYPE_ANCHOR__"
        anchored_frozen_globals = "__AUTOSPORT_CURRENT_BINDING_FROZEN_GLOBALS_ANCHOR__"
        anchored_frozen_load = "__AUTOSPORT_CURRENT_BINDING_FROZEN_LOAD_ANCHOR__"
        anchored_frozen_load_code = "__AUTOSPORT_CURRENT_BINDING_FROZEN_LOAD_CODE_ANCHOR__"
        anchored_frozen_load_globals = "__AUTOSPORT_CURRENT_BINDING_FROZEN_LOAD_GLOBALS_ANCHOR__"
        anchored_function_type = "__AUTOSPORT_CURRENT_BINDING_FUNCTION_TYPE_ANCHOR__"
        anchored_graph = "__AUTOSPORT_CURRENT_BINDING_GRAPH_ANCHOR__"
        anchored_trusted_globals = "__AUTOSPORT_CURRENT_BINDING_TRUSTED_GLOBALS_ANCHOR__"
        anchored_verifier = "__AUTOSPORT_CURRENT_BINDING_VERIFIER_ANCHOR__"

        if (
            empty_cell is not anchored_empty_cell
            or exact_type is not anchored_exact_type
            or frozen_globals is not anchored_frozen_globals
            or frozen_load is not anchored_frozen_load
            or frozen_load_code is not anchored_frozen_load_code
            or frozen_load_globals is not anchored_frozen_load_globals
            or function_type is not anchored_function_type
            or graph is not anchored_graph
            or trusted_globals is not anchored_trusted_globals
            or verifier is not anchored_verifier
        ):
            raise ValueError("PaperBook current-binding resolver closure authority changed")

        if (
            anchored_trusted_globals.get("_FROZEN_LOAD") is not anchored_frozen_load
            or anchored_exact_type(anchored_frozen_load) is not anchored_function_type
            or anchored_frozen_load.__code__ is not anchored_frozen_load_code
            or anchored_frozen_load.__globals__ is not anchored_frozen_load_globals
            or anchored_frozen_globals.get("_require_bound_book") is not anchored_verifier
        ):
            raise ValueError("PaperBook current-binding persistence authority changed")

        for (
            function,
            code,
            globals_mapping,
            defaults,
            kwdefaults,
            closure,
            closure_values,
            bindings,
        ) in anchored_graph:
            if (
                anchored_exact_type(function) is not anchored_function_type
                or function.__code__ is not code
                or function.__globals__ is not globals_mapping
                or function.__defaults__ != defaults
                or function.__kwdefaults__ != kwdefaults
                or function.__closure__ != closure
            ):
                raise ValueError(
                    "PaperBook current-binding verifier executable authority changed"
                )
            current_closure = function.__closure__
            if current_closure is None:
                if closure_values is not None:
                    raise ValueError(
                        "PaperBook current-binding verifier closure authority changed"
                    )
            else:
                if closure_values is None or len(current_closure) != len(closure_values):
                    raise ValueError(
                        "PaperBook current-binding verifier closure authority changed"
                    )
                for cell, expected in zip(current_closure, closure_values):
                    try:
                        current = cell.cell_contents
                    except ValueError as exc:
                        raise ValueError(
                            "PaperBook current-binding verifier closure authority changed"
                        ) from exc
                    if current is not expected:
                        raise ValueError(
                            "PaperBook current-binding verifier closure authority changed"
                        )
            for name, expected, expected_code in bindings:
                if globals_mapping.get(name, anchored_empty_cell) is not expected:
                    raise ValueError(
                        "PaperBook current-binding verifier global authority changed"
                    )
                if expected_code is not None and (
                    anchored_exact_type(expected) is not anchored_function_type
                    or expected.__code__ is not expected_code
                ):
                    raise ValueError(
                        "PaperBook current-binding verifier dependency executable changed"
                    )
        return anchored_verifier

    values = {
        "empty_cell": empty_cell,
        "exact_type": exact_type,
        "frozen_globals": frozen_globals,
        "frozen_load": frozen_load,
        "frozen_load_code": frozen_load_code,
        "frozen_load_globals": frozen_load_globals,
        "function_type": function_type,
        "graph": graph,
        "trusted_globals": trusted_globals,
        "verifier": verifier,
    }
    constants = hardened_resolver.__code__.co_consts
    anchors = tuple((markers[name], values[name]) for name in markers)
    if any(sum(item == marker for item in constants) != 1 for marker, _ in anchors):
        raise RuntimeError("RunTransaction current-binding resolver anchor is ambiguous")
    hardened_code = hardened_resolver.__code__.replace(
        co_consts=tuple(
            next(
                (anchored for marker, anchored in anchors if item == marker),
                item,
            )
            for item in constants
        )
    )
    if hardened_code.co_freevars != resolver.__code__.co_freevars:
        raise RuntimeError("RunTransaction current-binding resolver freevar graph changed")

    resolver.__code__ = hardened_code
    resolver_code_cell.cell_contents = hardened_code


def _install() -> None:
    owner = _run_transaction.RunTransaction
    stage = vars(owner).get("_stage_paper_book_snapshot")
    promotion = vars(owner).get("_promote_paper_book_snapshot")
    if type(stage) is not FunctionType or type(promotion) is not FunctionType:
        raise RuntimeError("canonical RunTransaction binding consumers are unavailable")
    _harden_resolver(stage)
    _harden_resolver(promotion)


_install()
del _install
del _harden_resolver
del _closure_cell
